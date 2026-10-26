"""A6: input validation for tasks, addresses, retries, priorities and window ends."""

from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    EmailPriority,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.scheduler import TaskScheduler
from mail_cadence.web import create_app


def _task(**overrides: Any) -> MailTask:
    data: dict[str, Any] = {
        "id": "T1",
        "schedule_ref": "S1",
        "sender": "sender@example.com",
        "recipients": Recipients(to=["user@example.com"]),
        "subject": "Subject",
        "body_template": BodyTemplate(template_path="t.html.j2"),
    }
    data.update(overrides)
    return MailTask(**data)


# --- priority ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("high", EmailPriority.HIGH), (" LOW ", EmailPriority.LOW), ("Normal", EmailPriority.NORMAL)],
)
def test_priority_is_normalized(raw: str, expected: EmailPriority) -> None:
    assert _task(priority=raw).priority is expected


def test_unknown_priority_is_rejected() -> None:
    with pytest.raises(ValidationError, match="priority"):
        _task(priority="urgent")


# --- max_retries ------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 3, 10])
def test_max_retries_in_range_is_accepted(value: int) -> None:
    assert _task(max_retries=value).max_retries == value


@pytest.mark.parametrize("value", [-1, 11])
def test_max_retries_out_of_range_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="max_retries"):
        _task(max_retries=value)


# --- addresses and recipients -------------------------------------------------


@pytest.mark.parametrize(
    "address", ["user@example.com", "Ann Smith <ann@example.com>", '"Doe, J" <j@localhost>']
)
def test_valid_addresses_are_accepted(address: str) -> None:
    task = _task(sender=address, recipients=Recipients(to=[address]))
    assert task.recipients.to == [address]


@pytest.mark.parametrize(
    "address", ["not-an-address", "a@example.com, b@example.com", "a b@example.com", "@example.com"]
)
def test_invalid_recipient_is_rejected(address: str) -> None:
    with pytest.raises(ValidationError, match="invalid email address"):
        Recipients(cc=[address])


def test_invalid_sender_is_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid email address"):
        _task(sender="no-at-sign")


def test_empty_sender_is_allowed_for_smtp_user_fallback() -> None:
    assert _task(sender="").sender == ""


def test_invalid_report_recipient_is_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid email address"):
        ReportSettings(recipient="admin")


def test_task_without_recipients_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no recipients"):
        _task(recipients=Recipients())


def test_bcc_only_task_is_accepted() -> None:
    assert _task(recipients=Recipients(bcc=["hidden@example.com"])).recipients.bcc


# --- window end 24:00 ---------------------------------------------------------


def _scheduler(start: str, end: str) -> TaskScheduler:
    window = HourWindow(start=start, end=end)
    return TaskScheduler(
        WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[window])])
    )


def test_window_ending_at_24_00_covers_last_minute_of_day() -> None:
    scheduler = _scheduler("00:00", "24:00")
    late = datetime(2026, 1, 5, 23, 59, 30, tzinfo=UTC)
    assert scheduler.active_window("S1", late) is not None
    assert HourWindow(start="18:00", end="24:00").end_time == time.max


def test_window_ending_at_23_59_still_has_the_gap() -> None:
    late = datetime(2026, 1, 5, 23, 59, 30, tzinfo=UTC)
    assert _scheduler("00:00", "23:59").active_window("S1", late) is None


def test_dispatch_at_end_of_day_counts_for_24_00_window() -> None:
    scheduler = _scheduler("00:00", "24:00")
    ref = datetime(2026, 1, 5, 23, 59, 50, tzinfo=UTC)
    status = scheduler.window_status(
        _task(),
        reference_dt=ref,
        last_run=datetime(2026, 1, 5, 23, 59, 40, tzinfo=UTC),
        last_status=TaskExecutionStatus.SUCCESS,
    )
    assert status.dispatched_in_window


def test_24_00_is_not_a_valid_window_start() -> None:
    with pytest.raises(ValidationError):
        HourWindow(start="24:00", end="24:00")


# --- legacy rows --------------------------------------------------------------


def test_init_db_normalizes_legacy_priority_and_retries(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "legacy.db")
    init_db(conn)
    repo = DatabaseRepository(conn)
    repo.save_work_schedules(
        WorkSchedules(
            schedules=[Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])]
        )
    )
    for task_id in ("T_UPPER", "T_UNKNOWN", "T_NEGATIVE", "T_HUGE"):
        repo.save_single_task(_task(id=task_id))
    with conn:
        conn.execute("UPDATE tasks SET priority = 'HIGH' WHERE id = 'T_UPPER'")
        conn.execute("UPDATE tasks SET priority = 'urgent' WHERE id = 'T_UNKNOWN'")
        conn.execute("UPDATE tasks SET max_retries = -2 WHERE id = 'T_NEGATIVE'")
        conn.execute("UPDATE tasks SET max_retries = 99 WHERE id = 'T_HUGE'")

    init_db(conn)
    tasks = {t.id: t for t in repo.get_mail_tasks()}
    conn.close()

    assert tasks["T_UPPER"].priority is EmailPriority.HIGH
    assert tasks["T_UNKNOWN"].priority is EmailPriority.NORMAL
    assert tasks["T_NEGATIVE"].max_retries == 0
    assert tasks["T_HUGE"].max_retries == 10


# --- web form ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "web.db"
    conn = get_connection(db_path)
    init_db(conn)
    DatabaseRepository(conn).save_work_schedules(
        WorkSchedules(
            schedules=[Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])]
        )
    )
    conn.close()
    app = create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    return TestClient(app)


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "id": "T1",
        "schedule_ref": "S1",
        "sender": "sender@example.com",
        "priority": "normal",
        "max_retries": "3",
        "subject": "Subject",
        "body_format": "html",
        "body_template_path": "t.html.j2",
        "to": "user@example.com",
        "cc": "",
        "bcc": "",
        "context_variables": "",
    }
    data.update(overrides)
    return data


def test_web_task_with_unknown_schedule_returns_400(client: TestClient) -> None:
    resp = client.post("/tasks", data=_form(schedule_ref="MISSING"))
    assert resp.status_code == 400
    assert "Schedule 'MISSING' does not exist" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("to", "", "no recipients"),
        ("to", "not-an-address", "invalid email address"),
        ("max_retries", "-1", "max_retries"),
        ("max_retries", "abc", "invalid literal"),
        ("priority", "urgent", "priority"),
    ],
)
def test_web_task_validation_returns_400(
    client: TestClient, field: str, value: str, detail: str
) -> None:
    resp = client.post("/tasks", data=_form(**{field: value}))
    assert resp.status_code == 400
    assert detail in resp.json()["detail"]


def test_web_valid_task_is_saved(client: TestClient) -> None:
    resp = client.post("/tasks", data=_form(priority="HIGH"))
    assert resp.status_code == 200
    assert "saved successfully" in resp.text
