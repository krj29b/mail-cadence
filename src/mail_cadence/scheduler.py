import asyncio
import sqlite3
import time as time_module
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mail_cadence.models import (
    HourWindow,
    MailTask,
    Schedule,
    TaskExecutionStatus,
    WorkSchedules,
    parse_time_str,
)
from mail_cadence.repository import DatabaseRepository

WEEKDAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

__all__ = [
    "TaskScheduler",
    "TokenBucketRateLimiter",
    "get_active_window",
    "is_day_matching",
    "is_time_in_window",
    "parse_time_str",
]


def is_day_matching(days_of_week: str, day_idx: int) -> bool:
    if days_of_week == "*":
        return True
    tokens = {t.strip().lower() for t in days_of_week.split(",") if t.strip()}
    return WEEKDAYS[day_idx] in tokens


def is_time_in_window(current_time: time, start_time: time, end_time: time) -> bool:
    if start_time <= end_time:
        return start_time <= current_time <= end_time
    else:
        # Window spans across midnight (e.g., 22:00 to 04:00)
        return current_time >= start_time or current_time <= end_time


def get_active_window(schedule: Schedule, dt: datetime) -> HourWindow | None:
    current_time = dt.time()
    for window in schedule.allowed_hours:
        start_t = window.start_time
        end_t = window.end_time
        if start_t <= end_t:
            if start_t <= current_time <= end_t and is_day_matching(
                window.days_of_week, dt.weekday()
            ):
                return window
        # Cross-midnight window (e.g., 22:00 to 04:00)
        elif current_time >= start_t:
            if is_day_matching(window.days_of_week, dt.weekday()):
                return window
        elif current_time <= end_t:
            prev_day_idx = (dt - timedelta(days=1)).weekday()
            if is_day_matching(window.days_of_week, prev_day_idx):
                return window
    return None


class TokenBucketRateLimiter:
    def __init__(self, rate_limit_per_min: int, initial_tokens: float | None = None) -> None:
        self.rate_limit_per_min = rate_limit_per_min
        self.capacity = float(max(1, rate_limit_per_min))
        if initial_tokens is None:
            self.tokens = self.capacity
        else:
            self.tokens = min(self.capacity, float(max(0.0, initial_tokens)))
        self.fill_rate = self.capacity / 60.0  # tokens per second
        self.last_update = time_module.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate_limit_per_min <= 0:
            return

        async with self._lock:
            while True:
                now = time_module.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return

                needed = 1.0 - self.tokens
                wait_time = needed / self.fill_rate
                await asyncio.sleep(wait_time)


class TaskScheduler:
    def __init__(self, work_schedules: WorkSchedules | sqlite3.Connection) -> None:
        if isinstance(work_schedules, sqlite3.Connection):
            repo = DatabaseRepository(work_schedules)
            self.work_schedules = repo.get_work_schedules()
        else:
            self.work_schedules = work_schedules

        self.timezone = ZoneInfo(self.work_schedules.timezone)
        self._schedules_by_id: dict[str, Schedule] = {
            s.id: s for s in self.work_schedules.schedules
        }

    def _normalize_dt(self, dt: datetime | None, default_tz: ZoneInfo | None = None) -> datetime:
        tz = default_tz or self.timezone
        if dt is None:
            return datetime.now(self.timezone)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz).astimezone(self.timezone)
        return dt.astimezone(self.timezone)

    def _get_window_bounds(
        self, window: HourWindow, reference_dt: datetime
    ) -> tuple[datetime, datetime]:
        start_t = window.start_time
        end_t = window.end_time
        ref_date = reference_dt.date()

        if start_t <= end_t:
            return (
                datetime.combine(ref_date, start_t, tzinfo=self.timezone),
                datetime.combine(ref_date, end_t, tzinfo=self.timezone),
            )
        if reference_dt.time() >= start_t:
            return (
                datetime.combine(ref_date, start_t, tzinfo=self.timezone),
                datetime.combine(ref_date + timedelta(days=1), end_t, tzinfo=self.timezone),
            )
        return (
            datetime.combine(ref_date - timedelta(days=1), start_t, tzinfo=self.timezone),
            datetime.combine(ref_date, end_t, tzinfo=self.timezone),
        )

    def is_task_eligible(
        self,
        task: MailTask,
        reference_dt: datetime | None = None,
        last_run: datetime | None = None,
        last_status: TaskExecutionStatus | str | None = None,
    ) -> tuple[bool, HourWindow | None]:
        schedule = self._schedules_by_id.get(task.schedule_ref)
        if not schedule:
            return False, None

        ref_dt = self._normalize_dt(reference_dt)
        window = get_active_window(schedule, ref_dt)
        if window is None:
            return False, None

        is_success = last_status == TaskExecutionStatus.SUCCESS or (
            isinstance(last_status, str) and last_status.upper() == "SUCCESS"
        )
        if is_success and last_run is not None:
            last_run_tz = self._normalize_dt(last_run, default_tz=UTC)  # type: ignore[arg-type]
            window_start_dt, window_end_dt = self._get_window_bounds(window, ref_dt)
            if window_start_dt <= last_run_tz <= window_end_dt:
                return False, None

        return True, window
