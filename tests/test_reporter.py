from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mail_cadence.mailer import AsyncMailer
from mail_cadence.models import (
    ReportSettings,
    Settings,
    TaskExecutionResult,
    TaskExecutionStatus,
)
from mail_cadence.reporter import ReportGenerator, aggregate_summary_report


def test_aggregate_summary_report() -> None:
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)

    results = [
        TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=2),
        TaskExecutionResult(
            task_id="T2", status=TaskExecutionStatus.FAILED, failed_recipients=["a@b.com"]
        ),
        TaskExecutionResult(task_id="T3", status=TaskExecutionStatus.SKIPPED_SCHEDULE),
    ]

    report = aggregate_summary_report(results, t1, t2)
    assert report.total_tasks == 3
    assert report.successful_tasks == 1
    assert report.failed_tasks == 1
    assert report.skipped_tasks == 1
    assert report.total_duration_ms == 60000.0


@pytest.mark.asyncio
async def test_send_report_dry_run() -> None:
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender="mailer@example.com",
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )

    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    results = [
        TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=1),
    ]
    report = aggregate_summary_report(results, t1, t2)

    gen = ReportGenerator(template_dir=None)
    mailer = AsyncMailer(Settings(), dry_run=True)

    sent = await gen.send_report(report, report_settings, mailer)
    assert sent is True


def test_render_report_success() -> None:
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender="mailer@example.com",
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    results = [
        TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=1),
    ]
    report = aggregate_summary_report(results, t1, t2)

    gen = ReportGenerator(template_dir=None)
    html_content, txt_content = gen.render_report(report, report_settings)

    assert "MailCadence Task Completion Status Report" in html_content
    assert "MailCadence Task Completion Status Report" in txt_content


def test_render_report_fallback(caplog: pytest.LogCaptureFixture) -> None:
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender="mailer@example.com",
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    results = [
        TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=1),
    ]
    report = aggregate_summary_report(results, t1, t2)

    # Non-existent template directory forces Jinja environment lookup to fail
    gen = ReportGenerator(template_dir="non_existent_dir_12345")
    with caplog.at_level("DEBUG"):
        html_content, txt_content = gen.render_report(report, report_settings)

    assert html_content == "<h2>MailCadence Report</h2><p>Tasks total: 1, Success: 1, Failed: 0</p>"
    assert txt_content == "MailCadence Report\nTasks total: 1, Success: 1, Failed: 0"
    assert "Falling back to default report content for report_email.html.j2" in caplog.text


@pytest.mark.asyncio
async def test_send_report_headers() -> None:
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender="mailer@example.com",
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    report = aggregate_summary_report([], t1, t2)

    gen = ReportGenerator(template_dir=None)
    mock_mailer = AsyncMailer(Settings(), dry_run=True)

    sent_msg = None

    async def mock_send(
        msg: Any, sender: str, recipients: list[str]
    ) -> tuple[bool, str | None, dict[str, Any]]:
        nonlocal sent_msg
        sent_msg = msg
        return True, None, {}

    mock_mailer.send_single_message = mock_send  # type: ignore[method-assign]

    await gen.send_report(report, report_settings, mock_mailer)
    assert sent_msg is not None
    assert sent_msg["Date"] is not None
    assert len(sent_msg["Date"]) > 0
    assert sent_msg["Message-ID"] is not None
    assert len(sent_msg["Message-ID"]) > 0


@pytest.mark.asyncio
async def test_send_report_sender_falls_back_to_smtp_user() -> None:
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender=None,
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    report = aggregate_summary_report([], t1, t2)

    gen = ReportGenerator(template_dir=None)
    mock_mailer = AsyncMailer(Settings(smtp_user="auth_user@example.com"), dry_run=True)

    captured_sender: str | None = None
    captured_msg: Any = None

    async def mock_send(
        msg: Any, sender: str, recipients: list[str]
    ) -> tuple[bool, str | None, dict[str, Any]]:
        nonlocal captured_sender, captured_msg
        captured_sender = sender
        captured_msg = msg
        return True, None, {}

    mock_mailer.send_single_message = mock_send  # type: ignore[method-assign]

    await gen.send_report(report, report_settings, mock_mailer)
    assert captured_sender == "auth_user@example.com"
    assert captured_msg["From"] == "auth_user@example.com"


def test_render_report_default_template_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    report_settings = ReportSettings(
        recipient="admin@example.com",
        sender="mailer@example.com",
        subject_prefix="[TEST]",
        include_full_error_trace=True,
    )
    t1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    results = [
        TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS, sent_count=1),
    ]
    report = aggregate_summary_report(results, t1, t2)

    gen = ReportGenerator()
    html_content, txt_content = gen.render_report(report, report_settings)

    assert "MailCadence Task Completion Status Report" in html_content
    assert "MailCadence Task Completion Status Report" in txt_content
