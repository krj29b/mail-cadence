from pathlib import Path

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
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
def repo(tmp_path: Path) -> DatabaseRepository:
    db_p = tmp_path / "test_repo.db"
    conn = get_connection(db_p)
    init_db(conn)
    return DatabaseRepository(conn)


def test_repository_count_and_pagination_with_filters_and_search(
    repo: DatabaseRepository,
) -> None:
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task_a",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=10,
            duration_ms=100.0,
        ),
        batch_id="batch_alpha",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task_a",
            status=TaskExecutionStatus.FAILED,
            sent_count=0,
            duration_ms=50.0,
            error_details="Timeout connecting to SMTP host",
        ),
        batch_id="batch_alpha",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task_b",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=5,
            duration_ms=120.0,
        ),
        batch_id="batch_beta",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task_b",
            status=TaskExecutionStatus.PARTIAL,
            sent_count=2,
            duration_ms=80.0,
            error_details="550 Mailbox unavailable",
        ),
        batch_id="batch_beta",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task_c",
            status=TaskExecutionStatus.SKIPPED_SCHEDULE,
            sent_count=0,
            duration_ms=5.0,
        ),
        batch_id="batch_gamma",
    )

    assert repo.count_execution_logs() == 5

    assert repo.count_execution_logs(status_filter="SUCCESS") == 2
    success_logs = repo.get_recent_execution_logs(limit=10, status_filter="SUCCESS")
    assert len(success_logs) == 2
    assert {log_item["task_id"] for log_item in success_logs} == {"task_a", "task_b"}

    assert repo.count_execution_logs(task_id_filter="task_b") == 2
    b_logs = repo.get_recent_execution_logs(limit=10, task_id_filter="task_b")
    assert len(b_logs) == 2

    assert repo.count_execution_logs(search_query="alpha") == 2

    assert repo.count_execution_logs(search_query="Mailbox unavailable") == 1
    err_logs = repo.get_recent_execution_logs(search_query="Mailbox unavailable")
    assert err_logs[0]["task_id"] == "task_b"

    p1 = repo.get_recent_execution_logs(limit=2, offset=0)
    assert len(p1) == 2
    p2 = repo.get_recent_execution_logs(limit=2, offset=2)
    assert len(p2) == 2
    p3 = repo.get_recent_execution_logs(limit=2, offset=4)
    assert len(p3) == 1

    p1_ids = [log_item["id"] for log_item in p1]
    p2_ids = [log_item["id"] for log_item in p2]
    p3_ids = [log_item["id"] for log_item in p3]
    assert set(p1_ids).isdisjoint(set(p2_ids))
    assert set(p2_ids).isdisjoint(set(p3_ids))


def _config(schedule_id: str, task_id: str) -> tuple[MailConfiguration, WorkSchedules]:
    mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="ops@example.com"),
        tasks=[
            MailTask(
                id=task_id,
                schedule_ref=schedule_id,
                sender="s@example.com",
                recipients=Recipients(to=["u@example.com"]),
                subject="Subject",
                body_template=BodyTemplate(template_path="builtin:daily_dispatch.html.j2"),
            )
        ],
    )
    work_schedules = WorkSchedules(
        timezone="UTC",
        schedules=[
            Schedule(id=schedule_id, allowed_hours=[HourWindow(start="06:00", end="12:00")])
        ],
    )
    return mail_config, work_schedules


def test_import_configuration_replace_removes_old_tasks_and_schedules(
    repo: DatabaseRepository,
) -> None:
    repo.import_configuration(*_config("OLD-SCHED", "OLD-TASK"))
    # Replacing must delete tasks before their (RESTRICT-referenced) schedules.
    repo.import_configuration(*_config("NEW-SCHED", "NEW-TASK"), mode="replace")

    assert [t.id for t in repo.get_mail_tasks()] == ["NEW-TASK"]
    assert [s.id for s in repo.get_work_schedules().schedules] == ["NEW-SCHED"]


def test_import_configuration_upsert_keeps_existing(repo: DatabaseRepository) -> None:
    repo.import_configuration(*_config("S1", "T1"))
    repo.import_configuration(*_config("S2", "T2"))

    assert [t.id for t in repo.get_mail_tasks()] == ["T1", "T2"]


def test_get_logged_task_ids(repo: DatabaseRepository) -> None:
    for task_id in ("a", "b", "a"):
        repo.log_task_execution(
            TaskExecutionResult(task_id=task_id, status=TaskExecutionStatus.SUCCESS)
        )
    assert sorted(repo.get_logged_task_ids()) == ["a", "b"]


def test_delete_schedule_success(repo: DatabaseRepository) -> None:
    schedule = Schedule(
        id="SCHED-1",
        description="Daily Morning",
        allowed_hours=[HourWindow(start="08:00", end="12:00")],
    )
    repo.save_single_schedule(schedule)
    assert repo.schedule_exists("SCHED-1") is True

    # Verify hour window was inserted
    hw_count_before = repo.conn.execute(
        "SELECT COUNT(*) FROM hour_windows WHERE schedule_id = 'SCHED-1'"
    ).fetchone()[0]
    assert hw_count_before == 1

    result = repo.delete_schedule("SCHED-1")
    assert result is True
    assert repo.schedule_exists("SCHED-1") is False

    # Verify hour windows deleted via ON DELETE CASCADE
    hw_count_after = repo.conn.execute(
        "SELECT COUNT(*) FROM hour_windows WHERE schedule_id = 'SCHED-1'"
    ).fetchone()[0]
    assert hw_count_after == 0


def test_delete_schedule_non_existent(repo: DatabaseRepository) -> None:
    result = repo.delete_schedule("NON-EXISTENT-SCHED")
    assert result is False


def test_delete_schedule_referenced_by_task_raises_integrity_error(
    repo: DatabaseRepository,
) -> None:
    mail_config, work_schedules = _config("SCHED-REF", "TASK-REF")
    repo.import_configuration(mail_config, work_schedules)

    assert repo.schedule_exists("SCHED-REF") is True
    assert len(repo.get_mail_tasks()) == 1

    with pytest.raises(pytest.importorskip("sqlite3").IntegrityError):
        repo.delete_schedule("SCHED-REF")
