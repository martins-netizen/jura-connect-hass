"""Clock synchronization guards, HA time, and a real simulated wire round trip."""

from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntry
from jura_connect.clock import TIMER_ADDRESSES, parse_clock, parse_date, parse_timer
from jura_connect.simulator import SimulatorConfig, run_in_thread

from custom_components.jura.backends import clock as wire
from custom_components.jura.backends.base import ClockWriteUnverified, JuraBackendError, MachineSnapshot
from custom_components.jura.backends.jura import JuraConnectBackend
from custom_components.jura.clock import (
    ClockReading,
    ClockUpdate,
    clock_auto_period,
    validate_write_window,
)
from custom_components.jura.coordinator import JuraCoordinator

ZONE = ZoneInfo("Europe/Zurich")
RAW = {
    "05": "220A12",
    "06": "231A0B06",
    **dict(
        zip(
            TIMER_ADDRESSES,
            (
                "24061E8000",
                "24061E8000",
                "2406328000",
                "2406328000",
                "2406328000",
                "2407288000",
                "2407288000",
                "2480008000",
            ),
            strict=True,
        )
    ),
}


class SimReference:
    zone = ZONE
    epoch = datetime(2026, 11, 6, 9, 19, 53, tzinfo=ZONE).timestamp()

    def now(self):
        return datetime.fromtimestamp(self.epoch, self.zone)


def quick_wire(monkeypatch):
    def advance(reference, epoch, stop):
        assert not stop.is_set()
        reference.epoch = max(reference.epoch, epoch)

    monkeypatch.setattr(wire, "_wait_until", advance)
    monkeypatch.setattr(wire, "_READBACK_OFFSETS", (0.0,) * 4)


def clock_reading(clock=(9, 19), day=date(2026, 11, 6), observed=None, overrides=None):
    settings = {**RAW, **(overrides or {})}
    return ClockReading(
        time(*clock),
        day,
        tuple(parse_timer(settings[slot]) for slot in TIMER_ADDRESSES),
        observed or datetime(2026, 11, 6, 9, 19, 53, tzinfo=ZONE),
    )


def test_tagged_values_and_hour_formats_are_validated():
    assert parse_clock("220714") == time(7, 20)
    assert parse_date("231A0B06") == date(2026, 11, 6)
    for raw in ("", "230714", "22073C", "221800", "22071400", "22 714"):
        with pytest.raises(ValueError):
            parse_clock(raw)
    with pytest.raises(ValueError):
        parse_date("231A021D")


@pytest.mark.parametrize("clock", [(5, 59), (7, 37), (9, 20)])
def test_timer_crossing_margin_and_switch_off(clock):
    reading = clock_reading(
        clock=clock,
        overrides={"1B": "2480000916"} if clock == (9, 20) else None,
    )
    with pytest.raises(ValueError, match="timer"):
        validate_write_window(reading, reading.observed_at.replace(second=0))


def test_write_window_rejects_date_midnight_and_dst_transition():
    now = datetime(2026, 11, 6, 0, 2, tzinfo=ZONE)
    with pytest.raises(ValueError, match="midnight"):
        validate_write_window(clock_reading(clock=(0, 4), observed=now), now)
    with pytest.raises(ValueError, match="dates differ"):
        validate_write_window(clock_reading(day=date(2026, 11, 5)), now)
    now = datetime(2026, 3, 29, 1, 59, tzinfo=ZONE)
    with pytest.raises(ValueError, match="offset changed"):
        validate_write_window(clock_reading(clock=(1, 30), day=now.date(), observed=now), now)


def test_readback_uses_ha_local_time_and_minute_boundary():
    now = datetime(2026, 7, 1, 7, 20, 0, tzinfo=ZONE)
    assert clock_reading(clock=(7, 19), day=now.date(), observed=now).matches_ha()
    assert not clock_reading(clock=(5, 20), day=now.date(), observed=now).matches_ha()
    assert not clock_reading(clock=(7, 20), day=date(2026, 6, 30), observed=now).matches_ha()


@pytest.mark.parametrize(
    "month,day,old_hour,new_hour,fold,old_offset,new_offset", [(3, 29, 1, 3, 0, 1, 2), (10, 25, 2, 2, 1, 2, 1)]
)
def test_seasonal_offset_change_gets_new_automatic_check(month, day, old_hour, new_hour, fold, old_offset, new_offset):
    before = datetime(2026, month, day, old_hour, 30, tzinfo=ZONE)
    after = datetime(2026, month, day, new_hour, 30, tzinfo=ZONE, fold=fold)
    assert before.utcoffset().total_seconds() == 3600 * old_offset
    assert after.utcoffset().total_seconds() == 3600 * new_offset
    assert clock_auto_period(before) != clock_auto_period(after)
    assert clock_reading(clock=(new_hour, 30), day=after.date(), observed=after).matches_ha()


async def test_simulator_only_clock_slot_written_and_aligned_is_read_only(monkeypatch):
    quick_wire(monkeypatch)
    cfg = SimulatorConfig(allow_setting_writes=True, status_payload=bytes.fromhex("0004000000000000"))
    cfg.settings.update(RAW)
    with run_in_thread(cfg) as sim:
        backend = JuraConnectBackend(*sim.address, conn_id="clock-test", machine_type="EF566", clock_sync=True)
        await backend.pair()
        ref = SimReference()
        result = await backend.start_clock_sync(ref)
        assert result.wrote
        assert result.reading.clock == time(9, 20)
        assert cfg.settings["05"] == "220914"
        assert {key: cfg.settings[key] for key in RAW if key != "05"} == {
            key: value for key, value in RAW.items() if key != "05"
        }
        assert not cfg.screen_locked
        result = await backend.start_clock_sync(ref)
        assert not result.wrote and result.reading.clock == time(9, 20)


@pytest.mark.parametrize(("model", "enabled"), [("EF1091", True), ("EF566", False)])
async def test_requires_opt_in_and_supported_profile(monkeypatch, model, enabled):
    backend = JuraConnectBackend("127.0.0.1", conn_id="clock-test", machine_type=model, clock_sync=enabled)
    client = MagicMock()
    monkeypatch.setattr(backend, "_make_client", client)
    with pytest.raises(JuraBackendError, match="not enabled"):
        await backend.start_clock_sync(SimReference())
    client.assert_not_called()


@pytest.mark.parametrize("failure", ["busy", "date", "timer", "nack", "unlock", "mismatch", "collateral"])
async def test_unsafe_or_unconfirmed_operation_never_retries(monkeypatch, failure):
    quick_wire(monkeypatch)
    backend = JuraConnectBackend("127.0.0.1", conn_id="clock-test", machine_type="EF566", clock_sync=True)
    client = MagicMock()
    client.connect.return_value.state = "CORRECT"
    client.read_status.return_value.active_alerts = (
        ("coffee_ready", "lockedkeys") if failure == "busy" else ("coffee_ready",)
    )
    client.read_status.return_value.errors = ()
    settings = RAW.copy()
    if failure == "date":
        settings["06"] = "231A0B05"
    if failure == "timer":
        settings["1B"] = "2480000930"
    client.read_clock.side_effect = lambda: parse_clock(settings["05"])
    client.read_clock_date.side_effect = lambda: parse_date(settings["06"])
    client.read_clock_timers.side_effect = lambda: tuple(parse_timer(settings[slot]) for slot in TIMER_ADDRESSES)
    sent = []

    def set_clock(value):
        sent.append(value)
        if failure in {"nack", "unlock"}:
            raise ValueError("Write or unlock unconfirmed")
        if failure != "mismatch":
            settings["05"] = "220914"
        if failure == "collateral":
            settings["17"] = "2406318000"
        return "@tm:05"

    client.set_clock.side_effect = set_clock
    monkeypatch.setattr(backend, "_make_client", lambda: client)
    with pytest.raises(JuraBackendError):
        await backend.start_clock_sync(SimReference())
    if failure in {"busy", "date", "timer"}:
        assert not sent
    else:
        assert sent == [time(9, 20)]
    client.close.assert_called_once()


@pytest.mark.parametrize("advances", [True, False])
async def test_ha_system_time_requires_running_readback(monkeypatch, advances):
    hass = MagicMock()
    hass.config.time_zone = "Europe/Zurich"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *args: f(*args))
    backend = MagicMock()
    backend.start_clock_sync = AsyncMock(
        return_value=ClockUpdate(
            clock_reading(clock=(9, 20), observed=datetime(2026, 11, 6, 9, 20, 10, tzinfo=ZONE)),
            wrote=True,
        )
    )
    backend.read_clock = AsyncMock(
        return_value=clock_reading(
            clock=(9, 21) if advances else (9, 20),
            observed=datetime(2026, 11, 6, 9, 21, 15, tzinfo=ZONE),
        )
    )
    entry = ConfigEntry(
        data={"host": "127.0.0.1", "conn_id": "clock", "machine_type": "EF566"}, options={"clock_sync": "manual"}
    )
    c = JuraCoordinator(hass, entry, backend=backend)
    monkeypatch.setattr("custom_components.jura.coordinator.CLOCK_CHECK_SECONDS", 0)
    await c._async_clock_sync()
    backend.start_clock_sync.assert_awaited_once()
    backend.read_clock.assert_awaited_once()
    assert c.clock_sync_status.state == ("synchronized" if advances else "failed")


@pytest.mark.parametrize("failure", ["preflight", "unverified", "aligned"])
async def test_automatic_day_state_persists_and_only_defers_known_preflight(monkeypatch, failure):
    hass = MagicMock()
    hass.config.time_zone = "Europe/Zurich"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *args: f(*args))
    backend = MagicMock()
    backend.start_clock_sync = AsyncMock(
        return_value=ClockUpdate(clock_reading(), wrote=False),
        side_effect={
            "preflight": JuraBackendError("No safe window"),
            "unverified": ClockWriteUnverified("Write not confirmed"),
            "aligned": None,
        }[failure],
    )
    entry = ConfigEntry(
        data={"host": "127.0.0.1", "conn_id": "clock", "machine_type": "EF566"},
        options={"clock_sync": "automatic"},
    )
    c = JuraCoordinator(hass, entry, backend=backend)
    await c.async_load_clock_auto_state()
    await c._async_clock_sync(automatic=True)
    assert c.clock_sync_status.state == ("already_aligned" if failure == "aligned" else "failed")
    restored = JuraCoordinator(hass, entry, backend=backend)
    await restored.async_load_clock_auto_state()
    period = clock_auto_period(datetime.now(ZONE))
    assert (period in restored._clock_auto_periods) == (failure != "preflight")
    if failure != "preflight":
        await restored._async_clock_sync(automatic=True)
        backend.start_clock_sync.assert_awaited_once()


def test_automatic_start_waits_for_a_fresh_ready_poll(monkeypatch):
    entry = ConfigEntry(
        data={"host": "127.0.0.1", "conn_id": "clock", "machine_type": "EF566"},
        options={"clock_sync": "automatic"},
    )
    c = JuraCoordinator(MagicMock(), entry, backend=MagicMock())
    start = MagicMock()
    monkeypatch.setattr(c, "_start_clock_sync", start)
    c.data = MachineSnapshot("127.0.0.1", "clock", "CORRECT", ("coffee_ready",))
    c.maybe_auto_clock_sync()
    start.assert_not_called()
    c.last_poll_success = True
    c.maybe_auto_clock_sync()
    start.assert_called_once_with(automatic=True)
    c.maybe_auto_clock_sync()
    start.assert_called_once()


async def test_last_successful_clock_check_survives_reload_and_failed_attempt(monkeypatch):
    hass = MagicMock()
    hass.config.time_zone = "Europe/Zurich"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *args: f(*args))
    entry = ConfigEntry(
        data={"host": "127.0.0.1", "conn_id": "clock", "machine_type": "EF566"}, options={"clock_sync": "manual"}
    )
    reading = clock_reading(clock=(8, 33), observed=datetime(2026, 11, 6, 8, 33, 43, tzinfo=ZONE))
    backend = MagicMock()
    backend.start_clock_sync = AsyncMock(return_value=ClockUpdate(reading, wrote=False))

    c = JuraCoordinator(hass, entry, backend=backend)
    await c.async_load_clock_last_check()
    assert c.clock_last_check is None
    await c._async_clock_sync()
    assert c.clock_sync_status.state == "already_aligned"
    assert c.clock_last_check == reading.observed_at

    restored = JuraCoordinator(hass, entry, backend=backend)
    await restored.async_load_clock_last_check()
    assert restored.clock_sync_status.state == "not_run"
    assert restored.clock_last_check == reading.observed_at
    backend.start_clock_sync.side_effect = JuraBackendError("Machine unavailable")
    await restored._async_clock_sync()
    assert restored.clock_sync_status.state == "failed"
    assert restored.clock_last_check == reading.observed_at
    assert backend.start_clock_sync.await_count == 2


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Kathmandu", "Australia/Sydney"])
def test_readback_follows_the_configured_ha_zone(zone):
    local = datetime.fromtimestamp(1_793_960_000, ZoneInfo(zone))
    reading = clock_reading(clock=(local.hour, local.minute), day=local.date(), observed=local)
    assert reading.matches_ha()
    assert clock_auto_period(local).startswith(str(local.date()))


def test_system_clock_jump_stops_the_reference(monkeypatch):
    from custom_components.jura import clock

    reference = clock.ClockReference(ZONE, epoch=1000, monotonic=500)
    monkeypatch.setattr(clock.system_time, "monotonic", lambda: 501)
    monkeypatch.setattr(clock.system_time, "time", lambda: 1010)
    with pytest.raises(ValueError, match="clock changed"):
        reference.now()
