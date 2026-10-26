from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import aiosmtplib
import pytest

from mail_cadence.mailer import AsyncMailer, TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    MailTask,
    Recipients,
    ReportSettings,
    Settings,
    TaskExecutionStatus,
)
from mail_cadence.reporter import ReportGenerator, aggregate_summary_report
from mail_cadence.xml_loader import XMLValidationError, load_mail_configuration, load_work_schedules


def test_mail_task_get_context_dict() -> None:
    # Test empty context variables
    task_empty = MailTask(
        id="T_EMPTY",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subj",
        body_template=BodyTemplate(template_path="t.j2"),
        context_variables=[],
    )
    assert task_empty.get_context_dict() == {}

    # Test context variables mapping name -> value
    task_with_vars = MailTask(
        id="T_VARS",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subj",
        body_template=BodyTemplate(template_path="t.j2"),
        context_variables=[
            ContextVar(name="user_name", value="Alice"),
            ContextVar(name="account_id", value="12345"),
        ],
    )
    assert task_with_vars.get_context_dict() == {
        "user_name": "Alice",
        "account_id": "12345",
    }

    # Test duplicate keys (last key overwrites previous value)
    task_duplicates = MailTask(
        id="T_DUP",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subj",
        body_template=BodyTemplate(template_path="t.j2"),
        context_variables=[
            ContextVar(name="key", value="initial"),
            ContextVar(name="key", value="overwritten"),
        ],
    )
    assert task_duplicates.get_context_dict() == {"key": "overwritten"}


def test_template_renderer_absolute_path(tmp_path: Path) -> None:
    tpl_file = tmp_path / "abs_test.j2"
    tpl_file.write_text("Abs {{ var }}")

    renderer = TemplateRenderer(template_dir=tmp_path)
    res = renderer.render(str(tpl_file), {"var": "Val"})
    assert res == "Abs Val"


@pytest.mark.asyncio
async def test_mailer_no_recipients() -> None:
    settings = Settings()
    mailer = AsyncMailer(settings, dry_run=True)
    # Validation rejects such a task; model_construct bypasses it to test the mailer guard.
    task = MailTask.model_construct(
        id="T_NO_RECIP",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=[]),
        subject="Subj",
        body_template=BodyTemplate(template_path="t.j2"),
        context_variables=[],
    )
    res = await mailer.dispatch_task(task)
    assert res.status == TaskExecutionStatus.FAILED
    assert res.sent_count == 0
    assert "No recipients defined" in (res.error_details or "")


@pytest.mark.asyncio
async def test_mailer_template_render_error() -> None:
    settings = Settings()
    renderer = TemplateRenderer(template_dir="non_existent_dir_12345")
    mailer = AsyncMailer(settings, renderer=renderer, dry_run=True)
    task = MailTask(
        id="T_BAD_TPL",
        schedule_ref="S1",
        sender="s@example.com",
        recipients=Recipients(to=["r@example.com"]),
        subject="Subj",
        body_template=BodyTemplate(template_path="missing_template.j2"),
    )
    res = await mailer.dispatch_task(task)
    assert res.status == TaskExecutionStatus.FAILED
    assert "Template rendering error" in (res.error_details or "")


@pytest.mark.asyncio
async def test_mailer_smtp_exception_and_generic_exception() -> None:
    settings = Settings()
    mock_smtp = AsyncMock()
    mock_smtp.is_connected = False
    mock_smtp.connect = AsyncMock(side_effect=aiosmtplib.SMTPException("SMTP Exception Test"))

    mailer = AsyncMailer(settings, dry_run=False, smtp_client=mock_smtp)
    msg = AsyncMock()
    success, err, _ = await mailer.send_single_message(msg, "s@example.com", ["r@example.com"])
    assert success is False
    assert "SMTP Exception Test" in (err or "")

    # Connection error
    mock_smtp.connect = AsyncMock(side_effect=ConnectionRefusedError("Conn error"))
    success, err, _ = await mailer.send_single_message(msg, "s@example.com", ["r@example.com"])
    assert success is False
    assert "Connection error" in (err or "")


def test_xml_loader_invalid_schemas(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.xml"
    bad_file.write_bytes(b"<Root><Child/></Root>")

    with pytest.raises(XMLValidationError, match="Invalid MailConfiguration XML schema"):
        load_mail_configuration(bad_file)

    with pytest.raises(XMLValidationError, match="Invalid WorkSchedules XML schema"):
        load_work_schedules(bad_file)


def test_reporter_fallback(tmp_path: Path) -> None:
    gen = ReportGenerator(template_dir=tmp_path / "empty_dir")
    report = aggregate_summary_report([], datetime.now(UTC), datetime.now(UTC))
    report_settings = ReportSettings(recipient="r@example.com")
    html_c, txt_c = gen.render_report(report, report_settings)
    assert "MailCadence Report" in html_c
    assert "MailCadence Report" in txt_c


@pytest.mark.asyncio
async def test_send_report_failure() -> None:
    gen = ReportGenerator()
    report = aggregate_summary_report([], datetime.now(UTC), datetime.now(UTC))
    report_settings = ReportSettings(recipient="r@example.com")

    mock_smtp = AsyncMock()
    mock_smtp.is_connected = False
    mock_smtp.connect = AsyncMock(side_effect=aiosmtplib.SMTPException("Send report error"))

    mailer = AsyncMailer(Settings(), dry_run=False, smtp_client=mock_smtp)
    sent = await gen.send_report(report, report_settings, mailer)
    assert sent is False
