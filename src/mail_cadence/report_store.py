"""Durable status-report deliveries (docs/adr/0001-execution-state-model.md, D14).

A report is rendered once per run and stored in ``report_deliveries``. Every later run
retries the reports that are still unconfirmed, from the stored bytes, before it sends
its own. A failed report never resends task messages, and an unknown outcome is never
retried automatically. ``DatabaseRepository`` inherits these methods; each public one
is one ``BEGIN IMMEDIATE`` transaction and none runs during SMTP.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from mail_cadence.database import to_utc_iso
from mail_cadence.execution_store import DEFAULT_LEASE, ExecutionStoreMixin, StaleClaimError
from mail_cadence.models import Outcome, RecipientResult

REPORT_SENDABLE = ("PENDING", "RETRY_WAIT")


@dataclass(frozen=True, slots=True)
class ReportClaim:
    report_id: int
    owner: str
    fencing_token: int
    recipient: str
    data: bytes


def _report_state(result: RecipientResult, attempts: int, max_attempts: int) -> str:
    if result.outcome == Outcome.TRANSIENT:
        return "RETRY_WAIT" if attempts < max_attempts else "RETRY_EXHAUSTED"
    return {
        Outcome.ACCEPTED: "ACCEPTED",
        Outcome.PERMANENT: "PERMANENT_FAILURE",
        Outcome.UNKNOWN: "UNKNOWN",
    }[result.outcome]


class ReportStoreMixin(ExecutionStoreMixin):
    def _report_event(
        self, now: datetime, kind: str, report_id: int, details: dict[str, object]
    ) -> None:
        self.conn.execute(
            "INSERT INTO execution_events (occurred_at, kind, report_id, details_json) "
            "VALUES (?, ?, ?, ?)",
            (to_utc_iso(now), kind, report_id, json.dumps(details, sort_keys=True)),
        )

    def create_report(self, run_id: str, recipient: str, message_id: str, data: bytes) -> int:
        """Store this run's rendered report; it is sent through ``claim_reports``."""
        with self._write_transaction() as now:
            stamp = to_utc_iso(now)
            cursor = self.conn.execute(
                "INSERT INTO report_deliveries (run_id, recipient, message_id, message_bytes, "
                "state, created_at, updated_at) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)",
                (run_id, recipient, message_id, data, stamp, stamp),
            )
            report_id = int(cursor.lastrowid or 0)
            self._report_event(now, "report_created", report_id, {"run_id": run_id})
            return report_id

    def claim_reports(
        self, owner: str, report_ids: list[int] | None = None, lease: timedelta = DEFAULT_LEASE
    ) -> list[ReportClaim]:
        """Take the reports that may be sent now (all unconfirmed ones, or ``report_ids``).

        A report left in flight by a worker whose lease expired becomes UNKNOWN first.
        """
        with self._write_transaction() as now:
            self._recover_reports(now)
            rows = self.conn.execute(
                "SELECT report_id, owner, lease_expires_at, fencing_token, recipient, "
                "message_bytes FROM report_deliveries "
                "WHERE state IN ('PENDING', 'RETRY_WAIT') AND attempt_count < max_attempts "
                "AND message_bytes IS NOT NULL ORDER BY report_id"
            ).fetchall()
            claims = []
            for row in rows:
                if report_ids is not None and row["report_id"] not in report_ids:
                    continue
                if row["owner"] is not None and not self._lease_expired(row, now):
                    continue
                token = int(row["fencing_token"]) + 1
                self.conn.execute(
                    "UPDATE report_deliveries SET owner = ?, fencing_token = ?, "
                    "lease_expires_at = ? WHERE report_id = ?",
                    (owner, token, to_utc_iso(now + lease), row["report_id"]),
                )
                claims.append(
                    ReportClaim(
                        row["report_id"],
                        owner,
                        token,
                        row["recipient"],
                        bytes(row["message_bytes"]),
                    )
                )
            return claims

    def _recover_reports(self, now: datetime) -> None:
        rows = self.conn.execute(
            "SELECT report_id, lease_expires_at FROM report_deliveries WHERE state = 'IN_FLIGHT'"
        ).fetchall()
        for row in rows:
            if self._lease_expired(row, now):
                self.conn.execute(
                    "UPDATE report_deliveries SET state = 'UNKNOWN', owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE report_id = ?",
                    (to_utc_iso(now), row["report_id"]),
                )
                self._report_event(now, "report_recovered", row["report_id"], {"state": "UNKNOWN"})

    def _owned_report(self, claim: ReportClaim, *states: str) -> None:
        row = self.conn.execute(
            "SELECT state, owner, fencing_token FROM report_deliveries WHERE report_id = ?",
            (claim.report_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] not in states
            or row["owner"] != claim.owner
            or row["fencing_token"] != claim.fencing_token
        ):
            msg = f"Report {claim.report_id} is no longer owned by {claim.owner}"
            raise StaleClaimError(msg)

    def start_report_attempt(self, claim: ReportClaim) -> None:
        """Mark the report IN_FLIGHT and count the attempt; raises StaleClaimError."""
        with self._write_transaction() as now:
            self._owned_report(claim, *REPORT_SENDABLE)
            self.conn.execute(
                "UPDATE report_deliveries SET state = 'IN_FLIGHT', "
                "attempt_count = attempt_count + 1, updated_at = ? WHERE report_id = ?",
                (to_utc_iso(now), claim.report_id),
            )

    def record_report_result(self, claim: ReportClaim, result: RecipientResult) -> str:
        """Store the outcome of the attempt, release the report and return its state."""
        with self._write_transaction() as now:
            self._owned_report(claim, "IN_FLIGHT")
            row = self.conn.execute(
                "SELECT attempt_count, max_attempts FROM report_deliveries WHERE report_id = ?",
                (claim.report_id,),
            ).fetchone()
            state = _report_state(result, row["attempt_count"], row["max_attempts"])
            self.conn.execute(
                "UPDATE report_deliveries SET state = ?, owner = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL, last_code = ?, last_error = ?, updated_at = ? "
                "WHERE report_id = ?",
                (state, result.code, result.message, to_utc_iso(now), claim.report_id),
            )
            self._report_event(now, "report_attempt_finished", claim.report_id, {"state": state})
            return state

    def list_report_deliveries(self, limit: int = 20) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT report_id, run_id, recipient, state, attempt_count, max_attempts, "
            "last_code, last_error, created_at, updated_at FROM report_deliveries "
            "ORDER BY report_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
