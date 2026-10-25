from pathlib import Path

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import TaskExecutionResult, TaskExecutionStatus
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
