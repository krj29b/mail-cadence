"""Durable status reports (ADR 0001, D14) against the SMTP sink.

A report is stored once per run and retried by later runs from the same bytes, at most
three attempts; a 5xx or an unknown outcome is never retried, and task messages are
never resent because of the report.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    EXIT_UNKNOWN,
    DispatchOptions,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from smtp_sink import SMTPSink

A = "a@example.com"
REPORT_TO = "report@example.com"
REF_DT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _prepare(tmp_path: Path) -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Hello</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="18:00")
        repo.save_work_schedules(
            WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[window])])
        )
        repo.save_single_task(
            MailTask(
                id="T1",
                schedule_ref="S1",
                sender="cadence@example.com",
                subject="Task message",
                recipients=Recipients(to=[A]),
                body_template=BodyTemplate(template_path="body.html.j2"),
            )
        )
        repo.save_report_settings(
            ReportSettings(recipient=REPORT_TO, sender="cadence@example.com", subject_prefix="[R]")
        )
    return db_path


def _run(db_path: Path, port: int, *, day: int = 0, dry_run: bool = False) -> int:
    options = DispatchOptions(
        db_path=db_path,
        template_dir=db_path.parent,
        ref_dt=REF_DT + timedelta(days=day),
        dry_run=dry_run,
    )
    settings = Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]
    return asyncio.run(run_dispatch(options, settings))


def _reports(db_path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT state, attempt_count FROM report_deliveries ORDER BY report_id"
            )
        ]


def _report_deliveries(sink: SMTPSink) -> list[bytes]:
    return [d.raw for d in sink.deliveries if REPORT_TO in d.recipients]


def test_report_is_stored_and_accepted(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port) == EXIT_OK
    assert _reports(db_path) == [("ACCEPTED", 1)]
    assert len(_report_deliveries(sink)) == 1


def test_failed_report_is_retried_from_the_same_bytes(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.rcpt_codes[REPORT_TO] = 450
    assert _run(db_path, sink.port) == EXIT_REPORT_FAILED

    sink.rcpt_codes.clear()
    sink.final_code = 250
    assert _run(db_path, sink.port) == EXIT_OK

    assert _reports(db_path) == [("ACCEPTED", 2)]
    [report] = _report_deliveries(sink)
    assert sink.accepted_count(A) == 1, "the task message was not resent"
    assert b"[R] Task Execution Summary" in report


def test_report_budget_ends_after_three_attempts(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.rcpt_codes[REPORT_TO] = 450
    codes = [_run(db_path, sink.port) for _ in range(4)]

    assert codes == [EXIT_REPORT_FAILED] * 3 + [EXIT_OK]
    assert _reports(db_path) == [("RETRY_EXHAUSTED", 3)]
    assert sink.rcpt_count(REPORT_TO) == 3


def test_refused_report_is_not_retried(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.rcpt_codes[REPORT_TO] = 550
    assert _run(db_path, sink.port) == EXIT_REPORT_FAILED
    assert _run(db_path, sink.port) == EXIT_OK
    assert _reports(db_path) == [("PERMANENT_FAILURE", 1)]
    assert sink.rcpt_count(REPORT_TO) == 1


def test_report_with_unknown_outcome_is_never_resent(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.drop_after_data = 2  # the task message is the first DATA, the report the second

    assert _run(db_path, sink.port) == EXIT_UNKNOWN
    assert _run(db_path, sink.port) == EXIT_OK

    assert _reports(db_path) == [("UNKNOWN", 1)]
    assert len(_report_deliveries(sink)) == 1


def test_each_run_with_executed_tasks_stores_its_own_report(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port) == EXIT_OK
    assert _run(db_path, sink.port) == EXIT_OK  # T1 already sent: no task, no report
    assert _run(db_path, sink.port, day=1) == EXIT_OK

    assert _reports(db_path) == [("ACCEPTED", 1), ("ACCEPTED", 1)]


def test_dry_run_stores_no_report(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port, dry_run=True) == EXIT_OK
    assert _reports(db_path) == []
    assert sink.commands == []
