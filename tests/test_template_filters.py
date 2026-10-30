from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.mailer import TemplateRenderer
from mail_cadence.template_filters import (
    add_days,
    add_months,
    date_filter,
    month_start,
)


def test_date_filter_date_and_datetime() -> None:
    d = date(2026, 3, 15)
    assert date_filter(d) == "2026-03-15"
    assert date_filter(d, "%d/%m/%Y") == "15/03/2026"

    tz = ZoneInfo("Europe/Warsaw")
    dt = datetime(2026, 3, 15, 14, 30, tzinfo=tz)
    assert date_filter(dt) == "2026-03-15"
    assert date_filter(dt, "%Y-%m-%d %H:%M") == "2026-03-15 14:30"


def test_add_days_date_and_datetime() -> None:
    d = date(2026, 3, 15)
    assert add_days(d, 5) == date(2026, 3, 20)
    assert add_days(d, -10) == date(2026, 3, 5)

    tz = ZoneInfo("Europe/Warsaw")
    # Last Sunday of October 2026 is Oct 25 (DST transition from CEST +02:00 to CET +01:00)
    dt = datetime(2026, 10, 24, 12, 0, tzinfo=tz)
    dt_after_dst = add_days(dt, 1)
    assert dt_after_dst == datetime(2026, 10, 25, 12, 0, tzinfo=tz)
    assert dt_after_dst.time() == dt.time()
    assert dt_after_dst.tzinfo == dt.tzinfo


def test_add_months_year_boundaries() -> None:
    d = date(2026, 12, 15)
    assert add_months(d, 1) == date(2027, 1, 15)
    assert add_months(d, 14) == date(2028, 2, 15)

    d2 = date(2026, 1, 15)
    assert add_months(d2, -1) == date(2025, 12, 15)
    assert add_months(d2, -13) == date(2024, 12, 15)


def test_add_months_month_end_clamping_and_leap_years() -> None:
    # 2026-03-31 minus 1 month = 2026-02-28 (non-leap year)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    # 2028-03-31 minus 1 month = 2028-02-29 (leap year)
    assert add_months(date(2028, 3, 31), -1) == date(2028, 2, 29)
    # January 31 plus 1 month
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)


def test_add_months_zero_and_datetime() -> None:
    tz = ZoneInfo("Europe/Warsaw")
    dt = datetime(2026, 3, 31, 10, 20, 30, tzinfo=tz)
    assert add_months(dt, 0) == dt
    dt_sub = add_months(dt, -1)
    assert dt_sub == datetime(2026, 2, 28, 10, 20, 30, tzinfo=tz)


def test_month_start_date_and_datetime() -> None:
    d = date(2026, 3, 15)
    assert month_start(d) == date(2026, 3, 1)

    tz = ZoneInfo("Europe/Warsaw")
    dt = datetime(2026, 3, 15, 18, 45, tzinfo=tz)
    assert month_start(dt) == datetime(2026, 3, 1, 18, 45, tzinfo=tz)


def test_type_errors_for_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="'date' filter expects date or datetime, got str"):
        date_filter("2026-03-15")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="'date' filter expects date or datetime, got NoneType"):
        date_filter(None)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="'add_days' filter expects date or datetime, got str"):
        add_days("2026-03-15", 1)  # type: ignore[type-var]

    with pytest.raises(TypeError, match="'add_days' filter expects int for days, got str"):
        add_days(date(2026, 3, 15), "1")  # type: ignore[arg-type]

    msg_months_type = "'add_months' filter expects date or datetime, got NoneType"
    with pytest.raises(TypeError, match=msg_months_type):
        add_months(None, 1)  # type: ignore[type-var]

    with pytest.raises(TypeError, match="'add_months' filter expects int for months, got str"):
        add_months(date(2026, 3, 15), "1")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="'month_start' filter expects date or datetime, got str"):
        month_start("2026-03-15")  # type: ignore[type-var]


def test_filters_registered_on_template_renderer_env(tmp_path: Path) -> None:
    renderer = TemplateRenderer(template_dir=tmp_path)
    d = date(2026, 3, 15)

    tpl_date = renderer.env.from_string("{{ dt | date('%Y/%m/%d') }}")
    assert tpl_date.render(dt=d) == "2026/03/15"

    tpl_add_days = renderer.env.from_string("{{ dt | add_days(5) | date }}")
    assert tpl_add_days.render(dt=d) == "2026-03-20"

    tpl_add_months = renderer.env.from_string("{{ dt | add_months(-1) | date }}")
    assert tpl_add_months.render(dt=d) == "2026-02-15"

    tpl_month_start = renderer.env.from_string("{{ dt | month_start | date }}")
    assert tpl_month_start.render(dt=d) == "2026-03-01"
