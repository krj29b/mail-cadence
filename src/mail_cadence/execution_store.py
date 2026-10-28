"""Durable execution state (docs/adr/0001-execution-state-model.md): claim, attempts,
results, lease, recovery and operator resolution.

``DatabaseRepository`` inherits these methods. Every public method is one short
``BEGIN IMMEDIATE`` transaction opened by ``_write_transaction``; none of them may be
called while another transaction is open on the connection, and none runs during SMTP.
Writes made on behalf of a worker are compare-and-set on its owner and fencing token,
so a worker that lost its claim cannot change the state (``StaleClaimError``).
"""

import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr
from enum import StrEnum

from mail_cadence.database import to_utc_iso
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    MailTask,
    Outcome,
    RecipientResult,
    Recipients,
)

Clock = Callable[[], datetime]

DEFAULT_LEASE = timedelta(minutes=5)
# In-run backoff after a transient failure: 0.5 s, 1 s, 2 s, ... (ADR 0001, D9).
RETRY_BACKOFF_BASE_SECONDS = 0.5

OWNED_STATES = ("CLAIMED", "ACTIVE")
FINAL_STATES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "NEEDS_REVIEW", "EXPIRED"})
SENDABLE_STATES = frozenset({"PENDING", "RETRY_WAIT"})


class ClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"  # the caller owns the execution and may start attempts
    BUSY = "BUSY"  # another live owner holds an execution of this task
    # Another execution of this task has a recipient whose outcome is unknown; nothing of
    # the task is sent until an operator resolves it (D12).
    REVIEW = "REVIEW"
    DONE = "DONE"  # the execution for this key has already finished
    NO_TASK = "NO_TASK"  # no live task with this id


class Resolution(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    RETRY = "retry"


class StaleClaimError(RuntimeError):
    """The caller no longer owns the execution or the attempt; nothing was written."""


class ExecutionBusyError(RuntimeError):
    """The execution still has an owner with a valid lease."""


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    task_id: str
    window_key: str
    run_id: str
    owner: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    schedule_version: int | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    execution_id: int
    task_uid: int
    task_version: int
    owner: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: ClaimStatus
    execution_id: int | None = None
    claim: Claim | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What an execution achieved so far, read from its recipient rows."""

    execution_id: int
    state: str
    accepted: tuple[str, ...]
    failed: tuple[str, ...]
    unknown: tuple[str, ...]
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: int
    attempt_no: int
    recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Event:
    kind: str
    execution_id: int
    attempt_id: int | None = None
    reason: str | None = None
    actor: str | None = None
    details: Mapping[str, object] | None = None


def envelope_address(address: str) -> str:
    """Deduplication key: local part unchanged, domain lower-cased (ADR 0001, D9)."""
    addr = parseaddr(address)[1] or address.strip()
    local, at, domain = addr.rpartition("@")
    return f"{local}@{domain.lower()}" if at else addr


def derive_execution_state(counts: Mapping[str, int]) -> str:
    """Execution state from the number of recipients in each state (no IN_FLIGHT left)."""
    if counts.get("UNKNOWN") or counts.get("IN_FLIGHT"):
        return "NEEDS_REVIEW"
    if counts.get("PENDING") or counts.get("RETRY_WAIT"):
        return "RETRY_WAIT"
    accepted = counts.get("ACCEPTED", 0)
    if accepted and accepted == sum(counts.values()):
        return "SUCCEEDED"
    if accepted:
        return "PARTIAL"
    return "EXPIRED" if counts.get("WINDOW_EXPIRED") else "FAILED"


def task_from_definition(definition_json: str) -> MailTask:
    """Rebuild the task exactly as it was pinned (database.sync_definitions format)."""
    d = json.loads(definition_json)
    recipients: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
    for recipient_type, address in d["recipients"]:
        recipients[recipient_type].append(address)
    return MailTask(
        id=d["id"],
        max_retries=d["max_retries"],
        priority=d["priority"],
        schedule_ref=d["schedule_ref"],
        sender=d["sender"],
        subject=d["subject"],
        recipients=Recipients(**recipients),
        body_template=BodyTemplate(format=d["body_format"], template_path=d["body_template_path"]),
        context_variables=[ContextVar(name=n, value=v) for n, v in d["context_variables"]],
    )


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _backoff(attempt_count: int) -> timedelta:
    return timedelta(seconds=RETRY_BACKOFF_BASE_SECONDS * 2 ** max(0, attempt_count - 1))


def _next_recipient_state(result: RecipientResult, attempts: int, max_attempts: int) -> str:
    if result.outcome == Outcome.TRANSIENT:
        return "RETRY_WAIT" if attempts < max_attempts else "RETRY_EXHAUSTED"
    return {
        Outcome.ACCEPTED: "ACCEPTED",
        Outcome.PERMANENT: "PERMANENT_FAILURE",
        Outcome.UNKNOWN: "UNKNOWN",
    }[result.outcome]


class ExecutionStoreMixin:
    conn: sqlite3.Connection
    _clock: Clock

    # --- transaction and events --------------------------------------------------------

    @contextlib.contextmanager
    def _write_transaction(self) -> Iterator[datetime]:
        """BEGIN IMMEDIATE ... COMMIT; yields the time read after the write lock is held."""
        if self.conn.in_transaction:
            msg = "A transaction is already open on this connection"
            raise RuntimeError(msg)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._clock()
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _record_event(self, now: datetime, event: _Event) -> None:
        self.conn.execute(
            """
            INSERT INTO execution_events
            (occurred_at, kind, task_uid, execution_id, attempt_id, actor, reason, details_json)
            VALUES (?, ?, (SELECT task_uid FROM task_executions WHERE execution_id = ?),
                    ?, ?, ?, ?, ?)
            """,
            (
                to_utc_iso(now),
                event.kind,
                event.execution_id,
                event.execution_id,
                event.attempt_id,
                event.actor,
                event.reason,
                json.dumps(dict(event.details), sort_keys=True) if event.details else None,
            ),
        )

    # --- runs ---------------------------------------------------------------------------

    def start_run(self, run_id: str, source: str, *, dry_run: bool = False) -> None:
        with self._write_transaction() as now:
            self.conn.execute(
                "INSERT INTO dispatch_runs (run_id, source, dry_run, started_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, source, int(dry_run), to_utc_iso(now)),
            )

    def finish_run(
        self, run_id: str, exit_code: int, task_outcome: str, report_outcome: str | None
    ) -> None:
        with self._write_transaction() as now:
            self.conn.execute(
                "UPDATE dispatch_runs SET finished_at = ?, exit_code = ?, task_outcome = ?, "
                "report_outcome = ? WHERE run_id = ?",
                (to_utc_iso(now), exit_code, task_outcome, report_outcome, run_id),
            )

    # --- claim --------------------------------------------------------------------------

    def claim_execution(
        self, request: ClaimRequest, lease: timedelta = DEFAULT_LEASE
    ) -> ClaimResult:
        """Create or take over the execution of ``request.task_id`` for its window key.

        At most one execution per task identity is owned at a time, across generations,
        window keys and manual sends; an expired owner is recovered first (D8, D10).
        While another execution of the task has an UNKNOWN recipient, the answer is
        REVIEW: neither a new window, a new generation nor a manual send may bypass the
        operator's decision (D12).
        """
        with self._write_transaction() as now:
            identity = self.conn.execute(
                "SELECT uid, dispatch_generation FROM task_identities "
                "WHERE task_id = ? AND deleted_at IS NULL",
                (request.task_id,),
            ).fetchone()
            if identity is None:
                return ClaimResult(ClaimStatus.NO_TASK)
            row = self.conn.execute(
                "SELECT * FROM task_executions "
                "WHERE task_uid = ? AND dispatch_generation = ? AND window_key = ?",
                (identity["uid"], identity["dispatch_generation"], request.window_key),
            ).fetchone()
            if row is None:
                return self._claim_new(request, identity, now, lease)
            return self._claim_existing(request, row, now, lease)

    def _claim_new(
        self, request: ClaimRequest, identity: sqlite3.Row, now: datetime, lease: timedelta
    ) -> ClaimResult:
        blocked = self._blocked(identity["uid"], now, None)
        if blocked is not None:
            return blocked
        version = self.conn.execute(
            "SELECT version, definition_json FROM task_versions WHERE task_uid = ? "
            "ORDER BY version DESC LIMIT 1",
            (identity["uid"],),
        ).fetchone()
        cursor = self.conn.execute(
            """
            INSERT INTO task_executions
            (task_uid, dispatch_generation, window_key, task_version, schedule_version,
             window_start, window_end, run_id, state, owner, fencing_token, lease_expires_at,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED', ?, 1, ?, ?)
            """,
            (
                identity["uid"],
                identity["dispatch_generation"],
                request.window_key,
                version["version"],
                request.schedule_version,
                to_utc_iso(request.window_start) if request.window_start else None,
                to_utc_iso(request.window_end) if request.window_end else None,
                request.run_id,
                request.owner,
                to_utc_iso(now + lease),
                to_utc_iso(now),
            ),
        )
        execution_id = int(cursor.lastrowid or 0)
        self._insert_recipients(execution_id, version["definition_json"], now)
        self._record_event(now, _Event("claimed", execution_id=execution_id, actor=request.owner))
        claim = Claim(execution_id, identity["uid"], version["version"], request.owner, 1)
        return ClaimResult(ClaimStatus.CLAIMED, execution_id, claim)

    def _insert_recipients(self, execution_id: int, definition_json: str, now: datetime) -> None:
        definition = json.loads(definition_json)
        max_attempts = int(definition["max_retries"]) + 1
        rows: dict[str, tuple[object, ...]] = {}
        for recipient_type, address in definition["recipients"]:
            key = envelope_address(address)
            rows.setdefault(
                key,
                (execution_id, key, address, recipient_type, max_attempts, to_utc_iso(now)),
            )
        self.conn.executemany(
            """
            INSERT INTO execution_recipients
            (execution_id, envelope_address, display_address, recipient_type, state,
             max_attempts, updated_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            list(rows.values()),
        )

    def _claim_existing(
        self, request: ClaimRequest, row: sqlite3.Row, now: datetime, lease: timedelta
    ) -> ClaimResult:
        execution_id = int(row["execution_id"])
        if row["state"] in FINAL_STATES:
            return ClaimResult(ClaimStatus.DONE, execution_id)
        if row["state"] in OWNED_STATES:
            if not self._lease_expired(row, now):
                if row["owner"] != request.owner:
                    return ClaimResult(ClaimStatus.BUSY, execution_id)
                return ClaimResult(ClaimStatus.CLAIMED, execution_id, self._claim_of(row))
            if self._settle(execution_id, now, "lease_expired") in FINAL_STATES:
                return ClaimResult(ClaimStatus.DONE, execution_id)
        blocked = self._blocked(row["task_uid"], now, execution_id)
        if blocked is not None:
            return blocked
        token = int(row["fencing_token"]) + 1
        self.conn.execute(
            "UPDATE task_executions SET state = 'CLAIMED', owner = ?, fencing_token = ?, "
            "lease_expires_at = ?, run_id = ? WHERE execution_id = ?",
            (request.owner, token, to_utc_iso(now + lease), request.run_id, execution_id),
        )
        self._record_event(
            now,
            _Event(
                "claimed", execution_id=execution_id, actor=request.owner, details={"token": token}
            ),
        )
        claim = Claim(execution_id, row["task_uid"], row["task_version"], request.owner, token)
        return ClaimResult(ClaimStatus.CLAIMED, execution_id, claim)

    @staticmethod
    def _claim_of(row: sqlite3.Row) -> Claim:
        return Claim(
            row["execution_id"],
            row["task_uid"],
            row["task_version"],
            row["owner"],
            row["fencing_token"],
        )

    @staticmethod
    def _lease_expired(row: sqlite3.Row, now: datetime) -> bool:
        expires = _parse(row["lease_expires_at"])
        return expires is None or expires <= now

    def _owned_execution_of(self, task_uid: int, now: datetime) -> int | None:
        """The execution of ``task_uid`` with a live owner; an expired one is recovered."""
        row = self.conn.execute(
            "SELECT execution_id, lease_expires_at FROM task_executions "
            "WHERE task_uid = ? AND state IN ('CLAIMED', 'ACTIVE')",
            (task_uid,),
        ).fetchone()
        if row is None:
            return None
        if self._lease_expired(row, now):
            self._settle(int(row["execution_id"]), now, "lease_expired")
            return None
        return int(row["execution_id"])

    def _unresolved_execution_of(self, task_uid: int, exclude: int | None) -> int | None:
        """The oldest execution of ``task_uid`` (other than ``exclude``) with a recipient
        that is UNKNOWN or still IN_FLIGHT, that is, whose outcome nobody knows yet."""
        row = self.conn.execute(
            "SELECT MIN(e.execution_id) FROM task_executions e "
            "JOIN execution_recipients r ON r.execution_id = e.execution_id "
            "WHERE e.task_uid = ? AND e.execution_id IS NOT ? "
            "AND r.state IN ('UNKNOWN', 'IN_FLIGHT')",
            (task_uid, exclude),
        ).fetchone()
        return None if row[0] is None else int(row[0])

    def _blocked(self, task_uid: int, now: datetime, exclude: int | None) -> ClaimResult | None:
        """BUSY for a live owner, REVIEW for an unresolved outcome, None if free.

        The owner check runs first: settling an expired owner turns its in-flight
        recipients into UNKNOWN, which then blocks the claim as REVIEW.
        """
        owner = self._owned_execution_of(task_uid, now)
        if owner is not None:
            return ClaimResult(ClaimStatus.BUSY, owner)
        unresolved = self._unresolved_execution_of(task_uid, exclude)
        if unresolved is not None:
            return ClaimResult(ClaimStatus.REVIEW, unresolved)
        return None

    # --- settle and recover -----------------------------------------------------------

    def _settle(self, execution_id: int, now: datetime, reason: str) -> str:
        """Drop the owner and derive the state; in-flight recipients become UNKNOWN."""
        stamp = to_utc_iso(now)
        self.conn.execute(
            "UPDATE execution_recipients SET state = 'UNKNOWN', updated_at = ? "
            "WHERE execution_id = ? AND state = 'IN_FLIGHT'",
            (stamp, execution_id),
        )
        self.conn.execute(
            "UPDATE delivery_attempts SET state = 'UNKNOWN', finished_at = ? "
            "WHERE execution_id = ? AND state = 'IN_FLIGHT'",
            (stamp, execution_id),
        )
        counts = {
            r["state"]: r["n"]
            for r in self.conn.execute(
                "SELECT state, COUNT(*) AS n FROM execution_recipients "
                "WHERE execution_id = ? GROUP BY state",
                (execution_id,),
            )
        }
        state = derive_execution_state(counts)
        self.conn.execute(
            "UPDATE task_executions SET state = ?, owner = NULL, lease_expires_at = NULL, "
            "finished_at = ? WHERE execution_id = ?",
            (state, stamp if state in FINAL_STATES else None, execution_id),
        )
        self._record_event(
            now,
            _Event("settled", execution_id=execution_id, reason=reason, details={"state": state}),
        )
        return state

    def recover_abandoned(self) -> list[int]:
        """Settle every owned execution whose lease has expired (D10)."""
        with self._write_transaction() as now:
            rows = self.conn.execute(
                "SELECT execution_id, lease_expires_at FROM task_executions "
                "WHERE state IN ('CLAIMED', 'ACTIVE')"
            ).fetchall()
            expired = [int(r["execution_id"]) for r in rows if self._lease_expired(r, now)]
            for execution_id in expired:
                self._settle(execution_id, now, "lease_expired")
            return expired

    def heartbeat(self, owner: str, lease: timedelta = DEFAULT_LEASE) -> int:
        """Extend the lease of every execution ``owner`` still holds."""
        with self._write_transaction() as now:
            cursor = self.conn.execute(
                "UPDATE task_executions SET lease_expires_at = ? "
                "WHERE owner = ? AND state IN ('CLAIMED', 'ACTIVE')",
                (to_utc_iso(now + lease), owner),
            )
            return cursor.rowcount

    # --- attempts -----------------------------------------------------------------------

    def _owned_row(self, claim: Claim, now: datetime, *, allow_expired: bool) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM task_executions WHERE execution_id = ?", (claim.execution_id,)
        ).fetchone()
        if (
            not isinstance(row, sqlite3.Row)
            or row["state"] not in OWNED_STATES
            or row["owner"] != claim.owner
            or row["fencing_token"] != claim.fencing_token
            or (not allow_expired and self._lease_expired(row, now))
        ):
            msg = f"Execution {claim.execution_id} is no longer owned by {claim.owner}"
            raise StaleClaimError(msg)
        return row

    def _expire_if_window_closed(self, row: sqlite3.Row, now: datetime) -> bool:
        window_end = _parse(row["window_end"])
        if window_end is None or now <= window_end:
            return False
        self.conn.execute(
            "UPDATE execution_recipients SET state = 'WINDOW_EXPIRED', updated_at = ? "
            "WHERE execution_id = ? AND state IN ('PENDING', 'RETRY_WAIT')",
            (to_utc_iso(now), row["execution_id"]),
        )
        return True

    def start_attempt(self, claim: Claim) -> Attempt | None:
        """Mark the recipients due now as IN_FLIGHT and record the attempt.

        Returns None when no recipient may be sent now (all final, waiting for backoff,
        or the window has ended, which expires the rest). Raises StaleClaimError if the
        claim was lost or its lease has expired.
        """
        with self._write_transaction() as now:
            row = self._owned_row(claim, now, allow_expired=False)
            if self._expire_if_window_closed(row, now):
                return None
            due = [
                r["envelope_address"]
                for r in self.conn.execute(
                    "SELECT envelope_address, next_attempt_at FROM execution_recipients "
                    "WHERE execution_id = ? AND state IN ('PENDING', 'RETRY_WAIT') "
                    "AND attempt_count < max_attempts ORDER BY rowid",
                    (claim.execution_id,),
                )
                if (_parse(r["next_attempt_at"]) or now) <= now
            ]
            if not due:
                return None
            return self._begin_attempt(claim, due, now)

    def _begin_attempt(self, claim: Claim, recipients: list[str], now: datetime) -> Attempt:
        stamp = to_utc_iso(now)
        self.conn.executemany(
            "UPDATE execution_recipients SET state = 'IN_FLIGHT', "
            "attempt_count = attempt_count + 1, next_attempt_at = NULL, updated_at = ? "
            "WHERE execution_id = ? AND envelope_address = ?",
            [(stamp, claim.execution_id, r) for r in recipients],
        )
        attempt_no = int(
            self.conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM delivery_attempts "
                "WHERE execution_id = ?",
                (claim.execution_id,),
            ).fetchone()[0]
        )
        cursor = self.conn.execute(
            "INSERT INTO delivery_attempts (execution_id, attempt_no, fencing_token, "
            "recipients_json, state, started_at) VALUES (?, ?, ?, ?, 'IN_FLIGHT', ?)",
            (claim.execution_id, attempt_no, claim.fencing_token, json.dumps(recipients), stamp),
        )
        self.conn.execute(
            "UPDATE task_executions SET state = 'ACTIVE' WHERE execution_id = ?",
            (claim.execution_id,),
        )
        attempt = Attempt(int(cursor.lastrowid or 0), attempt_no, tuple(recipients))
        self._record_event(
            now,
            _Event(
                "attempt_started", execution_id=claim.execution_id, attempt_id=attempt.attempt_id
            ),
        )
        return attempt

    def record_attempt_result(
        self, claim: Claim, attempt: Attempt, results: Mapping[str, RecipientResult]
    ) -> None:
        """Store the outcome per recipient; a recipient without a result becomes UNKNOWN.

        Allowed after the lease expired as long as nobody recovered the attempt;
        otherwise StaleClaimError and nothing is written (late result, D8).
        """
        extra = set(results) - set(attempt.recipients)
        if extra:
            msg = f"Results for recipients outside the attempt: {sorted(extra)}"
            raise ValueError(msg)
        with self._write_transaction() as now:
            self._owned_row(claim, now, allow_expired=True)
            state = self.conn.execute(
                "SELECT state FROM delivery_attempts "
                "WHERE attempt_id = ? AND execution_id = ? AND fencing_token = ?",
                (attempt.attempt_id, claim.execution_id, claim.fencing_token),
            ).fetchone()
            if state is None or state["state"] != "IN_FLIGHT":
                msg = f"Attempt {attempt.attempt_id} is no longer in flight"
                raise StaleClaimError(msg)
            unknown = self._store_results(claim.execution_id, attempt, results, now)
            self.conn.execute(
                "UPDATE delivery_attempts SET state = ?, finished_at = ? WHERE attempt_id = ?",
                ("UNKNOWN" if unknown else "COMPLETED", to_utc_iso(now), attempt.attempt_id),
            )
            self._record_event(
                now,
                _Event(
                    "attempt_finished",
                    execution_id=claim.execution_id,
                    attempt_id=attempt.attempt_id,
                    details={r: res.outcome.value for r, res in results.items()},
                ),
            )

    def _store_results(
        self,
        execution_id: int,
        attempt: Attempt,
        results: Mapping[str, RecipientResult],
        now: datetime,
    ) -> bool:
        """Write recipient states; returns whether any outcome is unknown."""
        counters = {
            r["envelope_address"]: (r["attempt_count"], r["max_attempts"])
            for r in self.conn.execute(
                "SELECT envelope_address, attempt_count, max_attempts FROM execution_recipients "
                "WHERE execution_id = ? AND state = 'IN_FLIGHT'",
                (execution_id,),
            )
        }
        updates = []
        for recipient in attempt.recipients:
            result = results.get(recipient, RecipientResult(Outcome.UNKNOWN))
            attempts, max_attempts = counters[recipient]
            state = _next_recipient_state(result, attempts, max_attempts)
            retry_at = to_utc_iso(now + _backoff(attempts)) if state == "RETRY_WAIT" else None
            updates.append(
                (
                    state,
                    retry_at,
                    result.code,
                    result.message,
                    to_utc_iso(now),
                    execution_id,
                    recipient,
                )
            )
        self.conn.executemany(
            "UPDATE execution_recipients SET state = ?, next_attempt_at = ?, last_code = ?, "
            "last_error = ?, updated_at = ? WHERE execution_id = ? AND envelope_address = ?",
            updates,
        )
        return any(u[0] == "UNKNOWN" for u in updates)

    def pin_message(self, claim: Claim, message_id: str, data: bytes) -> bytes:
        """Store the rendered message once; every attempt sends the stored bytes (D7).

        Returns the stored message: the given one on the first call, the earlier one
        afterwards, so a template change between attempts has no effect.
        """
        with self._write_transaction() as now:
            row = self._owned_row(claim, now, allow_expired=False)
            if row["message_bytes"] is not None:
                return bytes(row["message_bytes"])
            self.conn.execute(
                "UPDATE task_executions SET message_id = ?, message_bytes = ? "
                "WHERE execution_id = ?",
                (message_id, data, claim.execution_id),
            )
            self._record_event(
                now,
                _Event("message_pinned", claim.execution_id, details={"message_id": message_id}),
            )
            return data

    def pinned_task(self, claim: Claim) -> MailTask:
        """The task definition this execution was claimed with (may differ from today's)."""
        row = self.conn.execute(
            "SELECT definition_json FROM task_versions WHERE task_uid = ? AND version = ?",
            (claim.task_uid, claim.task_version),
        ).fetchone()
        return task_from_definition(row["definition_json"])

    def pinned_message(self, execution_id: int) -> bytes | None:
        row = self.conn.execute(
            "SELECT message_bytes FROM task_executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return bytes(row[0]) if row is not None and row[0] is not None else None

    def abort_execution(self, claim: Claim, error: str) -> str:
        """End an execution that cannot send (e.g. its template does not render).

        Recipients not yet attempted fail permanently for this execution; a new
        generation of the task (edit or reset) is needed to send it again.
        """
        with self._write_transaction() as now:
            self._owned_row(claim, now, allow_expired=True)
            self.conn.execute(
                "UPDATE execution_recipients SET state = 'PERMANENT_FAILURE', last_error = ?, "
                "updated_at = ? WHERE execution_id = ? AND state IN ('PENDING', 'RETRY_WAIT')",
                (error, to_utc_iso(now), claim.execution_id),
            )
            return self._settle(claim.execution_id, now, "aborted")

    def execution_outcome(self, execution_id: int) -> ExecutionOutcome:
        state = self.conn.execute(
            "SELECT state FROM task_executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()[0]
        rows = self.conn.execute(
            "SELECT display_address, state, attempt_count, last_error FROM execution_recipients "
            "WHERE execution_id = ? ORDER BY rowid",
            (execution_id,),
        ).fetchall()
        by_state: dict[str, list[str]] = {"ACCEPTED": [], "UNKNOWN": [], "other": []}
        for r in rows:
            by_state.get(r["state"], by_state["other"]).append(r["display_address"])
        errors = [r["last_error"] for r in rows if r["last_error"]]
        return ExecutionOutcome(
            execution_id=execution_id,
            state=state,
            accepted=tuple(by_state["ACCEPTED"]),
            failed=tuple(by_state["other"]),
            unknown=tuple(by_state["UNKNOWN"]),
            attempts=int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM delivery_attempts WHERE execution_id = ?", (execution_id,)
                ).fetchone()[0]
            ),
            last_error=errors[-1] if errors else None,
        )

    def peek_claim(self, task_id: str, window_key: str) -> ClaimResult:
        """What claim_execution would answer now, without writing anything (dry runs)."""
        now = self._clock()
        identity = self.conn.execute(
            "SELECT uid, dispatch_generation FROM task_identities "
            "WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()
        if identity is None:
            return ClaimResult(ClaimStatus.NO_TASK)
        rows = self.conn.execute(
            "SELECT execution_id, state, lease_expires_at, "
            "dispatch_generation = ? AND window_key = ? AS same_key "
            "FROM task_executions WHERE task_uid = ?",
            (identity["dispatch_generation"], window_key, identity["uid"]),
        ).fetchall()
        for row in rows:
            if row["same_key"] and row["state"] in FINAL_STATES:
                return ClaimResult(ClaimStatus.DONE, int(row["execution_id"]))
            if row["state"] in OWNED_STATES and not self._lease_expired(row, now):
                return ClaimResult(ClaimStatus.BUSY, int(row["execution_id"]))
        same = next((int(r["execution_id"]) for r in rows if r["same_key"]), None)
        unresolved = self._unresolved_execution_of(identity["uid"], same)
        if unresolved is not None:
            return ClaimResult(ClaimStatus.REVIEW, unresolved)
        return ClaimResult(ClaimStatus.CLAIMED)

    def next_attempt_at(self, execution_id: int) -> datetime | None:
        """When the earliest waiting recipient becomes due, or None if none waits."""
        row = self.conn.execute(
            "SELECT MIN(next_attempt_at) FROM execution_recipients "
            "WHERE execution_id = ? AND state = 'RETRY_WAIT' AND attempt_count < max_attempts",
            (execution_id,),
        ).fetchone()
        return _parse(row[0])

    def release_execution(self, claim: Claim) -> str:
        """Give up ownership and return the derived state (RETRY_WAIT if work is left)."""
        with self._write_transaction() as now:
            row = self._owned_row(claim, now, allow_expired=True)
            self._expire_if_window_closed(row, now)
            return self._settle(claim.execution_id, now, "released")

    # --- operator -----------------------------------------------------------------------

    def resolve_unknown(
        self,
        execution_id: int,
        recipient: str,
        resolution: Resolution,
        *,
        actor: str,
        reason: str,
        attempt_id: int | None = None,
    ) -> str:
        """Resolve an UNKNOWN recipient (D12) and return the execution's new state.

        RETRY gives the recipient a fresh attempt budget; the operator must have made
        sure that the old worker is stopped. ``attempt_id`` is the attempt the operator
        looked at; if it is not the recipient's latest attempt, the decision is refused.
        Raises ExecutionBusyError while a live owner holds the execution, ValueError for
        a missing reason, a stale attempt or a recipient that is not UNKNOWN.
        """
        if not reason.strip():
            msg = "A reason is required to resolve an unknown delivery"
            raise ValueError(msg)
        with self._write_transaction() as now:
            row = self.conn.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                msg = f"Execution {execution_id} does not exist"
                raise ValueError(msg)
            if row["state"] in OWNED_STATES:
                if not self._lease_expired(row, now):
                    msg = f"Execution {execution_id} is still owned by {row['owner']}"
                    raise ExecutionBusyError(msg)
                self._settle(execution_id, now, "lease_expired")
            key = envelope_address(recipient)
            if attempt_id is not None:
                self._check_latest_attempt(execution_id, key, attempt_id)
            self._apply_resolution(row, key, resolution, now)
            self._record_event(
                now,
                _Event(
                    "unknown_resolved",
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    actor=actor,
                    reason=reason,
                    details={"recipient": recipient, "resolution": resolution.value},
                ),
            )
            return self._settle(execution_id, now, "resolved")

    def _check_latest_attempt(self, execution_id: int, recipient: str, attempt_id: int) -> None:
        latest = None
        for row in self.conn.execute(
            "SELECT attempt_id, recipients_json FROM delivery_attempts "
            "WHERE execution_id = ? ORDER BY attempt_no",
            (execution_id,),
        ):
            if recipient in json.loads(row["recipients_json"]):
                latest = row["attempt_id"]
        if latest != attempt_id:
            msg = (
                f"Attempt {attempt_id} is not the latest attempt for {recipient} "
                f"(latest: {latest}); reload the page"
            )
            raise ValueError(msg)

    def _apply_resolution(
        self, execution: sqlite3.Row, recipient: str, resolution: Resolution, now: datetime
    ) -> None:
        definition = self.conn.execute(
            "SELECT definition_json FROM task_versions WHERE task_uid = ? AND version = ?",
            (execution["task_uid"], execution["task_version"]),
        ).fetchone()
        budget = int(json.loads(definition["definition_json"])["max_retries"]) + 1
        new_state = {
            Resolution.ACCEPTED: "ACCEPTED",
            Resolution.FAILED: "PERMANENT_FAILURE",
            Resolution.RETRY: "PENDING",
        }[resolution]
        extra_attempts = budget if resolution == Resolution.RETRY else 0
        cursor = self.conn.execute(
            "UPDATE execution_recipients SET state = ?, max_attempts = max_attempts + ?, "
            "next_attempt_at = NULL, updated_at = ? "
            "WHERE execution_id = ? AND envelope_address = ? AND state = 'UNKNOWN'",
            (new_state, extra_attempts, to_utc_iso(now), execution["execution_id"], recipient),
        )
        if cursor.rowcount != 1:
            msg = f"Recipient {recipient} of execution {execution['execution_id']} is not UNKNOWN"
            raise ValueError(msg)
