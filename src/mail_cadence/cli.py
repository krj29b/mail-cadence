import asyncio
import logging
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer

from mail_cadence import __version__
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
from mail_cadence.xml_loader import (
    XMLValidationError,
    export_to_xml,
    import_from_xml,
)

app = typer.Typer(
    name="mail-cadence",
    help="Asynchronous MailCadence email dispatch and reporting engine",
    invoke_without_command=True,
)


TEMPLATE_DIR_HELP = (
    "Directory that task template paths are resolved against "
    "(built-in templates use the 'builtin:' prefix)"
)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def _resolve_db_path(db: str) -> str:
    if db == "mail_cadence.db":
        return Settings.load().mail_cadence_db
    return db


def _seed_xml_if_provided(
    conn: sqlite3.Connection,
    tasks_xml: Path | None,
    schedule_xml: Path | None,
    logger: logging.Logger,
) -> bool:
    if not (tasks_xml and schedule_xml):
        return True
    try:
        import_from_xml(conn, tasks_xml, schedule_xml, mode="upsert")
        return True
    except XMLValidationError as exc:
        logger.error(f"Configuration error: {exc}")
        return False
    except Exception as exc:
        logger.error(f"Unexpected error loading XML configs: {exc}")
        return False


def _partition_eligible_tasks(
    mail_config: MailConfiguration,
    work_schedules: WorkSchedules,
    execution_states: dict[str, tuple[datetime | None, TaskExecutionStatus | None]],
    ref_dt: datetime | None,
    repo: DatabaseRepository,
    batch_id: str,
    logger: logging.Logger,
) -> tuple[
    list[tuple[MailTask, TokenBucketRateLimiter, asyncio.Semaphore]],
    list[TaskExecutionResult],
]:
    scheduler = TaskScheduler(work_schedules)
    eligible_tasks: list[tuple[MailTask, TokenBucketRateLimiter, asyncio.Semaphore]] = []
    results: list[TaskExecutionResult] = []
    window_controls: dict[
        tuple[str, str, str], tuple[TokenBucketRateLimiter, asyncio.Semaphore]
    ] = {}

    for task in mail_config.tasks:
        last_run, last_status = execution_states.get(task.id, (None, None))
        window_status = scheduler.window_status(
            task, reference_dt=ref_dt, last_run=last_run, last_status=last_status
        )
        window = window_status.window
        if window_status.eligible and window is not None:
            key = (task.schedule_ref, window.start, window.end)
            if key not in window_controls:
                window_controls[key] = (
                    TokenBucketRateLimiter(
                        window.rate_limit_per_min,
                        initial_tokens=float(window.max_concurrency),
                    ),
                    asyncio.Semaphore(window.max_concurrency),
                )
            rate_limiter, semaphore = window_controls[key]
            eligible_tasks.append((task, rate_limiter, semaphore))
        else:
            skip_reason = window_status.skip_reason(task.schedule_ref)
            logger.info(f"Task {task.id} skipped due to schedule rules ({skip_reason}).")
            skipped_res = TaskExecutionResult(
                task_id=task.id,
                status=TaskExecutionStatus.SKIPPED_SCHEDULE,
                sent_count=0,
                duration_ms=0.0,
                error_details=skip_reason,
            )
            results.append(skipped_res)
            repo.log_task_execution(skipped_res, batch_id=batch_id)

    return eligible_tasks, results


async def _send_report_if_needed(
    report: BatchSummaryReport,
    mail_config: MailConfiguration,
    has_report_settings: bool,
    mailer: AsyncMailer,
    logger: logging.Logger,
) -> None:
    executed = report.successful_tasks + report.partial_tasks + report.failed_tasks
    if executed == 0:
        logger.info("No tasks were executed in this batch; status report not sent.")
        return
    if not has_report_settings:
        logger.warning("Report settings are not configured; status report not sent.")
        return
    await ReportGenerator().send_report(report, mail_config.report_settings, mailer)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mail-cadence {__version__}")
        raise typer.Exit(code=0)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def run_dispatch_pipeline(
    tasks_xml: Path | None = None,
    schedule_xml: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    ref_dt: datetime | None = None,
    db_path: str = "mail_cadence.db",
    template_dir: str | Path = ".",
) -> int:
    setup_logging(verbose)
    logger = logging.getLogger("mail_cadence.cli")

    settings = Settings.load()
    resolved_db = _resolve_db_path(db_path)

    start_time = datetime.now(UTC)
    logger.info(
        f"Starting MailCadence email dispatch engine (dry_run={dry_run}, db={resolved_db})..."
    )

    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)

        if not _seed_xml_if_provided(conn, tasks_xml, schedule_xml, logger):
            return 1

        try:
            mail_config = repo.get_mail_configuration()
            work_schedules = repo.get_work_schedules()
            execution_states = repo.get_task_execution_states()
        except Exception as exc:
            logger.error(f"Failed to load configuration from database: {exc}")
            return 1

        if not mail_config.tasks:
            logger.warning("No tasks found in database.")
            return 0

        renderer = TemplateRenderer(template_dir=template_dir)
        mailer = AsyncMailer(settings=settings, renderer=renderer, dry_run=dry_run)
        batch_id = str(uuid.uuid4())

        eligible_tasks, results = _partition_eligible_tasks(
            mail_config, work_schedules, execution_states, ref_dt, repo, batch_id, logger
        )

        logger.info(
            f"Scheduled {len(eligible_tasks)} tasks for execution ({len(results)} skipped)."
        )

        async with mailer:

            async def _execute_single_task(
                task: MailTask,
                rate_limiter: TokenBucketRateLimiter,
                sem: asyncio.Semaphore,
            ) -> TaskExecutionResult:
                async with sem:
                    logger.info(f"Executing task {task.id}...")
                    res = await mailer.dispatch_task(task, rate_limiter=rate_limiter)
                    repo.log_task_execution(
                        res, batch_id=batch_id, dry_run=dry_run, executed_at=ref_dt
                    )
                    return res

            if eligible_tasks:
                eligible_tasks.sort(
                    key=lambda item: (
                        PRIORITY_ORDER.get(item[0].priority.lower(), 1),
                        item[0].id,
                    )
                )
                async with asyncio.TaskGroup() as tg:
                    tasks_tg = [
                        tg.create_task(_execute_single_task(t, rl, sem))
                        for t, rl, sem in eligible_tasks
                    ]
                exec_results = [completed_task.result() for completed_task in tasks_tg]
                results.extend(exec_results)

            end_time = datetime.now(UTC)
            report = aggregate_summary_report(results, start_time, end_time)
            await _send_report_if_needed(
                report, mail_config, repo.has_report_settings(), mailer, logger
            )

        logger.info("MailCadence execution complete.")
        return 0 if report.failed_tasks == 0 else 2


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
    tasks_xml: Annotated[
        Path | None,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Path to tasks configuration XML file to seed database",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    schedule_xml: Annotated[
        Path | None,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Path to hourly work schedules XML file to seed database",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = "mail_cadence.db",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-d",
            help="Execute without sending real network SMTP requests",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose debug logging",
        ),
    ] = False,
    template_dir: Annotated[
        str,
        typer.Option(
            "--template-dir",
            envvar="MAIL_CADENCE_TEMPLATE_DIR",
            help=TEMPLATE_DIR_HELP,
        ),
    ] = ".",
) -> None:
    if ctx.invoked_subcommand is None:
        exit_code = asyncio.run(
            run_dispatch_pipeline(
                tasks_xml=tasks_xml,
                schedule_xml=schedule_xml,
                dry_run=dry_run,
                verbose=verbose,
                db_path=db,
                template_dir=template_dir,
            )
        )
        raise typer.Exit(code=exit_code)


@app.command("import-xml")
def cli_import_xml(
    tasks_xml: Annotated[
        Path,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Path to tasks configuration XML file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    schedule_xml: Annotated[
        Path,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Path to hourly work schedules XML file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = "mail_cadence.db",
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            "-m",
            help="Import mode: 'upsert' (update existing) or 'replace' (wipe and replace)",
        ),
    ] = "upsert",
) -> None:
    """Import tasks and schedules XML into the SQLite database."""
    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)
        import_mode: Literal["upsert", "replace"] = (
            "replace" if mode.lower() == "replace" else "upsert"
        )

        try:
            import_from_xml(conn, tasks_xml, schedule_xml, mode=import_mode)
            typer.echo(
                f"Successfully imported {tasks_xml} and {schedule_xml} into "
                f"{resolved_db} ({import_mode})."
            )
        except XMLValidationError as exc:
            typer.echo(f"XML Validation Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            typer.echo(f"Error importing XML: {exc}", err=True)
            raise typer.Exit(code=1) from exc


@app.command("export-xml")
def cli_export_xml(
    tasks_xml: Annotated[
        Path,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Target path for tasks configuration XML file",
        ),
    ],
    schedule_xml: Annotated[
        Path,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Target path for hourly work schedules XML file",
        ),
    ],
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = "mail_cadence.db",
) -> None:
    """Export current SQLite database configuration to XML files."""
    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)

        try:
            export_to_xml(conn, tasks_xml, schedule_xml)
            typer.echo(
                f"Successfully exported database {resolved_db} to {tasks_xml} and {schedule_xml}."
            )
        except Exception as exc:
            typer.echo(f"Error exporting XML: {exc}", err=True)
            raise typer.Exit(code=1) from exc


@app.command("serve")
def cli_serve(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            "-h",
            help="Host address to bind web server",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port to bind web server",
        ),
    ] = 8000,
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = "mail_cadence.db",
    template_dir: Annotated[
        str,
        typer.Option(
            "--template-dir",
            "-t",
            envvar="MAIL_CADENCE_TEMPLATE_DIR",
            help=TEMPLATE_DIR_HELP,
        ),
    ] = ".",
    insecure_no_auth: Annotated[
        bool,
        typer.Option(
            "--insecure-no-auth",
            help="Allow access without ADMIN_USER/ADMIN_PASSWORD (only with a loopback --host)",
        ),
    ] = False,
) -> None:
    """Launch the web administration and observability panel."""
    import uvicorn

    from mail_cadence.web import create_app

    if insecure_no_auth and host not in _LOOPBACK_HOSTS:
        typer.echo(
            "--insecure-no-auth is only allowed with --host 127.0.0.1, ::1 or localhost.",
            err=True,
        )
        raise typer.Exit(code=1)

    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)

    web_app = create_app(
        db_path=resolved_db,
        template_dir=template_dir,
        allow_insecure_no_auth=insecure_no_auth,
    )
    uvicorn.run(web_app, host=host, port=port)


if __name__ == "__main__":
    app()
