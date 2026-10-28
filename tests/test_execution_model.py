"""Reference model of the execution state (plan 7.4, ADR 0001), checked with Hypothesis.

``Model`` restates the rules of ADR 0001 (D7-D10) in plain Python, without SQLite and
without importing the implementation. ``ExecutionMachine`` drives the repository and
the model with the same random sequence of operations - claim, attempt, results
(accepted, 4xx, 5xx, unknown, missing), release, heartbeat, lease expiry, recovery,
edit, reset, next window, restart under a new owner, late results of old owners,
operator resolution of UNKNOWN recipients - and
after every step compares the returned values and the whole stored state with the
model. Safety invariants are also checked on the database directly.

The examples are derandomised, so a run is reproducible; a failure prints the
shrunk sequence of steps and a ``@reproduce_failure`` blob.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    multiple,
    rule,
)

from mail_cadence.database import get_connection, init_db
from mail_cadence.execution_store import (
    Attempt,
    Claim,
    ClaimRequest,
    ExecutionBusyError,
    Resolution,
    StaleClaimError,
)
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Outcome,
    RecipientResult,
    Recipients,
    Schedule,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository

A = "a@example.com"
B = "b@example.com"
RECIPIENTS = (A, B)
MAX_RETRIES = 1
LEASE_SECONDS = 300.0
OWNERS = ("worker-1", "worker-2", "worker-3")
KEY_KINDS = ("scheduled", "scheduled", "click-1", "click-2")
# 4xx is listed twice: it is what drives retries, the deepest part of the state machine.
OUTCOMES = (
    Outcome.ACCEPTED,
    Outcome.PERMANENT,
    Outcome.TRANSIENT,
    Outcome.TRANSIENT,
    Outcome.UNKNOWN,
    None,
)

OWNED = ("CLAIMED", "ACTIVE")
FINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "NEEDS_REVIEW", "EXPIRED")
EPOCH = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)


# --- The model -------------------------------------------------------------------------


@dataclass
class MRecipient:
    state: str = "PENDING"
    attempts: int = 0
    next_at: float | None = None
    budget: int = MAX_RETRIES + 1  # an operator's RETRY adds a fresh budget


@dataclass
class MExecution:
    id: int
    generation: int
    key: str
    version: int
    owner: str | None
    token: int
    lease_until: float | None
    state: str = "CLAIMED"
    recipients: dict[str, MRecipient] = field(
        default_factory=lambda: {r: MRecipient() for r in RECIPIENTS}
    )
    in_flight: dict[int, int] = field(default_factory=dict)  # attempt id -> token


class Model:
    """ADR 0001 in plain words for one task: scheduled window occurrences, one after the
    other, and manual sends with their own keys."""

    def __init__(self) -> None:
        self.clock = 0.0
        self.generation = 1
        self.version = 1
        self.window = 0
        self.executions: dict[int, MExecution] = {}
        self.next_attempt_id = 1

    def expired(self, e: MExecution) -> bool:
        return e.lease_until is None or e.lease_until <= self.clock

    def settle(self, e: MExecution) -> str:
        for r in e.recipients.values():
            if r.state == "IN_FLIGHT":
                r.state = "UNKNOWN"
        e.in_flight.clear()
        states = Counter(r.state for r in e.recipients.values())
        accepted = states["ACCEPTED"]
        if states["UNKNOWN"]:
            e.state = "NEEDS_REVIEW"
        elif states["PENDING"] or states["RETRY_WAIT"]:
            e.state = "RETRY_WAIT"
        elif accepted == len(e.recipients):
            e.state = "SUCCEEDED"
        elif accepted:
            e.state = "PARTIAL"
        else:
            e.state = "FAILED"
        e.owner, e.lease_until = None, None
        return e.state

    def blocker(self) -> int | None:
        """The execution with a live owner; an expired one is settled first."""
        for e in self.executions.values():
            if e.state in OWNED:
                if not self.expired(e):
                    return e.id
                self.settle(e)
        return None

    def unresolved(self, exclude: int | None) -> int | None:
        """The oldest other execution with a recipient whose outcome nobody knows."""
        return min(
            (
                e.id
                for e in self.executions.values()
                if e.id != exclude
                and any(r.state in ("UNKNOWN", "IN_FLIGHT") for r in e.recipients.values())
            ),
            default=None,
        )

    def blocked(self, exclude: int | None) -> tuple[str, int | None, int | None] | None:
        blocker = self.blocker()
        if blocker is not None:
            return "BUSY", blocker, None
        unresolved = self.unresolved(exclude)
        if unresolved is not None:
            return "REVIEW", unresolved, None
        return None

    def key(self, kind: str) -> str:
        return f"S1@w{self.window}" if kind == "scheduled" else f"manual:{kind}"

    def claim(self, owner: str, key: str) -> tuple[str, int | None, int | None]:
        """(status, execution id, fencing token) as claim_execution must answer."""
        current = next(
            (
                e
                for e in self.executions.values()
                if (e.generation, e.key) == (self.generation, key)
            ),
            None,
        )
        if current is None:
            return self.claim_new(owner, key)
        return self.claim_existing(owner, current)

    def claim_new(self, owner: str, key: str) -> tuple[str, int | None, int | None]:
        blocked = self.blocked(None)
        if blocked is not None:
            return blocked
        e = MExecution(
            id=len(self.executions) + 1,
            generation=self.generation,
            key=key,
            version=self.version,
            owner=owner,
            token=1,
            lease_until=self.clock + LEASE_SECONDS,
        )
        self.executions[e.id] = e
        return "CLAIMED", e.id, 1

    def claim_existing(self, owner: str, e: MExecution) -> tuple[str, int | None, int | None]:
        if e.state in FINAL:
            return "DONE", e.id, None
        if e.state in OWNED and not self.expired(e):
            return ("CLAIMED", e.id, e.token) if e.owner == owner else ("BUSY", e.id, None)
        if e.state in OWNED and self.settle(e) in FINAL:
            return "DONE", e.id, None
        blocked = self.blocked(e.id)
        if blocked is not None:
            return blocked
        e.token += 1
        e.owner, e.state = owner, "CLAIMED"
        e.lease_until = self.clock + LEASE_SECONDS
        return "CLAIMED", e.id, e.token

    def owns(self, claim: Claim, *, allow_expired: bool) -> MExecution | None:
        e = self.executions[claim.execution_id]
        ok = (
            e.state in OWNED
            and e.owner == claim.owner
            and e.token == claim.fencing_token
            and (allow_expired or not self.expired(e))
        )
        return e if ok else None

    def start_attempt(self, claim: Claim) -> tuple[str, ...] | StaleClaimError | None:
        e = self.owns(claim, allow_expired=False)
        if e is None:
            return StaleClaimError()
        due = tuple(
            addr
            for addr, r in e.recipients.items()
            if r.state in ("PENDING", "RETRY_WAIT")
            and r.attempts < r.budget
            and (r.next_at is None or r.next_at <= self.clock)
        )
        if not due:
            return None
        for addr in due:
            r = e.recipients[addr]
            r.state, r.attempts, r.next_at = "IN_FLIGHT", r.attempts + 1, None
        e.in_flight[self.next_attempt_id] = claim.fencing_token
        e.state = "ACTIVE"
        self.next_attempt_id += 1
        return due

    def record(
        self, claim: Claim, attempt: Attempt, results: dict[str, Outcome | None]
    ) -> StaleClaimError | None:
        e = self.owns(claim, allow_expired=True)
        if e is None or e.in_flight.get(attempt.attempt_id) != claim.fencing_token:
            return StaleClaimError()
        del e.in_flight[attempt.attempt_id]
        for addr in attempt.recipients:
            r = e.recipients[addr]
            outcome = results.get(addr)
            if outcome == Outcome.TRANSIENT:
                if r.attempts < r.budget:
                    r.state = "RETRY_WAIT"
                    r.next_at = self.clock + 0.5 * 2 ** max(0, r.attempts - 1)
                else:
                    r.state = "RETRY_EXHAUSTED"
            else:
                r.state = {
                    Outcome.ACCEPTED: "ACCEPTED",
                    Outcome.PERMANENT: "PERMANENT_FAILURE",
                }.get(outcome, "UNKNOWN")  # type: ignore[arg-type]
        return None

    def release(self, claim: Claim) -> str | StaleClaimError:
        e = self.owns(claim, allow_expired=True)
        return StaleClaimError() if e is None else self.settle(e)

    def heartbeat(self, owner: str) -> int:
        renewed = [e for e in self.executions.values() if e.state in OWNED and e.owner == owner]
        for e in renewed:
            e.lease_until = self.clock + LEASE_SECONDS
        return len(renewed)

    def resolve(
        self, execution_id: int, address: str, resolution: Resolution
    ) -> str | ExecutionBusyError:
        """D12: allowed once the owner is gone; RETRY adds a fresh budget."""
        e = self.executions[execution_id]
        if e.state in OWNED:
            if not self.expired(e):
                return ExecutionBusyError()
            self.settle(e)
        r = e.recipients[address]
        r.state = {
            Resolution.ACCEPTED: "ACCEPTED",
            Resolution.FAILED: "PERMANENT_FAILURE",
            Resolution.RETRY: "PENDING",
        }[resolution]
        r.next_at = None
        if resolution == Resolution.RETRY:
            r.budget += MAX_RETRIES + 1
        return self.settle(e)

    def recover(self) -> list[int]:
        expired = [e for e in self.executions.values() if e.state in OWNED and self.expired(e)]
        for e in expired:
            self.settle(e)
        return [e.id for e in expired]

    def snapshot(self) -> set[tuple[object, ...]]:
        return {
            (e.id, e.state, e.owner, e.token, e.version, e.generation, e.key)
            for e in self.executions.values()
        } | {
            (e.id, addr, r.state, r.attempts)
            for e in self.executions.values()
            for addr, r in e.recipients.items()
        }


# --- The machine -----------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> datetime:
        return EPOCH + timedelta(seconds=self.seconds)


def _task(subject: str) -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="cadence@example.com",
        subject=subject,
        max_retries=MAX_RETRIES,
        recipients=Recipients(to=list(RECIPIENTS)),
        body_template=BodyTemplate(template_path="builtin:x.html.j2"),
    )


class ExecutionMachine(RuleBasedStateMachine):
    claims = Bundle("claims")
    attempts = Bundle("attempts")

    def __init__(self) -> None:
        super().__init__()
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.clock = FakeClock()
        self.repo = DatabaseRepository(self.conn, clock=self.clock)
        self.repo.save_work_schedules(
            WorkSchedules(
                schedules=[
                    Schedule(id="S1", allowed_hours=[HourWindow(start="00:00", end="24:00")])
                ]
            )
        )
        self.repo.save_single_task(_task("v1"))
        self.repo.start_run("run", "cli")
        self.model = Model()
        self.edits = 1

    def teardown(self) -> None:
        self.conn.close()

    # Operations of workers.

    @rule(target=claims, owner=st.sampled_from(OWNERS), kind=st.sampled_from(KEY_KINDS))
    def claim(self, owner: str, kind: str) -> object:
        """A scheduled run of the current window occurrence, or a manual send."""
        key = self.model.key(kind)
        result = self.repo.claim_execution(ClaimRequest("T1", key, "run", owner))
        status, execution_id, token = self.model.claim(owner, key)
        assert (result.status.value, result.execution_id) == (status, execution_id)
        if result.claim is None:
            return multiple()
        assert result.claim.fencing_token == token
        return result.claim

    @rule(target=attempts, claim=claims)
    def start_attempt(self, claim: Claim) -> object:
        expected = self.model.start_attempt(claim)
        try:
            attempt = self.repo.start_attempt(claim)
        except StaleClaimError:
            assert isinstance(expected, StaleClaimError)
            return multiple()
        assert not isinstance(expected, StaleClaimError)
        if attempt is None:
            assert expected is None
            return multiple()
        assert attempt.recipients == expected
        return (claim, attempt)

    @rule(claim=claims, data=st.data())
    def full_round(self, claim: Claim, data: st.DataObject) -> None:
        """Start an attempt and store its results at once (reaches deep states sooner)."""
        expected = self.model.start_attempt(claim)
        try:
            attempt = self.repo.start_attempt(claim)
        except StaleClaimError:
            assert isinstance(expected, StaleClaimError)
            return
        assert not isinstance(expected, StaleClaimError)
        if attempt is None:
            assert expected is None
            return
        assert attempt.recipients == expected
        self.record((claim, attempt), data)

    @rule(claim=claims, data=st.data())
    def retry_after_backoff(self, claim: Claim, data: st.DataObject) -> None:
        """Let the backoff pass, then run a full round: makes second attempts common."""
        self.tick(1.0)
        self.full_round(claim, data)

    @rule(item=consumes(attempts), data=st.data())
    def record(self, item: tuple[Claim, Attempt], data: st.DataObject) -> None:
        claim, attempt = item
        outcomes = {r: data.draw(st.sampled_from(OUTCOMES)) for r in attempt.recipients}
        results = {r: RecipientResult(o, 250) for r, o in outcomes.items() if o is not None}
        expected = self.model.record(claim, attempt, outcomes)
        try:
            self.repo.record_attempt_result(claim, attempt, results)
        except StaleClaimError:
            assert isinstance(expected, StaleClaimError)
            return
        assert expected is None

    @rule(claim=claims)
    def release(self, claim: Claim) -> None:
        expected = self.model.release(claim)
        try:
            state = self.repo.release_execution(claim)
        except StaleClaimError:
            assert isinstance(expected, StaleClaimError)
            return
        assert state == expected

    @rule(owner=st.sampled_from(OWNERS))
    def heartbeat(self, owner: str) -> None:
        assert self.repo.heartbeat(owner) == self.model.heartbeat(owner)

    @rule(data=st.data())
    def resolve(self, data: st.DataObject) -> None:
        """An operator decides on one UNKNOWN recipient (D12)."""
        unknown = sorted(
            (e.id, addr)
            for e in self.model.executions.values()
            for addr, r in e.recipients.items()
            if r.state == "UNKNOWN"
        )
        if not unknown:
            return
        execution_id, address = data.draw(st.sampled_from(unknown))
        resolution = data.draw(st.sampled_from(list(Resolution)))
        expected = self.model.resolve(execution_id, address, resolution)
        try:
            state = self.repo.resolve_unknown(
                execution_id, address, resolution, actor="operator", reason="checked"
            )
        except ExecutionBusyError:
            assert isinstance(expected, ExecutionBusyError)
            return
        assert state == expected

    @rule()
    def recover(self) -> None:
        assert self.repo.recover_abandoned() == self.model.recover()

    # Time and configuration.

    @rule(seconds=st.sampled_from([0.5, 1.0, 1.0, 2.0, 5.0, 60.0, LEASE_SECONDS + 1]))
    def tick(self, seconds: float) -> None:
        self.clock.seconds += seconds
        self.model.clock += seconds

    @rule()
    def edit(self) -> None:
        """A changed definition: a new version and, with the default reset, a new generation."""
        self.edits += 1
        self.repo.save_single_task(_task(f"v{self.edits}"))
        self.model.version += 1
        self.model.generation += 1

    @rule()
    def reset_window(self) -> None:
        assert self.repo.reset_task_window_state("T1") is True
        self.model.generation += 1

    @rule()
    def next_window(self) -> None:
        self.model.window += 1

    # Checks after every step.

    @invariant()
    def matches_the_model(self) -> None:
        executions = {
            tuple(r)
            for r in self.conn.execute(
                "SELECT execution_id, state, owner, fencing_token, task_version, "
                "dispatch_generation, window_key FROM task_executions"
            )
        }
        recipients = {
            tuple(r)
            for r in self.conn.execute(
                "SELECT execution_id, envelope_address, state, attempt_count "
                "FROM execution_recipients"
            )
        }
        assert executions | recipients == self.model.snapshot()

    @invariant()
    def safety(self) -> None:
        """Checked on the database alone (I1, I4, I12)."""
        owned = self.conn.execute(
            "SELECT COUNT(*) FROM task_executions WHERE state IN ('CLAIMED', 'ACTIVE')"
        ).fetchone()[0]
        assert owned <= 1
        over_budget = self.conn.execute(
            "SELECT COUNT(*) FROM execution_recipients WHERE attempt_count > max_attempts"
        ).fetchone()[0]
        assert over_budget == 0
        # D12: while an outcome is unresolved, no other execution of the task sends.
        unresolved = {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT execution_id FROM execution_recipients WHERE state = 'UNKNOWN'"
            )
        }
        active = {
            r[0]
            for r in self.conn.execute(
                "SELECT execution_id FROM delivery_attempts WHERE state = 'IN_FLIGHT'"
            )
        }
        for execution_id in active:
            assert not unresolved - {execution_id}, (execution_id, unresolved)
        attempts_per_recipient: Counter[tuple[int, str]] = Counter()
        for execution_id, recipients_json in self.conn.execute(
            "SELECT execution_id, recipients_json FROM delivery_attempts"
        ):
            for address in json.loads(recipients_json):
                attempts_per_recipient[(execution_id, address)] += 1
        for execution_id, address, attempt_count in self.conn.execute(
            "SELECT execution_id, envelope_address, attempt_count FROM execution_recipients"
        ):
            assert attempts_per_recipient[(execution_id, address)] == attempt_count


ExecutionMachine.TestCase.settings = settings(
    max_examples=500,
    stateful_step_count=60,
    derandomize=True,
    deadline=None,
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
TestExecutionModel = ExecutionMachine.TestCase
