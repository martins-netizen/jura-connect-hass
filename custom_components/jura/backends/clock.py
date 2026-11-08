"""Library-backed transaction for reading and setting the EF566 clock."""

from __future__ import annotations

import time
from datetime import datetime
from threading import Event

from jura_connect import JuraClient

from ..clock import (
    ClockReading,
    ClockReference,
    ClockUpdate,
    clock_machine_ready,
    validate_schedule,
    validate_write_window,
)
from .base import ClockWriteUnverified, JuraBackendError

_READBACK_OFFSETS = (0.0, 3.0, 6.0, 10.0)


def read_clock(client: JuraClient, reference: ClockReference, stop: Event) -> ClockReading:
    if stop.is_set():
        raise JuraBackendError("JURA clock synchronization was cancelled")
    clock = client.read_clock()
    observed_at = reference.now()
    day = client.read_clock_date()
    timers = client.read_clock_timers()
    reading = ClockReading(clock, day, timers, observed_at)
    validate_schedule(reading)
    return reading


def _check_ready(client: JuraClient, *, timeout: float = 3) -> None:
    status = client.read_status(timeout=timeout)
    if not clock_machine_ready(status.active_alerts, status.errors) or any(
        frame.startswith("@TV:") for frame in client.status_history
    ):
        raise JuraBackendError("Leave the JURA on its ready screen before synchronizing the clock")


def _wait_until(reference: ClockReference, epoch: float, stop: Event) -> None:
    if stop.wait(max(0, epoch - reference.now().timestamp())):
        raise JuraBackendError("JURA clock synchronization was cancelled")


def _write_clock(client: JuraClient, reference: ClockReference, epoch: float, stop: Event) -> None:
    actual = reference.now()
    if stop.is_set() or not 0 <= actual.timestamp() - epoch <= 3:
        raise JuraBackendError("The safe JURA write window passed; press the button again")
    client.set_clock(actual.time().replace(second=0, microsecond=0))


def start_clock_sync(client: JuraClient, reference: ClockReference, stop: Event) -> ClockUpdate:
    """Perform at most one correction, verifying it after releasing the keys."""
    _check_ready(client)
    before = read_clock(client, reference, stop)
    if before.day != before.observed_at.date():
        raise JuraBackendError("The JURA and Home Assistant dates differ; set the JURA date on the machine")
    if before.matches_ha():
        return ClockUpdate(before, wrote=False)
    now = reference.now()
    epoch = int(now.timestamp() // 60 + 1) * 60
    target = datetime.fromtimestamp(epoch, reference.zone)
    validate_write_window(before, target)
    # Re-read close to the write so a timer edited on the machine during the
    # wait cannot silently invalidate the schedule guard.
    _wait_until(reference, epoch - 10, stop)
    fresh = read_clock(client, reference, stop)
    if not fresh.same_schedule(before):
        raise JuraBackendError("The JURA date or timers changed during the check; no clock write performed")
    validate_write_window(fresh, target)
    if fresh.matches_ha():
        return ClockUpdate(fresh, wrote=False)
    _wait_until(reference, epoch + 0.15, stop)
    _check_ready(client, timeout=2)
    if not 0 <= reference.now().timestamp() - epoch <= 3:
        raise JuraBackendError("The safe JURA write window passed; press the button again")
    try:
        _write_clock(client, reference, epoch, stop)
        anchor = time.monotonic()
        for offset in _READBACK_OFFSETS:
            if stop.wait(max(0, anchor + offset - time.monotonic())):
                raise JuraBackendError("JURA clock verification was cancelled after the write")
            client.read_clock()
        settled = read_clock(client, reference, stop)
        if not before.same_schedule(settled) or not settled.matches_ha():
            raise JuraBackendError("JURA clock write could not be verified; check the machine before trying again")
    except (JuraBackendError, OSError, ValueError) as err:
        raise ClockWriteUnverified(str(err)) from err
    return ClockUpdate(settled, wrote=True)
