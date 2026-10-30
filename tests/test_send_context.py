"""The send context (templates: ``cadence``): built at claim, stored with the execution,
used by every render of it, the dry run, manual sends and the panel preview."""

import email
import email.policy
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from mail_cadence.database import get_connection, init_db, schema_version
from mail_cadence.execution_store import (
    ClaimRequest,
    ClaimStatus,
    send_context_from_json,
    send_context_to_json,
)
from mail_cadence.mailer import AsyncMailer, TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    SendContext,
    Settings,
    WorkSchedules,
)
from mail_cadence.pipeline import (
    DispatchOptions,
    dispatch_single_task,
    manual_send_context,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.web import create_app
from smtp_sink import SMTPSink

WARSAW = ZoneInfo("Europe/Warsaw")
TEMPLATE = (
    "<p>D={{ cadence.date }} K={{ cadence.kind }} T={{ cadence.task_id }} "
    "Z={{ cadence.timezone }} "
    "S={{ cadence.window_start.strftime('%Y-%m-%d %H:%M') if cadence.window_start else '-' }}"
    "</p>"
)


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _task(*, variables: list[ContextVar] | None = None) -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="cadence@example.com",
        subject="Report",
        recipients=Recipients(to=["a@example.com"]),
        body_template=BodyTemplate(template_path="body.html.j2"),
        context_variables=variables or [],
    )


def _prepare(tmp_path: Path, window: HourWindow, task: MailTask | None = None) -> Path:
    (tmp_path / "body.html.j2").write_text(TEMPLATE, encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        repo.save_work_schedules(
            WorkSchedules(
                timezone="Europe/Warsaw", schedules=[Schedule(id="S1", allowed_hours=[window])]
            )
        )
        repo.save_single_task(task or _task())
    return db_path


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


async def _run(db_path: Path, port: int, ref_dt: datetime, *, dry_run: bool = False) -> int:
    options = DispatchOptions(
        db_path=db_path, template_dir=db_path.parent, dry_run=dry_run, ref_dt=ref_dt
    )
    return await run_dispatch(options, _settings(port))


def _text(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    part = msg.get_body(preferencelist=("plain",))
    assert part is not None
    return str(part.get_content()).strip()


def _stored_contexts(db_path: Path) -> list[str | None]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [row[0] for row in conn.execute("SELECT send_context_json FROM task_executions")]


# --- SendContext -----------------------------------------------------------------------


def test_build_uses_the_local_day_the_window_started() -> None:
    start = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)  # 22:00 in Warsaw
    end = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    at = datetime(2026, 10, 29, 0, 30, tzinfo=UTC)

    context = SendContext.build("T1", "window", "Europe/Warsaw", (start, end), at)

    assert context.date == date(2026, 10, 28)
    assert context.window_start == datetime(2026, 10, 28, 22, 0, tzinfo=WARSAW)
    assert context.window_start.tzinfo == WARSAW
    assert context.window_end == datetime(2026, 10, 29, 2, 0, tzinfo=WARSAW)


def test_build_without_a_window_uses_the_local_day_of_the_claim() -> None:
    at = datetime(2026, 10, 28, 23, 30, tzinfo=UTC)  # already 29.10 in Warsaw

    context = SendContext.build("T1", "manual", "Europe/Warsaw", None, at)

    assert (context.window_start, context.window_end) == (None, None)
    assert context.date == date(2026, 10, 29)


@pytest.mark.parametrize("with_window", [True, False], ids=["window", "no-window"])
def test_json_round_trip_keeps_every_value(with_window: bool) -> None:
    window = (
        (datetime(2026, 10, 28, 5, 0, tzinfo=UTC), datetime(2026, 10, 28, 7, 0, tzinfo=UTC))
        if with_window
        else None
    )
    context = SendContext.build("T1", "window", "Europe/Warsaw", window, datetime.now(UTC))

    restored = send_context_from_json(send_context_to_json(context))

    assert restored == context
    if with_window:
        assert restored.window_start is not None
        assert restored.window_start.tzinfo == WARSAW


def test_unknown_kind_in_stored_json_is_rejected() -> None:
    context = SendContext.build("T1", "manual", "UTC", None, datetime.now(UTC))
    raw = send_context_to_json(context).replace('"manual"', '"other"')

    with pytest.raises(ValueError, match="Unknown send kind"):
        send_context_from_json(raw)


# --- Templates -------------------------------------------------------------------------


def test_template_sees_the_context_as_cadence(tmp_path: Path) -> None:
    (tmp_path / "body.html.j2").write_text(TEMPLATE, encoding="utf-8")
    start = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)
    context = SendContext.build("T1", "window", "Europe/Warsaw", (start, start), datetime.now(UTC))

    body = TemplateRenderer(tmp_path).render_task_body(_task(), context)

    assert "D=2026-10-28 K=window T=T1 Z=Europe/Warsaw S=2026-10-28 06:00" in body


def test_task_variable_named_cadence_shadows_the_context(tmp_path: Path) -> None:
    (tmp_path / "body.html.j2").write_text("<p>{{ cadence }}</p>", encoding="utf-8")
    task = _task(variables=[ContextVar(name="cadence", value="own value")])
    context = SendContext.build("T1", "manual", "UTC", None, datetime.now(UTC))

    assert TemplateRenderer(tmp_path).render_task_body(task, context) == "<p>own value</p>"


def test_render_without_context_describes_the_current_moment(tmp_path: Path) -> None:
    (tmp_path / "body.html.j2").write_text(TEMPLATE, encoding="utf-8")
    before = datetime.now(UTC).date()

    body = TemplateRenderer(tmp_path).render_task_body(_task())

    assert "K=manual T=T1 Z=UTC S=-" in body
    assert any(f"D={day}" in body for day in {before, datetime.now(UTC).date()})


# --- Dispatch --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "ref_dt", "expected"),
    [
        (
            HourWindow(start="06:00", end="08:00"),
            datetime(2026, 10, 28, 7, 0, tzinfo=WARSAW),
            "D=2026-10-28 K=window T=T1 Z=Europe/Warsaw S=2026-10-28 06:00",
        ),
        (
            HourWindow(start="22:00", end="02:00"),
            datetime(2026, 10, 29, 1, 0, tzinfo=WARSAW),
            "D=2026-10-28 K=window T=T1 Z=Europe/Warsaw S=2026-10-28 22:00",
        ),
    ],
    ids=["daytime", "across-midnight"],
)
async def test_scheduled_send_renders_and_stores_the_occurrence(
    tmp_path: Path, sink: SMTPSink, window: HourWindow, ref_dt: datetime, expected: str
) -> None:
    db_path = _prepare(tmp_path, window)

    await _run(db_path, sink.port, ref_dt)

    assert [_text(d.raw) for d in sink.deliveries] == [expected]
    (stored,) = _stored_contexts(db_path)
    assert stored is not None
    assert send_context_from_json(stored).kind == "window"


async def test_dry_run_renders_the_context_a_real_run_stores(
    tmp_path: Path, sink: SMTPSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _prepare(tmp_path, HourWindow(start="06:00", end="08:00"))
    ref_dt = datetime(2026, 10, 28, 7, 0, tzinfo=WARSAW)
    seen: list[SendContext | None] = []
    original = AsyncMailer.render_task_message

    def recording(self: AsyncMailer, task: MailTask, context: SendContext | None = None) -> object:
        seen.append(context)
        return original(self, task, context)

    monkeypatch.setattr(AsyncMailer, "render_task_message", recording)

    await _run(db_path, sink.port, ref_dt, dry_run=True)
    assert _stored_contexts(db_path) == [], "a dry run writes no execution"
    await _run(db_path, sink.port, ref_dt)

    (stored,) = _stored_contexts(db_path)
    assert stored is not None
    assert seen == [send_context_from_json(stored)] * 2


def test_takeover_keeps_the_context_of_the_first_claim(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, HourWindow(start="06:00", end="08:00"))
    first = SendContext.build("T1", "window", "UTC", None, datetime(2026, 10, 1, tzinfo=UTC))
    later = SendContext.build("T1", "window", "UTC", None, datetime(2026, 10, 9, tzinfo=UTC))

    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        repo.start_run("run-1", "cli")
        repo.start_run("run-2", "cli")
        claimed = repo.claim_execution(ClaimRequest("T1", "W", "run-1", "a", send_context=first))
        assert claimed.status == ClaimStatus.CLAIMED
        assert claimed.execution_id is not None
        conn.execute("UPDATE task_executions SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
        taken = repo.claim_execution(ClaimRequest("T1", "W", "run-2", "b", send_context=later))
        assert taken.status == ClaimStatus.CLAIMED
        assert taken.execution_id == claimed.execution_id

        assert repo.send_context(claimed.execution_id, "UTC") == first


async def test_manual_send_uses_the_latest_started_occurrence(
    tmp_path: Path, sink: SMTPSink
) -> None:
    task = _task()
    window = HourWindow(start="00:00", end="00:01")  # closed for all but one minute a day
    db_path = _prepare(tmp_path, window, task)
    schedules = WorkSchedules(
        timezone="Europe/Warsaw", schedules=[Schedule(id="S1", allowed_hours=[window])]
    )
    expected = {manual_send_context(task, schedules)}

    await dispatch_single_task(db_path, "T1", _settings(sink.port), tmp_path)

    expected.add(manual_send_context(task, schedules))  # a midnight between the two
    (stored,) = _stored_contexts(db_path)
    assert stored is not None
    context = send_context_from_json(stored)
    assert context in expected
    assert context.kind == "manual"
    assert context.window_start is not None
    assert context.window_start.time().isoformat() == "00:00:00"
    assert [_text(d.raw) for d in sink.deliveries] == [
        f"D={context.date} K=manual T=T1 Z=Europe/Warsaw "
        f"S={context.window_start.strftime('%Y-%m-%d %H:%M')}"
    ]


def test_manual_context_for_a_schedule_without_windows_has_no_window() -> None:
    schedules = WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[])])
    ref_dt = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    context = manual_send_context(_task(), schedules, ref_dt)

    assert (context.kind, context.window_start, context.date) == (
        "manual",
        None,
        date(2026, 10, 28),
    )


def test_panel_preview_uses_the_manual_send_context(tmp_path: Path) -> None:
    task = _task()
    window = HourWindow(start="06:00", end="08:00")
    db_path = _prepare(tmp_path, window, task)
    schedules = WorkSchedules(
        timezone="Europe/Warsaw", schedules=[Schedule(id="S1", allowed_hours=[window])]
    )
    expected = {manual_send_context(task, schedules).date}
    client = TestClient(
        create_app(db_path=db_path, template_dir=tmp_path, allow_insecure_no_auth=True)
    )

    body = client.get("/tasks/T1/preview").text

    expected.add(manual_send_context(task, schedules).date)
    assert "K=manual T=T1 Z=Europe/Warsaw S=" in body
    assert any(f"D={day}" in body for day in expected)


# --- Migration -------------------------------------------------------------------------


def test_migration_adds_the_column_and_old_executions_get_a_derived_context(
    tmp_path: Path,
) -> None:
    db_path = _prepare(tmp_path, HourWindow(start="06:00", end="08:00"))
    start = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)
    end = datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        repo.start_run("run-1", "cli")
        repo.start_run("run-2", "cli")
        window_claim = repo.claim_execution(
            ClaimRequest("T1", "S1@x", "run-1", "a", window_start=start, window_end=end)
        )
        assert window_claim.claim is not None
        repo.release_execution(window_claim.claim)
        manual_claim = repo.claim_execution(ClaimRequest("T1", "manual:k", "run-2", "b"))
        conn.execute("ALTER TABLE task_executions DROP COLUMN send_context_json")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()

    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        assert schema_version(conn) == 7
        assert window_claim.execution_id is not None
        assert manual_claim.execution_id is not None
        window_context = repo.send_context(window_claim.execution_id, "Europe/Warsaw")
        manual_context = repo.send_context(manual_claim.execution_id, "Europe/Warsaw")

    assert window_context.kind == "window"
    assert window_context.window_start == datetime(2026, 10, 28, 6, 0, tzinfo=WARSAW)
    assert window_context.date == date(2026, 10, 28)
    assert manual_context.kind == "manual"
    assert manual_context.window_start is None


def test_send_context_of_a_missing_execution_raises(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, HourWindow(start="06:00", end="08:00"))
    with closing(get_connection(db_path)) as conn, pytest.raises(LookupError):
        DatabaseRepository(conn).send_context(999, "UTC")
