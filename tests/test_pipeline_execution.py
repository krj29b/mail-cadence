"""Stage 5 of ADR 0001: the pipeline on the durable execution state.

Window occurrences (T12), dry runs that never write execution state (T10), render
errors without SMTP, manual sends with an idempotency key (T09), no attempt after the
window end (T11) and the conversion of legacy window state (migration 6). The SMTP sink
and direct SQL queries are the oracle.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import ClaimRequest, ClaimStatus
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    Settings,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.pipeline import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    EXIT_UNKNOWN,
    DispatchOptions,
    dispatch_single_task,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.scheduler import TaskScheduler
from smtp_sink import SMTPSink

A = "a@example.com"
B = "b@example.com"
WARSAW = ZoneInfo("Europe/Warsaw")
REF_DT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
EXECUTION_TABLES = ("dispatch_runs", "task_executions", "execution_recipients", "delivery_attempts")


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


def _task(task_id: str = "T1", *, template: str = "body.html.j2", max_retries: int = 0) -> MailTask:
    return MailTask(
        id=task_id,
        schedule_ref="S1",
        sender="cadence@example.com",
        subject=f"Subject {task_id}",
        max_retries=max_retries,
        recipients=Recipients(to=[A, B]),
        body_template=BodyTemplate(template_path=template),
    )


def _prepare(tmp_path: Path, *tasks: MailTask, end: str = "18:00") -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Hello</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end=end)
        repo.save_work_schedules(
            WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[window])])
        )
        for task in tasks or (_task(),):
            repo.save_single_task(task)
    return db_path


def _run(db_path: Path, port: int, *, dry_run: bool = False, ref_dt: datetime = REF_DT) -> int:
    options = DispatchOptions(
        db_path=db_path, template_dir=db_path.parent, dry_run=dry_run, ref_dt=ref_dt
    )
    return asyncio.run(run_dispatch(options, _settings(port)))


def _count(db_path: Path, table: str) -> int:
    assert table in EXECUTION_TABLES
    with closing(sqlite3.connect(db_path)) as conn:
        sql = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table names
        return int(conn.execute(sql).fetchone()[0])


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [tuple(r) for r in conn.execute(sql)]


# --- T12: window occurrences -----------------------------------------------------------


def _scheduler(*windows: HourWindow, timezone: str = "Europe/Warsaw") -> TaskScheduler:
    return TaskScheduler(
        WorkSchedules(timezone=timezone, schedules=[Schedule(id="S", allowed_hours=list(windows))])
    )


@pytest.mark.parametrize(
    ("window", "ref", "expected_start", "expected_end"),
    [
        # Plain day window, both ends inclusive.
        (("06:00", "18:00"), datetime(2026, 10, 17, 6, 0), "2026-10-17T04:00", "2026-10-17T16:00"),
        (("06:00", "18:00"), datetime(2026, 10, 17, 18, 0), "2026-10-17T04:00", "2026-10-17T16:00"),
        # After midnight the occurrence started the evening before.
        (("22:00", "04:00"), datetime(2026, 10, 18, 1, 0), "2026-10-17T20:00", "2026-10-18T02:00"),
        # Spring forward (2026-03-29): 02:30 does not exist and maps to 03:30 CEST.
        (("02:30", "05:00"), datetime(2026, 3, 29, 3, 45), "2026-03-29T01:30", "2026-03-29T03:00"),
        # Fall back (2026-10-25): the first 02:10 (CEST) is inside 02:00-02:30.
        (("02:00", "02:30"), datetime(2026, 10, 25, 2, 10), "2026-10-25T00:00", "2026-10-25T00:30"),
        # End of day.
        (
            ("20:00", "24:00"),
            datetime(2026, 10, 17, 23, 59),
            "2026-10-17T18:00",
            "2026-10-17T21:59",
        ),
    ],
)
def test_window_occurrence_in_utc(
    window: tuple[str, str], ref: datetime, expected_start: str, expected_end: str
) -> None:
    instance = _scheduler(HourWindow(start=window[0], end=window[1])).window_instance(
        "S", ref.replace(tzinfo=WARSAW)
    )
    assert instance is not None
    assert instance.start_utc.strftime("%Y-%m-%dT%H:%M") == expected_start
    assert instance.end_utc.strftime("%Y-%m-%dT%H:%M") == expected_end
    assert instance.key == f"S@{instance.start_utc.isoformat()}"


@pytest.mark.parametrize(
    ("window", "ref"),
    [
        (HourWindow(start="06:00", end="18:00"), datetime(2026, 10, 17, 18, 0, 0, 1)),
        # Spring forward: 02:30 maps to 03:30 CEST, so 03:15 CEST is before the start.
        (HourWindow(start="02:30", end="05:00"), datetime(2026, 3, 29, 3, 15)),
        # Fall back: only the first 02:10 (CEST) is inside, not the second one (CET).
        (HourWindow(start="02:00", end="02:30"), datetime(2026, 10, 25, 2, 10, fold=1)),
    ],
)
def test_instants_outside_the_occurrence_are_not_active(window: HourWindow, ref: datetime) -> None:
    assert _scheduler(window).window_instance("S", ref.replace(tzinfo=WARSAW)) is None


def test_overlapping_windows_use_configuration_order() -> None:
    first = HourWindow(start="08:00", end="12:00", max_concurrency=1)
    second = HourWindow(start="06:00", end="18:00", max_concurrency=5)
    instance = _scheduler(first, second).window_instance(
        "S", datetime(2026, 10, 17, 9, 0, tzinfo=WARSAW)
    )
    assert instance is not None
    assert instance.window.max_concurrency == 1


def test_cross_midnight_occurrence_belongs_to_its_start_day() -> None:
    friday_only = HourWindow(start="22:00", end="04:00", days_of_week="fri")
    scheduler = _scheduler(friday_only)
    saturday_night = datetime(2026, 10, 17, 1, 0, tzinfo=WARSAW)  # Friday's occurrence
    sunday_night = datetime(2026, 10, 18, 1, 0, tzinfo=WARSAW)  # would be Saturday's
    assert scheduler.window_instance("S", saturday_night) is not None
    assert scheduler.window_instance("S", sunday_night) is None


# --- T10: dry runs ---------------------------------------------------------------------


def test_dry_run_writes_no_execution_state_and_sends_nothing(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port, dry_run=True) == EXIT_OK

    assert sink.commands == []
    assert {t: _count(db_path, t) for t in EXECUTION_TABLES} == dict.fromkeys(EXECUTION_TABLES, 0)
    assert _rows(db_path, "SELECT DISTINCT dry_run FROM execution_logs") == [(1,)]


def test_dry_run_reports_an_executed_window_without_touching_it(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port) == EXIT_OK
    before = {t: _count(db_path, t) for t in EXECUTION_TABLES}

    assert _run(db_path, sink.port, dry_run=True) == EXIT_OK

    assert {t: _count(db_path, t) for t in EXECUTION_TABLES} == before
    assert len(sink.deliveries) == 1
    last = _rows(db_path, "SELECT status, error_details, dry_run FROM execution_logs")[-1]
    assert last == ("SKIPPED_SCHEDULE", "already executed in this window", 1)


# --- Render errors ---------------------------------------------------------------------


def test_template_error_ends_the_execution_without_smtp(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, _task(template="missing.html.j2"))

    assert _run(db_path, sink.port) == EXIT_TASK_FAILED

    assert sink.commands == []
    assert _rows(db_path, "SELECT state FROM task_executions") == [("FAILED",)]
    states = _rows(db_path, "SELECT state, last_error FROM execution_recipients")
    assert {s for s, _ in states} == {"PERMANENT_FAILURE"}
    assert all("missing.html.j2" in str(e) for _, e in states)


# --- T09: manual sends -----------------------------------------------------------------


def test_manual_send_with_the_same_key_is_sent_once(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)

    def send(key: str | None) -> str:
        result = asyncio.run(
            dispatch_single_task(db_path, "T1", _settings(sink.port), tmp_path, key)
        )
        return result.status.value

    assert send("click-1") == "SUCCESS"
    assert send("click-1") == "SUCCESS", "a repeated request reports the earlier send"
    assert len(sink.deliveries) == 1
    assert send("click-2") == "SUCCESS"
    assert len(sink.deliveries) == 2
    keys = _rows(db_path, "SELECT window_key FROM task_executions ORDER BY execution_id")
    assert keys == [("manual:click-1",), ("manual:click-2",)]


# --- T11: the window ends while retrying -----------------------------------------------


def test_no_attempt_starts_after_the_window_end(tmp_path: Path, sink: SMTPSink) -> None:
    """The window closes one second after the reference time while B keeps getting 4xx."""
    db_path = _prepare(tmp_path, _task(max_retries=5))
    sink.rcpt_codes[B] = 450
    ref = datetime(2026, 10, 17, 17, 59, 59, tzinfo=UTC)

    assert _run(db_path, sink.port, ref_dt=ref) == EXIT_TASK_FAILED

    [(window_end,)] = _rows(db_path, "SELECT window_end FROM task_executions")
    starts = [s for (s,) in _rows(db_path, "SELECT started_at FROM delivery_attempts")]
    assert all(
        datetime.fromisoformat(str(s)) <= datetime.fromisoformat(str(window_end)) for s in starts
    )
    assert sorted(_rows(db_path, "SELECT envelope_address, state FROM execution_recipients")) == [
        (A, "ACCEPTED"),
        (B, "WINDOW_EXPIRED"),
    ]
    assert sink.rcpt_count(B) < 6, "the retry budget was cut by the window end"


# --- Migration 6: legacy window state --------------------------------------------------


def _legacy(db_path: Path, rows: list[tuple[str, str | None, str]]) -> None:
    """Write last_run/last_status as the old path did and rerun migration 6."""
    with closing(get_connection(db_path)) as conn:
        for task_id, last_run, last_status in rows:
            conn.execute(
                "UPDATE tasks SET last_run = ?, last_status = ? WHERE id = ?",
                (last_run, last_status, task_id),
            )
        conn.execute("DELETE FROM task_executions")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        init_db(conn)


def test_legacy_state_blocks_only_its_own_window(tmp_path: Path, sink: SMTPSink) -> None:
    tasks = [_task(f"T{n}") for n in range(1, 5)]
    db_path = _prepare(tmp_path, *tasks)
    _legacy(
        db_path,
        [
            ("T1", "2026-10-17T09:00:00+00:00", "SUCCESS"),
            ("T2", "2026-10-17T09:00:00+00:00", "PARTIAL"),
            ("T3", "2026-10-17T03:00:00+00:00", "SUCCESS"),  # outside every window
            ("T4", "not a time", "FAILED"),
        ],
    )

    converted = _rows(
        db_path,
        "SELECT i.task_id, e.state, e.legacy FROM task_executions e "
        "JOIN task_identities i ON i.uid = e.task_uid ORDER BY i.task_id",
    )
    dropped = _rows(
        db_path,
        "SELECT i.task_id FROM execution_events e JOIN task_identities i ON i.uid = e.task_uid "
        "WHERE e.kind = 'legacy_state_dropped' ORDER BY i.task_id",
    )
    assert converted == [("T1", "SUCCEEDED", 1), ("T2", "NEEDS_REVIEW", 1)]
    assert dropped == [("T3",), ("T4",)]

    assert _run(db_path, sink.port) == EXIT_UNKNOWN, "T2 still needs review in this window"
    assert sorted(d.subject for d in sink.deliveries) == ["Subject T3", "Subject T4"]

    next_day = REF_DT + timedelta(days=1)
    assert _run(db_path, sink.port, ref_dt=next_day) == EXIT_OK
    assert len(sink.deliveries) == 6, "a later occurrence is free for every task"


# --- D15: the compatibility projection -------------------------------------------------


def test_old_version_result_does_not_update_the_projection(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    assert _run(db_path, sink.port) == EXIT_OK
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        [(old_execution,)] = conn.execute("SELECT execution_id FROM task_executions").fetchall()
        changed = _task()
        changed.subject = "Edited"
        repo.save_single_task(changed)  # new version; window state reset
        result = TaskExecutionResult(task_id="T1", status=TaskExecutionStatus.SUCCESS)

        repo.log_task_execution(result, execution_id=old_execution)
        assert repo.get_task_execution_states()["T1"] == (None, None)
        repo.log_task_execution(result)
        assert repo.get_task_execution_states()["T1"][1] == TaskExecutionStatus.SUCCESS


def test_taken_over_execution_sends_the_pinned_version(tmp_path: Path, sink: SMTPSink) -> None:
    """A worker claimed version 1 and died before rendering; the task was then edited
    without a window reset. The next run takes over and still sends version 1 (I5)."""
    db_path = _prepare(tmp_path)
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        repo.start_run("dead-run", "cli")
        dead = repo.claim_execution(
            ClaimRequest("T1", "S1@2026-10-17T06:00:00+00:00", "dead-run", "dead-worker"),
            lease=timedelta(seconds=-1),
        )
        assert dead.status == ClaimStatus.CLAIMED
        edited = _task()
        edited.subject = "Edited"
        repo.save_single_task(edited, reset_window_state=False)

    assert _run(db_path, sink.port) == EXIT_OK
    assert [d.subject for d in sink.deliveries] == ["Subject T1"]
