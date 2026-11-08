"""GIGA 6 clock values, local-time guards using the Home Assistant system clock."""

from __future__ import annotations

import time as system_time
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from jura_connect.clock import TimerTimes

CLOCK_MODEL = "EF566"
CONF_CLOCK_SYNC = "clock_sync"
CLOCK_SYNC_MODES = ("off", "manual", "automatic")
CLOCK_CHECK_SECONDS = 65
CLOCK_AUTO_RETRY_SECONDS = 900
TIMER_MARGIN_MINUTES = 5
CLOCK_DRIFT_TOLERANCE_SECONDS = 5
CLOCK_SYNC_STATES = ("not_run", "checking", "verifying", "already_aligned", "synchronized", "failed")


@dataclass(frozen=True, slots=True)
class ClockReference:
    """HA's wall clock in its configured zone, checked against elapsed time."""

    zone: ZoneInfo
    epoch: float
    monotonic: float

    @classmethod
    def capture(cls, zone: ZoneInfo) -> ClockReference:
        return cls(zone, system_time.time(), system_time.monotonic())

    def now(self) -> datetime:
        epoch = system_time.time()
        expected = self.epoch + system_time.monotonic() - self.monotonic
        if abs(epoch - expected) > CLOCK_DRIFT_TOLERANCE_SECONDS:
            raise ValueError("The Home Assistant clock changed during synchronization")
        return datetime.fromtimestamp(epoch, self.zone)


def _minute(value: time | datetime) -> int:
    return value.hour * 60 + value.minute


@dataclass(frozen=True, slots=True)
class ClockReading:
    clock: time
    day: date
    timers: tuple[TimerTimes, ...]
    observed_at: datetime

    def matches_ha(self) -> bool:
        # A read at the minute boundary can arrive just before or after it.
        reference = self.observed_at
        samples = (datetime.fromtimestamp(reference.timestamp() + delta, reference.tzinfo) for delta in (-2, 0, 2))
        return any(self.day == sample.date() and _minute(self.clock) == _minute(sample) for sample in samples)

    def same_schedule(self, other: ClockReading) -> bool:
        return self.day == other.day and self.timers == other.timers


@dataclass(frozen=True, slots=True)
class ClockUpdate:
    reading: ClockReading
    wrote: bool


@dataclass(frozen=True, slots=True)
class ClockSyncStatus:
    state: str = "not_run"
    reading: ClockReading | None = None
    detail: str | None = None


def clock_machine_ready(alerts: tuple[str, ...], errors: tuple[str, ...]) -> bool:
    busy = {
        "coffee_rinsing",
        "enjoy_product",
        "goodbye",
        "lockedkeys",
        "locked_keys",
        "program_mode_status",
        "remotescreen",
        "remote_screen",
        "system_emptying",
        "system_filling",
        "welcome",
    }
    return "coffee_ready" in alerts and not errors and not busy.intersection(alerts)


def clock_auto_period(local: datetime) -> str:
    """A fresh check is due after a date or UTC-offset change."""
    return f"{local.date()}/{local.tzinfo}/{local.utcoffset()}"


def validate_schedule(reading: ClockReading) -> tuple[int, ...]:
    if len(reading.timers) != 8:
        raise ValueError("Incomplete JURA timer block")
    return tuple(_minute(value) for pair in reading.timers for value in pair if value is not None)


def validate_write_window(reading: ClockReading, target: datetime) -> None:
    """Prevent date changes and crossing or approaching programmed timers."""
    if reading.day != target.date() or reading.day != reading.observed_at.date():
        raise ValueError("The JURA and Home Assistant dates differ; set the JURA date on the machine")
    later = datetime.fromtimestamp(target.timestamp() + CLOCK_CHECK_SECONDS + 15, target.tzinfo)
    if reading.observed_at.utcoffset() != target.utcoffset() or later.utcoffset() != target.utcoffset():
        raise ValueError("The Home Assistant time zone offset changed; try again later")
    low, high = sorted((_minute(reading.clock), _minute(target)))
    if low < 3 or high > 1436:
        raise ValueError("JURA clock synchronization is blocked near midnight")
    if any(
        low - TIMER_MARGIN_MINUTES <= timer + offset <= high + TIMER_MARGIN_MINUTES
        for timer in validate_schedule(reading)
        for offset in (-1440, 0, 1440)
    ):
        raise ValueError("The correction is too close to or crosses a programmed JURA timer")
