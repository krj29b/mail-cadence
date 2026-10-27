"""Reproductions of the delivery-guarantee defects (docs/adr/0001-execution-state-model.md).

Each test states a behaviour the ADR requires. They were added with
``xfail(strict=True)`` before the fix (stage 1) and lost the marker when the stage that
implements the behaviour made them pass. The oracle is the SMTP sink's log of accepted messages,
never the number of mailer calls or the code's own summary of the run.
"""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatch_worker import PROCESS_TIMEOUT, spawn_batch, stop
from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.pipeline import (
    EXIT_OK,
    EXIT_REPORT_FAILED,
    EXIT_TASK_FAILED,
    DispatchOptions,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from smtp_sink import SMTPSink

# Every run in a test uses the same instant, inside the 06:00-18:00 UTC window.
REF_DT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
A = "a@example.com"
B = "b@example.com"
REPORT_TO = "report@example.com"
TASK_ID = "TASK-1"


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


def _task(recipients: list[str], *, subject: str = "Version 1", max_retries: int = 0) -> MailTask:
    return MailTask(
        id=TASK_ID,
        schedule_ref="WORKDAY",
        sender="cadence@example.com",
        subject=subject,
        max_retries=max_retries,
        recipients=Recipients(to=recipients),
        body_template=BodyTemplate(template_path="body.html.j2"),
    )


def _prepare(tmp_path: Path, task: MailTask, *, report: bool = False) -> Path:
    """Database with one 06:00-18:00 UTC window, one task and its template."""
    (tmp_path / "body.html.j2").write_text("<p>Hello {{ name | default('there') }}</p>")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="18:00")
        repo.save_work_schedules(
            WorkSchedules(
                timezone="UTC", schedules=[Schedule(id="WORKDAY", allowed_hours=[window])]
            )
        )
        repo.save_single_task(task)
        if report:
            repo.save_report_settings(
                ReportSettings(
                    recipient=REPORT_TO, sender="cadence@example.com", subject_prefix="[Report]"
                )
            )
    return db_path


def _dispatch(db_path: Path, port: int) -> int:
    options = DispatchOptions(db_path=db_path, template_dir=db_path.parent, ref_dt=REF_DT)
    return asyncio.run(run_dispatch(options, _settings(port)))


def _edit_task(db_path: Path, task: MailTask, *, recreate: bool = False) -> None:
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        if recreate:
            repo.delete_mail_task(task.id)
        repo.save_single_task(task)


# --- Sanity: the oracle and today's in-run behaviour -----------------------------------


def test_sink_counts_one_message_per_recipient(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, _task([A, B]))
    assert _dispatch(db_path, sink.port) == EXIT_OK
    assert (sink.accepted_count(A), sink.accepted_count(B)) == (1, 1)
    assert [d.subject for d in sink.deliveries] == ["Version 1"]


def test_in_run_retry_targets_only_transient_recipients(tmp_path: Path, sink: SMTPSink) -> None:
    """Within one run, a 4xx recipient is retried alone; already passes today."""
    db_path = _prepare(tmp_path, _task([A, B], max_retries=1))
    sink.rcpt_codes[B] = 450
    assert _dispatch(db_path, sink.port) == EXIT_TASK_FAILED
    assert sink.rcpt_count(A) == 1
    assert sink.rcpt_count(B) == 2


# --- T01: concurrent processes ---------------------------------------------------------


def test_t01_concurrent_processes_send_once(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, _task([A, B]))
    sink.hold_at = ("DATA", 1)  # the first process stays inside SMTP

    first = spawn_batch(db_path, sink.port, REF_DT)
    try:
        sink.wait_held()
        second = spawn_batch(db_path, sink.port, REF_DT)
        try:
            second.join(PROCESS_TIMEOUT)
        finally:
            stop(second)
        sink.release()
        first.join(PROCESS_TIMEOUT)
    finally:
        stop(first)

    assert (sink.accepted_count(A), sink.accepted_count(B)) == (1, 1)
    assert len(sink.sessions_with_mail()) == 1, "the losing process must not start SMTP"


# --- T03 / T04 / T05: per-recipient state survives the run -----------------------------


def test_t03_restart_never_resends_to_accepted_recipient(tmp_path: Path, sink: SMTPSink) -> None:
    """A accepted, B 450; the process dies before its retry; the next run must not
    send to A again (B may be retried or left for review, but at most once)."""
    db_path = _prepare(tmp_path, _task([A, B], max_retries=1))
    sink.rcpt_codes[B] = 450
    sink.hold_at = ("MAIL", 2)  # the retry of B has started

    proc = spawn_batch(db_path, sink.port, REF_DT)
    try:
        sink.wait_held()
    finally:
        stop(proc)
    sink.release()
    sink.rcpt_codes[B] = 250

    _dispatch(db_path, sink.port)

    assert sink.accepted_count(A) == 1
    assert sink.accepted_count(B) <= 1


def test_t04_next_run_skips_accepted_and_permanently_refused(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path, _task([A, B], max_retries=2))
    sink.rcpt_codes[B] = 550
    assert _dispatch(db_path, sink.port) == EXIT_TASK_FAILED
    attempts_after_first_run = len(sink.rcpt_attempts)

    assert _dispatch(db_path, sink.port) == EXIT_TASK_FAILED

    assert len(sink.rcpt_attempts) == attempts_after_first_run, "no RCPT to A or B again"
    assert sink.accepted_count(A) == 1


def test_t05_retry_budget_is_shared_across_runs(tmp_path: Path, sink: SMTPSink) -> None:
    max_retries = 1
    db_path = _prepare(tmp_path, _task([B], max_retries=max_retries))
    sink.rcpt_codes[B] = 450

    _dispatch(db_path, sink.port)
    _dispatch(db_path, sink.port)

    assert sink.rcpt_count(B) <= max_retries + 1


# --- UNKNOWN: connection lost after DATA -----------------------------------------------


def test_lost_reply_after_data_is_not_resent(tmp_path: Path, sink: SMTPSink) -> None:
    """The sink accepts the message and drops the connection before replying: the
    outcome is unknown, so neither this run nor the next one may send it again."""
    db_path = _prepare(tmp_path, _task([A], max_retries=2))
    sink.drop_after_data = 1

    first = _dispatch(db_path, sink.port)
    second = _dispatch(db_path, sink.port)

    assert sink.accepted_count(A) == 1
    assert EXIT_OK not in (first, second), "an unknown outcome is never reported as success"


# --- T06 / T08: results belong to the version that produced them -----------------------


def _edit_while_sending(
    tmp_path: Path, sink: SMTPSink, *, recreate: bool
) -> tuple[Path, list[str]]:
    """Run 1 sends version 1; the task changes while SMTP is in progress; run 2 follows."""
    db_path = _prepare(tmp_path, _task([A]))
    sink.hold_at = ("DATA", 1)
    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(_dispatch(db_path, sink.port)))
    worker.start()
    try:
        sink.wait_held()
        _edit_task(db_path, _task([A], subject="Version 2"), recreate=recreate)
    finally:
        sink.release()
        worker.join(PROCESS_TIMEOUT)

    _dispatch(db_path, sink.port)
    return db_path, [d.subject for d in sink.deliveries]


def test_t06_old_version_result_does_not_complete_edited_task(
    tmp_path: Path, sink: SMTPSink
) -> None:
    _, subjects = _edit_while_sending(tmp_path, sink, recreate=False)
    assert subjects == ["Version 1", "Version 2"]


def test_t08_recreated_task_is_a_new_identity(tmp_path: Path, sink: SMTPSink) -> None:
    db_path, subjects = _edit_while_sending(tmp_path, sink, recreate=True)
    assert subjects == ["Version 1", "Version 2"]
    with closing(get_connection(db_path)) as conn:
        logged = DatabaseRepository(conn).get_recent_execution_logs(limit=10)
    assert len(logged) == 2, "the history of the deleted task is kept"


# --- T13: the report is retried on its own ---------------------------------------------


def test_t13_failed_report_is_retried_without_resending_tasks(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path, _task([A]), report=True)
    sink.rcpt_codes[REPORT_TO] = 450
    assert _dispatch(db_path, sink.port) == EXIT_REPORT_FAILED

    sink.rcpt_codes[REPORT_TO] = 250
    _dispatch(db_path, sink.port)

    assert sink.accepted_count(A) == 1
    assert sink.accepted_count(REPORT_TO) == 1
