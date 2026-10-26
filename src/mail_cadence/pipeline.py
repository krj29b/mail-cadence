"""Dispatch pipeline: the one place that runs tasks, logs results and sends the report.

The CLI (`cli.py`) and the web panel (`web.py`) are thin adapters over this module.
It opens its own SQLite connection, so callers pass a database path, not a connection.
"""

import asyncio
import logging
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from mail_cadence.database import get_connection, init_db
from mail_cadence.mailer import AsyncMailer, TemplateRenderer
from mail_cadence.models import (
    PRIORITY_ORDER,
    BatchSummaryReport,
    MailConfiguration,
    MailTask,
    Settings,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.reporter import ReportGenerator, aggregate_summary_report
from mail_cadence.repository import DatabaseRepository
from mail_cadence.scheduler import TaskScheduler, TokenBucketRateLimiter
from mail_cadence.xml_loader import XMLValidationError, import_from_xml

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_TASK_FAILED = 2

ExecutionStates = dict[str, tuple[datetime | None, TaskExecutionStatus | None]]
ScheduledTask = tuple[MailTask, TokenBucketRateLimiter, asyncio.Semaphore]
WindowKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class DispatchOptions:
    """What to run. ``db_path`` must already be resolved (no settings lookup here)."""

    db_path: str | Path
    template_dir: str | Path = "."
    dry_run: bool = False
    ref_dt: datetime | None = None
    tasks_xml: Path | None = None
    schedule_xml: Path | None = None


@dataclass(frozen=True, slots=True)
class _LoadedConfig:
    mail_config: MailConfiguration
    work_schedules: WorkSchedules
    execution_states: ExecutionStates


async def run_dispatch(options: DispatchOptions, settings: Settings) -> int:
    """Run one dispatch batch. Returns 0 (ok), 1 (configuration error) or 2 (a task failed)."""
    start_time = datetime.now(UTC)
    logger.info(
        f"Starting MailCadence email dispatch engine "
        f"(dry_run={options.dry_run}, db={options.db_path})..."
    )

    with closing(get_connection(options.db_path)) as conn:
        try:
            init_db(conn)
        except sqlite3.Error as exc:
            logger.error(f"Cannot open or migrate database {options.db_path}: {exc}")
            return EXIT_CONFIG_ERROR
        repo = DatabaseRepository(conn)

        if not _seed_xml_if_provided(conn, options):
            return EXIT_CONFIG_ERROR
        loaded = _load_config(repo)
        if loaded is None:
            return EXIT_CONFIG_ERROR
        if not loaded.mail_config.tasks:
            logger.warning("No tasks found in database.")
            return EXIT_OK

        batch_id = str(uuid.uuid4())
        scheduled, results = _partition_eligible_tasks(loaded, options.ref_dt, repo, batch_id)
        logger.info(f"Scheduled {len(scheduled)} tasks for execution ({len(results)} skipped).")

        renderer = TemplateRenderer(template_dir=options.template_dir)
        mailer = AsyncMailer(settings=settings, renderer=renderer, dry_run=options.dry_run)
        async with mailer:
            results.extend(await _execute_tasks(mailer, scheduled, repo, batch_id, options))
            report = aggregate_summary_report(results, start_time, datetime.now(UTC))
            await _send_report_if_needed(
                report, loaded.mail_config, repo.has_report_settings(), mailer
            )

    logger.info("MailCadence execution complete.")
    return EXIT_OK if report.failed_tasks == 0 else EXIT_TASK_FAILED


async def dispatch_single_task(
    db_path: str | Path, task_id: str, settings: Settings, template_dir: str | Path = "."
) -> TaskExecutionResult:
    """Send one task now, ignoring its schedule window, and log the result.

    Raises LookupError if the task does not exist.
    """
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        task = repo.get_mail_task(task_id)
        if task is None:
            msg = f"Task '{task_id}' does not exist"
            raise LookupError(msg)
        mailer = AsyncMailer(settings=settings, renderer=TemplateRenderer(template_dir))
        async with mailer:
            result = await mailer.dispatch_task(task)
        repo.log_task_execution(result)
        return result


def _seed_xml_if_provided(conn: sqlite3.Connection, options: DispatchOptions) -> bool:
    if not (options.tasks_xml and options.schedule_xml):
        return True
    try:
        import_from_xml(conn, options.tasks_xml, options.schedule_xml, mode="upsert")
    except XMLValidationError as exc:
        logger.error(f"Configuration error: {exc}")
        return False
    except sqlite3.Error as exc:
        logger.error(f"Database error while importing XML configs: {exc}")
        return False
    return True


def _load_config(repo: DatabaseRepository) -> _LoadedConfig | None:
    try:
        return _LoadedConfig(
            mail_config=repo.get_mail_configuration(),
            work_schedules=repo.get_work_schedules(),
            execution_states=repo.get_task_execution_states(),
        )
    except (sqlite3.Error, ValidationError) as exc:
        logger.error(f"Failed to load configuration from database: {exc}")
        return None


def _partition_eligible_tasks(
    loaded: _LoadedConfig,
    ref_dt: datetime | None,
    repo: DatabaseRepository,
    batch_id: str,
) -> tuple[list[ScheduledTask], list[TaskExecutionResult]]:
    """Split tasks into scheduled ones (with shared per-window limits) and logged skips."""
    scheduler = TaskScheduler(loaded.work_schedules)
    scheduled: list[ScheduledTask] = []
    skipped: list[TaskExecutionResult] = []
    window_controls: dict[WindowKey, tuple[TokenBucketRateLimiter, asyncio.Semaphore]] = {}

    for task in loaded.mail_config.tasks:
        last_run, last_status = loaded.execution_states.get(task.id, (None, None))
        status = scheduler.window_status(
            task, reference_dt=ref_dt, last_run=last_run, last_status=last_status
        )
        window = status.window
        if status.eligible and window is not None:
            key = (task.schedule_ref, window.start, window.end)
            if key not in window_controls:
                window_controls[key] = (
                    TokenBucketRateLimiter(
                        window.rate_limit_per_min,
                        initial_tokens=float(window.max_concurrency),
                    ),
                    asyncio.Semaphore(window.max_concurrency),
                )
            scheduled.append((task, *window_controls[key]))
            continue

        reason = status.skip_reason(task.schedule_ref)
        logger.info(f"Task {task.id} skipped due to schedule rules ({reason}).")
        result = TaskExecutionResult(
            task_id=task.id,
            status=TaskExecutionStatus.SKIPPED_SCHEDULE,
            error_details=reason,
        )
        skipped.append(result)
        repo.log_task_execution(result, batch_id=batch_id)

    return scheduled, skipped


async def _execute_tasks(
    mailer: AsyncMailer,
    scheduled: list[ScheduledTask],
    repo: DatabaseRepository,
    batch_id: str,
    options: DispatchOptions,
) -> list[TaskExecutionResult]:
    """Run all scheduled tasks concurrently; priority only sets the start order."""

    async def execute(
        task: MailTask, rate_limiter: TokenBucketRateLimiter, sem: asyncio.Semaphore
    ) -> TaskExecutionResult:
        async with sem:
            logger.info(f"Executing task {task.id}...")
            result = await mailer.dispatch_task(task, rate_limiter=rate_limiter)
            repo.log_task_execution(
                result, batch_id=batch_id, dry_run=options.dry_run, executed_at=options.ref_dt
            )
            return result

    if not scheduled:
        return []
    ordered = sorted(scheduled, key=lambda item: (PRIORITY_ORDER[item[0].priority], item[0].id))
    async with asyncio.TaskGroup() as tg:
        running = [tg.create_task(execute(*item)) for item in ordered]
    return [t.result() for t in running]


async def _send_report_if_needed(
    report: BatchSummaryReport,
    mail_config: MailConfiguration,
    has_report_settings: bool,
    mailer: AsyncMailer,
) -> None:
    executed = report.successful_tasks + report.partial_tasks + report.failed_tasks
    if executed == 0:
        logger.info("No tasks were executed in this batch; status report not sent.")
        return
    if not has_report_settings:
        logger.warning("Report settings are not configured; status report not sent.")
        return
    await ReportGenerator().send_report(report, mail_config.report_settings, mailer)
