from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from mail_cadence.mailer import (
    AUTOESCAPE_EXTENSIONS,
    AsyncMailer,
    new_email_message,
    set_html_body,
)
from mail_cadence.models import (
    BatchSummaryReport,
    ReportSettings,
    TaskExecutionResult,
    TaskExecutionStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_REPORT_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def aggregate_summary_report(
    results: list[TaskExecutionResult],
    start_time: datetime,
    end_time: datetime,
) -> BatchSummaryReport:
    total_duration = (end_time - start_time).total_seconds() * 1000.0
    successful = sum(1 for r in results if r.status == TaskExecutionStatus.SUCCESS)
    partial = sum(1 for r in results if r.status == TaskExecutionStatus.PARTIAL)
    failed = sum(1 for r in results if r.status == TaskExecutionStatus.FAILED)
    skipped = sum(1 for r in results if r.status == TaskExecutionStatus.SKIPPED_SCHEDULE)

    return BatchSummaryReport(
        total_tasks=len(results),
        successful_tasks=successful,
        partial_tasks=partial,
        failed_tasks=failed,
        skipped_tasks=skipped,
        total_duration_ms=total_duration,
        start_time=start_time,
        end_time=end_time,
        results=results,
    )


class ReportGenerator:
    def __init__(self, template_dir: str | Path | None = None) -> None:
        if template_dir is None:
            self.template_dir = DEFAULT_REPORT_TEMPLATE_DIR
        else:
            self.template_dir = Path(template_dir)

        loader = FileSystemLoader(str(self.template_dir)) if self.template_dir.exists() else None
        self.env = Environment(
            loader=loader,
            autoescape=select_autoescape(
                enabled_extensions=AUTOESCAPE_EXTENSIONS, default_for_string=True
            ),
        )

    def _render_template(
        self, template_name: str, context: dict[str, Any], fallback_content: str
    ) -> str:
        try:
            tpl = self.env.get_template(template_name)
            return tpl.render(**context)
        except (TemplateError, TypeError, ValueError) as exc:
            logger.debug(f"Falling back to default report content for {template_name}: {exc}")
            return fallback_content

    def render_report(
        self, report: BatchSummaryReport, report_settings: ReportSettings
    ) -> tuple[str, str]:
        html_template_name = "report_email.html.j2"
        txt_template_name = "report_email.txt.j2"

        context = {
            "report": report,
            "report_settings": report_settings,
            "include_error_trace": report_settings.include_full_error_trace,
        }

        html_fallback = (
            f"<h2>MailCadence Report</h2><p>Tasks total: {report.total_tasks}, "
            f"Success: {report.successful_tasks}, Failed: {report.failed_tasks}</p>"
        )
        txt_fallback = (
            f"MailCadence Report\nTasks total: {report.total_tasks}, "
            f"Success: {report.successful_tasks}, Failed: {report.failed_tasks}"
        )

        html_content = self._render_template(html_template_name, context, html_fallback)
        txt_content = self._render_template(txt_template_name, context, txt_fallback)

        return html_content, txt_content

    async def send_report(
        self,
        report: BatchSummaryReport,
        report_settings: ReportSettings,
        mailer: AsyncMailer,
    ) -> bool:
        html_body, txt_body = self.render_report(report, report_settings)
        sender_addr = (
            report_settings.sender or mailer.settings.smtp_user or "mail-cadence-daemon@localhost"
        )

        msg = new_email_message(
            subject=f"{report_settings.subject_prefix} Task Execution Summary",
            sender=sender_addr,
            to=[report_settings.recipient],
        )
        set_html_body(msg, html_body, plain_body=txt_body)

        success, err, _ = await mailer.send_single_message(
            msg=msg,
            sender=sender_addr,
            recipients=[report_settings.recipient],
        )

        if not success:
            logger.error(f"Failed to dispatch status report email: {err}")
            return False

        logger.info(f"Successfully dispatched status report email to {report_settings.recipient}")
        return True
