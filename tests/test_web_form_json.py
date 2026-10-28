"""JSON form fields of the panel (audit A4): the whole structure is validated before
anything is saved, so a malformed field is a 400 and leaves the stored data unchanged."""

from contextlib import closing
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
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app

WINDOW = HourWindow(start="06:00", end="18:00", max_concurrency=2, rate_limit_per_min=30)
CONTEXT = [ContextVar(name="region", value="PL")]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "web.db"
    with closing(get_connection(path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        repo.save_work_schedules(
            WorkSchedules(
                timezone="UTC",
                schedules=[Schedule(id="S1", description="Office", allowed_hours=[WINDOW])],
            )
        )
        repo.save_single_task(
            MailTask(
                id="T1",
                schedule_ref="S1",
                sender="cadence@example.com",
                subject="Hello",
                recipients=Recipients(to=["a@example.com"]),
                body_template=BodyTemplate(template_path="builtin:x.html.j2"),
                context_variables=CONTEXT,
            )
        )
    return path


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    )


def _schedule(db_path: Path) -> Schedule:
    with closing(get_connection(db_path)) as conn:
        schedules = DatabaseRepository(conn).get_work_schedules().schedules
    return next(s for s in schedules if s.id == "S1")


def _task(db_path: Path) -> MailTask:
    with closing(get_connection(db_path)) as conn:
        task = DatabaseRepository(conn).get_mail_task("T1")
    assert task is not None
    return task


def _task_form(context_variables: str) -> dict[str, str]:
    return {
        "id": "T1",
        "schedule_ref": "S1",
        "sender": "cadence@example.com",
        "priority": "normal",
        "max_retries": "3",
        "subject": "Changed subject",
        "body_format": "html",
        "body_template_path": "builtin:x.html.j2",
        "to": "a@example.com",
        "context_variables": context_variables,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "[{}]",
        "[null]",
        '{"start": "08:00"}',
        '"08:00"',
        "[1]",
        '[{"start": "08:00"}]',
        '[{"start": 8, "end": 17}]',
        '[{"start": "08:00", "end": "17:00", "max_concurrency": "2"}]',
        '[{"start": "08:00", "end": "17:00", "max_concurrency": true}]',
        '[{"start": "08:00", "end": "17:00", "rate_limit_per_min": 1.5}]',
        '[{"start": "08:00", "end": "17:00", "days": "mon"}]',
        '[{"start": "08:00", "end": "17:00", "days_of_week": null}]',
        "[{'start': '08:00'}]",
        "not json",
    ],
)
def test_malformed_hour_windows_are_400_and_keep_the_schedule(
    client: TestClient, db_path: Path, raw: str
) -> None:
    before = _schedule(db_path)
    resp = client.post(
        "/schedules", data={"id": "S1", "description": "Changed", "allowed_hours_json": raw}
    )
    assert resp.status_code == 400, resp.text
    assert "Invalid allowed_hours_json" in resp.json()["detail"]
    assert _schedule(db_path) == before


def test_hour_windows_from_the_editor_are_saved(client: TestClient, db_path: Path) -> None:
    raw = (
        '[{"start": "08:00", "end": "24:00", "days_of_week": "mon,tue",'
        ' "max_concurrency": 3, "rate_limit_per_min": 10}, {"start": "01:00", "end": "02:00"}]'
    )
    resp = client.post("/schedules", data={"id": "S1", "allowed_hours_json": raw})
    assert resp.status_code == 200
    assert _schedule(db_path).allowed_hours == [
        HourWindow(
            start="08:00",
            end="24:00",
            days_of_week="mon,tue",
            max_concurrency=3,
            rate_limit_per_min=10,
        ),
        HourWindow(start="01:00", end="02:00"),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "[{}]",
        "[null]",
        '[{"name": "a"}]',
        '[{"value": "x"}]',
        '[{"name": "", "value": "x"}]',
        '[{"name": "a", "value": null}]',
        '[{"name": "a", "value": {"x": 1}}]',
        '[{"name": "a", "value": "x", "extra": 1}]',
        '[{"name": 1, "value": "x"}]',
        '{"a": [1]}',
        '{"a": null}',
        '"text"',
        "5",
        "{bad",
    ],
)
def test_malformed_context_variables_are_400_and_keep_the_task(
    client: TestClient, db_path: Path, raw: str
) -> None:
    before = _task(db_path)
    resp = client.post("/tasks", data=_task_form(raw))
    assert resp.status_code == 400, resp.text
    assert "Invalid context_variables" in resp.json()["detail"]
    assert _task(db_path) == before


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('[{"name": "region", "value": "DE"}]', [ContextVar(name="region", value="DE")]),
        (
            '{"region": "DE", "year": 2026}',
            [
                ContextVar(name="region", value="DE"),
                ContextVar(name="year", value="2026"),
            ],
        ),
        ("", []),
    ],
)
def test_valid_context_variables_are_saved(
    client: TestClient, db_path: Path, raw: str, expected: list[ContextVar]
) -> None:
    assert client.post("/tasks", data=_task_form(raw)).status_code == 200
    assert _task(db_path).context_variables == expected
