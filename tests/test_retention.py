"""Retention of stored message bytes (ADR 0001, D16).

Thirty days after a delivery is over its message bytes are dropped; Message-IDs,
recipient states, attempts and events stay. Work that is not over, and executions that
wait for an operator decision, keep their message.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import Claim, ClaimRequest
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Outcome,
    RecipientResult,
    Recipients,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.pipeline import EXIT_OK, DispatchOptions, run_dispatch
from mail_cadence.report_store import MESSAGE_RETENTION
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app
from smtp_sink import SMTPSink

A = "a@example.com"
OK = RecipientResult(Outcome.ACCEPTED, 250)
BUSY = RecipientResult(Outcome.TRANSIENT, 450)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Iterator[DatabaseRepository]:
    with closing(get_connection(tmp_path / "cadence.db")) as conn:
        init_db(conn)
        repository = DatabaseRepository(conn, clock=clock)
        repository.save_work_schedules(
            WorkSchedules(
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])
                ]
            )
        )
        for task_id in ("DONE", "REVIEW", "WAITING"):
            repository.save_single_task(
                MailTask(
                    id=task_id,
                    schedule_ref="S1",
                    sender="cadence@example.com",
                    subject=task_id,
                    max_retries=2,
                    recipients=Recipients(to=[A]),
                    body_template=BodyTemplate(template_path="builtin:x.html.j2"),
                )
            )
        repository.start_run("run-1", "cli")
        yield repository


def _execute(repo: DatabaseRepository, task_id: str, result: RecipientResult | None) -> Claim:
    claim = repo.claim_execution(ClaimRequest(task_id, "S1@key", "run-1", "worker")).claim
    assert claim is not None
    repo.pin_message(claim, f"<{task_id}@example.com>", task_id.encode())
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    repo.record_attempt_result(claim, attempt, {A: result} if result else {})
    repo.release_execution(claim)
    return claim


def _stored(repo: DatabaseRepository) -> dict[str, tuple[str, bool]]:
    rows = repo.conn.execute(
        "SELECT i.task_id, e.message_id, e.message_bytes IS NOT NULL FROM task_executions e "
        "JOIN task_identities i ON i.uid = e.task_uid"
    )
    return {r[0]: (r[1], bool(r[2])) for r in rows}


def test_finished_deliveries_lose_their_bytes_after_thirty_days(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    _execute(repo, "DONE", OK)
    _execute(repo, "REVIEW", None)
    _execute(repo, "WAITING", BUSY)
    accepted = repo.create_report("run-1", A, "<r1@example.com>", b"report 1")
    [claim] = repo.claim_reports("worker", [accepted])
    repo.start_report_attempt(claim)
    repo.record_report_result(claim, OK)
    repo.start_run("run-2", "cli")  # one report per run
    pending = repo.create_report("run-2", A, "<r2@example.com>", b"report 2")

    clock.now += MESSAGE_RETENTION - timedelta(minutes=1)
    assert repo.purge_message_contents() == 0
    clock.now += timedelta(minutes=2)
    assert repo.purge_message_contents() == 2
    assert repo.purge_message_contents() == 0

    assert _stored(repo) == {
        "DONE": ("<DONE@example.com>", False),
        "REVIEW": ("<REVIEW@example.com>", True),
        "WAITING": ("<WAITING@example.com>", True),
    }
    reports = dict(
        repo.conn.execute(
            "SELECT report_id, message_bytes IS NOT NULL FROM report_deliveries"
        ).fetchall()
    )
    assert reports == {accepted: 0, pending: 1}
    recipients = repo.conn.execute("SELECT COUNT(*) FROM execution_recipients").fetchone()[0]
    attempts = repo.conn.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0]
    assert (recipients, attempts) == (3, 3), "the audit trail is kept"
    event = repo.conn.execute(
        "SELECT details_json FROM execution_events WHERE kind = 'messages_purged'"
    ).fetchone()[0]
    assert event == '{"executions": 1, "reports": 1}'


def test_dispatch_run_applies_retention(tmp_path: Path) -> None:
    (tmp_path / "body.html.j2").write_text("<p>Hello</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        repo.save_work_schedules(
            WorkSchedules(
                timezone="UTC",
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="06:00", end="18:00")])
                ],
            )
        )
        repo.save_single_task(
            MailTask(
                id="T1",
                schedule_ref="S1",
                sender="cadence@example.com",
                subject="Hello",
                recipients=Recipients(to=[A]),
                body_template=BodyTemplate(template_path="body.html.j2"),
            )
        )

    def run(day: int) -> int:
        options = DispatchOptions(
            db_path=db_path,
            template_dir=tmp_path,
            ref_dt=datetime(2026, 10, 17, 10, 0, tzinfo=UTC) + timedelta(days=day),
        )
        settings = Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]
        return asyncio.run(run_dispatch(options, settings))

    with SMTPSink() as sink:
        port = sink.port
        assert run(0) == EXIT_OK
        with closing(sqlite3.connect(db_path)) as conn, conn:
            old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
            conn.execute("UPDATE task_executions SET finished_at = ?", (old,))
        assert run(1) == EXIT_OK

    with closing(sqlite3.connect(db_path)) as conn:
        stored = conn.execute(
            "SELECT execution_id, message_bytes IS NOT NULL FROM task_executions "
            "ORDER BY execution_id"
        ).fetchall()
    assert stored == [(1, 0), (2, 1)]

    page = TestClient(
        create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    ).get("/executions/1")
    assert "content removed after the retention period" in page.text
