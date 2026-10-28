import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mail_cadence.database import get_connection, init_db
from mail_cadence.dispatch_jobs import DispatchJob
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
from mail_cadence.web import _discover_available_templates, create_app


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
                    allowed_hours=[HourWindow(start="00:00", end="24:00")],
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
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
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
    app = create_app(
        db_path=tmp_db_path,
        template_dir=tmp_path,
        settings=custom_settings,
        allow_insecure_no_auth=True,
    )
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


@pytest.mark.parametrize("content", ["<h1>Hello {{ nme }}</h1>", "<p></p>"])
def test_task_preview_rejects_broken_template(
    client: TestClient, tmp_path: Path, tmp_db_path: Path, content: str
) -> None:
    (tmp_path / "broken.html").write_text(content)
    conn = get_connection(tmp_db_path)
    DatabaseRepository(conn).save_single_task(
        MailTask.model_validate(
            {
                "id": "broken_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Hello",
                "body_template": {"format": "html", "template_path": "broken.html"},
                "context_variables": [{"name": "name", "value": "Bob"}],
            }
        )
    )
    conn.close()

    resp = client.get("/tasks/broken_task/preview")
    assert resp.status_code == 400
    assert "Template error" in resp.json()["detail"]


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
    # 1. Unset admin_user / admin_password -> access refused by default
    closed_app = create_app(
        db_path=tmp_db_path,
        template_dir=tmp_path,
        settings=Settings(admin_user=None, admin_password=None),
    )
    res_closed = TestClient(closed_app).get("/")
    assert res_closed.status_code == 503

    # 1b. Explicit opt-in -> open access
    open_app = create_app(
        db_path=tmp_db_path,
        template_dir=tmp_path,
        settings=Settings(admin_user=None, admin_password=None),
        allow_insecure_no_auth=True,
    )
    open_client = TestClient(open_app)
    res_open = open_client.get("/")
    assert res_open.status_code == 200

    # 2. Set admin_user & admin_password -> Protected access
    auth_settings = Settings(admin_user="admin", admin_password=SecretStr("secret123"))
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
    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    client = TestClient(app)

    # Trigger run with dry_run: it starts in the background and returns immediately
    res = client.post("/dispatch/run", data={"dry_run": "true"})
    assert res.status_code == 202
    assert "Dispatch started (dry_run=True)" in res.text

    job = app.state.dispatch_runner.wait(timeout=30)
    assert job is not None
    assert job.state == "finished"

    status_json = client.get("/api/dispatch/status").json()
    assert status_json["kind"] == "batch"
    assert status_json["state"] == "finished"
    assert status_json["dry_run"] is True
    assert status_json["exit_code"] == 0

    fragment = client.get("/dispatch/status")
    assert fragment.status_code == 200
    assert "Dispatch batch finished (dry run)" in fragment.text
    assert "hx-trigger" not in fragment.text  # polling stops once the job is done


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

    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    client = TestClient(app)

    res = client.post("/tasks/task_single_run/run")
    assert res.status_code == 202
    assert "task_single_run" in res.text

    job = app.state.dispatch_runner.wait(timeout=30)
    assert job is not None
    assert job.state == "finished"

    done = client.get("/dispatch/status")
    assert "task_single_run" in done.text
    assert "alert-error" in done.text
    assert "Template rendering error" in done.text

    # Test 404 for unknown task
    res_404 = client.post("/tasks/unknown_task/run")
    assert res_404.status_code == 404


def test_template_discovery_excludes_internal_and_web_templates(tmp_path: Path) -> None:
    (tmp_path / "custom.html.j2").write_text("x")
    (tmp_path / "report_email.html.j2").write_text("x")
    (tmp_path / "web_templates").mkdir()
    (tmp_path / "web_templates" / "page.html").write_text("x")

    discovered = _discover_available_templates(tmp_path)
    assert discovered[0] == "custom.html.j2"
    assert "builtin:daily_dispatch.html.j2" in discovered
    assert "builtin:settlement_summary.html.j2" in discovered
    assert not any("report_email" in t for t in discovered)
    assert not any("web_templates" in t for t in discovered)

    # Built-in templates are listed even without a template directory.
    assert "builtin:daily_dispatch.html.j2" in _discover_available_templates(tmp_path / "missing")


def test_task_edit_resets_window_idempotency_and_skip_preserves_success(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "editable_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["user@example.com"]},
                "subject": "Initial Subject",
                "body_template": {
                    "format": "html",
                    "template_path": "builtin:daily_dispatch.html.j2",
                },
            }
        )
    )

    # 1. Log SUCCESS -> task is Sent in Window
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="editable_task",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=1,
            duration_ms=10.0,
        )
    )
    # 2. Log SKIPPED_SCHEDULE -> must NOT overwrite tasks.last_status == SUCCESS
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="editable_task",
            status=TaskExecutionStatus.SKIPPED_SCHEDULE,
            sent_count=0,
            duration_ms=0.0,
            error_details="Task already dispatched in current 'sched_default' window",
        )
    )
    _, last_st = repo.get_task_execution_states()["editable_task"]
    assert last_st == TaskExecutionStatus.SUCCESS
    conn.close()

    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    c = TestClient(app)

    # Still shows Sent in Window on /tasks
    resp_before_edit = c.get("/tasks")
    assert "Sent in Window" in resp_before_edit.text

    # 3. Edit and save task via POST /tasks -> resets last_status/last_run so it's Ready again
    resp_after_edit = c.post(
        "/tasks",
        data={
            "id": "editable_task",
            "schedule_ref": "sched_default",
            "sender": "sender@example.com",
            "priority": "normal",
            "max_retries": 3,
            "subject": "Updated Subject",
            "body_format": "html",
            "body_template_path": "builtin:daily_dispatch.html.j2",
            "to": "user@example.com",
            "cc": "",
            "bcc": "",
            "context_variables": '[{"name": "region", "value": "PL"}]',
        },
    )
    assert resp_after_edit.status_code == 200
    assert "Window Open • Ready" in resp_after_edit.text


def test_web_dispatch_lock_conflict(tmp_db_path: Path, tmp_path: Path) -> None:
    conn = get_connection(tmp_db_path)
    DatabaseRepository(conn).save_single_task(
        MailTask.model_validate(
            {
                "id": "busy_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["to@example.com"]},
                "subject": "Busy",
                "body_template": {"format": "html", "template_path": "tpl.html"},
            }
        )
    )
    conn.close()

    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    client = TestClient(app)
    release = threading.Event()

    async def blocking_work(_job: DispatchJob) -> None:
        await asyncio.to_thread(release.wait, 30)

    app.state.dispatch_runner.start(DispatchJob(kind="batch"), blocking_work)
    try:
        running = client.get("/dispatch/status")
        assert "hx-trigger" in running.text  # keeps polling while running

        res_dispatch = client.post("/dispatch/run")
        assert res_dispatch.status_code == 409
        assert "A dispatch batch is already running." in res_dispatch.json()["detail"]

        res_task = client.post("/tasks/busy_task/run")
        assert res_task.status_code == 409
        assert "A dispatch batch is already running." in res_task.json()["detail"]
    finally:
        release.set()
        app.state.dispatch_runner.wait(timeout=30)

    assert client.get("/api/dispatch/status").json()["state"] == "finished"


def test_api_task_detail_and_edit_query(client: TestClient, tmp_db_path: Path) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "interactive_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["user@example.com"], "cc": ["cc@example.com"]},
                "subject": "Interactive Subject",
                "body_template": {"format": "html", "template_path": "templates/test.html.j2"},
                "context_variables": [{"name": "var1", "value": "val1"}],
            }
        )
    )
    conn.close()

    # GET /api/tasks/{task_id} -> 200 OK
    resp = client.get("/api/tasks/interactive_task")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "interactive_task"
    assert data["subject"] == "Interactive Subject"
    assert data["body_template_path"] == "templates/test.html.j2"
    assert data["to"] == ["user@example.com"]
    assert data["context_variables"] == [{"name": "var1", "value": "val1"}]

    # GET /api/tasks/unknown -> 404
    resp_404 = client.get("/api/tasks/non_existent_task")
    assert resp_404.status_code == 404

    # GET /tasks?edit=interactive_task -> 200 OK pre-populated in template
    edit_resp = client.get("/tasks?edit=interactive_task")
    assert edit_resp.status_code == 200
    assert 'value="interactive_task"' in edit_resp.text
    assert "Interactive Subject" in edit_resp.text


def test_template_discovery_and_window_status(tmp_db_path: Path, tmp_path: Path) -> None:
    # Create sample template files
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / "welcome.html.j2").write_text("<p>Welcome {{ name }}</p>")
    (tpl_dir / "notice.txt").write_text("Notice to {{ name }}")

    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.save_single_task(
        MailTask.model_validate(
            {
                "id": "status_task",
                "schedule_ref": "sched_default",
                "sender": "sender@example.com",
                "recipients": {"to": ["user@example.com"]},
                "subject": "Status Test",
                "body_template": {"format": "html", "template_path": "templates/welcome.html.j2"},
            }
        )
    )
    conn.close()

    app = create_app(db_path=tmp_db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    c = TestClient(app)

    # Check template discovery & window open indicator
    resp = c.get("/tasks")
    assert resp.status_code == 200
    assert "templates/welcome.html.j2" in resp.text
    assert "templates/notice.txt" in resp.text
    assert "Window Open • Ready" in resp.text

    # Manually log execution as SUCCESS to test Sent in Window indicator
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="status_task",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=1,
            duration_ms=10.0,
        )
    )
    conn.close()

    resp2 = c.get("/tasks")
    assert resp2.status_code == 200
    assert "Sent in Window" in resp2.text


def test_api_schedule_detail_edit_query_and_timezone_selector(
    client: TestClient, tmp_db_path: Path
) -> None:
    # Test GET /api/schedules/{schedule_id}
    resp = client.get("/api/schedules/sched_default")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "sched_default"
    assert data["description"] == "Default schedule"
    assert len(data["allowed_hours"]) == 1

    # Test GET /api/schedules/unknown -> 404
    resp_404 = client.get("/api/schedules/non_existent_schedule")
    assert resp_404.status_code == 404

    # Test GET /schedules?edit=sched_default -> pre-populates schedule
    edit_resp = client.get("/schedules?edit=sched_default")
    assert edit_resp.status_code == 200
    assert 'value="sched_default"' in edit_resp.text
    assert "Default schedule" in edit_resp.text

    # Test GET /schedules contains timezone selector with "UTC" and "Europe/Warsaw"
    sched_page_resp = client.get("/schedules")
    assert sched_page_resp.status_code == 200
    assert 'value="UTC"' in sched_page_resp.text
    assert 'value="Europe/Warsaw"' in sched_page_resp.text
    assert "Europe/Warsaw" in sched_page_resp.text


def test_dashboard_logs_htmx_filtering_and_pagination(
    client: TestClient, tmp_db_path: Path
) -> None:
    conn = get_connection(tmp_db_path)
    repo = DatabaseRepository(conn)

    repo.log_task_execution(
        TaskExecutionResult(
            task_id="t1",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=1,
            duration_ms=10.0,
        ),
        batch_id="b1",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="t1",
            status=TaskExecutionStatus.FAILED,
            sent_count=0,
            duration_ms=20.0,
            error_details="Connection reset by peer",
        ),
        batch_id="b1",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="t2",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=5,
            duration_ms=30.0,
        ),
        batch_id="b2",
    )
    repo.log_task_execution(
        TaskExecutionResult(
            task_id="t2",
            status=TaskExecutionStatus.PARTIAL,
            sent_count=1,
            duration_ms=15.0,
            error_details="Recipient error",
        ),
        batch_id="b2",
    )
    conn.close()

    resp_success = client.get("/dashboard/logs?status=SUCCESS")
    assert resp_success.status_code == 200
    assert "SUCCESS" in resp_success.text
    assert "FAILED" not in resp_success.text
    assert "Showing 1–2 of 2 execution logs" in resp_success.text  # noqa: RUF001

    resp_t1 = client.get("/dashboard/logs?task_id=t1")
    assert resp_t1.status_code == 200
    assert "t1" in resp_t1.text
    assert "Showing 1–2 of 2 execution logs" in resp_t1.text  # noqa: RUF001

    resp_q = client.get("/dashboard/logs?q=Connection reset")
    assert resp_q.status_code == 200
    assert "t1" in resp_q.text
    assert "Showing 1–1 of 1 execution logs" in resp_q.text  # noqa: RUF001

    resp_p2 = client.get("/dashboard/logs?page=2&page_size=2")
    assert resp_p2.status_code == 200
    assert "Showing 3–4 of 4 execution logs (Page 2 of 2)" in resp_p2.text  # noqa: RUF001

    resp_htmx = client.get(
        "/",
        headers={"HX-Request": "true", "HX-Target": "logs-table-container"},
    )
    assert resp_htmx.status_code == 200
    assert "<table" in resp_htmx.text
    assert "<html" not in resp_htmx.text


def test_web_rejects_cross_site_state_changes(client: TestClient) -> None:
    form = {"dry_run": "true"}

    cross_fetch = client.post("/dispatch/run", data=form, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_fetch.status_code == 403

    foreign_origin = client.post(
        "/dispatch/run", data=form, headers={"Origin": "https://evil.example.com"}
    )
    assert foreign_origin.status_code == 403

    cross_delete = client.delete("/tasks/any", headers={"Sec-Fetch-Site": "same-site"})
    assert cross_delete.status_code == 403

    same_origin = client.post(
        "/dispatch/run",
        data=form,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert same_origin.status_code == 202
    client.app.state.dispatch_runner.wait(timeout=30)  # type: ignore[attr-defined]

    # Safe methods are not affected.
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_discover_available_templates_lists_a_template_added_later(tmp_path: Path) -> None:
    (tmp_path / "first.html.j2").write_text("content")
    assert "second.html.j2" not in _discover_available_templates(tmp_path)

    (tmp_path / "second.html.j2").write_text("content")
    assert "second.html.j2" in _discover_available_templates(tmp_path)


def test_discover_available_templates_skips_hidden_and_ignored_dirs(tmp_path: Path) -> None:
    for rel in ["ok/a.j2", ".venv/b.j2", "node_modules/c.j2", "_drafts/d.j2", "ok/_e.j2"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("content")

    user_templates = [t for t in _discover_available_templates(tmp_path) if ":" not in t]
    assert user_templates == ["ok/a.j2"]
