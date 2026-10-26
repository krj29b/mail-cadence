"""A5: versioned schema migrations and one timestamp format."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import SCHEMA_VERSION, get_connection, init_db, schema_version
from mail_cadence.models import TaskExecutionResult, TaskExecutionStatus
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app, format_utc_timestamp


def test_fresh_database_gets_current_version(tmp_path: Path) -> None:
    with closing(get_connection(tmp_path / "fresh.db")) as conn:
        assert schema_version(conn) == 0
        init_db(conn)
        assert schema_version(conn) == SCHEMA_VERSION
        init_db(conn)  # second call is a no-op
        assert schema_version(conn) == SCHEMA_VERSION


def test_newer_database_is_refused(tmp_path: Path) -> None:
    with closing(get_connection(tmp_path / "future.db")) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with pytest.raises(sqlite3.DatabaseError, match="newer than this version"):
            init_db(conn)


def test_unversioned_database_is_upgraded_and_timestamps_converted(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as old, old:
        old.execute(
            "CREATE TABLE execution_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT NOT NULL, status TEXT NOT NULL, "
            "executed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        old.execute(
            "INSERT INTO execution_logs (task_id, status, executed_at) "
            "VALUES ('T1', 'SUCCESS', '2026-10-17 06:15:00')"
        )

    with closing(get_connection(path)) as conn:
        init_db(conn)
        row = conn.execute("SELECT executed_at, dry_run FROM execution_logs").fetchone()
        assert schema_version(conn) == SCHEMA_VERSION

    assert row["executed_at"] == "2026-10-17T06:15:00+00:00"
    assert row["dry_run"] == 0
    assert datetime.fromisoformat(row["executed_at"]) == datetime(2026, 10, 17, 6, 15, tzinfo=UTC)


def test_new_logs_and_last_run_use_the_same_iso_format(tmp_path: Path) -> None:
    with closing(get_connection(tmp_path / "fmt.db")) as conn:
        init_db(conn)
        conn.execute("INSERT INTO schedules (id) VALUES ('S1')")
        conn.execute(
            "INSERT INTO tasks (id, schedule_ref, sender, subject, body_template_path) "
            "VALUES ('T1', 'S1', 'a@example.com', 's', 'builtin:x.html')"
        )
        conn.commit()
        repo = DatabaseRepository(conn)
        repo.log_task_execution(
            TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS)
        )
        executed_at = conn.execute("SELECT executed_at FROM execution_logs").fetchone()[0]
        last_run = conn.execute("SELECT last_run FROM tasks").fetchone()[0]

    for value in (executed_at, last_run):
        parsed = datetime.fromisoformat(value)
        assert parsed.utcoffset() is not None
        assert value.endswith("+00:00")


def test_web_app_migrates_once_at_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    client = TestClient(create_app(db_path=db_path, allow_insecure_no_auth=True))
    with closing(get_connection(db_path)) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
    assert client.get("/").status_code == 200


def test_format_utc_timestamp() -> None:
    assert format_utc_timestamp("2026-10-17T06:15:00.123456+00:00") == "2026-10-17 06:15:00 UTC"
    assert format_utc_timestamp("2026-10-17T08:15:00+02:00") == "2026-10-17 06:15:00 UTC"
    assert format_utc_timestamp("not a date") == "not a date"
    assert format_utc_timestamp(None) == ""
