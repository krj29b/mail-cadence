import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    WorkSchedules,
)
from mail_cadence.scheduler import (
    TaskScheduler,
    TokenBucketRateLimiter,
    get_active_window,
    is_time_in_window,
    parse_time_str,
)


def test_get_active_window() -> None:
    w1 = HourWindow(start="08:00", end="12:00", max_concurrency=2, rate_limit_per_min=30)
    w2 = HourWindow(start="14:00", end="18:00", max_concurrency=4, rate_limit_per_min=60)
    w_overnight = HourWindow(start="22:00", end="04:00", max_concurrency=1, rate_limit_per_min=10)

    # Standard schedule with multiple windows
    sched_multi = Schedule(id="SCHED-MULTI", allowed_hours=[w1, w2])

    dt_w1 = datetime(2026, 10, 17, 10, 0)
    assert get_active_window(sched_multi, dt_w1) == w1

    dt_w2 = datetime(2026, 10, 17, 15, 30)
    assert get_active_window(sched_multi, dt_w2) == w2

    dt_gap = datetime(2026, 10, 17, 13, 0)
    assert get_active_window(sched_multi, dt_gap) is None

    # Overnight schedule
    sched_overnight = Schedule(id="SCHED-NIGHT", allowed_hours=[w_overnight])

    dt_night1 = datetime(2026, 10, 17, 23, 0)
    assert get_active_window(sched_overnight, dt_night1) == w_overnight

    dt_night2 = datetime(2026, 10, 17, 2, 0)
    assert get_active_window(sched_overnight, dt_night2) == w_overnight

    dt_day = datetime(2026, 10, 17, 12, 0)
    assert get_active_window(sched_overnight, dt_day) is None

    # Boundary conditions
    assert get_active_window(sched_multi, datetime(2026, 10, 17, 8, 0)) == w1
    assert get_active_window(sched_multi, datetime(2026, 10, 17, 12, 0)) == w1

    # Empty windows
    sched_empty = Schedule(id="SCHED-EMPTY", allowed_hours=[])
    assert get_active_window(sched_empty, dt_w1) is None


def test_parse_time_str() -> None:
    assert parse_time_str("08:30") == datetime.strptime("08:30", "%H:%M").time()
    assert parse_time_str("14:15:30") == datetime.strptime("14:15:30", "%H:%M:%S").time()


def test_is_time_in_window() -> None:
    t1 = parse_time_str("10:00")
    t2 = parse_time_str("05:00")
    t3 = parse_time_str("23:00")

    s = parse_time_str("06:00")
    e = parse_time_str("12:00")

    assert is_time_in_window(t1, s, e) is True
    assert is_time_in_window(t2, s, e) is False
    assert is_time_in_window(t3, s, e) is False

    # Midnight spanning
    s_overnight = parse_time_str("22:00")
    e_overnight = parse_time_str("04:00")
    assert is_time_in_window(parse_time_str("23:00"), s_overnight, e_overnight) is True
    assert is_time_in_window(parse_time_str("02:00"), s_overnight, e_overnight) is True
    assert is_time_in_window(parse_time_str("12:00"), s_overnight, e_overnight) is False

    # Midnight spanning edge cases: times before start_time and after end_time (outside window)
    assert is_time_in_window(parse_time_str("05:00"), s_overnight, e_overnight) is False
    assert is_time_in_window(parse_time_str("21:00"), s_overnight, e_overnight) is False
    assert is_time_in_window(parse_time_str("04:00:01"), s_overnight, e_overnight) is False
    assert is_time_in_window(parse_time_str("21:59:59"), s_overnight, e_overnight) is False

    # Midnight spanning boundaries: exact start_time and end_time
    assert is_time_in_window(parse_time_str("22:00"), s_overnight, e_overnight) is True
    assert is_time_in_window(parse_time_str("04:00"), s_overnight, e_overnight) is True


def test_task_scheduler_timezones() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )

    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # 08:00 Warsaw time (UTC+1/UTC+2 depending on DST)
    # 2026-10-17 08:00 Warsaw time
    dt_warsaw = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    eligible, window = scheduler.is_task_eligible(task, reference_dt=dt_warsaw)
    assert eligible is True
    assert window is not None
    assert window.max_concurrency == 5

    # 14:00 Warsaw time
    dt_afternoon = datetime(2026, 10, 17, 14, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    eligible, window = scheduler.is_task_eligible(task, reference_dt=dt_afternoon)
    assert eligible is False
    assert window is None


def test_is_task_eligible_naive_datetime() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # Naive datetime during allowed window (08:00)
    naive_dt = datetime(2026, 10, 17, 8, 0)
    assert naive_dt.tzinfo is None
    eligible, window = scheduler.is_task_eligible(task, reference_dt=naive_dt)
    assert eligible is True
    assert window is not None
    assert window.max_concurrency == 5

    # Naive datetime outside allowed window (15:00)
    naive_dt_outside = datetime(2026, 10, 17, 15, 0)
    assert naive_dt_outside.tzinfo is None
    eligible_out, window_out = scheduler.is_task_eligible(task, reference_dt=naive_dt_outside)
    assert eligible_out is False
    assert window_out is None


def test_is_task_eligible_none_datetime() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(
                        start="00:00", end="23:59:59", max_concurrency=5, rate_limit_per_min=60
                    )
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    eligible, window = scheduler.is_task_eligible(task, reference_dt=None)
    assert eligible is True
    assert window is not None


def test_is_task_eligible_tz_conversion() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # 07:00 UTC on Oct 17 is 09:00 in Europe/Warsaw (CEST / UTC+2 in Oct) -> Eligible
    utc_dt = datetime(2026, 10, 17, 7, 0, tzinfo=ZoneInfo("UTC"))
    eligible, window = scheduler.is_task_eligible(task, reference_dt=utc_dt)
    assert eligible is True
    assert window is not None


def test_is_task_eligible_missing_schedule() -> None:
    schedules = WorkSchedules(timezone="Europe/Warsaw", schedules=[])
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="NON-EXISTENT",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    eligible, window = scheduler.is_task_eligible(task)
    assert eligible is False
    assert window is None


def test_is_task_eligible_unknown_schedule_ref() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="UNKNOWN_REF",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    eligible, window = scheduler.is_task_eligible(task)
    assert eligible is False
    assert window is None


def test_is_task_eligible_idempotency_same_window() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # Oct 17, 2026 at 08:00 Warsaw time
    ref_dt = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    # 1. No last run -> eligible
    eligible, window = scheduler.is_task_eligible(task, reference_dt=ref_dt)
    assert eligible is True
    assert window is not None

    # 2. Last run was at 07:00 today (inside 06:00-12:00 window) with SUCCESS -> NOT eligible
    last_run_today = datetime(2026, 10, 17, 7, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    eligible, window = scheduler.is_task_eligible(
        task, reference_dt=ref_dt, last_run=last_run_today, last_status="SUCCESS"
    )
    assert eligible is False
    assert window is None

    # 3. Last run was at 07:00 yesterday with SUCCESS -> eligible
    last_run_yesterday = datetime(2026, 10, 16, 7, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    eligible, window = scheduler.is_task_eligible(
        task, reference_dt=ref_dt, last_run=last_run_yesterday, last_status="SUCCESS"
    )
    assert eligible is True
    assert window is not None


def test_is_task_eligible_idempotency_overnight_window() -> None:
    schedules = WorkSchedules(
        timezone="UTC",
        schedules=[
            Schedule(
                id="SCHED-NIGHT",
                allowed_hours=[
                    HourWindow(start="22:00", end="04:00", max_concurrency=1, rate_limit_per_min=10)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-NIGHT",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # Night run 1: 2026-10-17 at 23:00 UTC
    ref_dt_night1 = datetime(2026, 10, 17, 23, 0, tzinfo=ZoneInfo("UTC"))
    last_run = datetime(2026, 10, 17, 22, 30, tzinfo=ZoneInfo("UTC"))

    # Ran in current 22:00-04:00 window instance -> NOT eligible
    eligible, _ = scheduler.is_task_eligible(
        task, reference_dt=ref_dt_night1, last_run=last_run, last_status="SUCCESS"
    )
    assert eligible is False

    # Check at 02:00 on Oct 18 UTC (same overnight window instance 22:00-04:00) -> NOT eligible
    ref_dt_after_midnight = datetime(2026, 10, 18, 2, 0, tzinfo=ZoneInfo("UTC"))
    eligible, _ = scheduler.is_task_eligible(
        task, reference_dt=ref_dt_after_midnight, last_run=last_run, last_status="SUCCESS"
    )
    assert eligible is False

    # Check next night at 23:00 on Oct 18 UTC (new overnight window instance) -> eligible
    ref_dt_next_night = datetime(2026, 10, 18, 23, 0, tzinfo=ZoneInfo("UTC"))
    eligible, window = scheduler.is_task_eligible(
        task, reference_dt=ref_dt_next_night, last_run=last_run, last_status="SUCCESS"
    )
    assert eligible is True
    assert window is not None


def test_is_task_eligible_idempotency_failed_status() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-MORNING",
                allowed_hours=[
                    HourWindow(start="06:00", end="12:00", max_concurrency=5, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T1",
        schedule_ref="SCHED-MORNING",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    ref_dt = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    last_run_today = datetime(2026, 10, 17, 7, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    # FAILED status -> task should still be eligible to retry
    eligible, window = scheduler.is_task_eligible(
        task, reference_dt=ref_dt, last_run=last_run_today, last_status="FAILED"
    )
    assert eligible is True
    assert window is not None


@pytest.mark.asyncio
async def test_rate_limiter() -> None:
    limiter = TokenBucketRateLimiter(rate_limit_per_min=6000)  # fast limit for test
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 1.0


def test_token_bucket_initial_tokens() -> None:
    # When initial_tokens is None, tokens defaults to capacity
    limiter_default = TokenBucketRateLimiter(rate_limit_per_min=60)
    assert limiter_default.capacity == 60.0
    assert limiter_default.tokens == 60.0

    # When initial_tokens is explicitly provided
    limiter_custom = TokenBucketRateLimiter(rate_limit_per_min=60, initial_tokens=5.0)
    assert limiter_custom.capacity == 60.0
    assert limiter_custom.tokens == 5.0

    # When initial_tokens exceeds capacity, it is capped at capacity
    limiter_capped = TokenBucketRateLimiter(rate_limit_per_min=60, initial_tokens=100.0)
    assert limiter_capped.tokens == 60.0

    # When initial_tokens is negative, it is clamped to 0.0
    limiter_neg = TokenBucketRateLimiter(rate_limit_per_min=60, initial_tokens=-10.0)
    assert limiter_neg.tokens == 0.0


def test_get_active_window_days_of_week_daytime() -> None:
    w = HourWindow(start="08:00", end="17:00", days_of_week="mon,wed,fri")
    sched = Schedule(id="SCHED-MWF", allowed_hours=[w])

    # 2026-10-19 is a Monday
    dt_mon = datetime(2026, 10, 19, 10, 0)
    assert dt_mon.strftime("%a").lower().startswith("mon")
    assert get_active_window(sched, dt_mon) == w

    # 2026-10-20 is a Tuesday
    dt_tue = datetime(2026, 10, 20, 10, 0)
    assert dt_tue.strftime("%a").lower().startswith("tue")
    assert get_active_window(sched, dt_tue) is None


def test_get_active_window_days_of_week_cross_midnight() -> None:
    w = HourWindow(start="22:00", end="04:00", days_of_week="fri")
    sched = Schedule(id="SCHED-FRI-NIGHT", allowed_hours=[w])

    # Friday 2026-10-16 at 23:00 (starts today Friday)
    dt_fri_night = datetime(2026, 10, 16, 23, 0)
    assert get_active_window(sched, dt_fri_night) == w

    # Saturday 2026-10-17 at 02:00 (spans from previous day Friday)
    dt_sat_early = datetime(2026, 10, 17, 2, 0)
    assert get_active_window(sched, dt_sat_early) == w

    # Friday 2026-10-16 at 02:00 (spans from previous day Thursday)
    dt_fri_early = datetime(2026, 10, 16, 2, 0)
    assert get_active_window(sched, dt_fri_early) is None
