"""Jinja2 template filters for date and datetime arithmetic and formatting.

These filters are a building block for planned send variables (section 5 of plans)
and are not yet documented for end users as the variables themselves are not yet introduced.
"""

import calendar
from datetime import date, datetime, timedelta

import jinja2


def date_filter(value: date | datetime, fmt: str = "%Y-%m-%d") -> str:
    """Format a date or datetime object using strftime."""
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, date):
        return value.strftime(fmt)
    msg = f"'date' filter expects date or datetime, got {type(value).__name__}"
    raise TypeError(msg)


def add_days[D: (date, datetime)](value: D, n: int) -> D:
    """Add or subtract n days from a date or datetime object."""
    if not isinstance(n, int) or isinstance(n, bool):
        msg = f"'add_days' filter expects int for days, got {type(n).__name__}"
        raise TypeError(msg)
    if isinstance(value, datetime):
        return value + timedelta(days=n)
    if isinstance(value, date):
        return value + timedelta(days=n)
    msg = f"'add_days' filter expects date or datetime, got {type(value).__name__}"
    raise TypeError(msg)


def add_months[D: (date, datetime)](value: D, n: int) -> D:
    """Add or subtract n months from a date or datetime object, clamping day if needed."""
    if not isinstance(n, int) or isinstance(n, bool):
        msg = f"'add_months' filter expects int for months, got {type(n).__name__}"
        raise TypeError(msg)
    if isinstance(value, datetime):
        total_months = value.year * 12 + (value.month - 1) + n
        target_year = total_months // 12
        target_month = total_months % 12 + 1
        max_days = calendar.monthrange(target_year, target_month)[1]
        target_day = min(value.day, max_days)
        return value.replace(year=target_year, month=target_month, day=target_day)
    if isinstance(value, date):
        total_months = value.year * 12 + (value.month - 1) + n
        target_year = total_months // 12
        target_month = total_months % 12 + 1
        max_days = calendar.monthrange(target_year, target_month)[1]
        target_day = min(value.day, max_days)
        return value.replace(year=target_year, month=target_month, day=target_day)
    msg = f"'add_months' filter expects date or datetime, got {type(value).__name__}"
    raise TypeError(msg)


def month_start[D: (date, datetime)](value: D) -> D:
    """Return a date or datetime object corresponding to the first day of the month."""
    if isinstance(value, datetime):
        return value.replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    msg = f"'month_start' filter expects date or datetime, got {type(value).__name__}"
    raise TypeError(msg)


def register_date_filters(env: jinja2.Environment) -> None:
    """Register date filters into a Jinja2 Environment."""
    env.filters["date"] = date_filter
    env.filters["add_days"] = add_days
    env.filters["add_months"] = add_months
    env.filters["month_start"] = month_start
