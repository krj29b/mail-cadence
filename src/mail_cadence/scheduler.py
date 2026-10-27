import asyncio
import time as time_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from mail_cadence.models import (
    HourWindow,
    MailTask,
    Schedule,
    TaskExecutionStatus,
    WorkSchedules,
    parse_time_str,
)

WEEKDAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

__all__ = [
    "TaskScheduler",
    "TaskWindowStatus",
    "TokenBucketRateLimiter",
    "WindowInstance",
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


@dataclass(frozen=True, slots=True)
class WindowInstance:
    """One occurrence of a schedule window as UTC instants (ADR 0001, D5).

    ``key`` identifies the occurrence: the schedule and the instant it starts.
    """

    schedule_id: str
    window: HourWindow
    start_utc: datetime
    end_utc: datetime

    @property
    def key(self) -> str:
        return f"{self.schedule_id}@{self.start_utc.isoformat()}"


def _window_bounds(window: HourWindow, start_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC bounds of the occurrence that starts on ``start_date`` (local, fold=0)."""
    start_t, end_t = window.start_time, window.end_time
    end_date = start_date if start_t <= end_t else start_date + timedelta(days=1)
    start = datetime.combine(start_date, start_t, tzinfo=tz)
    end = datetime.combine(end_date, end_t, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TaskWindowStatus:
    """Outcome of evaluating a task against its schedule at a reference time."""

    schedule_found: bool
    window: HourWindow | None
    dispatched_in_window: bool

    @property
    def eligible(self) -> bool:
        return self.window is not None and not self.dispatched_in_window

    def skip_reason(self, schedule_ref: str) -> str | None:
        """Human-readable reason why the task is not eligible, or None if it is."""
        if not self.schedule_found:
            return f"Schedule '{schedule_ref}' not found"
        if self.window is None:
            return f"Schedule '{schedule_ref}' not active at current time"
        if self.dispatched_in_window:
            return f"Task already dispatched in current '{schedule_ref}' window"
        return None


class TaskScheduler:
    """Pure scheduling logic over WorkSchedules; it has no database access.

    Load schedules with ``DatabaseRepository.get_work_schedules()`` and pass them in.
    """

    def __init__(self, work_schedules: WorkSchedules) -> None:
        self.work_schedules = work_schedules
        self.timezone = ZoneInfo(self.work_schedules.timezone)
        self._schedules_by_id: dict[str, Schedule] = {
            s.id: s for s in self.work_schedules.schedules
        }

    def has_schedule(self, schedule_id: str) -> bool:
        return schedule_id in self._schedules_by_id

    def now(self) -> datetime:
        """Current time in the schedules' timezone."""
        return datetime.now(self.timezone)

    def active_window(
        self, schedule_id: str, reference_dt: datetime | None = None
    ) -> HourWindow | None:
        """Return the window of ``schedule_id`` active at ``reference_dt`` (default: now)."""
        schedule = self._schedules_by_id.get(schedule_id)
        if schedule is None:
            return None
        return get_active_window(schedule, self._normalize_dt(reference_dt))

    def window_instance(
        self, schedule_id: str, reference_dt: datetime | None = None
    ) -> WindowInstance | None:
        """The window occurrence of ``schedule_id`` that contains ``reference_dt``.

        Activity is decided by comparing UTC instants, so during the repeated hour of a
        DST change only the first occurrence of a local time is inside a window. The
        first window in configuration order wins when windows overlap.
        """
        schedule = self._schedules_by_id.get(schedule_id)
        if schedule is None:
            return None
        ref_utc = self._normalize_dt(reference_dt).astimezone(UTC)
        local_date = ref_utc.astimezone(self.timezone).date()
        for window in schedule.allowed_hours:
            for start_date in (local_date, local_date - timedelta(days=1)):
                if not is_day_matching(window.days_of_week, start_date.weekday()):
                    continue
                start_utc, end_utc = _window_bounds(window, start_date, self.timezone)
                if start_utc <= ref_utc <= end_utc:
                    return WindowInstance(schedule_id, window, start_utc, end_utc)
        return None

    def window_status(
        self,
        task: MailTask,
        reference_dt: datetime | None = None,
        last_run: datetime | None = None,
        last_status: TaskExecutionStatus | str | None = None,
    ) -> TaskWindowStatus:
        """Evaluate the task's schedule window and window-level idempotency at once."""
        schedule = self._schedules_by_id.get(task.schedule_ref)
        if schedule is None:
            return TaskWindowStatus(schedule_found=False, window=None, dispatched_in_window=False)

        ref_dt = self._normalize_dt(reference_dt)
        window = get_active_window(schedule, ref_dt)
        dispatched = window is not None and self._is_dispatched_in_window(
            window, ref_dt, last_run, last_status
        )
        return TaskWindowStatus(schedule_found=True, window=window, dispatched_in_window=dispatched)

    def _is_dispatched_in_window(
        self,
        window: HourWindow,
        ref_dt: datetime,
        last_run: datetime | None,
        last_status: TaskExecutionStatus | str | None,
    ) -> bool:
        is_success = last_status == TaskExecutionStatus.SUCCESS or (
            isinstance(last_status, str) and last_status.upper() == "SUCCESS"
        )
        if not is_success or last_run is None:
            return False
        last_run_tz = self._normalize_dt(last_run, default_tz=UTC)
        window_start_dt, window_end_dt = self._get_window_bounds(window, ref_dt)
        return window_start_dt <= last_run_tz <= window_end_dt

    def _normalize_dt(self, dt: datetime | None, default_tz: tzinfo | None = None) -> datetime:
        tz = default_tz or self.timezone
        if dt is None:
            return self.now()
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
        status = self.window_status(task, reference_dt, last_run, last_status)
        return status.eligible, status.window if status.eligible else None
