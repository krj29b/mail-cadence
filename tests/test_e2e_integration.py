import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.cli import run_dispatch_pipeline
from mail_cadence.database import get_connection
from mail_cadence.models import TaskExecutionStatus
from mail_cadence.repository import DatabaseRepository


@pytest.mark.asyncio
async def test_full_dispatch_pipeline_with_ref_dt(tmp_path: Path) -> None:
    tasks_file = Path("config/samples/tasks.xml")
    schedules_file = Path("config/samples/schedules.xml")

    # Reference time: 2026-10-17 08:00 Warsaw time (Morning schedule is active: 06:00-12:00)
    ref_dt = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    code = await run_dispatch_pipeline(
        tasks_xml=tasks_file,
        schedule_xml=schedules_file,
        dry_run=True,
        verbose=True,
        ref_dt=ref_dt,
    )

    assert code == 0


@pytest.mark.asyncio
async def test_run_dispatch_pipeline_idempotency_two_runs(tmp_path: Path) -> None:
    tasks_file = Path("config/samples/tasks.xml")
    schedules_file = Path("config/samples/schedules.xml")
    db_file = tmp_path / "test_idempotency.db"

    # Reference time: 2026-10-17 08:00 Warsaw time (Morning schedule active: 06:00-12:00)
    ref_dt = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    async def run(dry_run: bool) -> int:
        return await run_dispatch_pipeline(
            tasks_xml=tasks_file,
            schedule_xml=schedules_file,
            dry_run=dry_run,
            ref_dt=ref_dt,
            db_path=str(db_file),
        )

    def last_log(conn: sqlite3.Connection) -> tuple[str, str | None]:
        row = conn.execute(
            "SELECT status, error_details FROM execution_logs "
            "WHERE task_id = 'TASK-001' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["status"], row["error_details"]

    fake_smtp = AsyncMock(return_value=(True, None, {}))
    with patch("mail_cadence.mailer.AsyncMailer.send_single_message", fake_smtp):
        # 1. Dry-run: TASK-001 (in its window) is simulated but its state stays untouched.
        assert await run(dry_run=True) == 0
        with closing(get_connection(db_file)) as conn:
            states = DatabaseRepository(conn).get_task_execution_states()
            assert states["TASK-001"] == (None, None)
            assert last_log(conn)[0] == TaskExecutionStatus.SUCCESS.value

        # 2. Real run: TASK-001 is sent and recorded as SUCCESS at ref_dt.
        assert await run(dry_run=False) == 0
        with closing(get_connection(db_file)) as conn:
            last_run, last_status = DatabaseRepository(conn).get_task_execution_states()["TASK-001"]
            assert last_status == TaskExecutionStatus.SUCCESS
            assert last_run == ref_dt

        # 3. Second real run in the same window: TASK-001 is skipped by idempotency.
        assert await run(dry_run=False) == 0
        with closing(get_connection(db_file)) as conn:
            status, reason = last_log(conn)
            assert status == TaskExecutionStatus.SKIPPED_SCHEDULE.value
            assert reason is not None
            assert "already dispatched" in reason


@pytest.mark.asyncio
async def test_full_dispatch_pipeline_invalid_xml(tmp_path: Path) -> None:
    invalid_xml = tmp_path / "invalid.xml"
    invalid_xml.write_bytes(b"<MailConfiguration><BadTag></MailConfiguration>")
    schedules_file = Path("config/samples/schedules.xml")

    code = await run_dispatch_pipeline(
        tasks_xml=invalid_xml,
        schedule_xml=schedules_file,
        dry_run=True,
    )

    assert code == 1
