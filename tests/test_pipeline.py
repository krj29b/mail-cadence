"""A1/A8: the dispatch pipeline module used by both the CLI and the web panel."""

import os
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import Settings, TaskExecutionResult, TaskExecutionStatus
from mail_cadence.pipeline import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_TASK_FAILED,
    DispatchOptions,
    dispatch_single_task,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository

SAMPLES = Path(__file__).resolve().parents[1] / "config" / "samples"
# 08:00 in Warsaw: the sample morning schedule (06:00-12:00) is active.
MORNING = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _options(db_path: Path, **overrides: object) -> DispatchOptions:
    data: dict[str, object] = {
        "db_path": db_path,
        "dry_run": True,
        "ref_dt": MORNING,
        "tasks_xml": SAMPLES / "tasks.xml",
        "schedule_xml": SAMPLES / "schedules.xml",
    }
    data.update(overrides)
    return DispatchOptions(**data)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_dispatch_uses_given_settings_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    code = await run_dispatch(_options(db_path), Settings())

    assert code == EXIT_OK
    with closing(get_connection(db_path)) as conn:
        logs = DatabaseRepository(conn).get_recent_execution_logs(limit=50)
    assert logs, "the run must log into the database it was given"
    assert all(log["dry_run"] == 1 for log in logs)


@pytest.mark.asyncio
async def test_run_dispatch_reports_failed_task(tmp_path: Path) -> None:
    failed = AsyncMock(
        side_effect=lambda task, rate_limiter=None: TaskExecutionResult(
            task_id=task.id, status=TaskExecutionStatus.FAILED
        )
    )
    with patch("mail_cadence.pipeline.AsyncMailer.dispatch_task", failed):
        code = await run_dispatch(_options(tmp_path / "failed.db"), Settings())
    assert code == EXIT_TASK_FAILED


@pytest.mark.asyncio
async def test_run_dispatch_rejects_invalid_xml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text("<MailConfiguration><oops></MailConfiguration>", encoding="utf-8")
    code = await run_dispatch(_options(tmp_path / "bad.db", tasks_xml=bad), Settings())
    assert code == EXIT_CONFIG_ERROR


@pytest.mark.asyncio
async def test_run_dispatch_reports_rows_that_fail_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    assert await run_dispatch(_options(db_path), Settings()) == EXIT_OK

    # A row written by an older version that the models now reject.
    with closing(get_connection(db_path)) as conn, conn:
        conn.execute("DELETE FROM task_recipients")

    code = await run_dispatch(_options(db_path, tasks_xml=None, schedule_xml=None), Settings())
    assert code == EXIT_CONFIG_ERROR


@pytest.mark.asyncio
async def test_dispatch_single_task_unknown_task_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "single.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
    with pytest.raises(LookupError, match="does not exist"):
        await dispatch_single_task(db_path, "NOPE", Settings())


@pytest.mark.asyncio
async def test_dispatch_single_task_logs_result(tmp_path: Path) -> None:
    db_path = tmp_path / "single.db"
    assert await run_dispatch(_options(db_path), Settings()) == EXIT_OK
    ok = AsyncMock(
        return_value=TaskExecutionResult(task_id="TASK-001", status=TaskExecutionStatus.SUCCESS)
    )
    with patch("mail_cadence.pipeline.AsyncMailer.dispatch_task", ok):
        result = await dispatch_single_task(db_path, "TASK-001", Settings())

    assert result.status == TaskExecutionStatus.SUCCESS
    with closing(get_connection(db_path)) as conn:
        last_run, last_status = DatabaseRepository(conn).get_task_execution_states()["TASK-001"]
    assert last_status == TaskExecutionStatus.SUCCESS
    assert last_run is not None


def test_web_does_not_import_cli() -> None:
    code = "import sys, mail_cadence.web; print('mail_cadence.cli' in sys.modules)"
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    out = subprocess.run(  # noqa: S603 - fixed command, our own interpreter
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    ).stdout
    assert out.strip() == "False"
