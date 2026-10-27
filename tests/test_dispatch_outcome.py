"""Batch outcome: exit codes for partial deliveries and report failures, dry-run skips,
and the report subject prefix header check."""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mail_cadence.database import MIGRATIONS, get_connection, init_db
from mail_cadence.dispatch_jobs import DispatchJob
from mail_cadence.models import ReportSettings, Settings, TaskExecutionResult, TaskExecutionStatus
from mail_cadence.pipeline import (
    EXIT_OK,
    EXIT_REPORT_FAILED,
    EXIT_TASK_FAILED,
    DispatchOptions,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app

SAMPLES = Path(__file__).resolve().parents[1] / "config" / "samples"
TZ = ZoneInfo("Europe/Warsaw")
# The sample morning schedule (06:00-12:00) is active at 08:00 and inactive at 03:00.
MORNING = datetime(2026, 10, 17, 8, 0, tzinfo=TZ)
NIGHT = datetime(2026, 10, 17, 3, 0, tzinfo=TZ)


def _options(db_path: Path, *, dry_run: bool = True, ref_dt: datetime = MORNING) -> DispatchOptions:
    return DispatchOptions(
        db_path=db_path,
        dry_run=dry_run,
        ref_dt=ref_dt,
        tasks_xml=SAMPLES / "tasks.xml",
        schedule_xml=SAMPLES / "schedules.xml",
    )


def _result_mock(status: TaskExecutionStatus) -> AsyncMock:
    return AsyncMock(
        side_effect=lambda task, rate_limiter=None: TaskExecutionResult(
            task_id=task.id, status=status, sent_count=1
        )
    )


# --- Dry-run skips --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_skips_are_flagged_and_excluded_from_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "skips.db"
    assert await run_dispatch(_options(db_path, ref_dt=NIGHT), Settings()) == EXIT_OK

    with closing(get_connection(db_path)) as conn:
        rows = conn.execute("SELECT status, dry_run FROM execution_logs").fetchall()
        stats = DatabaseRepository(conn).get_dashboard_stats()

    assert rows, "skipped tasks must still be logged"
    assert {r["status"] for r in rows} == {TaskExecutionStatus.SKIPPED_SCHEDULE.value}
    assert all(r["dry_run"] == 1 for r in rows)
    assert stats["total_executions"] == 0
    assert stats["skipped_count"] == 0
    assert stats["dry_run_count"] == len(rows)


@pytest.mark.asyncio
async def test_real_run_skips_still_count_as_real(tmp_path: Path) -> None:
    db_path = tmp_path / "real-skips.db"
    assert await run_dispatch(_options(db_path, dry_run=False, ref_dt=NIGHT), Settings()) == 0

    with closing(get_connection(db_path)) as conn:
        flags = {r[0] for r in conn.execute("SELECT dry_run FROM execution_logs")}
    assert flags == {0}


# --- Exit codes -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_delivery_returns_task_failed(tmp_path: Path) -> None:
    with patch(
        "mail_cadence.pipeline.AsyncMailer.dispatch_task",
        _result_mock(TaskExecutionStatus.PARTIAL),
    ):
        code = await run_dispatch(_options(tmp_path / "partial.db"), Settings())
    assert code == EXIT_TASK_FAILED


@pytest.mark.asyncio
async def test_report_failure_after_successful_tasks_returns_report_failed(
    tmp_path: Path,
) -> None:
    send_report = AsyncMock(return_value=False)
    with (
        patch(
            "mail_cadence.pipeline.AsyncMailer.dispatch_task",
            _result_mock(TaskExecutionStatus.SUCCESS),
        ),
        patch("mail_cadence.pipeline.ReportGenerator.send_report", send_report),
    ):
        code = await run_dispatch(_options(tmp_path / "report.db"), Settings())
    send_report.assert_awaited_once()
    assert code == EXIT_REPORT_FAILED


@pytest.mark.asyncio
async def test_task_failure_takes_precedence_over_report_failure(tmp_path: Path) -> None:
    with (
        patch(
            "mail_cadence.pipeline.AsyncMailer.dispatch_task",
            _result_mock(TaskExecutionStatus.FAILED),
        ),
        patch("mail_cadence.pipeline.ReportGenerator.send_report", AsyncMock(return_value=False)),
    ):
        code = await run_dispatch(_options(tmp_path / "both.db"), Settings())
    assert code == EXIT_TASK_FAILED


@pytest.mark.asyncio
async def test_successful_tasks_and_report_return_ok(tmp_path: Path) -> None:
    send_report = AsyncMock(return_value=True)
    with (
        patch(
            "mail_cadence.pipeline.AsyncMailer.dispatch_task",
            _result_mock(TaskExecutionStatus.SUCCESS),
        ),
        patch("mail_cadence.pipeline.ReportGenerator.send_report", send_report),
    ):
        code = await run_dispatch(_options(tmp_path / "ok.db"), Settings())
    send_report.assert_awaited_once()
    assert code == EXIT_OK


def test_web_status_explains_report_failure(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "web.db", template_dir=tmp_path, allow_insecure_no_auth=True
    )
    client = TestClient(app)

    async def report_failed(job: DispatchJob) -> None:
        job.exit_code = EXIT_REPORT_FAILED

    app.state.dispatch_runner.start(DispatchJob(kind="batch"), report_failed)
    app.state.dispatch_runner.wait(timeout=30)

    fragment = client.get("/dispatch/status").text
    assert "the status report was not sent" in fragment
    assert "configuration or database error" not in fragment


# --- Report subject prefix ------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["[R]\r\nBcc: x@example.com", "[R]\nX", "[R]\rX"])
def test_report_subject_prefix_rejects_newlines(prefix: str) -> None:
    with pytest.raises(ValidationError, match="subject_prefix"):
        ReportSettings(recipient="admin@example.com", subject_prefix=prefix)


def test_web_rejects_report_subject_prefix_with_newline(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "web.db", allow_insecure_no_auth=True))
    resp = client.post(
        "/settings",
        data={"recipient": "admin@example.com", "subject_prefix": "[R]\r\nBcc: x@example.com"},
    )
    assert resp.status_code == 400


def test_migration_replaces_newlines_in_stored_report_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-prefix.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO report_settings (id, recipient, subject_prefix) VALUES (1, ?, ?)",
            ("admin@example.com", "[A]\r\n[B]\n[C]\r[D]"),
        )
        conn.commit()

        init_db(conn)
        settings = DatabaseRepository(conn).get_report_settings()

        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert settings.subject_prefix == "[A] [B] [C] [D]"


def test_report_prefix_migration_is_idempotent(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "idem.db")) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO report_settings (id, recipient, subject_prefix) VALUES (1, ?, ?)",
            ("admin@example.com", "[Clean]"),
        )
        MIGRATIONS[-1](conn)
        MIGRATIONS[-1](conn)
        value = conn.execute("SELECT subject_prefix FROM report_settings").fetchone()[0]
    assert value == "[Clean]"
