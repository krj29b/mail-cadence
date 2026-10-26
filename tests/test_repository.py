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


def test_delete_mail_task_existing_and_non_existing(repo: DatabaseRepository) -> None:
    schedule = Schedule(id="SCHED1", allowed_hours=[HourWindow(start="08:00", end="17:00")])
    repo.save_single_schedule(schedule)

    from mail_cadence.models import ContextVar

    task = MailTask(
        id="TASK1",
        schedule_ref="SCHED1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"], cc=["cc@example.com"]),
        subject="Test Task",
        body_template=BodyTemplate(template_path="builtin:daily_dispatch.html.j2"),
        context_variables=[ContextVar(name="var1", value="val1")],
    )
    repo.save_single_task(task)

    assert repo.get_mail_task("TASK1") is not None
    rec_count = repo.conn.execute(
        "SELECT COUNT(*) FROM task_recipients WHERE task_id = ?", ("TASK1",)
    ).fetchone()[0]
    var_count = repo.conn.execute(
        "SELECT COUNT(*) FROM task_context_variables WHERE task_id = ?", ("TASK1",)
    ).fetchone()[0]
    assert rec_count == 2
    assert var_count == 1

    # Delete existing task
    assert repo.delete_mail_task("TASK1") is True
    assert repo.get_mail_task("TASK1") is None

    # Check cascade deletions for recipients and context variables
    rec_count_after = repo.conn.execute(
        "SELECT COUNT(*) FROM task_recipients WHERE task_id = ?", ("TASK1",)
    ).fetchone()[0]
    var_count_after = repo.conn.execute(
        "SELECT COUNT(*) FROM task_context_variables WHERE task_id = ?", ("TASK1",)
    ).fetchone()[0]
    assert rec_count_after == 0
    assert var_count_after == 0

    # Delete non-existent task
    assert repo.delete_mail_task("NON_EXISTENT") is False
