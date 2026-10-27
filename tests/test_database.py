import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailConfiguration,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """An in-memory database, closed after the test."""
    with closing(get_connection(":memory:")) as connection:
        yield connection


def test_database_init(conn: sqlite3.Connection) -> None:
    init_db(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}
    assert "schedules" in tables
    assert "tasks" in tables
    assert "execution_logs" in tables


def test_get_connection_pragmas(tmp_path: Path) -> None:
    # Memory connection check
    mem_conn = get_connection(":memory:")
    busy_timeout = mem_conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    journal_mode = mem_conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert busy_timeout == 30000
    assert journal_mode.lower() == "memory"
    mem_conn.close()

    # File connection check
    db_file = tmp_path / "test.db"
    file_conn = get_connection(db_file)
    busy_timeout = file_conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    journal_mode = file_conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert busy_timeout == 30000
    assert journal_mode.lower() == "wal"
    file_conn.close()


def test_repository_save_and_get_schedules(conn: sqlite3.Connection) -> None:
    init_db(conn)
    repo = DatabaseRepository(conn)

    schedules = WorkSchedules(
        timezone="Europe/Warsaw",
        schedules=[
            Schedule(
                id="SCHED-01",
                description="Test schedule",
                allowed_hours=[
                    HourWindow(start="08:00", end="12:00", max_concurrency=2, rate_limit_per_min=30)
                ],
            )
        ],
    )

    repo.save_work_schedules(schedules)
    loaded = repo.get_work_schedules()

    assert loaded.timezone == "Europe/Warsaw"
    assert len(loaded.schedules) == 1
    assert loaded.schedules[0].id == "SCHED-01"
    assert loaded.schedules[0].description == "Test schedule"
    assert len(loaded.schedules[0].allowed_hours) == 1
    assert loaded.schedules[0].allowed_hours[0].start == "08:00"


def test_repository_save_and_get_mail_config(conn: sqlite3.Connection) -> None:
    init_db(conn)
    repo = DatabaseRepository(conn)

    schedules = WorkSchedules(
        schedules=[Schedule(id="SCHED-01", allowed_hours=[HourWindow(start="00:00", end="24:00")])]
    )
    repo.save_work_schedules(schedules)

    mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="admin@example.com"),
        tasks=[
            MailTask(
                id="TASK-1",
                schedule_ref="SCHED-01",
                sender="sender@example.com",
                recipients=Recipients(
                    to=["to1@example.com", "to2@example.com"],
                    cc=["cc1@example.com"],
                    bcc=["bcc1@example.com"],
                ),
                subject="Test Subject",
                body_template=BodyTemplate(template_path="tpl.j2"),
                context_variables=[ContextVar(name="k1", value="v1")],
            )
        ],
    )

    repo.save_mail_configuration(mail_config)
    loaded = repo.get_mail_configuration()

    assert loaded.report_settings.recipient == "admin@example.com"
    assert len(loaded.tasks) == 1
    assert loaded.tasks[0].id == "TASK-1"
    assert loaded.tasks[0].recipients.to == ["to1@example.com", "to2@example.com"]
    assert loaded.tasks[0].recipients.cc == ["cc1@example.com"]
    assert loaded.tasks[0].recipients.bcc == ["bcc1@example.com"]
    assert loaded.tasks[0].context_variables[0].name == "k1"

    rows = conn.execute(
        "SELECT recipient_type, email, seq FROM task_recipients "
        "WHERE task_id = 'TASK-1' ORDER BY seq"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"recipient_type": "to", "email": "to1@example.com", "seq": 0},
        {"recipient_type": "to", "email": "to2@example.com", "seq": 1},
        {"recipient_type": "cc", "email": "cc1@example.com", "seq": 2},
        {"recipient_type": "bcc", "email": "bcc1@example.com", "seq": 3},
    ]


def test_repository_log_execution(conn: sqlite3.Connection) -> None:
    init_db(conn)
    repo = DatabaseRepository(conn)

    schedules = WorkSchedules(
        schedules=[Schedule(id="SCHED-01", allowed_hours=[HourWindow(start="00:00", end="24:00")])]
    )
    repo.save_work_schedules(schedules)

    mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="admin@example.com"),
        tasks=[
            MailTask(
                id="TASK-1",
                schedule_ref="SCHED-01",
                sender="sender@example.com",
                recipients=Recipients(to=["user1@example.com"]),
                subject="Test Subject",
                body_template=BodyTemplate(template_path="tpl.j2"),
            )
        ],
    )
    repo.save_mail_configuration(mail_config)

    res = TaskExecutionResult(
        task_id="TASK-1",
        status=TaskExecutionStatus.SUCCESS,
        sent_count=1,
        duration_ms=12.5,
    )
    repo.log_task_execution(res, batch_id="BATCH-001")

    history = repo.get_execution_results_for_batch("BATCH-001")
    assert len(history) == 1
    assert history[0].task_id == "TASK-1"
    assert history[0].status == TaskExecutionStatus.SUCCESS


def test_repository_get_report_settings_default_and_saved(conn: sqlite3.Connection) -> None:
    init_db(conn)
    repo = DatabaseRepository(conn)

    # 1. Test default report settings when no row exists in report_settings table
    defaults = repo.get_report_settings()
    assert defaults.recipient == "admin@example.com"
    assert defaults.sender is None
    assert defaults.subject_prefix == "[MailCadence Report]"
    assert defaults.include_full_error_trace is True

    # 2. Test save_report_settings and get_report_settings with custom values
    custom = ReportSettings(
        recipient="reports@company.com",
        sender="noreply@company.com",
        subject_prefix="[Custom]",
        include_full_error_trace=False,
    )
    repo.save_report_settings(custom)

    loaded = repo.get_report_settings()
    assert loaded.recipient == "reports@company.com"
    assert loaded.sender == "noreply@company.com"
    assert loaded.subject_prefix == "[Custom]"
    assert loaded.include_full_error_trace is False

    # 3. Test updating existing settings (upsert behavior)
    updated = ReportSettings(
        recipient="new_admin@company.com",
        sender="notifications@company.com",
        subject_prefix="[Updated Report]",
        include_full_error_trace=True,
    )
    repo.save_report_settings(updated)

    loaded_updated = repo.get_report_settings()
    assert loaded_updated.recipient == "new_admin@company.com"
    assert loaded_updated.sender == "notifications@company.com"
    assert loaded_updated.subject_prefix == "[Updated Report]"
    assert loaded_updated.include_full_error_trace is True


def test_get_task_execution_states(conn: sqlite3.Connection) -> None:
    init_db(conn)
    repo = DatabaseRepository(conn)

    schedules = WorkSchedules(
        schedules=[Schedule(id="SCHED-01", allowed_hours=[HourWindow(start="00:00", end="24:00")])]
    )
    repo.save_work_schedules(schedules)

    mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="admin@example.com"),
        tasks=[
            MailTask(
                id="TASK-1",
                schedule_ref="SCHED-01",
                sender="sender@example.com",
                recipients=Recipients(to=["user1@example.com"]),
                subject="Test Subject",
                body_template=BodyTemplate(template_path="tpl.j2"),
            )
        ],
    )
    repo.save_mail_configuration(mail_config)

    states = repo.get_task_execution_states()
    assert "TASK-1" in states
    assert states["TASK-1"] == (None, None)

    res = TaskExecutionResult(
        task_id="TASK-1",
        status=TaskExecutionStatus.SUCCESS,
        sent_count=1,
        duration_ms=10.0,
    )
    repo.log_task_execution(res, batch_id="B1")

    updated_states = repo.get_task_execution_states()
    last_run_dt, last_status = updated_states["TASK-1"]
    assert last_run_dt is not None
    assert last_status == TaskExecutionStatus.SUCCESS


def test_init_db_schema_migration_days_of_week(conn: sqlite3.Connection) -> None:
    # Manually create legacy v0.1.0 schema missing days_of_week
    conn.executescript("""
    CREATE TABLE schedules (id TEXT PRIMARY KEY, description TEXT);
    CREATE TABLE hour_windows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        max_concurrency INTEGER NOT NULL DEFAULT 1,
        rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
        seq INTEGER NOT NULL DEFAULT 0
    );
    """)
    conn.commit()

    # Call init_db and verify migration adds days_of_week
    init_db(conn)
    cursor = conn.execute("PRAGMA table_info(hour_windows)")
    cols = [row["name"] for row in cursor.fetchall()]
    assert "days_of_week" in cols
