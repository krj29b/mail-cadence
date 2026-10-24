from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    HourWindow,
    MailTask,
    ReportSettings,
    Schedule,
    Settings,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    db_p = tmp_path / "test_web.db"
    conn = get_connection(db_p)
    init_db(conn)

    repo = DatabaseRepository(conn)
    repo.save_work_schedules(
        WorkSchedules(
            timezone="UTC",
            schedules=[
                Schedule(
                    id="sched_default",
                    description="Default schedule",
                    allowed_hours=[HourWindow(start="00:00", end="23:59")],
                )
            ],
        )
    )
    repo.save_report_settings(
        ReportSettings(
            recipient="admin@example.com",
            sender="reports@example.com",
            subject_prefix="[Test]",
            include_full_error_trace=True,
        )
    )
    conn.close()
    return db_p


@pytest.fixture
def client(tmp_db_path: Path, tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path)
    return TestClient(app)


def test_dashboard_and_api_stats(client: TestClient, tmp_db_path: Path) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)

    res1 = TaskExecutionResult(
        task_id="task1",
        status=TaskExecutionStatus.SUCCESS,
        sent_count=10,
        duration_ms=150.0,
    )
    res2 = TaskExecutionResult(
        task_id="task2",
        status=TaskExecutionStatus.FAILED,
        sent_count=0,
        failed_recipients=["bad@example.com"],
        duration_ms=50.0,
        error_details="Connection error",
    )
    repo.log_task_execution(res1, batch_id="batch1")
    repo.log_task_execution(res2, batch_id="batch1")
    conn.close()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert "task1" in resp.text

    api_resp = client.get("/api/stats")
    assert api_resp.status_code == 200
    data = api_resp.json()
    assert data["total_executions"] == 2
    assert data["success_count"] == 1
    assert data["failed_count"] == 1
    assert data["total_emails_sent"] == 10


def test_api_logs_filtering(client: TestClient, tmp_db_path: Path) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task1",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=5,
            duration_ms=100.0,
        )
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="task2",
            status=TaskExecutionStatus.FAILED,
            sent_count=0,
            duration_ms=200.0,
        )
    )
    conn.close()

    all_logs = client.get("/api/logs").json()
    assert len(all_logs) == 2

    success_logs = client.get("/api/logs?status=SUCCESS").json()
    assert len(success_logs) == 1
    assert success_logs[0]["task_id"] == "task1"

    task2_logs = client.get("/api/logs?task_id=task2").json()
    assert len(task2_logs) == 1
    assert task2_logs[0]["status"] == "FAILED"


def test_tasks_crud_and_validation(client: TestClient) -> None:
    resp = client.get("/tasks")
    assert resp.status_code == 200

    task_data = {
        "id": "newsletter_task",
        "schedule_ref": "sched_default",
        "sender": "sender@example.com",
        "priority": "high",
        "max_retries": 2,
        "subject": "Weekly Newsletter",
        "body_format": "html",
        "body_template_path": "templates/newsletter.html.j2",
        "to": "user1@example.com, user2@example.com",
        "cc": "cc@example.com",
        "bcc": "",
        "context_variables": '[{"name": "name", "value": "Alice"}]',
    }
    create_resp = client.post("/tasks", data=task_data)
    assert create_resp.status_code == 200
    assert "newsletter_task" in create_resp.text

    # Validation rejection: newline injection in sender
    bad_task_data = task_data.copy()
    bad_task_data["id"] = "bad_task"
    bad_task_data["sender"] = "sender@example.com\r\nBcc: evil@example.com"
    bad_resp = client.post("/tasks", data=bad_task_data)
    assert bad_resp.status_code == 400
    assert "illegal newline" in bad_resp.json()["detail"].lower()

    del_resp = client.delete("/tasks/newsletter_task")
    assert del_resp.status_code == 200

    del_resp404 = client.delete("/tasks/non_existent")
    assert del_resp404.status_code == 404


def test_tasks_default_sender_from_settings(tmp_db_path: Path, tmp_path: Path) -> None:
    custom_settings = Settings(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="default_sender@example.com",
        admin_user=None,
        admin_password=None,
    )
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, settings=custom_settings)
    c = TestClient(app)

    resp = c.get("/tasks")
    assert resp.status_code == 200
    assert "default_sender@example.com" in resp.text

    # Submit task with empty sender -> falls back to settings.smtp_user
    create_resp = c.post(
        "/tasks",
        data={
            "id": "task_default_sender",
            "schedule_ref": "sched_default",
            "sender": "",
            "subject": "Default Sender Test",
            "body_format": "html",
            "body_template_path": "tpl.html",
            "to": "u1@example.com",
        },
    )
    assert create_resp.status_code == 200

    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    saved_task = repo.get_mail_task("task_default_sender")
    conn.close()
    assert saved_task is not None
    assert saved_task.sender == "default_sender@example.com"


def test_task_preview_and_path_traversal_blocking(
    client: TestClient, tmp_path: Path, tmp_db_path: Path
) -> None:
    template_file = tmp_path / "hello.html"
    template_file.write_text("<h1>Hello {{ name }}</h1>")

    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "preview_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Hello",
                "body_template": {"format": "html", "template_path": "hello.html"},
                "context_variables": [{"name": "name", "value": "Bob"}],
            }
        )
    )
    conn.close()

    preview_resp = client.get("/tasks/preview_task/preview")
    assert preview_resp.status_code == 200
    assert "<h1>Hello Bob</h1>" in preview_resp.text

    # Test Path Traversal rejection
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "traversal_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Hack",
                "body_template": {"format": "html", "template_path": "../../etc/passwd"},
            }
        )
    )
    conn.close()

    bad_preview = client.get("/tasks/traversal_task/preview")
    assert bad_preview.status_code == 400
    assert (
        "Template error" in bad_preview.json()["detail"]
        or "Access denied" in bad_preview.json()["detail"]
    )


def test_schedule_crud_and_integrity_constraint(client: TestClient, tmp_db_path: Path) -> None:
    list_resp = client.get("/schedules")
    assert list_resp.status_code == 200

    sched_data = {
        "id": "restricted_sched",
        "description": "Restricted dispatch window",
        "allowed_hours_json": (
            '[{"start": "09:00", "end": "17:00", "max_concurrency": 2, "rate_limit_per_min": 30}]'
        ),
    }
    save_resp = client.post("/schedules", data=sched_data)
    assert save_resp.status_code == 200

    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "bound_task",
                "schedule_ref": "restricted_sched",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Bound Task",
                "body_template": {"format": "html", "template_path": "tpl.html"},
            }
        )
    )
    conn.close()

    del_resp = client.delete("/schedules/restricted_sched")
    assert del_resp.status_code == 409
    assert "Cannot delete schedule" in del_resp.json()["detail"]

    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.delete_mail_task("bound_task")
    conn.close()

    del_ok = client.delete("/schedules/restricted_sched")
    assert del_ok.status_code == 200


def test_settings_view_and_update(client: TestClient) -> None:
    view_resp = client.get("/settings")
    assert view_resp.status_code == 200
    assert "admin@example.com" in view_resp.text
    assert "Active SMTP Server (.env)" in view_resp.text

    post_resp = client.post(
        "/settings",
        data={
            "recipient": "new_admin@example.com",
            "sender": "new_sender@example.com",
            "subject_prefix": "[Updated Report]",
            "include_full_error_trace": "true",
        },
    )
    assert post_resp.status_code == 200
    assert "Report settings updated successfully." in post_resp.text

    v2_resp = client.get("/settings")
    assert v2_resp.status_code == 200
    assert "new_admin@example.com" in v2_resp.text


def test_web_basic_auth_disabled_and_enabled(tmp_db_path: Path, tmp_path: Path) -> None:
    # 1. Unset admin_user / admin_password -> Open access
    open_app = create_app(
        db_path=tmp_db_path,
        template_dir=tmp_path,
        settings=Settings(admin_user=None, admin_password=None),
    )
    open_client = TestClient(open_app)
    res_open = open_client.get("/")
    assert res_open.status_code == 200

    # 2. Set admin_user & admin_password -> Protected access
    auth_settings = Settings(admin_user="admin", admin_password="secret123")  # noqa: S106
    auth_app = create_app(db_path=tmp_db_path, template_dir=tmp_path, settings=auth_settings)
    auth_client = TestClient(auth_app)

    # Missing credentials -> 401
    res_no_auth = auth_client.get("/")
    assert res_no_auth.status_code == 401
    assert res_no_auth.headers.get("WWW-Authenticate") == "Basic"

    # Wrong credentials -> 401
    res_wrong = auth_client.get("/", auth=("admin", "wrong"))
    assert res_wrong.status_code == 401

    # Correct credentials -> 200
    res_ok = auth_client.get("/", auth=("admin", "secret123"))
    assert res_ok.status_code == 200


def test_web_dispatch_trigger_run(tmp_db_path: Path, tmp_path: Path) -> None:
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path)
    client = TestClient(app)

    # Trigger run with dry_run
    res = client.post("/dispatch/run", data={"dry_run": "true"})
    assert res.status_code == 200
    assert "Manual dispatch completed (dry_run=True)" in res.text


def test_web_dispatch_trigger_task_run(tmp_db_path: Path, tmp_path: Path) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "task_single_run",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Test Task Run",
                "body_template": {"format": "html", "template_path": "tpl.html"},
            }
        )
    )
    conn.close()

    app = create_app(db_path=tmp_db_path, template_dir=tmp_path)
    client = TestClient(app)

    res = client.post("/tasks/task_single_run/run")
    assert res.status_code == 200
    assert "task_single_run" in res.text

    # Test 404 for unknown task
    res_404 = client.post("/tasks/unknown_task/run")
    assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_web_dispatch_lock_conflict(tmp_db_path: Path, tmp_path: Path) -> None:
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path)
    client = TestClient(app)

    # Acquire lock manually
    await app.state.dispatch_lock.acquire()
    try:
        res_dispatch = client.post("/dispatch/run")
        assert res_dispatch.status_code == 409
        assert "A dispatch batch is already running." in res_dispatch.json()["detail"]

        res_task = client.post("/tasks/sched_default/run")
        assert res_task.status_code == 409
        assert "A dispatch batch is already running." in res_task.json()["detail"]
    finally:
        app.state.dispatch_lock.release()
