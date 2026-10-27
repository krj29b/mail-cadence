"""Execution state in the repository (ADR 0001, stage 3): claim, fencing, attempts,
retry budget, window expiry, recovery and operator resolution. No SMTP is involved.

A fake clock drives leases and backoff. Checks read the tables directly.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dispatch_worker import PROCESS_TIMEOUT, spawn_claimers, stop
from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import (
    DEFAULT_LEASE,
    Claim,
    ClaimRequest,
    ClaimStatus,
    ExecutionBusyError,
    Outcome,
    RecipientResult,
    Resolution,
    StaleClaimError,
    derive_execution_state,
    envelope_address,
)
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    Schedule,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository

A = "a@example.com"
B = "b@example.com"
WINDOW = "S1@2026-10-27T06:00:00+00:00"
OK = RecipientResult(Outcome.ACCEPTED, 250)
BUSY_4XX = RecipientResult(Outcome.TRANSIENT, 450, "Mailbox busy")
REFUSED_5XX = RecipientResult(Outcome.PERMANENT, 550, "No such user")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cadence.db"


@pytest.fixture
def repo(db_path: Path, clock: FakeClock) -> Iterator[DatabaseRepository]:
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repository = DatabaseRepository(conn, clock=clock)
        repository.save_work_schedules(
            WorkSchedules(
                timezone="UTC",
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="06:00", end="18:00")])
                ],
            )
        )
        repository.save_single_task(_task())
        repository.start_run("run-1", "cli")
        yield repository


def _task(
    task_id: str = "T1",
    *,
    to: tuple[str, ...] = (A, B),
    subject: str = "Hello",
    max_retries: int = 1,
    **recipients: list[str],
) -> MailTask:
    return MailTask(
        id=task_id,
        schedule_ref="S1",
        sender="cadence@example.com",
        subject=subject,
        max_retries=max_retries,
        recipients=Recipients(to=list(to), **recipients),
        body_template=BodyTemplate(template_path="builtin:x.html.j2"),
    )


def _claim(
    repo: DatabaseRepository,
    owner: str = "owner-1",
    *,
    task_id: str = "T1",
    window_key: str = WINDOW,
    window_end: datetime | None = None,
) -> Claim:
    result = repo.claim_execution(
        ClaimRequest(task_id, window_key, "run-1", owner, window_end=window_end)
    )
    assert result.status == ClaimStatus.CLAIMED, result
    assert result.claim is not None
    return result.claim


def _status(repo: DatabaseRepository, owner: str, **kwargs: str) -> ClaimStatus:
    request = ClaimRequest(
        kwargs.get("task_id", "T1"), kwargs.get("window_key", WINDOW), "run-1", owner
    )
    return repo.claim_execution(request).status


def _recipients(repo: DatabaseRepository, execution_id: int) -> dict[str, tuple[str, int]]:
    rows = repo.conn.execute(
        "SELECT envelope_address, state, attempt_count FROM execution_recipients "
        "WHERE execution_id = ?",
        (execution_id,),
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def _execution_state(repo: DatabaseRepository, execution_id: int) -> str:
    row = repo.conn.execute(
        "SELECT state FROM task_executions WHERE execution_id = ?", (execution_id,)
    ).fetchone()
    return str(row[0])


# --- Pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("Ann <Ann.Smith@Example.COM>", "Ann.Smith@example.com"),
        ("b@EXAMPLE.com", "b@example.com"),
        ("not-an-address", "not-an-address"),
    ],
)
def test_envelope_address_keeps_local_part_and_lowers_domain(address: str, key: str) -> None:
    assert envelope_address(address) == key


@pytest.mark.parametrize(
    ("counts", "state"),
    [
        ({"ACCEPTED": 2}, "SUCCEEDED"),
        ({"ACCEPTED": 1, "PERMANENT_FAILURE": 1}, "PARTIAL"),
        ({"ACCEPTED": 1, "RETRY_EXHAUSTED": 1}, "PARTIAL"),
        ({"ACCEPTED": 1, "UNKNOWN": 1}, "NEEDS_REVIEW"),
        ({"ACCEPTED": 1, "RETRY_WAIT": 1}, "RETRY_WAIT"),
        ({"PERMANENT_FAILURE": 2}, "FAILED"),
        ({"WINDOW_EXPIRED": 1, "PERMANENT_FAILURE": 1}, "EXPIRED"),
    ],
)
def test_derive_execution_state(counts: dict[str, int], state: str) -> None:
    assert derive_execution_state(counts) == state


# --- Claim -----------------------------------------------------------------------------


def test_claim_pins_version_and_deduplicates_recipients(repo: DatabaseRepository) -> None:
    repo.save_single_task(
        _task(to=("Ann <a@Example.COM>", B), cc=["a@example.com"], bcc=["c@example.com"])
    )
    claim = _claim(repo)

    rows = repo.conn.execute(
        "SELECT envelope_address, display_address, recipient_type, max_attempts "
        "FROM execution_recipients ORDER BY rowid"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (A, "Ann <a@Example.COM>", "to", 2),
        (B, B, "to", 2),
        ("c@example.com", "c@example.com", "bcc", 2),
    ]
    assert (claim.task_version, claim.fencing_token) == (2, 1)


def test_only_one_owner_per_task(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    again = repo.claim_execution(ClaimRequest("T1", WINDOW, "run-1", "owner-1"))

    assert again.claim == claim, "re-claiming by the same owner is idempotent"
    assert _status(repo, "owner-2") == ClaimStatus.BUSY
    assert _status(repo, "owner-2", window_key="manual:key-1") == ClaimStatus.BUSY
    assert _status(repo, "owner-2", task_id="NOPE") == ClaimStatus.NO_TASK


def test_finished_execution_is_done_for_its_key(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    repo.record_attempt_result(claim, attempt, {A: OK, B: OK})
    assert repo.start_attempt(claim) is None
    assert repo.release_execution(claim) == "SUCCEEDED"

    assert _status(repo, "owner-2") == ClaimStatus.DONE
    assert _status(repo, "owner-2", window_key="S1@2026-10-28T06:00:00+00:00") == (
        ClaimStatus.CLAIMED
    )


# --- Attempts, retry budget and window -------------------------------------------------


def test_accepted_and_refused_recipients_are_never_attempted_again(
    repo: DatabaseRepository,
) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    assert attempt.recipients == (A, B)
    repo.record_attempt_result(claim, attempt, {A: OK, B: REFUSED_5XX})

    assert repo.start_attempt(claim) is None
    assert repo.release_execution(claim) == "PARTIAL"
    assert _status(repo, "owner-2") == ClaimStatus.DONE
    assert _recipients(repo, claim.execution_id) == {
        A: ("ACCEPTED", 1),
        B: ("PERMANENT_FAILURE", 1),
    }


def test_transient_recipient_waits_for_backoff_then_is_retried_alone(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    claim = _claim(repo)
    first = repo.start_attempt(claim)
    assert first is not None
    repo.record_attempt_result(claim, first, {A: OK, B: BUSY_4XX})

    assert repo.start_attempt(claim) is None, "backoff has not elapsed"
    assert repo.next_attempt_at(claim.execution_id) == clock.now + timedelta(seconds=0.5)
    clock.advance(seconds=1)
    second = repo.start_attempt(claim)
    assert second is not None
    assert (second.attempt_no, second.recipients) == (2, (B,))

    repo.record_attempt_result(claim, second, {B: BUSY_4XX})
    assert repo.next_attempt_at(claim.execution_id) is None
    assert repo.release_execution(claim) == "PARTIAL"
    assert _recipients(repo, claim.execution_id)[B] == ("RETRY_EXHAUSTED", 2)


def test_retry_budget_survives_a_new_owner(repo: DatabaseRepository, clock: FakeClock) -> None:
    """T05 at the repository level: the next run continues the budget."""
    first_claim = _claim(repo, "run-a")
    attempt = repo.start_attempt(first_claim)
    assert attempt is not None
    repo.record_attempt_result(first_claim, attempt, {A: OK, B: BUSY_4XX})
    assert repo.release_execution(first_claim) == "RETRY_WAIT"

    clock.advance(seconds=1)
    second_claim = _claim(repo, "run-b")
    retry = repo.start_attempt(second_claim)
    assert retry is not None
    assert retry.recipients == (B,)
    repo.record_attempt_result(second_claim, retry, {B: BUSY_4XX})

    assert second_claim.fencing_token == first_claim.fencing_token + 1
    assert _recipients(repo, second_claim.execution_id)[B] == ("RETRY_EXHAUSTED", 2)


def test_no_attempt_starts_after_the_window_end(repo: DatabaseRepository, clock: FakeClock) -> None:
    claim = _claim(repo, window_end=clock.now + timedelta(minutes=1))
    clock.advance(minutes=2)

    assert repo.start_attempt(claim) is None
    assert repo.release_execution(claim) == "EXPIRED"
    assert {s for s, _ in _recipients(repo, claim.execution_id).values()} == {"WINDOW_EXPIRED"}


def test_missing_result_is_unknown_and_blocks_the_key(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    repo.record_attempt_result(claim, attempt, {A: OK})

    attempt_state = repo.conn.execute("SELECT state FROM delivery_attempts").fetchone()[0]
    assert attempt_state == "UNKNOWN"
    assert repo.release_execution(claim) == "NEEDS_REVIEW"
    assert _status(repo, "owner-2") == ClaimStatus.DONE


def test_an_attempt_result_is_recorded_once(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    repo.record_attempt_result(claim, attempt, {A: OK, B: REFUSED_5XX})
    with pytest.raises(StaleClaimError, match="no longer in flight"):
        repo.record_attempt_result(claim, attempt, {A: OK, B: OK})
    assert _recipients(repo, claim.execution_id)[B] == ("PERMANENT_FAILURE", 1)


def test_results_for_other_recipients_are_rejected(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    with pytest.raises(ValueError, match="outside the attempt"):
        repo.record_attempt_result(claim, attempt, {"x@example.com": OK})
    assert {s for s, _ in _recipients(repo, claim.execution_id).values()} == {"IN_FLIGHT"}


# --- Lease, fencing and recovery -------------------------------------------------------


def test_expired_claim_without_attempt_is_taken_over(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    old = _claim(repo, "owner-1")
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)
    new = _claim(repo, "owner-2")

    assert new.execution_id == old.execution_id
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleClaimError):
        repo.start_attempt(old)
    with pytest.raises(StaleClaimError):
        repo.release_execution(old)


def test_fencing_token_rejects_an_old_claim_of_the_same_owner(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    """An owner name reused after a restart does not revive the old claim."""
    old = _claim(repo, "owner-1")
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)
    new = _claim(repo, "owner-1")
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleClaimError):
        repo.start_attempt(old)
    assert repo.start_attempt(new) is not None


def test_no_attempt_starts_on_an_expired_lease(repo: DatabaseRepository, clock: FakeClock) -> None:
    claim = _claim(repo)
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)
    with pytest.raises(StaleClaimError):
        repo.start_attempt(claim)


def test_heartbeat_keeps_the_claim(repo: DatabaseRepository, clock: FakeClock) -> None:
    _claim(repo, "owner-1")
    clock.advance(minutes=4)
    assert repo.heartbeat("owner-1") == 1
    clock.advance(minutes=4)
    assert _status(repo, "owner-2") == ClaimStatus.BUSY


def test_lost_owner_during_attempt_needs_review_not_resend(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    old = _claim(repo, "owner-1")
    attempt = repo.start_attempt(old)
    assert attempt is not None
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)

    assert _status(repo, "owner-2") == ClaimStatus.DONE
    assert _execution_state(repo, old.execution_id) == "NEEDS_REVIEW"
    with pytest.raises(StaleClaimError, match="no longer"):
        repo.record_attempt_result(old, attempt, {A: OK, B: OK})
    assert {s for s, _ in _recipients(repo, old.execution_id).values()} == {"UNKNOWN"}


def test_late_result_is_kept_when_nobody_took_over(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)

    repo.record_attempt_result(claim, attempt, {A: OK, B: OK})
    assert repo.release_execution(claim) == "SUCCEEDED"


def test_recover_abandoned_settles_expired_owners(
    repo: DatabaseRepository, clock: FakeClock
) -> None:
    claim = _claim(repo)
    assert repo.start_attempt(claim) is not None
    assert repo.recover_abandoned() == []
    clock.advance(seconds=DEFAULT_LEASE.total_seconds() + 1)
    assert repo.recover_abandoned() == [claim.execution_id]
    assert _execution_state(repo, claim.execution_id) == "NEEDS_REVIEW"


# --- Operator resolution ---------------------------------------------------------------


def _needs_review(repo: DatabaseRepository) -> Claim:
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    repo.record_attempt_result(claim, attempt, {})
    assert repo.release_execution(claim) == "NEEDS_REVIEW"
    return claim


def test_resolving_unknown_recipients(repo: DatabaseRepository, clock: FakeClock) -> None:
    claim = _needs_review(repo)
    resolve = repo.resolve_unknown

    assert resolve(claim.execution_id, A, Resolution.ACCEPTED, actor="op", reason="log") == (
        "NEEDS_REVIEW"
    )
    assert resolve(claim.execution_id, B, Resolution.RETRY, actor="op", reason="stopped") == (
        "RETRY_WAIT"
    )
    retry = _claim(repo, "owner-2")
    attempt = repo.start_attempt(retry)
    assert attempt is not None
    assert attempt.recipients == (B,)
    assert _recipients(repo, claim.execution_id)[B] == ("IN_FLIGHT", 2)

    events = repo.conn.execute(
        "SELECT actor, reason FROM execution_events WHERE kind = 'unknown_resolved'"
    ).fetchall()
    assert [tuple(e) for e in events] == [("op", "log"), ("op", "stopped")]


def test_resolution_is_refused_when_not_allowed(repo: DatabaseRepository, clock: FakeClock) -> None:
    claim = _needs_review(repo)
    with pytest.raises(ValueError, match="reason"):
        repo.resolve_unknown(claim.execution_id, A, Resolution.FAILED, actor="op", reason=" ")
    repo.resolve_unknown(claim.execution_id, A, Resolution.FAILED, actor="op", reason="bounce")
    with pytest.raises(ValueError, match="not UNKNOWN"):
        repo.resolve_unknown(claim.execution_id, A, Resolution.RETRY, actor="op", reason="again")
    assert _recipients(repo, claim.execution_id)[A] == ("PERMANENT_FAILURE", 1)

    repo.resolve_unknown(claim.execution_id, B, Resolution.RETRY, actor="op", reason="stopped")
    active = _claim(repo, "owner-2")
    assert repo.start_attempt(active) is not None
    with pytest.raises(ExecutionBusyError):
        repo.resolve_unknown(claim.execution_id, B, Resolution.FAILED, actor="op", reason="x")


# --- Versions, generations and identities ----------------------------------------------


def test_execution_keeps_its_version_and_generations_do_not_overlap(
    repo: DatabaseRepository,
) -> None:
    first = _claim(repo)
    repo.save_single_task(_task(subject="Changed"), reset_window_state=False)
    assert _claim(repo).task_version == first.task_version, "same execution, pinned version"

    repo.save_single_task(_task(subject="Changed again"))  # new generation
    assert _status(repo, "owner-2") == ClaimStatus.BUSY, "no parallel send across generations"
    repo.release_execution(first)
    second = _claim(repo, "owner-2")
    assert second.execution_id != first.execution_id
    assert second.task_version == first.task_version + 2


def test_recreated_task_is_claimed_as_a_new_identity(repo: DatabaseRepository) -> None:
    old = _claim(repo)
    repo.delete_mail_task("T1")
    repo.save_single_task(_task())
    new = _claim(repo, "owner-2")
    assert new.task_uid != old.task_uid
    with pytest.raises(StaleClaimError):
        repo.start_attempt(Claim(new.execution_id, old.task_uid, 1, "owner-1", 1))


# --- Transactions ----------------------------------------------------------------------


def test_no_transaction_stays_open_between_calls(repo: DatabaseRepository, db_path: Path) -> None:
    """T18 at the repository level: another process can write while SMTP would run."""
    claim = _claim(repo)
    attempt = repo.start_attempt(claim)
    assert attempt is not None
    assert not repo.conn.in_transaction

    with closing(sqlite3.connect(db_path, timeout=0.2)) as other:
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE report_settings SET sender = NULL")
        other.commit()


def test_write_transaction_refuses_to_nest(repo: DatabaseRepository) -> None:
    repo.conn.execute("UPDATE tasks SET subject = subject")
    assert repo.conn.in_transaction
    with pytest.raises(RuntimeError, match="already open"):
        _claim(repo)
    repo.conn.rollback()


def test_failed_operation_leaves_no_partial_write(repo: DatabaseRepository) -> None:
    claim = _claim(repo)
    repo.conn.execute("DROP INDEX idx_delivery_attempts_execution_id")
    repo.conn.execute(
        "CREATE TRIGGER fail_attempt BEFORE INSERT ON delivery_attempts "
        "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        repo.start_attempt(claim)
    assert {s for s, _ in _recipients(repo, claim.execution_id).values()} == {"PENDING"}
    assert _execution_state(repo, claim.execution_id) == "CLAIMED"


# --- Several processes -----------------------------------------------------------------


def test_concurrent_processes_claim_each_task_once(db_path: Path, tmp_path: Path) -> None:
    """T01 at the repository level: separate processes and connections, no SMTP."""
    task_ids = [f"T{n}" for n in range(8)]
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repository = DatabaseRepository(conn)
        repository.save_work_schedules(
            WorkSchedules(
                timezone="UTC",
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="06:00", end="18:00")])
                ],
            )
        )
        for task_id in task_ids:
            repository.save_single_task(_task(task_id))
        repository.start_run("run-1", "cli")

    # Every worker starts at a different task, so all of them collide on every task.
    orders = [task_ids[n:] + task_ids[:n] for n in range(6)]
    procs = spawn_claimers(db_path, orders, tmp_path)
    try:
        for proc in procs:
            proc.join(PROCESS_TIMEOUT)
            assert proc.exitcode == 0
    finally:
        for proc in procs:
            stop(proc)

    statuses = [json.loads(path.read_text()) for path in sorted(tmp_path.glob("worker-*.json"))]
    with closing(get_connection(db_path)) as conn:
        owners = dict(
            conn.execute(
                "SELECT i.task_id, e.owner FROM task_executions e "
                "JOIN task_identities i ON i.uid = e.task_uid"
            ).fetchall()
        )
    assert len(statuses) == len(orders)
    for task_id in task_ids:
        winners = [n for n, s in enumerate(statuses) if s[task_id] == "CLAIMED"]
        assert len(winners) == 1, (task_id, statuses)
        assert owners[task_id] == f"worker-{winners[0]}"
        assert all(s[task_id] in ("CLAIMED", "BUSY") for s in statuses)
