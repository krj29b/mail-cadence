import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    ENV_FILE_VAR,
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    Settings,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app


def _task(subject: str = "Subject") -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=["u@example.com"]),
        subject=subject,
        body_template=BodyTemplate(template_path="builtin:daily_dispatch.html.j2"),
    )


def _success() -> TaskExecutionResult:
    return TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=1)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = get_connection(path)
    init_db(conn)
    repo = DatabaseRepository(conn)
    repo.save_work_schedules(
        WorkSchedules(
            timezone="UTC",
            schedules=[Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])],
        )
    )
    repo.save_single_task(_task())
    conn.close()
    return path


@pytest.fixture
def repo(db_path: Path) -> Iterator[DatabaseRepository]:
    with closing(get_connection(db_path)) as conn:
        yield DatabaseRepository(conn)


# --- N1: saving a task keeps the window state unless it changed ---------------------------


def test_unchanged_save_keeps_window_state(repo: DatabaseRepository) -> None:
    repo.log_task_execution(_success())
    assert repo.save_single_task(_task()) is False
    assert repo.get_task_execution_states()["T1"][1] == TaskExecutionStatus.SUCCESS


def test_changed_save_resets_window_state(repo: DatabaseRepository) -> None:
    repo.log_task_execution(_success())
    assert repo.save_single_task(_task(subject="Changed")) is True
    assert repo.get_task_execution_states()["T1"] == (None, None)


def test_forced_save_modes(repo: DatabaseRepository) -> None:
    repo.log_task_execution(_success())
    assert repo.save_single_task(_task(subject="Changed"), reset_window_state=False) is False
    assert repo.get_task_execution_states()["T1"][1] == TaskExecutionStatus.SUCCESS
    assert repo.save_single_task(_task(subject="Changed"), reset_window_state=True) is True
    assert repo.get_task_execution_states()["T1"] == (None, None)


def test_explicit_reset_window_state(repo: DatabaseRepository) -> None:
    repo.log_task_execution(_success())
    assert repo.reset_task_window_state("T1") is True
    assert repo.get_task_execution_states()["T1"] == (None, None)
    assert repo.reset_task_window_state("missing") is False


def test_web_unchanged_save_and_allow_resend(db_path: Path, repo: DatabaseRepository) -> None:
    repo.log_task_execution(_success())
    client = TestClient(create_app(db_path=db_path, allow_insecure_no_auth=True))
    form = {
        "id": "T1",
        "schedule_ref": "S1",
        "sender": "s@example.com",
        "priority": "normal",
        "max_retries": "3",
        "subject": "Subject",
        "body_format": "html",
        "body_template_path": "builtin:daily_dispatch.html.j2",
        "to": "u@example.com",
        "cc": "",
        "bcc": "",
        "context_variables": "[]",
    }

    saved = client.post("/tasks", data=form)
    assert saved.status_code == 200
    assert "No changes detected" in saved.text
    assert "Sent in Window" in saved.text
    assert "Allow Resend" in saved.text

    reset = client.post("/tasks/T1/reset-window")
    assert reset.status_code == 200
    assert "Window Open • Ready" in reset.text
    assert client.post("/tasks/missing/reset-window").status_code == 404


# --- Dry-run entries are flagged and excluded from statistics -----------------------------


def test_dry_run_logs_are_flagged_and_excluded_from_stats(
    db_path: Path, repo: DatabaseRepository
) -> None:
    repo.log_task_execution(_success(), dry_run=True)
    repo.log_task_execution(_success(), dry_run=True)

    assert repo.get_task_execution_states()["T1"] == (None, None)
    logs = repo.get_recent_execution_logs()
    assert [log["dry_run"] for log in logs] == [1, 1]

    stats = repo.get_dashboard_stats()
    assert stats["total_executions"] == 0
    assert stats["success_count"] == 0
    assert stats["total_emails_sent"] == 0
    assert stats["dry_run_count"] == 2

    repo.log_task_execution(_success())
    stats = repo.get_dashboard_stats()
    assert stats["total_executions"] == 1
    assert stats["total_emails_sent"] == 1

    client = TestClient(create_app(db_path=db_path, allow_insecure_no_auth=True))
    assert client.get("/dashboard/logs").text.count("DRY-RUN") == 2


def test_init_db_adds_dry_run_column_to_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE execution_logs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT)"
        )
        old.execute("INSERT INTO execution_logs (task_id, status) VALUES ('T1', 'SUCCESS')")
    old.close()

    conn = get_connection(path)
    init_db(conn)
    rows = conn.execute("SELECT dry_run FROM execution_logs").fetchall()
    assert [r["dry_run"] for r in rows] == [0]
    conn.close()


# --- A9: settings file location --------------------------------------------------------------


def test_settings_load_reads_env_file_from_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("SMTP_HOST=smtp.custom.example.com\nADMIN_USER=ops\n", encoding="utf-8")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))

    settings = Settings.load()
    assert settings.smtp_host == "smtp.custom.example.com"
    assert settings.admin_user == "ops"


def test_settings_load_defaults_to_env_in_current_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("SMTP_PORT=2525\n", encoding="utf-8")
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    assert Settings.load().smtp_port == 2525


def test_settings_load_fails_for_missing_explicit_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "missing.env"))
    with pytest.raises(FileNotFoundError):
        Settings.load()
