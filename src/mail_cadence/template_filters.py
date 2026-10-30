"""Jinja2 filters for date and datetime arithmetic and formatting in task templates.

Meant for the send context (``cadence.date``, ``cadence.window_start``), for example
``{{ cadence.date | add_days(-1) | date("%d.%m.%Y") }}``. Each filter accepts a
``date`` or a ``datetime`` and returns the same type; a datetime keeps its time and
tzinfo, and arithmetic on it is wall-clock arithmetic. Any other value raises
TypeError, which ends the execution without SMTP traffic (``mailer.RENDER_ERRORS``).
"""

import calendar
from datetime import date, datetime, timedelta

import jinja2


def _require_date(value: object, name: str) -> None:
    # datetime is a subclass of date, so this accepts both.
    if not isinstance(value, date):
        msg = f"'{name}' filter expects date or datetime, got {type(value).__name__}"
        raise TypeError(msg)


def _require_int(value: object, name: str, unit: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"'{name}' filter expects int for {unit}, got {type(value).__name__}"
        raise TypeError(msg)


def date_filter(value: date, fmt: str = "%Y-%m-%d") -> str:
    """Format a date or datetime with strftime."""
    _require_date(value, "date")
    return value.strftime(fmt)


def add_days[D: (date, datetime)](value: D, n: int) -> D:
    """Shift by ``n`` days (negative allowed)."""
    _require_date(value, "add_days")
    _require_int(n, "add_days", "days")
    return value + timedelta(days=n)


def add_months[D: (date, datetime)](value: D, n: int) -> D:
    """Shift by ``n`` months (negative allowed); the day is clamped to the month's end."""
    _require_date(value, "add_months")
    _require_int(n, "add_months", "months")
    year, month_index = divmod(value.year * 12 + value.month - 1 + n, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def month_start[D: (date, datetime)](value: D) -> D:
    """The same value on the first day of its month."""
    _require_date(value, "month_start")
    return value.replace(day=1)


def register_date_filters(env: jinja2.Environment) -> None:
    """Register the filters on ``env`` (task templates; later also subject lines)."""
    env.filters["date"] = date_filter
    env.filters["add_days"] = add_days
    env.filters["add_months"] = add_months
    env.filters["month_start"] = month_start
