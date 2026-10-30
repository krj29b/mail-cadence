"""Panel views of the send context: the task message page, the execution page and
the render_subject field of the task form."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import ClaimRequest
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    SendContext,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app


def _task(subject: str = "Report {{ cadence.task_id }}", *, render: bool = True) -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="cadence@example.com",
        subject=subject,
        render_subject=render,
        recipients=Recipients(to=["a@example.com"], cc=["c@example.com"]),
        body_template=BodyTemplate(template_path="body.html.j2"),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Body</p>", encoding="utf-8")
    path = tmp_path / "cadence.db"
    with closing(get_connection(path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="08:00")
        repo.save_work_schedules(
            WorkSchedules(
                timezone="Europe/Warsaw", schedules=[Schedule(id="S1", allowed_hours=[window])]
            )
        )
        repo.save_single_task(_task())
    return path


def _client(db_path: Path) -> TestClient:
    return TestClient(
        create_app(db_path=db_path, template_dir=db_path.parent, allow_insecure_no_auth=True)
    )


def test_message_page_shows_the_rendered_subject_and_the_context(db_path: Path) -> None:
    page = _client(db_path).get("/tasks/T1/message")

    assert page.status_code == 200
    assert 'data-testid="subject">Report T1<' in page.text
    assert "Europe/Warsaw" in page.text
    assert "manual send" in page.text
    assert "window " in page.text
    assert 'href="/tasks/T1/preview"' in page.text
    assert "c@example.com" in page.text


def test_message_page_reports_a_broken_subject_template(db_path: Path) -> None:
    with closing(get_connection(db_path)) as conn:
        DatabaseRepository(conn).save_single_task(_task("Report {{ missing }}"))

    page = _client(db_path).get("/tasks/T1/message")

    assert page.status_code == 200
    assert 'data-testid="subject-error"' in page.text
    assert "missing" in page.text


def test_message_page_of_an_unknown_task_is_404(db_path: Path) -> None:
    assert _client(db_path).get("/tasks/nope/message").status_code == 404


def test_task_list_links_the_message_page(db_path: Path) -> None:
    assert 'href="/tasks/T1/message"' in _client(db_path).get("/tasks").text


@pytest.mark.parametrize(("checked", "expected"), [(True, True), (False, False)])
def test_task_form_saves_render_subject(db_path: Path, checked: bool, expected: bool) -> None:
    form = {
        "id": "T2",
        "schedule_ref": "S1",
        "sender": "cadence@example.com",
        "subject": "Report {{ cadence.date }}",
        "to": "a@example.com",
        "body_format": "html",
        "body_template_path": "body.html.j2",
        "context_variables": "[]",
    }
    if checked:
        form["render_subject"] = "true"

    assert _client(db_path).post("/tasks", data=form).status_code == 200

    with closing(get_connection(db_path)) as conn:
        saved = DatabaseRepository(conn).get_mail_task("T2")
    assert saved is not None
    assert saved.render_subject is expected


def test_task_form_shows_the_stored_flag_when_editing(db_path: Path) -> None:
    page = _client(db_path).get("/tasks", params={"edit": "T1"})

    assert 'id="render_subject"' in page.text
    assert "checked" in page.text.split('id="render_subject"', 1)[1].split(">", 1)[0]


def test_execution_page_shows_the_stored_context(db_path: Path) -> None:
    start = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)
    end = datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    context = SendContext.build("T1", "window", "Europe/Warsaw", (start, end), start)
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        repo.start_run("run-1", "cli")
        claimed = repo.claim_execution(
            ClaimRequest("T1", "S1@x", "run-1", "owner", send_context=context)
        )
        assert claimed.claim is not None
        repo.release_execution(claimed.claim)
        bare = repo.claim_execution(ClaimRequest("T1", "manual:k", "run-1", "owner"))
    assert claimed.execution_id is not None
    assert bare.execution_id is not None

    client = _client(db_path)
    page = client.get(f"/executions/{claimed.execution_id}").text
    old = client.get(f"/executions/{bare.execution_id}").text

    assert "Sent for 2026-10-28" in page
    assert "2026-10-28 06:00" in page
    assert "scheduled send" in page
    assert "Send context not stored" in old
