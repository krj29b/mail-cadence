from datetime import UTC, datetime
from pathlib import Path

import pytest

from mail_cadence.mailer import TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    MailTask,
    Recipients,
    ReportSettings,
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
