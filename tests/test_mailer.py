from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
from mail_cadence.models import BodyTemplate, MailTask, Recipients, Settings, TaskExecutionStatus


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


@pytest.mark.asyncio
async def test_async_mailer_dispatch_missing_template(tmp_path: Path) -> None:
    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)
    mailer = AsyncMailer(settings, renderer=renderer, dry_run=True)

    task = MailTask(
        id="TASK-MISSING-TPL",
        max_retries=1,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Test Missing Template",
        body_template=BodyTemplate(template_path="missing_template.j2"),
    )

    result = await mailer.dispatch_task(task)
    assert result.status == TaskExecutionStatus.FAILED
    assert result.sent_count == 0
    assert "Template rendering error" in (result.error_details or "")
    assert "missing_template.j2" in (result.error_details or "")


@pytest.mark.asyncio
async def test_async_mailer_dry_run(tmp_path: Path) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)
    mailer = AsyncMailer(settings, renderer=renderer, dry_run=True)

    task = MailTask(
        id="TASK-01",
        max_retries=2,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)
    assert result.status == TaskExecutionStatus.SUCCESS
    assert result.sent_count == 1
    assert result.attempt_count == 1
    assert result.error_details is None


@pytest.mark.asyncio
async def test_async_mailer_retry_and_failure(tmp_path: Path) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True
    mock_smtp.connect = AsyncMock()
    smtp_exc = aiosmtplib.SMTPResponseException(451, "Temporary failure")
    mock_smtp.send_message = AsyncMock(side_effect=smtp_exc)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-02",
        max_retries=2,  # total 3 attempts
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)
    assert result.status == TaskExecutionStatus.FAILED
    assert result.sent_count == 0
    assert result.attempt_count == 3
    assert "SMTP error 451" in (result.error_details or "")


@pytest.mark.asyncio
async def test_dispatch_task_failure_with_rate_limiter_and_network_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = False
    mock_smtp.connect = AsyncMock(side_effect=ConnectionRefusedError("Connection refused by host"))

    mock_rate_limiter = AsyncMock()
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-NET-ERR",
        max_retries=2,  # 3 attempts total
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com", "cc@example.com"]),
        subject="Test Network Error",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task, rate_limiter=mock_rate_limiter)

    assert result.status == TaskExecutionStatus.FAILED
    assert result.sent_count == 0
    assert result.failed_recipients == ["to@example.com", "cc@example.com"]
    assert result.attempt_count == 3
    assert result.error_details is not None
    assert "Network/Connection error: Connection refused by host" in result.error_details

    # Verify rate limiter was acquired on each attempt
    assert mock_rate_limiter.acquire.call_count == 3

    # Verify exponential backoff sleep was called for attempts 1 and 2 (0.5s and 1.0s)
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(0.5)
    mock_sleep.assert_any_call(1.0)


@pytest.mark.asyncio
async def test_dispatch_task_retry_success_with_rate_limiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True
    mock_smtp.send_message = AsyncMock(
        side_effect=[
            aiosmtplib.SMTPException("Temporary network disconnect"),
            None,  # Success on second attempt
        ]
    )

    mock_rate_limiter = AsyncMock()
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-RETRY-SUCCESS",
        max_retries=2,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Test Retry Success",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task, rate_limiter=mock_rate_limiter)

    assert result.status == TaskExecutionStatus.SUCCESS
    assert result.sent_count == 1
    assert result.failed_recipients == []
    assert result.attempt_count == 2
    assert result.error_details is None

    assert mock_rate_limiter.acquire.call_count == 2
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_once_with(0.5)


@pytest.mark.asyncio
async def test_async_mailer_partial_delivery(tmp_path: Path) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True

    errors_dict = {"user2@example.com": (450, "Mailbox busy")}
    mock_smtp.send_message = AsyncMock(return_value=(errors_dict, "250 OK"))

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-PARTIAL",
        max_retries=1,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["user1@example.com", "user2@example.com"]),
        subject="Test Partial",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)

    assert result.status == TaskExecutionStatus.PARTIAL
    assert result.sent_count == 1
    assert result.failed_recipients == ["user2@example.com"]
    assert result.attempt_count == 2
    assert "user2@example.com" in (result.error_details or "")

    assert mock_smtp.send_message.call_count == 2
    second_call_recipients = mock_smtp.send_message.call_args_list[1].kwargs["recipients"]
    assert second_call_recipients == ["user2@example.com"]


@pytest.mark.asyncio
async def test_partial_delivery_permanent_5xx_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True

    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    first_attempt_errors = {
        "temp_fail_450@example.com": (450, "Temporary failure"),
        "perm_fail_550@example.com": (550, "User unknown"),
    }
    second_attempt_errors: dict[str, Any] = {}

    mock_smtp.send_message = AsyncMock(
        side_effect=[
            (first_attempt_errors, "250 OK"),
            (second_attempt_errors, "250 OK"),
        ]
    )

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-PARTIAL-550-NO-RETRY",
        max_retries=2,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(
            to=["ok@example.com", "temp_fail_450@example.com", "perm_fail_550@example.com"]
        ),
        subject="Test Partial Permanent 5xx",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)

    assert result.status == TaskExecutionStatus.PARTIAL
    assert result.sent_count == 2
    assert result.failed_recipients == ["perm_fail_550@example.com"]
    assert result.attempt_count == 2

    assert mock_smtp.send_message.call_count == 2
    second_call_recipients = mock_smtp.send_message.call_args_list[1].kwargs["recipients"]
    assert second_call_recipients == ["temp_fail_450@example.com"]


@pytest.mark.asyncio
async def test_permanent_5xx_smtp_error_no_retry(tmp_path: Path) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True
    smtp_exc = aiosmtplib.SMTPResponseException(550, "User unknown")
    mock_smtp.send_message = AsyncMock(side_effect=smtp_exc)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-PERM-550",
        max_retries=3,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["unknown@example.com"]),
        subject="Test Permanent Error",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)
    assert result.status == TaskExecutionStatus.FAILED
    assert result.sent_count == 0
    assert result.attempt_count == 1
    assert "SMTP error 550" in (result.error_details or "")


@pytest.mark.asyncio
async def test_transient_4xx_smtp_error_retries_fully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = True
    smtp_exc = aiosmtplib.SMTPResponseException(421, "Service not available")
    mock_smtp.send_message = AsyncMock(side_effect=smtp_exc)

    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False, smtp_client=mock_smtp)

    task = MailTask(
        id="TASK-TRANSIENT-421",
        max_retries=3,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Transient Error",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    result = await mailer.dispatch_task(task)
    assert result.status == TaskExecutionStatus.FAILED
    assert result.sent_count == 0
    assert result.attempt_count == 4
    assert "SMTP error 421" in (result.error_details or "")


@pytest.mark.asyncio
async def test_async_mailer_context_manager_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    # Mock aiosmtplib.SMTP class
    class FakeSMTP:
        def __init__(self, *args: list[Any], **kwargs: dict[str, Any]) -> None:
            self.is_connected = False
            self.connect_calls = 0
            self.quit_calls = 0
            self.send_calls = 0

        async def connect(self) -> None:
            pass

        async def login(self, u: str, p: str) -> None:
            pass

        async def send_message(
            self, msg: Any, sender: str, recipients: list[str]
        ) -> tuple[dict[str, Any], str]:
            self.send_calls += 1
            return ({}, "OK")

        async def quit(self) -> None:
            self.quit_calls += 1
            self.is_connected = False

    fake_client = FakeSMTP()

    async def mock_connect() -> None:
        fake_client.connect_calls += 1
        fake_client.is_connected = True

    fake_client.connect = mock_connect  # type: ignore

    monkeypatch.setattr("aiosmtplib.SMTP", lambda *a, **kw: fake_client)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False)

    task = MailTask(
        id="TASK-CTX",
        max_retries=0,
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test CTX",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    async with mailer:
        for _ in range(5):
            res = await mailer.dispatch_task(task)
            assert res.status == TaskExecutionStatus.SUCCESS

    # Connect should have been called only once, quit only once at exit
    assert fake_client.connect_calls == 1
    assert fake_client.send_calls == 5
    assert fake_client.quit_calls == 1


@pytest.mark.asyncio
async def test_async_mailer_context_manager_reconnect_on_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl_file = tmp_path / "test.j2"
    tpl_file.write_text("Hello {{ name }}!")

    settings = Settings()
    renderer = TemplateRenderer(template_dir=tmp_path)

    clients = []

    class FakeSMTP:
        def __init__(self, *args: list[Any], **kwargs: dict[str, Any]) -> None:
            self.is_connected = False
            self.connect_calls = 0
            self.quit_calls = 0
            self.send_calls = 0
            clients.append(self)

        async def connect(self) -> None:
            self.connect_calls += 1
            self.is_connected = True

        async def login(self, u: str, p: str) -> None:
            pass

        async def send_message(
            self, msg: Any, sender: str, recipients: list[str]
        ) -> tuple[dict[str, Any], str]:
            self.send_calls += 1
            if self.send_calls == 1 and len(clients) == 1:
                # Disconnect on first send attempt on client 1
                self.is_connected = False
                raise aiosmtplib.SMTPServerDisconnected("Disconnected")
            return ({}, "OK")

        async def quit(self) -> None:
            self.quit_calls += 1
            self.is_connected = False

    monkeypatch.setattr("aiosmtplib.SMTP", FakeSMTP)

    mailer = AsyncMailer(settings, renderer=renderer, dry_run=False)

    task = MailTask(
        id="TASK-DISCONNECT",
        max_retries=1,  # Allows retry
        schedule_ref="SCHED-1",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Disconnect",
        body_template=BodyTemplate(template_path="test.j2"),
    )

    async with mailer:
        res = await mailer.dispatch_task(task)
        assert res.status == TaskExecutionStatus.SUCCESS

    # Two clients created: first failed due to disconnect, second succeeded on retry
    assert len(clients) == 2
    assert clients[0].connect_calls == 1
    assert clients[1].connect_calls == 1
    assert clients[1].send_calls == 1
