from pathlib import Path

import aiosmtplib
import jinja2
import pytest

from mail_cadence.mailer import (
    AsyncMailer,
    TemplateRenderer,
    _extract_sender_domain,
    _html_to_plain_text,
    build_email_message,
    list_builtin_templates,
)
from mail_cadence.models import BodyTemplate, MailTask, Outcome, Recipients, Settings


def test_template_renderer(tmp_path: Path) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    renderer = TemplateRenderer(template_dir=tmp_path)
    res = renderer.render("test.j2", {"name": "World"})
    assert res == "Hello World!"


def test_template_renderer_nested_posix_path(tmp_path: Path) -> None:
    sub_dir = tmp_path / "nested"
    sub_dir.mkdir()
    tpl_file = sub_dir / "tpl.j2"
    tpl_file.write_text("Nested {{ name }}!")

    renderer = TemplateRenderer(template_dir=tmp_path)
    res = renderer.render("nested/tpl.j2", {"name": "World"})
    assert res == "Nested World!"


def test_template_renderer_builtin_prefix_works_from_any_dir(tmp_path: Path) -> None:
    for base_dir in (tmp_path, tmp_path / "does_not_exist"):
        renderer = TemplateRenderer(template_dir=base_dir)
        out = renderer.render(
            "builtin:daily_dispatch.html.j2", {"region": "EMEA", "report_date": "2026-10-25"}
        )
        assert "Hello EMEA" in out


def test_template_renderer_has_no_fallback_locations(tmp_path: Path) -> None:
    renderer = TemplateRenderer(template_dir=tmp_path)
    for tpl_path in (
        "daily_dispatch.html.j2",
        "templates/daily_dispatch.html.j2",
        "src/mail_cadence/templates/daily_dispatch.html.j2",
    ):
        with pytest.raises(jinja2.TemplateNotFound):
            renderer.render(tpl_path, {})


def test_template_renderer_user_dir_takes_plain_paths(tmp_path: Path) -> None:
    (tmp_path / "daily_dispatch.html.j2").write_text("Custom {{ region }}")
    renderer = TemplateRenderer(template_dir=tmp_path)
    assert renderer.render("daily_dispatch.html.j2", {"region": "PL"}) == "Custom PL"
    assert "Hello PL" in renderer.render("builtin:daily_dispatch.html.j2", {"region": "PL"})


def test_template_renderer_rejects_unknown_and_internal_builtins(tmp_path: Path) -> None:
    renderer = TemplateRenderer(template_dir=tmp_path)
    for tpl_path in (
        "builtin:missing.html.j2",
        "builtin:report_email.html.j2",
        "builtin:../mailer.py",
    ):
        with pytest.raises(jinja2.TemplateNotFound):
            renderer.render(tpl_path, {})


def test_list_builtin_templates_excludes_internal() -> None:
    names = list_builtin_templates()
    assert "builtin:daily_dispatch.html.j2" in names
    assert "builtin:settlement_summary.html.j2" in names
    assert not any("report_email" in n for n in names)


def test_build_email_message_headers() -> None:
    task = MailTask(
        id="TASK-HDR",
        max_retries=1,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Header Test",
        body_template=BodyTemplate(template_path="test.j2"),
    )
    msg = build_email_message(task, "Hello")

    assert msg["Date"] is not None
    assert len(msg["Date"]) > 0
    assert msg["Message-ID"] is not None
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg["X-Mailer"] is None


def test_extract_sender_domain_helper() -> None:
    assert _extract_sender_domain("user@example.com") == "example.com"
    assert _extract_sender_domain("Sender <user@sub.domain.org>") == "sub.domain.org"
    assert _extract_sender_domain("nodomain") == "localhost"
    assert _extract_sender_domain("") == "localhost"
    assert _extract_sender_domain(None) == "localhost"


def test_html_to_plain_text_helper() -> None:
    raw_html = (
        "<html><head><style>body { color: red; }</style><script>alert('hi');</script></head>"
        "<body><h1>Header</h1><p>Para 1</p><div>Div line<br/>Next line</div>"
        "<ul><li>Item 1</li><li>Item 2</li></ul>&amp; &lt;tag&gt;</body></html>"
    )
    plain = _html_to_plain_text(raw_html)
    assert "color: red" not in plain
    assert "alert" not in plain
    assert "Header" in plain
    assert "Para 1" in plain
    assert "& <tag>" in plain
    assert "<h1>" not in plain


def test_build_email_message_html_multipart() -> None:
    task = MailTask(
        id="TASK-HTML",
        max_retries=1,
        schedule_ref="SCHED-1",
        sender="Alice <alice@company.com>",
        recipients=Recipients(to=["bob@example.com"], cc=["carol@example.com"]),
        subject="HTML Email Test",
        body_template=BodyTemplate(format="html", template_path="test.j2"),
    )
    html_body = "<h1>Welcome &amp; Hello!</h1><p>Line 1<br/>Line 2</p>"
    msg = build_email_message(task, html_body)

    assert msg.is_multipart()
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Message-ID"].endswith("@company.com>")
    assert msg["X-Mailer"] is None

    parts = list(msg.iter_parts())
    assert len(parts) == 2

    # Part 1: text/plain fallback
    plain_part = parts[0]
    assert plain_part.get_content_type() == "text/plain"
    plain_text = plain_part.get_content()
    assert "Welcome & Hello!" in plain_text
    assert "Line 1" in plain_text
    assert "Line 2" in plain_text
    assert "<style>" not in plain_text
    assert "<h1>" not in plain_text

    # Part 2: text/html
    html_part = parts[1]
    assert html_part.get_content_type() == "text/html"
    assert html_part.get_content().rstrip("\r\n") == html_body


def test_build_email_message_text_singlepart() -> None:
    task = MailTask(
        id="TASK-TEXT",
        max_retries=1,
        schedule_ref="SCHED-1",
        sender="bob@example.com",
        recipients=Recipients(to=["alice@example.com"]),
        subject="Text Email Test",
        body_template=BodyTemplate(format="text", template_path="test.j2"),
    )
    text_body = "Hello Plain World!"
    msg = build_email_message(task, text_body)

    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().rstrip("\r\n") == text_body


def test_template_renderer_missing_template_relative(tmp_path: Path) -> None:
    renderer = TemplateRenderer(template_dir=tmp_path)
    with pytest.raises(jinja2.exceptions.TemplateNotFound):
        renderer.render("non_existent_template.j2", {"name": "World"})


def test_template_renderer_missing_template_absolute(tmp_path: Path) -> None:
    renderer = TemplateRenderer(template_dir=tmp_path)
    abs_missing_path = str(tmp_path / "non_existent_abs_template.j2")
    with pytest.raises(jinja2.exceptions.TemplateNotFound):
        renderer.render(abs_missing_path, {"name": "World"})


def test_template_renderer_directory_traversal_blocked(tmp_path: Path) -> None:
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("SUPER_SECRET_DATA")

    renderer = TemplateRenderer(template_dir=tpl_dir)

    # Relative path traversal
    with pytest.raises(ValueError, match="Access denied for template path outside base directory"):
        renderer.render("../secret.txt", {})

    # Absolute path outside base directory
    with pytest.raises(ValueError, match="Access denied for template path outside base directory"):
        renderer.render(str(secret_file), {})


def test_render_task_message_raises_for_missing_template(tmp_path: Path) -> None:
    mailer = AsyncMailer(Settings(), renderer=TemplateRenderer(template_dir=tmp_path))
    task = MailTask(
        id="TASK-MISSING-TPL",
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Test Missing Template",
        body_template=BodyTemplate(template_path="missing_template.j2"),
    )
    with pytest.raises(jinja2.TemplateNotFound, match=r"missing_template.j2"):
        mailer.render_task_message(task)


MESSAGE = b"From: s@example.com\r\nSubject: x\r\n\r\nHi\r\n"


class _CountingSMTP:
    """Accepts every command and counts connections and QUITs."""

    def __init__(self) -> None:
        self.is_connected = False
        self.connect_calls = 0
        self.quit_calls = 0
        self.data_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def mail(self, sender: str, /) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "OK")

    async def rcpt(self, recipient: str, /) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "OK")

    async def data(self, message: bytes, /) -> aiosmtplib.SMTPResponse:
        self.data_calls += 1
        return aiosmtplib.SMTPResponse(250, "Queued")

    async def rset(self) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "OK")

    async def quit(self) -> None:
        self.quit_calls += 1
        self.is_connected = False

    def close(self) -> None:
        self.is_connected = False


@pytest.mark.asyncio
async def test_async_mailer_context_manager_reuses_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CountingSMTP()
    monkeypatch.setattr("aiosmtplib.SMTP", lambda *a, **kw: client)
    mailer = AsyncMailer(Settings())

    async with mailer:
        for _ in range(5):
            results = await mailer.deliver(MESSAGE, ["u@example.com"])
            assert results["u@example.com"].outcome == Outcome.ACCEPTED

    assert client.connect_calls == 1
    assert client.data_calls == 5
    assert client.quit_calls == 1, "the pooled connection is closed on exit"
