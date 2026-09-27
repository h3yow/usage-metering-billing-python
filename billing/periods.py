"""Calendar-month billing period helpers.

A billing period is a natural calendar month.  All functions are pure
and side-effect free.
"""

from __future__ import annotations

import calendar
from datetime import date


def period_of(day: date) -> tuple[date, date]:
    """Return (first_day, last_day) of the month containing ``day``."""
    first = day.replace(day=1)
    last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return first, last


def days_in_period(day: date) -> int:
    """Number of days in the calendar month containing ``day``."""
    return calendar.monthrange(day.year, day.month)[1]


def period_key(day: date) -> str:
    """Return the ``YYYY-MM`` key of the period containing ``day``."""
    return day.strftime("%Y-%m")
