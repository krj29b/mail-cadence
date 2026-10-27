"""Dispatch pipeline: the one place that runs tasks, logs results and sends the report.

The CLI (`cli.py`) and the web panel (`web.py`) are thin adapters over this module.
It opens its own SQLite connection, so callers pass a database path, not a connection.

Real runs follow docs/adr/0001-execution-state-model.md: every task is claimed for its
window occurrence before anything is sent, the message is rendered once and stored,
each SMTP attempt and its per-recipient result are persisted, and a result that may
have been delivered is never sent again automatically. Dry runs only read that state.
"""

import asyncio
import contextlib
import logging
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import (
    Claim,
    ClaimRequest,
    ClaimStatus,
    ExecutionOutcome,
    StaleClaimError,
)
from mail_cadence.mailer import (
    INSECURE_SMTP_AUTH_MESSAGE,
    RENDER_ERRORS,
    AsyncMailer,
    TemplateRenderer,
)
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
from mail_cadence.scheduler import TaskScheduler, TokenBucketRateLimiter, WindowInstance
from mail_cadence.xml_loader import XMLValidationError, import_from_xml

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_TASK_FAILED = 2
EXIT_UNKNOWN = 3
EXIT_REPORT_FAILED = 4

HEARTBEAT_SECONDS = 60.0

WindowControls = tuple[TokenBucketRateLimiter, asyncio.Semaphore]
SimulatedTask = tuple[MailTask, TokenBucketRateLimiter, asyncio.Semaphore]

# Execution state -> status reported for the task (ADR 0001, D9/D11).
_STATE_STATUS: dict[str, TaskExecutionStatus] = {
    "SUCCEEDED": TaskExecutionStatus.SUCCESS,
    "PARTIAL": TaskExecutionStatus.PARTIAL,
    "NEEDS_REVIEW": TaskExecutionStatus.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class DispatchOptions:
    """What to run. ``db_path`` must already be resolved (no settings lookup here).

    ``ref_dt`` chooses the window occurrence; leases and timestamps use the real clock.
    """

    db_path: str | Path
    template_dir: str | Path = "."
    dry_run: bool = False
    ref_dt: datetime | None = None
    tasks_xml: Path | None = None
    schedule_xml: Path | None = None
    source: str = "cli"


@dataclass(frozen=True, slots=True)
class _LoadedConfig:
    mail_config: MailConfiguration
    work_schedules: WorkSchedules
    schedule_versions: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Scheduled:
    task: MailTask
    claim: Claim
    limiter: TokenBucketRateLimiter
    semaphore: asyncio.Semaphore


@dataclass(slots=True)
class _Plan:
    """Claimed tasks, logged skips, and outcomes of executions finished earlier in the
    same window, which still decide the exit code (ADR 0001, D11)."""

    scheduled: list[_Scheduled] = field(default_factory=list)
    skipped: list[TaskExecutionResult] = field(default_factory=list)
    carried: list[TaskExecutionStatus] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Run:
    repo: DatabaseRepository
    mailer: AsyncMailer
    run_id: str
    owner: str
    ref_dt: datetime | None


async def run_dispatch(options: DispatchOptions, settings: Settings) -> int:
    """Run one dispatch batch and return its exit code.

    0: ok; 1: configuration or database error; 2: a task failed or was delivered only
    partially; 3: the outcome of a delivery is unknown and needs review; 4: all tasks
    ok, but the status report was not sent. Precedence: 3, 1, 2, 4.
    """
    if not options.dry_run and settings.smtp_auth_blocked:
        logger.error(f"Configuration error: {INSECURE_SMTP_AUTH_MESSAGE}")
        return EXIT_CONFIG_ERROR
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

        renderer = TemplateRenderer(template_dir=options.template_dir)
        mailer = AsyncMailer(settings=settings, renderer=renderer, dry_run=options.dry_run)
        if options.dry_run:
            code = await _simulate(repo, mailer, loaded, options, start_time)
        else:
            code = await _dispatch(repo, mailer, loaded, options, start_time)

    logger.info("MailCadence execution complete.")
    return code


def _exit_code(
    statuses: Iterable[TaskExecutionStatus], *, infra_error: bool = False, report_ok: bool
) -> int:
    seen = set(statuses)
    if TaskExecutionStatus.UNKNOWN in seen:
        return EXIT_UNKNOWN
    if infra_error:
        return EXIT_CONFIG_ERROR
    if seen & {TaskExecutionStatus.FAILED, TaskExecutionStatus.PARTIAL}:
        return EXIT_TASK_FAILED
    return EXIT_OK if report_ok else EXIT_REPORT_FAILED


# --- Real dispatch ---------------------------------------------------------------------


async def _dispatch(
    repo: DatabaseRepository,
    mailer: AsyncMailer,
    loaded: _LoadedConfig,
    options: DispatchOptions,
    start_time: datetime,
) -> int:
    run_id = str(uuid.uuid4())
    run = _Run(repo, mailer, run_id, _owner_id(run_id), options.ref_dt)
    try:
        repo.start_run(run.run_id, options.source)
        recovered = repo.recover_abandoned()
        if recovered:
            logger.warning(f"Recovered abandoned executions {recovered}; see NEEDS_REVIEW.")
        plan = _plan(run, loaded)
    except sqlite3.Error as exc:
        logger.error(f"Database error while claiming tasks: {exc}")
        return EXIT_CONFIG_ERROR
    logger.info(f"Claimed {len(plan.scheduled)} tasks ({len(plan.skipped)} skipped).")

    async with mailer, _heartbeat(repo, run.owner):
        results = await _execute_claimed(run, plan.scheduled)
        report = aggregate_summary_report(plan.skipped + results, start_time, datetime.now(UTC))
        report_ok = await _send_report_if_needed(
            report, loaded.mail_config, repo.has_report_settings(), mailer
        )

    statuses = [r.status for r in results] + plan.carried
    code = _exit_code(statuses, report_ok=report_ok)
    with contextlib.suppress(sqlite3.Error):
        repo.finish_run(run.run_id, code, _summary(statuses), "ok" if report_ok else "failed")
    return code


def _owner_id(run_id: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{run_id[:8]}"


def _summary(statuses: list[TaskExecutionStatus]) -> str:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
    return ",".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"


def _effective_bounds(
    instance: WindowInstance, ref_dt: datetime | None
) -> tuple[datetime, datetime]:
    """Window bounds on the real clock: with ``ref_dt`` the remaining time is kept."""
    if ref_dt is None:
        return instance.start_utc, instance.end_utc
    shift = datetime.now(UTC) - ref_dt.astimezone(UTC)
    return instance.start_utc + shift, instance.end_utc + shift


def _plan(run: _Run, loaded: _LoadedConfig) -> _Plan:
    """Claim every task whose window is open now; log the others as skipped."""
    scheduler = TaskScheduler(loaded.work_schedules)
    controls: dict[str, WindowControls] = {}
    plan = _Plan()
    for task in loaded.mail_config.tasks:
        instance = scheduler.window_instance(task.schedule_ref, run.ref_dt)
        if instance is None:
            _skip(run, plan, task.id, _inactive_reason(scheduler, task))
            continue
        start, end = _effective_bounds(instance, run.ref_dt)
        result = run.repo.claim_execution(
            ClaimRequest(
                task_id=task.id,
                window_key=instance.key,
                run_id=run.run_id,
                owner=run.owner,
                window_start=start,
                window_end=end,
                schedule_version=loaded.schedule_versions.get(task.schedule_ref),
            )
        )
        if result.status == ClaimStatus.CLAIMED and result.claim is not None:
            limiter, semaphore = _window_controls(controls, instance)
            plan.scheduled.append(_Scheduled(task, result.claim, limiter, semaphore))
        else:
            _skip_claimed(run, plan, task.id, result.status, result.execution_id)
    return plan


def _inactive_reason(scheduler: TaskScheduler, task: MailTask) -> str:
    if not scheduler.has_schedule(task.schedule_ref):
        return f"Schedule '{task.schedule_ref}' not found"
    return f"Schedule '{task.schedule_ref}' not active at current time"


def _window_controls(
    controls: dict[str, WindowControls], instance: WindowInstance
) -> WindowControls:
    """One rate limiter and semaphore per window occurrence, shared by its tasks."""
    if instance.key not in controls:
        window = instance.window
        controls[instance.key] = (
            TokenBucketRateLimiter(
                window.rate_limit_per_min, initial_tokens=float(window.max_concurrency)
            ),
            asyncio.Semaphore(window.max_concurrency),
        )
    return controls[instance.key]


def _skip(run: _Run, plan: _Plan, task_id: str, reason: str, *, busy: bool = False) -> None:
    status = TaskExecutionStatus.SKIPPED_BUSY if busy else TaskExecutionStatus.SKIPPED_SCHEDULE
    logger.info(f"Task {task_id} skipped ({reason}).")
    result = TaskExecutionResult(task_id=task_id, status=status, error_details=reason)
    plan.skipped.append(result)
    run.repo.log_task_execution(result, batch_id=run.run_id)


def _skip_claimed(
    run: _Run, plan: _Plan, task_id: str, status: ClaimStatus, execution_id: int | None
) -> None:
    if status == ClaimStatus.BUSY:
        _skip(run, plan, task_id, f"execution {execution_id} is running elsewhere", busy=True)
        return
    if status == ClaimStatus.NO_TASK or execution_id is None:
        _skip(run, plan, task_id, "task was deleted")
        return
    outcome = run.repo.execution_outcome(execution_id)
    carried = _status_of(outcome)
    if carried != TaskExecutionStatus.SUCCESS:
        plan.carried.append(carried)
    _skip(
        run,
        plan,
        task_id,
        f"already executed in this window: execution {execution_id} is {outcome.state}",
    )


def _status_of(outcome: ExecutionOutcome) -> TaskExecutionStatus:
    status = _STATE_STATUS.get(outcome.state)
    if status is not None:
        return status
    return TaskExecutionStatus.PARTIAL if outcome.accepted else TaskExecutionStatus.FAILED


@contextlib.asynccontextmanager
async def _heartbeat(repo: DatabaseRepository, owner: str) -> AsyncIterator[None]:
    """Renew the lease of everything ``owner`` holds while the body runs."""
    stop = asyncio.Event()

    async def beat() -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), HEARTBEAT_SECONDS)
                return
            try:
                repo.heartbeat(owner)
            except sqlite3.Error as exc:
                logger.warning(f"Lease renewal failed: {exc}")

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        stop.set()
        await task


async def _execute_claimed(run: _Run, scheduled: list[_Scheduled]) -> list[TaskExecutionResult]:
    """Run claimed tasks concurrently; priority only sets the start order."""
    if not scheduled:
        return []
    ordered = sorted(scheduled, key=lambda s: (PRIORITY_ORDER[s.task.priority], s.task.id))
    async with asyncio.TaskGroup() as tg:
        running = [tg.create_task(_execute_one(run, item)) for item in ordered]
    return [t.result() for t in running]


async def _execute_one(run: _Run, item: _Scheduled) -> TaskExecutionResult:
    async with item.semaphore:
        logger.info(f"Executing task {item.task.id} (execution {item.claim.execution_id})...")
        started = time.monotonic()
        try:
            outcome = await _drive(run.repo, run.mailer, item.claim, item.limiter)
            result = _result(item.task.id, outcome, started)
        except (StaleClaimError, sqlite3.Error) as exc:
            # The stored state is authoritative; recovery turns in-flight work into UNKNOWN.
            logger.error(f"Task {item.task.id} lost its execution state: {exc}")
            result = TaskExecutionResult(
                task_id=item.task.id,
                status=TaskExecutionStatus.UNKNOWN,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error_details=f"Execution state could not be updated: {exc}",
            )
        _log(run, result, item.claim.execution_id)
        return result


def _log(run: _Run, result: TaskExecutionResult, execution_id: int) -> None:
    try:
        run.repo.log_task_execution(
            result, batch_id=run.run_id, executed_at=run.ref_dt, execution_id=execution_id
        )
    except sqlite3.Error as exc:
        logger.error(f"Could not log the result of task {result.task_id}: {exc}")


async def _drive(
    repo: DatabaseRepository,
    mailer: AsyncMailer,
    claim: Claim,
    limiter: TokenBucketRateLimiter | None,
) -> ExecutionOutcome:
    """Attempt the claimed execution until no recipient is due, then release it."""
    data = _message_for(repo, mailer, claim)
    while data is not None:
        if limiter is not None:
            await limiter.acquire()
        # Checked after waiting: lease, window end and the recipients due now.
        attempt = repo.start_attempt(claim)
        if attempt is None:
            due = repo.next_attempt_at(claim.execution_id)
            if due is None:
                break
            await asyncio.sleep(max(0.0, (due - datetime.now(UTC)).total_seconds()))
            continue
        results = await mailer.deliver(data, attempt.recipients)
        repo.record_attempt_result(claim, attempt, results)
    if data is not None:
        repo.release_execution(claim)
    return repo.execution_outcome(claim.execution_id)


def _message_for(repo: DatabaseRepository, mailer: AsyncMailer, claim: Claim) -> bytes | None:
    """The stored message, rendered from the pinned definition on the first attempt.

    A definition that cannot be rendered ends the execution without any SMTP traffic.
    """
    stored = repo.pinned_message(claim.execution_id)
    if stored is not None:
        return stored
    try:
        rendered = mailer.render_task_message(repo.pinned_task(claim))
    except RENDER_ERRORS as exc:
        logger.error(f"Failed to render execution {claim.execution_id}: {exc}")
        repo.abort_execution(claim, f"Template rendering error: {exc}")
        return None
    return repo.pin_message(claim, rendered.message_id, rendered.data)


def _result(task_id: str, outcome: ExecutionOutcome, started: float) -> TaskExecutionResult:
    error = outcome.last_error
    if outcome.unknown:
        error = f"Outcome unknown for {list(outcome.unknown)}; review before resending"
    return TaskExecutionResult(
        task_id=task_id,
        status=_status_of(outcome),
        sent_count=len(outcome.accepted),
        failed_recipients=list(outcome.failed + outcome.unknown),
        duration_ms=(time.monotonic() - started) * 1000.0,
        attempt_count=outcome.attempts,
        error_details=None if outcome.state == "SUCCEEDED" else error,
    )


# --- Manual dispatch -------------------------------------------------------------------


async def dispatch_single_task(
    db_path: str | Path,
    task_id: str,
    settings: Settings,
    template_dir: str | Path = ".",
    idempotency_key: str | None = None,
) -> TaskExecutionResult:
    """Send one task now, ignoring its schedule window, and log the result.

    The same ``idempotency_key`` returns the result of the earlier send instead of
    sending again; without a key every call is a new send. A task that another worker
    is sending is reported as SKIPPED_BUSY. Raises LookupError if the task does not exist.
    """
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        if repo.get_mail_task(task_id) is None:
            msg = f"Task '{task_id}' does not exist"
            raise LookupError(msg)
        run_id = str(uuid.uuid4())
        mailer = AsyncMailer(settings=settings, renderer=TemplateRenderer(template_dir))
        run = _Run(repo, mailer, run_id, _owner_id(run_id), None)
        repo.start_run(run_id, "manual")
        repo.recover_abandoned()
        key = f"manual:{idempotency_key or run_id}"
        claimed = repo.claim_execution(ClaimRequest(task_id, key, run_id, run.owner))
        result = await _manual_result(
            run, task_id, claimed.status, claimed.claim, claimed.execution_id
        )
        repo.finish_run(
            run_id, _exit_code([result.status], report_ok=True), result.status.value, None
        )
        return result


async def _manual_result(
    run: _Run,
    task_id: str,
    status: ClaimStatus,
    claim: Claim | None,
    execution_id: int | None,
) -> TaskExecutionResult:
    started = time.monotonic()
    if status == ClaimStatus.CLAIMED and claim is not None:
        async with run.mailer, _heartbeat(run.repo, run.owner):
            outcome = await _drive(run.repo, run.mailer, claim, None)
        result = _result(task_id, outcome, started)
        _log(run, result, claim.execution_id)
        return result
    if status == ClaimStatus.DONE and execution_id is not None:
        # Same key again (e.g. a repeated HTTP request): report, never resend.
        return _result(task_id, run.repo.execution_outcome(execution_id), started)
    reason = f"execution {execution_id} is running elsewhere"
    result = TaskExecutionResult(
        task_id=task_id, status=TaskExecutionStatus.SKIPPED_BUSY, error_details=reason
    )
    run.repo.log_task_execution(result, batch_id=run.run_id)
    return result


# --- Dry run ---------------------------------------------------------------------------


async def _simulate(
    repo: DatabaseRepository,
    mailer: AsyncMailer,
    loaded: _LoadedConfig,
    options: DispatchOptions,
    start_time: datetime,
) -> int:
    """Render and log what would be sent; read execution state, never write it (D13)."""
    batch_id = str(uuid.uuid4())
    scheduled, results = _plan_simulation(repo, loaded, options.ref_dt, batch_id)
    logger.info(f"Simulating {len(scheduled)} tasks ({len(results)} skipped).")
    async with mailer:
        results.extend(await _simulate_tasks(mailer, scheduled, repo, batch_id, options.ref_dt))
        report = aggregate_summary_report(results, start_time, datetime.now(UTC))
        report_ok = await _send_report_if_needed(
            report, loaded.mail_config, repo.has_report_settings(), mailer
        )
    return _exit_code([r.status for r in results], report_ok=report_ok)


def _plan_simulation(
    repo: DatabaseRepository, loaded: _LoadedConfig, ref_dt: datetime | None, batch_id: str
) -> tuple[list[SimulatedTask], list[TaskExecutionResult]]:
    scheduler = TaskScheduler(loaded.work_schedules)
    controls: dict[str, WindowControls] = {}
    scheduled: list[SimulatedTask] = []
    skipped: list[TaskExecutionResult] = []
    for task in loaded.mail_config.tasks:
        instance = scheduler.window_instance(task.schedule_ref, ref_dt)
        preview = repo.peek_claim(task.id, instance.key) if instance else None
        if instance is not None and preview is not None and preview.status == ClaimStatus.CLAIMED:
            scheduled.append((task, *_window_controls(controls, instance)))
            continue
        if instance is None:
            reason, status = _inactive_reason(scheduler, task), TaskExecutionStatus.SKIPPED_SCHEDULE
        elif preview is not None and preview.status == ClaimStatus.BUSY:
            reason, status = "running elsewhere", TaskExecutionStatus.SKIPPED_BUSY
        else:
            reason, status = "already executed in this window", TaskExecutionStatus.SKIPPED_SCHEDULE
        result = TaskExecutionResult(task_id=task.id, status=status, error_details=reason)
        skipped.append(result)
        repo.log_task_execution(result, batch_id=batch_id, dry_run=True)
    return scheduled, skipped


async def _simulate_tasks(
    mailer: AsyncMailer,
    scheduled: list[SimulatedTask],
    repo: DatabaseRepository,
    batch_id: str,
    ref_dt: datetime | None,
) -> list[TaskExecutionResult]:
    async def simulate(
        task: MailTask, rate_limiter: TokenBucketRateLimiter, sem: asyncio.Semaphore
    ) -> TaskExecutionResult:
        async with sem:
            logger.info(f"Simulating task {task.id}...")
            result = await mailer.dispatch_task(task, rate_limiter=rate_limiter)
            repo.log_task_execution(result, batch_id=batch_id, dry_run=True, executed_at=ref_dt)
            return result

    if not scheduled:
        return []
    ordered = sorted(scheduled, key=lambda item: (PRIORITY_ORDER[item[0].priority], item[0].id))
    async with asyncio.TaskGroup() as tg:
        running = [tg.create_task(simulate(*item)) for item in ordered]
    return [t.result() for t in running]


# --- Shared ----------------------------------------------------------------------------


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
            schedule_versions=repo.get_schedule_versions(),
        )
    except (sqlite3.Error, ValidationError) as exc:
        logger.error(f"Failed to load configuration from database: {exc}")
        return None


async def _send_report_if_needed(
    report: BatchSummaryReport,
    mail_config: MailConfiguration,
    has_report_settings: bool,
    mailer: AsyncMailer,
) -> bool:
    """Send the status report when one is due; False only if sending it failed."""
    executed = report.successful_tasks + report.partial_tasks + report.failed_tasks
    if executed == 0:
        logger.info("No tasks were executed in this batch; status report not sent.")
        return True
    if not has_report_settings:
        logger.warning("Report settings are not configured; status report not sent.")
        return True
    return await ReportGenerator().send_report(report, mail_config.report_settings, mailer)
