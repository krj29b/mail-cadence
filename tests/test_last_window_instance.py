"""TaskScheduler.last_window_instance: the occurrence a manual send and the panel
preview use (plans: send variables, manual run = latest started occurrence)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.models import HourWindow, Schedule, WorkSchedules
from mail_cadence.scheduler import TaskScheduler

WARSAW = ZoneInfo("Europe/Warsaw")


def _scheduler(*windows: HourWindow, schedule_id: str = "S") -> TaskScheduler:
    return TaskScheduler(
        WorkSchedules(
            timezone="Europe/Warsaw",
            schedules=[Schedule(id=schedule_id, allowed_hours=list(windows))],
        )
    )


def _local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=WARSAW)


def _start(scheduler: TaskScheduler, ref: datetime) -> datetime | None:
    instance = scheduler.last_window_instance("S", ref)
    return None if instance is None else instance.start_utc.astimezone(WARSAW)


def test_open_window_is_the_scheduled_occurrence() -> None:
    scheduler = _scheduler(HourWindow(start="06:00", end="08:00"))
    ref = _local(2026, 10, 28, 7, 0)

    assert scheduler.last_window_instance("S", ref) == scheduler.window_instance("S", ref)
    instance = scheduler.last_window_instance("S", ref)
    assert instance is not None
    assert instance.key == "S@2026-10-28T05:00:00+00:00"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (_local(2026, 10, 28, 10, 0), _local(2026, 10, 28, 6, 0)),  # ended today
        (_local(2026, 10, 28, 5, 0), _local(2026, 10, 27, 6, 0)),  # not started yet today
    ],
    ids=["ended-today", "before-today"],
)
def test_daily_window(ref: datetime, expected: datetime) -> None:
    assert _start(_scheduler(HourWindow(start="06:00", end="08:00")), ref) == expected


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (_local(2026, 10, 29, 3, 0), _local(2026, 10, 28, 22, 0)),  # after it ended
        (_local(2026, 10, 29, 21, 0), _local(2026, 10, 28, 22, 0)),  # before the next one
        (_local(2026, 10, 29, 1, 0), _local(2026, 10, 28, 22, 0)),  # still open
    ],
    ids=["after-end", "before-next", "open"],
)
def test_window_across_midnight_belongs_to_its_start_day(ref: datetime, expected: datetime) -> None:
    assert _start(_scheduler(HourWindow(start="22:00", end="02:00")), ref) == expected


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (_local(2026, 11, 1, 12, 0), _local(2026, 10, 26, 6, 0)),  # Sunday -> last Monday
        (_local(2026, 10, 26, 5, 0), _local(2026, 10, 19, 6, 0)),  # Monday, before it starts
    ],
    ids=["six-days-later", "seven-days-back"],
)
def test_weekly_window(ref: datetime, expected: datetime) -> None:
    window = HourWindow(start="06:00", end="07:00", days_of_week="mon")
    assert _start(_scheduler(window), ref) == expected


def test_weekly_window_across_midnight_seven_days_back() -> None:
    window = HourWindow(start="22:00", end="02:00", days_of_week="sun")
    # Sunday 21:00: today's occurrence has not started; last Sunday's has.
    assert _start(_scheduler(window), _local(2026, 11, 1, 21, 0)) == _local(2026, 10, 25, 22, 0)


@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
def test_latest_start_wins_whatever_the_configuration_order(reverse: bool) -> None:
    windows = [HourWindow(start="06:00", end="07:00"), HourWindow(start="12:00", end="13:00")]
    scheduler = _scheduler(*(reversed(windows) if reverse else windows))

    assert _start(scheduler, _local(2026, 10, 28, 14, 0)) == _local(2026, 10, 28, 12, 0)
    assert _start(scheduler, _local(2026, 10, 28, 9, 0)) == _local(2026, 10, 28, 6, 0)


def test_equal_starts_take_the_first_window_in_configuration_order() -> None:
    short = HourWindow(start="06:00", end="07:00")
    long = HourWindow(start="06:00", end="09:00")
    scheduler = _scheduler(short, long)

    instance = scheduler.last_window_instance("S", _local(2026, 10, 28, 10, 0))
    assert instance is not None
    assert instance.window == short


@pytest.mark.parametrize(
    ("ref", "expected_utc"),
    [
        # Winter time starts on 2026-10-25: Sunday 08:00 CET is 07:00 UTC.
        (_local(2026, 10, 26, 10, 0), datetime(2026, 10, 25, 7, 0, tzinfo=UTC)),
        # Summer time starts on 2026-03-29: Sunday 08:00 CEST is 06:00 UTC.
        (_local(2026, 3, 30, 10, 0), datetime(2026, 3, 29, 6, 0, tzinfo=UTC)),
    ],
    ids=["to-winter-time", "to-summer-time"],
)
def test_dst_change_uses_the_offset_of_the_start_day(ref: datetime, expected_utc: datetime) -> None:
    window = HourWindow(start="08:00", end="09:00", days_of_week="sun")
    instance = _scheduler(window).last_window_instance("S", ref)

    assert instance is not None
    assert instance.start_utc == expected_utc


def test_reference_in_utc_is_converted_to_the_schedule_timezone() -> None:
    scheduler = _scheduler(HourWindow(start="00:30", end="01:00"))
    # 2026-10-27 23:45 UTC is 00:45 on 2026-10-28 in Warsaw: today's window is open.
    ref = datetime(2026, 10, 27, 23, 45, tzinfo=UTC)

    assert _start(scheduler, ref) == _local(2026, 10, 28, 0, 30)


def test_schedule_without_windows_or_unknown_schedule_has_no_occurrence() -> None:
    scheduler = _scheduler()
    ref = _local(2026, 10, 28, 10, 0)

    assert scheduler.last_window_instance("S", ref) is None
    assert scheduler.last_window_instance("missing", ref) is None
