"""Panel views of the execution state and operator decisions (ADR 0001, D12, stage 6)."""

import base64
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import Claim, ClaimRequest
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app

A = "a@example.com"
B = "b@example.com"


def _setup(db_path: Path, *, release: bool = True) -> Claim:
    """Task T1 with execution 1, whose single attempt has no result: both UNKNOWN."""
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        repo.save_work_schedules(
            WorkSchedules(
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])
                ]
            )
        )
        repo.save_single_task(
            MailTask(
                id="T1",
                schedule_ref="S1",
                sender="noreply@example.com",
                recipients=Recipients(to=[A, B]),
                subject="Hello",
                body_template=BodyTemplate(template_path="builtin:x.html.j2"),
            )
        )
        repo.start_run("run-1", "cli")
        claim = repo.claim_execution(ClaimRequest("T1", "S1@key", "run-1", "worker")).claim
        assert claim is not None
        attempt = repo.start_attempt(claim)
        assert attempt is not None
        repo.record_attempt_result(claim, attempt, {})
        if release:
            repo.release_execution(claim)
        return claim


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "web.db"


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> TestClient:
    _setup(db_path)
    return TestClient(
        create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    )


def _recipient_state(db_path: Path, address: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT state FROM execution_recipients WHERE envelope_address = ?", (address,)
        ).fetchone()
    return str(row[0])


def _resolve(client: TestClient, **form: str) -> int:
    data = {"recipient": A, "resolution": "accepted", "reason": "server log", "attempt_id": "1"}
    data.update(form)
    return client.post("/executions/1/resolve", data=data).status_code


def test_execution_list_and_detail_show_progress_and_the_resolve_form(client: TestClient) -> None:
    listing = client.get("/executions", params={"state": "NEEDS_REVIEW"}).text
    assert 'href="/executions/1"' in listing
    assert "NEEDS_REVIEW" in listing
    assert "0 / 2" in listing

    detail = client.get("/executions/1").text
    assert detail.count('action="/executions/1/resolve"') == 2
    assert 'name="attempt_id" value="1"' in detail
    assert "Resolve (attempt #1)" in detail


def test_dashboard_points_to_executions_that_need_review(client: TestClient) -> None:
    html = client.get("/").text
    assert "1 execution needs review" in html
    assert 'href="/executions?state=NEEDS_REVIEW"' in html


def test_operator_resolves_unknown_recipients(client: TestClient, db_path: Path) -> None:
    assert _resolve(client) == 200
    resp = client.post(
        "/executions/1/resolve",
        data={"recipient": B, "resolution": "retry", "reason": "worker stopped", "attempt_id": "1"},
    )

    assert resp.status_code == 200
    assert "execution is now RETRY_WAIT" in resp.text
    assert _recipient_state(db_path, A) == "ACCEPTED"
    assert _recipient_state(db_path, B) == "PENDING"
    with closing(sqlite3.connect(db_path)) as conn:
        events = conn.execute(
            "SELECT actor, reason, attempt_id FROM execution_events "
            "WHERE kind = 'unknown_resolved' ORDER BY event_id"
        ).fetchall()
    assert events == [("panel", "server log", 1), ("panel", "worker stopped", 1)]


@pytest.mark.parametrize(
    "form",
    [
        {"reason": "  "},
        {"resolution": "maybe"},
        {"attempt_id": "99"},
        {"attempt_id": "x"},
        {"recipient": "nobody@example.com"},
    ],
)
def test_invalid_decisions_are_rejected_without_changes(
    client: TestClient, db_path: Path, form: dict[str, str]
) -> None:
    assert _resolve(client, **form) == 400
    assert _recipient_state(db_path, A) == "UNKNOWN"


def test_unknown_execution_is_404(client: TestClient) -> None:
    assert client.get("/executions/99").status_code == 404
    assert client.post("/executions/99/resolve", data={"reason": "x"}).status_code == 404


def test_cross_site_decision_is_refused(client: TestClient, db_path: Path) -> None:
    resp = client.post(
        "/executions/1/resolve",
        data={"recipient": A, "resolution": "accepted", "reason": "x", "attempt_id": "1"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
    assert _recipient_state(db_path, A) == "UNKNOWN"


def test_decision_while_the_worker_still_owns_the_execution_is_409(
    db_path: Path, tmp_path: Path
) -> None:
    _setup(db_path, release=False)
    client = TestClient(
        create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    )
    assert _resolve(client) == 409


def test_actor_is_the_panel_user(db_path: Path, tmp_path: Path) -> None:
    _setup(db_path)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, admin_user="admin", admin_password=SecretStr("s3cret")
    )
    client = TestClient(create_app(db_path=db_path, template_dir=tmp_path, settings=settings))
    token = base64.b64encode(b"admin:s3cret").decode()
    resp = client.post(
        "/executions/1/resolve",
        data={"recipient": A, "resolution": "failed", "reason": "bounce", "attempt_id": "1"},
        headers={"Authorization": f"Basic {token}"},
    )
    assert resp.status_code == 200
    with closing(sqlite3.connect(db_path)) as conn:
        actor = conn.execute(
            "SELECT actor FROM execution_events WHERE kind = 'unknown_resolved'"
        ).fetchone()[0]
    assert actor == "panel:admin"


def test_task_list_links_the_last_execution_and_keys_the_run_button(client: TestClient) -> None:
    html = client.get("/tasks").text
    assert 'href="/executions/1"' in html
    keys = re.findall(r'hx-post="/tasks/T1/run\?idempotency_key=([0-9a-f]{32})"', html)
    assert len(keys) == 1
    assert keys[0] not in client.get("/tasks").text, "a new page render gets a new key"
