from datetime import time

import pytest
from pydantic import ValidationError

from mail_cadence.models import (
    END_OF_DAY,
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    WorkSchedules,
    parse_time_str,
    parse_window_end,
)


def test_get_context_dict_empty() -> None:
    task = MailTask(
        id="TASK-EMPTY",
        schedule_ref="SCHED-01",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="template.html"),
        context_variables=[],
    )
    assert task.get_context_dict() == {}


def test_get_context_dict_single_and_multiple_vars() -> None:
    task = MailTask(
        id="TASK-VARS",
        schedule_ref="SCHED-01",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="template.html"),
        context_variables=[
            ContextVar(name="first_name", value="John"),
            ContextVar(name="last_name", value="Doe"),
            ContextVar(name="company", value="Acme Corp"),
        ],
    )
    expected = {
        "first_name": "John",
        "last_name": "Doe",
        "company": "Acme Corp",
    }
    assert task.get_context_dict() == expected


def test_get_context_dict_duplicate_keys() -> None:
    task = MailTask(
        id="TASK-DUP",
        schedule_ref="SCHED-01",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="template.html"),
        context_variables=[
            ContextVar(name="role", value="developer"),
            ContextVar(name="role", value="maintainer"),
        ],
    )
    assert task.get_context_dict() == {"role": "maintainer"}


def test_get_context_dict_special_chars_and_empty_values() -> None:
    task = MailTask(
        id="TASK-SPECIAL",
        schedule_ref="SCHED-01",
        sender="sender@example.com",
        recipients=Recipients(to=["user@example.com"]),
        subject="Test Subject",
        body_template=BodyTemplate(template_path="template.html"),
        context_variables=[
            ContextVar(name="empty_val", value=""),
            ContextVar(name="var_with_spaces", value="hello world"),
            ContextVar(name="special_chars!@#$", value="123 & 456 <tag>"),
        ],
    )
    assert task.get_context_dict() == {
        "empty_val": "",
        "var_with_spaces": "hello world",
        "special_chars!@#$": "123 & 456 <tag>",
    }


def test_recipients_all_recipients() -> None:
    recipients = Recipients(
        to=["to1@example.com", "to2@example.com"],
        cc=["cc1@example.com"],
        bcc=["bcc1@example.com"],
    )
    assert recipients.all_recipients == [
        "to1@example.com",
        "to2@example.com",
        "cc1@example.com",
        "bcc1@example.com",
    ]


def test_parse_time_str() -> None:
    assert parse_time_str("09:30") == time(9, 30, 0)
    assert parse_time_str("18:45:30") == time(18, 45, 30)

    with pytest.raises(ValueError):
        parse_time_str("12")

    with pytest.raises(ValueError):
        parse_time_str("25:00")

    with pytest.raises(ValueError):
        parse_time_str("invalid")


def test_parse_window_end() -> None:
    assert parse_window_end(END_OF_DAY) == time.max
    assert parse_window_end(f"{END_OF_DAY}:00") == time.max
    assert parse_window_end(" 24:00 ") == time.max
    assert parse_window_end(" 24:00:00 ") == time.max

    assert parse_window_end("17:00") == time(17, 0, 0)
    assert parse_window_end("08:30:15") == time(8, 30, 15)

    with pytest.raises(ValueError):
        parse_window_end("24:01")

    with pytest.raises(ValueError):
        parse_window_end("invalid")

    with pytest.raises(ValueError):
        parse_window_end(123)  # type: ignore[arg-type]


def test_hour_window_start_and_end_time() -> None:
    hw = HourWindow(start="08:00:00", end="17:00:00")
    assert hw.start_time == time(8, 0, 0)
    assert hw.end_time == time(17, 0, 0)


def test_hour_window_validation() -> None:
    with pytest.raises(ValidationError):
        HourWindow(start="25:00", end="17:00")

    with pytest.raises(ValidationError):
        HourWindow(start="08:00", end="17:00", max_concurrency=0)

    with pytest.raises(ValidationError):
        HourWindow(start="08:00", end="17:00", rate_limit_per_min=-1)


def test_work_schedules_timezone_validation() -> None:
    ws = WorkSchedules(timezone="UTC")
    assert ws.timezone == "UTC"

    with pytest.raises(ValidationError):
        WorkSchedules(timezone="Mars/Phobos")


def test_smtp_header_injection_validation() -> None:
    with pytest.raises(ValidationError):
        MailTask(
            id="T1",
            schedule_ref="S1",
            sender="sender\r\nInjected-Header: bad",
            recipients=Recipients(to=["user@example.com"]),
            subject="Valid Subject",
            body_template=BodyTemplate(template_path="tpl.html"),
        )

    with pytest.raises(ValidationError):
        MailTask(
            id="T2",
            schedule_ref="S1",
            sender="sender@example.com",
            recipients=Recipients(to=["user@example.com"]),
            subject="Subject\nInjected-Header: bad",
            body_template=BodyTemplate(template_path="tpl.html"),
        )

    with pytest.raises(ValidationError):
        Recipients(to=["user@example.com\r\nBcc: evil@example.com"])

    with pytest.raises(ValidationError):
        ReportSettings(recipient="admin@example.com\nSubject: Hacked")


def test_hour_window_days_of_week_validation() -> None:
    hw1 = HourWindow(start="08:00", end="17:00", days_of_week="mon,wed,fri")
    assert hw1.days_of_week == "mon,wed,fri"

    hw2 = HourWindow(start="08:00", end="17:00", days_of_week="*")
    assert hw2.days_of_week == "*"

    hw_norm = HourWindow(start="08:00", end="17:00", days_of_week=" MON , wed ")
    assert hw_norm.days_of_week == "mon,wed"

    with pytest.raises(ValidationError):
        HourWindow(start="08:00", end="17:00", days_of_week="monday,invalid")

    with pytest.raises(ValidationError):
        HourWindow(start="08:00", end="17:00", days_of_week="")
