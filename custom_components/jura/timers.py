"""Home Assistant opt-in and staging policy for weekday switch-on timers."""

from __future__ import annotations

from datetime import time
from typing import Literal

from jura_connect.timers import (
    ALL_DAYS_SLOT,
    TIMER_MODEL,
    TIMER_SLOTS,
    WEEKDAY_SLOTS,
    WEEKDAYS,
    TimerSlot,
    TimerUpdate,
    timer_minute,
)

__all__ = [
    "ALL_DAYS_SLOT",
    "CONF_WEEKDAY_TIMERS",
    "TIMER_MODEL",
    "TIMER_SLOTS",
    "WEEKDAYS",
    "WEEKDAY_SLOTS",
    "TimerAction",
    "TimerSlot",
    "TimerUpdate",
    "target_timer",
    "timer_minute",
]

CONF_WEEKDAY_TIMERS = "weekday_timers"
TimerAction = Literal["set_time", "enable", "disable"]


def target_timer(current: TimerSlot, action: TimerAction, value: time | None) -> TimerSlot:
    """Derive a write from live state, preserving the switch-off pair."""
    if action == "disable":
        return current.with_on(None)
    if action == "enable" and current.on is not None:
        return current
    if action not in ("enable", "set_time"):
        raise ValueError("Unknown JURA timer action")
    if value is None:
        raise ValueError("Set a switch-on time before enabling this timer")
    timer_minute(value)
    # Editing a disabled timer only stages its time in HA.
    if action == "set_time" and current.on is None:
        return current
    return current.with_on(value)
