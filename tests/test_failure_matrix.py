"""Failure matrix of the implementation plan (section 7.3) for ADR 0001.

Each test stops the work at one point - by killing a separate process, freezing a
worker inside SMTP, failing a database write, cancelling the batch or moving the wall
clock - and checks the state the next run finds. The SMTP sink's log is the oracle:
a message is never accepted twice for the same recipient, and a delivery whose outcome
the database cannot prove is UNKNOWN, never retried automatically.

"A certain refusal before DATA" is covered by tests/test_mailer_attempts.py (one test
per reply code) and by the T04/T05 reproductions in tests/test_delivery_guarantees.py.
The passage of a lease (5 minutes) is simulated by moving lease_expires_at into the past.
"""

import asyncio
import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dispatch_worker import CRASH_EXIT, PROCESS_TIMEOUT, spawn_batch, stop
from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    Settings,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.pipeline import (
    EXIT_OK,
    EXIT_UNKNOWN,
    DispatchOptions,
    dispatch_single_task,
    run_dispatch,
)
from mail_cadence.repository import DatabaseRepository
from smtp_sink import SMTPSink

A = "a@example.com"
B = "b@example.com"
REF_DT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _prepare(tmp_path: Path, tasks: dict[str, list[str]] | None = None) -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Hello</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="18:00", max_concurrency=2)
        repo.save_work_schedules(
            WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[window])])
        )
        for task_id, recipients in (tasks or {"T1": [A, B]}).items():
            repo.save_single_task(
                MailTask(
                    id=task_id,
                    schedule_ref="S1",
                    sender="cadence@example.com",
                    subject=f"Subject {task_id}",
                    max_retries=2,
                    recipients=Recipients(to=recipients),
                    body_template=BodyTemplate(template_path="body.html.j2"),
                )
            )
    return db_path


def _options(db_path: Path) -> DispatchOptions:
    return DispatchOptions(db_path=db_path, template_dir=db_path.parent, ref_dt=REF_DT)


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


def _run(db_path: Path, port: int) -> int:
    return asyncio.run(run_dispatch(_options(db_path), _settings(port)))


def _expire_leases(db_path: Path) -> None:
    """What five minutes without a heartbeat would do."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE task_executions SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE lease_expires_at IS NOT NULL"
        )


def _query(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [tuple(r) for r in conn.execute(sql)]


def _recipients(db_path: Path) -> dict[str, str]:
    return dict(_query(db_path, "SELECT envelope_address, state FROM execution_recipients"))  # type: ignore[arg-type]


def _mail_commands(sink: SMTPSink) -> int:
    return sum(1 for _, verb in sink.commands if verb == "MAIL")


def _assert_at_most_once(sink: SMTPSink, *recipients: str) -> None:
    for recipient in recipients:
        assert sink.accepted_count(recipient) <= 1, recipient


# --- Separate processes killed at a point ----------------------------------------------


def _crash(db_path: Path, port: int, point: str) -> None:
    proc = spawn_batch(db_path, port, REF_DT, crash_at=point)
    try:
        proc.join(PROCESS_TIMEOUT)
        assert proc.exitcode == CRASH_EXIT, "the crash point was reached"
    finally:
        stop(proc)


def _kill_when_held(db_path: Path, sink: SMTPSink) -> None:
    proc = spawn_batch(db_path, sink.port, REF_DT)
    try:
        sink.wait_held()
    finally:
        stop(proc)
        sink.release()


def test_crash_after_claim_is_taken_over_and_sent_once(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    _crash(db_path, sink.port, "after_claim")
    assert sink.commands == []

    _expire_leases(db_path)
    assert _run(db_path, sink.port) == EXIT_OK

    assert (sink.accepted_count(A), sink.accepted_count(B)) == (1, 1)
    assert _query(db_path, "SELECT state, fencing_token FROM task_executions") == [("SUCCEEDED", 2)]


def test_crash_before_connecting_needs_review(tmp_path: Path, sink: SMTPSink) -> None:
    """IN_FLIGHT is stored, the process dies before the greeting: nothing proves that
    nothing was sent, so the recipients are UNKNOWN and nobody sends them again."""
    db_path = _prepare(tmp_path)
    sink.hold_at = ("CONNECT", 1)
    _kill_when_held(db_path, sink)

    _expire_leases(db_path)
    assert _run(db_path, sink.port) == EXIT_UNKNOWN

    assert _mail_commands(sink) == 0
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}
    assert _query(db_path, "SELECT state FROM task_executions") == [("NEEDS_REVIEW",)]


def test_crash_while_waiting_for_the_reply_after_data(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.hold_at = ("DATA", 1)
    _kill_when_held(db_path, sink)
    assert len(sink.deliveries) == 1, "the server accepted the message"

    _expire_leases(db_path)
    assert _run(db_path, sink.port) == EXIT_UNKNOWN

    assert _mail_commands(sink) == 1
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}


def test_crash_after_the_reply_before_the_result_is_stored(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    _crash(db_path, sink.port, "after_smtp")
    assert len(sink.deliveries) == 1

    _expire_leases(db_path)
    assert _run(db_path, sink.port) == EXIT_UNKNOWN

    assert _mail_commands(sink) == 1, "a lost success is never resent"
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}


def test_crash_after_the_result_is_stored(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    _crash(db_path, sink.port, "after_release")

    assert _run(db_path, sink.port) == EXIT_OK

    assert _mail_commands(sink) == 1
    assert _query(db_path, "SELECT state FROM task_executions") == [("SUCCEEDED",)]


# --- In-process faults -----------------------------------------------------------------


def _held_worker(db_path: Path, sink: SMTPSink) -> tuple[threading.Thread, list[int]]:
    """A run in another thread that stays inside SMTP (after the body) until released."""
    sink.hold_at = ("DATA", 1)
    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(_run(db_path, sink.port)))
    worker.start()
    sink.wait_held()
    return worker, codes


def test_frozen_worker_is_superseded_without_a_parallel_send(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path)
    worker, codes = _held_worker(db_path, sink)
    try:
        _expire_leases(db_path)
        assert _run(db_path, sink.port) == EXIT_UNKNOWN, "the new run must not send"
        assert _mail_commands(sink) == 1
    finally:
        sink.release()
        worker.join(PROCESS_TIMEOUT)

    assert codes == [EXIT_UNKNOWN], "the late result of the old worker is refused"
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}
    assert len(sink.deliveries) == 1


def test_frozen_worker_blocks_new_windows_manual_sends_and_resets(
    tmp_path: Path, sink: SMTPSink
) -> None:
    """Audit A2: an expired lease does not prove that the old SMTP session has ended.
    While the old worker is held after the body, no other execution of the task may
    start a transmission; its late replies are kept only as evidence."""
    db_path = _prepare(tmp_path)
    settings = _settings(sink.port)
    worker, codes = _held_worker(db_path, sink)
    try:
        _expire_leases(db_path)
        next_day = DispatchOptions(
            db_path=db_path, template_dir=tmp_path, ref_dt=REF_DT + timedelta(days=1)
        )
        assert asyncio.run(run_dispatch(next_day, settings)) == EXIT_UNKNOWN
        manual = asyncio.run(
            dispatch_single_task(db_path, "T1", settings, tmp_path, idempotency_key="click-1")
        )
        assert manual.status == TaskExecutionStatus.SKIPPED_BUSY
        with closing(get_connection(db_path)) as conn:
            assert DatabaseRepository(conn).reset_task_window_state("T1") is True
        assert _run(db_path, sink.port) == EXIT_UNKNOWN
        assert _mail_commands(sink) == 1, "only the old worker's session"
    finally:
        sink.release()
        worker.join(PROCESS_TIMEOUT)

    assert codes == [EXIT_UNKNOWN]
    assert len(sink.deliveries) == 1
    assert _query(db_path, "SELECT COUNT(*) FROM task_executions") == [(1,)]
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}
    late = _query(db_path, "SELECT details_json FROM execution_events WHERE kind = 'late_result'")
    assert late == [(json.dumps({A: "ACCEPTED 250", B: "ACCEPTED 250"}, sort_keys=True),)]


def test_database_error_after_smtp_is_never_a_success(
    tmp_path: Path, sink: SMTPSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _prepare(tmp_path)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(DatabaseRepository, "record_attempt_result", locked)
        assert _run(db_path, sink.port) == EXIT_UNKNOWN
    assert len(sink.deliveries) == 1

    _expire_leases(db_path)
    assert _run(db_path, sink.port) == EXIT_UNKNOWN
    assert _mail_commands(sink) == 1
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}


def test_cancelled_batch_leaves_every_started_attempt_traceable(
    tmp_path: Path, sink: SMTPSink
) -> None:
    db_path = _prepare(tmp_path, {"T1": [A], "T2": [B]})
    sink.hold_at = ("DATA", 1)
    loop = asyncio.new_event_loop()
    batch = loop.create_task(run_dispatch(_options(db_path), _settings(sink.port)))

    def run_batch() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(batch)
        loop.close()

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        sink.wait_held()
        loop.call_soon_threadsafe(batch.cancel)
    finally:
        sink.release()
        worker.join(PROCESS_TIMEOUT)
    held = sink.deliveries[0].recipients[0]

    _expire_leases(db_path)
    _run(db_path, sink.port)

    states = _recipients(db_path)
    assert states[held] == "UNKNOWN", "the cancelled attempt is not retried"
    attempted = _query(
        db_path,
        "SELECT envelope_address, state FROM execution_recipients WHERE attempt_count > 0",
    )
    assert all(state != "PENDING" for _, state in attempted)
    _assert_at_most_once(sink, A, B)


def test_wall_clock_jump_does_not_cause_a_second_send(
    tmp_path: Path, sink: SMTPSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall clock jumps an hour ahead while a worker is inside SMTP: its lease
    looks expired, but its attempt becomes UNKNOWN instead of being repeated."""
    db_path = _prepare(tmp_path)
    jump = timedelta()

    class JumpingDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "JumpingDatetime":
            return super().now(tz) + jump  # type: ignore[arg-type]

    monkeypatch.setattr("mail_cadence.repository.datetime", JumpingDatetime)
    worker, codes = _held_worker(db_path, sink)
    try:
        jump = timedelta(hours=1)
        assert _run(db_path, sink.port) == EXIT_UNKNOWN
    finally:
        sink.release()
        worker.join(PROCESS_TIMEOUT)

    assert codes == [EXIT_UNKNOWN]
    assert _mail_commands(sink) == 1
    assert _recipients(db_path) == {A: "UNKNOWN", B: "UNKNOWN"}
    durations = [d for (d,) in _query(db_path, "SELECT duration_ms FROM execution_logs")]
    assert all(float(d) >= 0 for d in durations), "durations use the monotonic clock"  # type: ignore[arg-type]
