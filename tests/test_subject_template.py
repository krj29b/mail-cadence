"""Subject lines as templates (MailTask.render_subject): opt-in, sandboxed, not
HTML-escaped, one line of at most 255 characters, stored with the task definition."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import jinja2
import pytest

from mail_cadence.database import SCHEMA_VERSION, get_connection, init_db, schema_version
from mail_cadence.execution_store import ClaimRequest
from mail_cadence.mailer import SUBJECT_MAX_LENGTH, SubjectRenderError, TemplateRenderer
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
from mail_cadence.pipeline import EXIT_TASK_FAILED, DispatchOptions, run_dispatch
from mail_cadence.repository import DatabaseRepository
from smtp_sink import SMTPSink

START = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)  # 06:00 in Warsaw
CONTEXT = SendContext.build("T1", "window", "Europe/Warsaw", (START, START), START)


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _task(
    subject: str, *, render: bool = True, variables: dict[str, str] | None = None
) -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="cadence@example.com",
        subject=subject,
        render_subject=render,
        recipients=Recipients(to=["a@example.com"]),
        body_template=BodyTemplate(template_path="body.html.j2"),
        context_variables=[ContextVar(name=k, value=v) for k, v in (variables or {}).items()],
    )


def _subject(task: MailTask) -> str:
    return TemplateRenderer().render_task_subject(task, CONTEXT)


# --- Rendering -------------------------------------------------------------------------


def test_subject_is_literal_unless_rendering_is_on() -> None:
    assert _subject(_task("Report {{ cadence.date }}", render=False)) == (
        "Report {{ cadence.date }}"
    )


def test_rendered_subject_uses_the_context_and_the_date_filters() -> None:
    task = _task(
        "Report {{ cadence.date | add_days(-1) | date('%d.%m.%Y') }} ({{ team }})",
        variables={"team": "Ops"},
    )

    assert _subject(task) == "Report 27.10.2026 (Ops)"


def test_rendered_subject_is_not_html_escaped() -> None:
    task = _task("{{ who }} & {{ what }}", variables={"who": "R&D", "what": "<summary>"})

    assert _subject(task) == "R&D & <summary>"


def test_surrounding_whitespace_is_dropped() -> None:
    assert _subject(_task("  {{ cadence.task_id }}  ")) == "T1"


@pytest.mark.parametrize(
    ("subject", "variables", "message"),
    [
        ("{{ line }}", {"line": "first\nsecond"}, "line break"),
        ("{{ line }}", {"line": "first\rsecond"}, "line break"),
        ("{{ empty }}", {"empty": "   "}, "empty subject"),
        ("{{ long }}", {"long": "x" * (SUBJECT_MAX_LENGTH + 1)}, "at most 255"),
    ],
    ids=["newline", "carriage-return", "empty", "too-long"],
)
def test_invalid_rendered_subject_is_refused(
    subject: str, variables: dict[str, str], message: str
) -> None:
    with pytest.raises(SubjectRenderError, match=message):
        _subject(_task(subject, variables=variables))


def test_subject_of_exactly_the_limit_is_accepted() -> None:
    task = _task("{{ text }}", variables={"text": "x" * SUBJECT_MAX_LENGTH})

    assert len(_subject(task)) == SUBJECT_MAX_LENGTH


def test_undefined_variable_in_the_subject_fails() -> None:
    with pytest.raises(jinja2.UndefinedError):
        _subject(_task("Report {{ missing }}"))


def test_subject_template_is_sandboxed() -> None:
    with pytest.raises(jinja2.TemplateError):
        _subject(_task("{{ cadence.__class__.__mro__ }}"))


# --- Storage ---------------------------------------------------------------------------


def _prepare(tmp_path: Path, task: MailTask) -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Body</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="08:00")
        repo.save_work_schedules(
            WorkSchedules(
                timezone="Europe/Warsaw", schedules=[Schedule(id="S1", allowed_hours=[window])]
            )
        )
        repo.save_single_task(task)
    return db_path


def _definitions(db_path: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT definition_json FROM task_versions ORDER BY version")
        return [json.loads(row[0]) for row in rows]


def test_flag_is_saved_loaded_and_pinned(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, _task("Report {{ cadence.date }}"))

    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        loaded = repo.get_mail_task("T1")
        repo.start_run("run-1", "cli")
        claimed = repo.claim_execution(ClaimRequest("T1", "W", "run-1", "owner"))
        assert claimed.claim is not None
        pinned = repo.pinned_task(claimed.claim)

    assert loaded is not None
    assert loaded.render_subject is True
    assert pinned.render_subject is True
    assert _definitions(db_path)[-1]["render_subject"] is True


def test_definition_without_the_flag_keeps_its_exact_form(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, _task("Report", render=False))

    (definition,) = _definitions(db_path)
    assert "render_subject" not in definition


def test_turning_the_flag_on_is_a_change_of_the_task(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, _task("Report {{ cadence.date }}", render=False))
    with closing(get_connection(db_path)) as conn:
        changed = DatabaseRepository(conn).save_single_task(_task("Report {{ cadence.date }}"))

    assert changed, "a new definition resets the window state"
    assert [d.get("render_subject", False) for d in _definitions(db_path)] == [False, True]


def test_migration_adds_the_flag_off_for_existing_tasks(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path, _task("Report", render=False))
    with closing(get_connection(db_path)) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN render_subject")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        conn.commit()

    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        task = DatabaseRepository(conn).get_mail_task("T1")
        assert schema_version(conn) == SCHEMA_VERSION

    assert task is not None
    assert task.render_subject is False
    assert len(_definitions(db_path)) == 1, "existing definitions are not re-versioned"


# --- Dispatch --------------------------------------------------------------------------


def _options(db_path: Path) -> DispatchOptions:
    return DispatchOptions(
        db_path=db_path,
        template_dir=db_path.parent,
        ref_dt=datetime(2026, 10, 28, 6, 0, tzinfo=UTC),  # 07:00 in Warsaw, inside 06:00-08:00
    )


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


async def test_sent_message_carries_the_rendered_subject(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, _task("Report {{ cadence.date | date('%d.%m.%Y') }}"))

    await run_dispatch(_options(db_path), _settings(sink.port))

    assert [d.subject for d in sink.deliveries] == ["Report 28.10.2026"]


async def test_invalid_subject_ends_the_execution_without_smtp(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path, _task("{{ line }}", variables={"line": "a\nb"}))

    code = await run_dispatch(_options(db_path), _settings(sink.port))

    assert code == EXIT_TASK_FAILED
    assert sink.commands == [], "nothing was sent"
    with closing(sqlite3.connect(db_path)) as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM task_executions")]
    assert states == ["FAILED"], "the execution was claimed and aborted"
