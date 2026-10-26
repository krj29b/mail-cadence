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


def test_dst_europe_warsaw_summer_and_winter() -> None:
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-WARSAW-DAY",
                allowed_hours=[
                    HourWindow(start="08:00", end="10:00", max_concurrency=1, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T-DST",
        schedule_ref="SCHED-WARSAW-DAY",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # Summer Time (CEST, UTC+2): 08:00-10:00 CEST == 06:00-08:00 UTC
    utc_summer_0600 = datetime(2026, 7, 15, 6, 0, tzinfo=ZoneInfo("UTC"))
    eligible_summer_0600, w_summer = scheduler.is_task_eligible(task, reference_dt=utc_summer_0600)
    assert eligible_summer_0600 is True
    assert w_summer is not None

    utc_summer_0559 = datetime(2026, 7, 15, 5, 59, tzinfo=ZoneInfo("UTC"))
    eligible_summer_0559, _ = scheduler.is_task_eligible(task, reference_dt=utc_summer_0559)
    assert eligible_summer_0559 is False

    # Winter Time (CET, UTC+1): 08:00-10:00 CET == 07:00-09:00 UTC
    utc_winter_0700 = datetime(2026, 1, 15, 7, 0, tzinfo=ZoneInfo("UTC"))
    eligible_winter_0700, w_winter = scheduler.is_task_eligible(task, reference_dt=utc_winter_0700)
    assert eligible_winter_0700 is True
    assert w_winter is not None

    utc_winter_0600 = datetime(2026, 1, 15, 6, 0, tzinfo=ZoneInfo("UTC"))
    eligible_winter_0600, _ = scheduler.is_task_eligible(task, reference_dt=utc_winter_0600)
    assert eligible_winter_0600 is False


def test_dst_europe_warsaw_cross_midnight_transitions() -> None:
    # Overnight window 22:00 to 04:00
    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-WARSAW-NIGHT",
                allowed_hours=[
                    HourWindow(start="22:00", end="04:00", max_concurrency=1, rate_limit_per_min=60)
                ],
            )
        ],
    )
    scheduler = TaskScheduler(schedules)
    task = MailTask(
        id="T-NIGHT",
        schedule_ref="SCHED-WARSAW-NIGHT",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )

    # 1. Spring-forward transition (March 28 to March 29, 2026):
    # Transition occurs at 02:00 CET -> 03:00 CEST on March 29.
    # On March 28 at 23:00 Warsaw (22:00 UTC):
    ref_spring_start = datetime(2026, 3, 28, 22, 0, tzinfo=ZoneInfo("UTC"))
    eligible_spring_start, _ = scheduler.is_task_eligible(task, reference_dt=ref_spring_start)
    assert eligible_spring_start is True

    # Execution during spring night at 23:30 Warsaw (22:30 UTC):
    last_run_spring = datetime(2026, 3, 28, 22, 30, tzinfo=ZoneInfo("UTC"))

    # Early morning March 29 after spring-forward shift at 03:30 CEST (01:30 UTC):
    ref_spring_morning = datetime(2026, 3, 29, 1, 30, tzinfo=ZoneInfo("UTC"))
    eligible_spring_morning, _ = scheduler.is_task_eligible(
        task, reference_dt=ref_spring_morning, last_run=last_run_spring, last_status="SUCCESS"
    )
    assert eligible_spring_morning is False

    # 2. Autumn fall-back transition (October 24 to October 25, 2026):
    # Transition occurs at 03:00 CEST -> 02:00 CET on October 25.
    # On Oct 24 at 23:00 Warsaw (21:00 UTC):
    ref_autumn_start = datetime(2026, 10, 24, 21, 0, tzinfo=ZoneInfo("UTC"))
    eligible_autumn_start, _ = scheduler.is_task_eligible(task, reference_dt=ref_autumn_start)
    assert eligible_autumn_start is True

    # Execution during autumn night at 23:30 Warsaw (21:30 UTC):
    last_run_autumn = datetime(2026, 10, 24, 21, 30, tzinfo=ZoneInfo("UTC"))

    # Early morning Oct 25 after fall-back shift at 03:00 CET (02:00 UTC) in same window instance:
    ref_autumn_morning = datetime(2026, 10, 25, 2, 0, tzinfo=ZoneInfo("UTC"))
    eligible_autumn_morning, _ = scheduler.is_task_eligible(
        task, reference_dt=ref_autumn_morning, last_run=last_run_autumn, last_status="SUCCESS"
    )
    assert eligible_autumn_morning is False


def _window_status_scheduler() -> TaskScheduler:
    return TaskScheduler(
        WorkSchedules(
            timezone="Europe/Warsaw",
            schedules=[
                Schedule(
                    id="SCHED-MORNING",
                    allowed_hours=[HourWindow(start="06:00", end="12:00")],
                )
            ],
        )
    )


def _window_status_task(schedule_ref: str = "SCHED-MORNING") -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref=schedule_ref,
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(template_path="t.j2"),
    )


def test_window_status_and_skip_reasons() -> None:
    scheduler = _window_status_scheduler()
    warsaw = ZoneInfo("Europe/Warsaw")
    in_window = datetime(2026, 10, 17, 8, 0, tzinfo=warsaw)
    outside = datetime(2026, 10, 17, 3, 0, tzinfo=warsaw)
    sent_today = datetime(2026, 10, 17, 6, 30, tzinfo=ZoneInfo("UTC"))  # 08:30 Warsaw

    ready = scheduler.window_status(_window_status_task(), reference_dt=in_window)
    assert ready.eligible
    assert ready.window is not None
    assert ready.skip_reason("SCHED-MORNING") is None

    sent = scheduler.window_status(
        _window_status_task(), reference_dt=in_window, last_run=sent_today, last_status="SUCCESS"
    )
    assert sent.dispatched_in_window
    assert not sent.eligible
    assert sent.window is not None
    assert "already dispatched" in (sent.skip_reason("SCHED-MORNING") or "")

    failed = scheduler.window_status(
        _window_status_task(), reference_dt=in_window, last_run=sent_today, last_status="FAILED"
    )
    assert failed.eligible

    closed = scheduler.window_status(_window_status_task(), reference_dt=outside)
    assert closed.window is None
    assert "not active" in (closed.skip_reason("SCHED-MORNING") or "")

    missing = scheduler.window_status(_window_status_task("NOPE"), reference_dt=in_window)
    assert not missing.schedule_found
    assert missing.skip_reason("NOPE") == "Schedule 'NOPE' not found"


def test_active_window_and_now() -> None:
    scheduler = _window_status_scheduler()
    warsaw = ZoneInfo("Europe/Warsaw")
    assert scheduler.active_window("SCHED-MORNING", datetime(2026, 10, 17, 8, 0, tzinfo=warsaw))
    assert (
        scheduler.active_window("SCHED-MORNING", datetime(2026, 10, 17, 3, 0, tzinfo=warsaw))
        is None
    )
    assert scheduler.active_window("NOPE") is None
    assert scheduler.now().tzinfo == warsaw
