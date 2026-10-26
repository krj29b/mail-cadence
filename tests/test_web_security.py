"""Web panel hardening: security headers, CSP-compatible markup, local assets, XSS."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import (
    CONTENT_SECURITY_POLICY,
    PACKAGE_STATIC_DIR,
    PREVIEW_CONTENT_SECURITY_POLICY,
    create_app,
)

# An ID that broke out of the old onclick="loadScheduleToEdit('{{ id }}')" markup.
EVIL_ID = "x');alert(document.domain);('"
EVIL_VALUE = "</textarea><script>alert(1)</script>\"'"

PAGES = [
    "/",
    "/tasks",
    f"/tasks?edit={EVIL_ID}",
    "/schedules",
    f"/schedules?edit={EVIL_ID}",
    "/settings",
    "/dashboard/logs",
    "/dispatch/status",
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "sec.db"
    conn = get_connection(db_path)
    init_db(conn)
    repo = DatabaseRepository(conn)
    repo.save_work_schedules(
        WorkSchedules(
            schedules=[Schedule(id=EVIL_ID, allowed_hours=[HourWindow(start="00:00", end="24:00")])]
        )
    )
    repo.save_single_task(
        MailTask(
            id=EVIL_ID,
            schedule_ref=EVIL_ID,
            sender="noreply@example.com",
            recipients=Recipients(to=["to@example.com"]),
            subject="<b>subject</b>",
            body_template=BodyTemplate(format="html", template_path="t.html"),
            context_variables=[ContextVar(name="name", value=EVIL_VALUE)],
        )
    )
    conn.close()
    (tmp_path / "t.html").write_text("<p>{{ name }}</p><script>alert(1)</script>")
    app = create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    return TestClient(app)


@pytest.mark.parametrize("path", PAGES)
def test_pages_send_security_headers(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # "no-referrer" would make browsers send "Origin: null" on form posts (rejected as 403).
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", PAGES)
def test_pages_have_no_inline_code_or_external_assets(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag or 'type="application/json"' in tag, f"inline script: {tag}"
    assert not re.search(r"<style\b|\sstyle\s*=", html), "inline style"
    assert not re.search(r'(?:src|href)="(?:https?:)?//', html), "external asset"


@pytest.mark.parametrize("path", PAGES)
def test_referenced_static_assets_exist(client: TestClient, path: str) -> None:
    html = client.get(path).text
    for ref in set(re.findall(r'(?:src|href)="(/static/[^"?]+)', html)):
        resp = client.get(ref)
        assert resp.status_code == 200, ref
        assert resp.headers["cache-control"] == "no-cache"


def test_malicious_id_is_only_data(client: TestClient) -> None:
    html = client.get("/schedules").text
    # The ID is an HTML-escaped attribute value read by JavaScript as text, never code.
    assert 'data-schedule-id="x&#39;);alert(document.domain);(&#39;"' in html
    assert "alert(document.domain);('" not in html


def test_edit_form_values_cannot_break_out(client: TestClient) -> None:
    html = client.get("/tasks", params={"edit": EVIL_ID}).text
    raw = re.search(r'<textarea id="context_variables"[^>]*>(.*?)</textarea>', html, re.S)
    assert raw is not None
    assert "<script>" not in raw.group(1)
    data = json.loads(
        raw.group(1).replace("&#34;", '"').replace("&#39;", "'").replace("&amp;", "&")
    )
    assert data == [{"name": "name", "value": EVIL_VALUE}]


def test_schedule_edit_data_is_a_json_block(client: TestClient) -> None:
    html = client.get("/schedules", params={"edit": EVIL_ID}).text
    block = re.search(
        r'<script type="application/json" id="edit-schedule-windows">(.*?)</script>', html, re.S
    )
    assert block is not None
    assert json.loads(block.group(1))[0]["end"] == "24:00"


def test_preview_is_sandboxed(client: TestClient) -> None:
    resp = client.get(f"/tasks/{EVIL_ID}/preview")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == PREVIEW_CONTENT_SECURITY_POLICY
    assert PREVIEW_CONTENT_SECURITY_POLICY.startswith("sandbox;")
    assert "script-src" not in PREVIEW_CONTENT_SECURITY_POLICY  # default-src 'none' applies


def test_static_assets_do_not_need_credentials(tmp_path: Path) -> None:
    settings = Settings(admin_user="admin", admin_password="secret")  # noqa: S106
    client = TestClient(create_app(db_path=tmp_path / "auth.db", settings=settings))
    assert client.get("/").status_code == 401
    assert client.get("/static/app.css").status_code == 200


def test_static_dir_holds_no_unexpected_files() -> None:
    allowed = {".css", ".js", ".woff2", ".txt"}
    files = [p for p in PACKAGE_STATIC_DIR.rglob("*") if p.is_file()]
    assert files
    assert {p.suffix for p in files} <= allowed
