"""Read-only views of the execution state for the web panel (ADR 0001, stage 6).

Plain bulk queries, no writes. ``DatabaseRepository`` inherits these methods.
"""

import json
import sqlite3
from typing import Any

Row = dict[str, Any]


class ExecutionViewsMixin:
    conn: sqlite3.Connection

    def list_executions(self, limit: int = 50, state: str | None = None) -> list[Row]:
        """Latest executions with recipient progress, newest first."""
        rows = self.conn.execute(
            """
            SELECT e.execution_id, i.task_id, e.task_version, e.dispatch_generation,
                   e.window_key, e.state, e.legacy, e.owner, e.created_at, e.finished_at,
                   COUNT(r.envelope_address) AS recipients,
                   COALESCE(SUM(r.state = 'ACCEPTED'), 0) AS accepted,
                   COALESCE(SUM(r.state = 'UNKNOWN'), 0) AS unknown,
                   (SELECT COUNT(*) FROM delivery_attempts AS a
                    WHERE a.execution_id = e.execution_id) AS attempts
            FROM task_executions AS e
            JOIN task_identities AS i ON i.uid = e.task_uid
            LEFT JOIN execution_recipients AS r ON r.execution_id = e.execution_id
            WHERE (? IS NULL OR e.state = ?)
            GROUP BY e.execution_id
            ORDER BY e.execution_id DESC
            LIMIT ?
            """,
            (state, state, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_executions_needing_review(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM task_executions WHERE state = 'NEEDS_REVIEW'"
        ).fetchone()
        return int(row[0])

    def count_unconfirmed_reports(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM report_deliveries "
            "WHERE state IN ('PENDING', 'RETRY_WAIT', 'IN_FLIGHT', 'UNKNOWN')"
        ).fetchone()
        return int(row[0])

    def latest_execution_by_task(self) -> dict[str, Row]:
        """The newest execution of every live task, with the task's current version."""
        rows = self.conn.execute(
            """
            SELECT i.task_id, e.execution_id, e.state, e.task_version, e.window_key,
                   e.dispatch_generation, i.dispatch_generation AS current_generation,
                   (SELECT MAX(version) FROM task_versions AS v
                    WHERE v.task_uid = i.uid) AS current_version
            FROM task_identities AS i
            JOIN task_executions AS e ON e.execution_id = (
                SELECT MAX(execution_id) FROM task_executions WHERE task_uid = i.uid
            )
            WHERE i.deleted_at IS NULL
            """
        ).fetchall()
        return {r["task_id"]: dict(r) for r in rows}

    def get_execution_detail(self, execution_id: int) -> Row | None:
        """Execution, its recipients (with the attempt to quote when resolving), its
        attempts and its events."""
        execution = self.conn.execute(
            """
            SELECT e.*, i.task_id, i.deleted_at AS task_deleted_at
            FROM task_executions AS e JOIN task_identities AS i ON i.uid = e.task_uid
            WHERE e.execution_id = ?
            """,
            (execution_id,),
        ).fetchone()
        if execution is None:
            return None
        detail = {k: execution[k] for k in execution.keys() if k != "message_bytes"}  # noqa: SIM118
        detail["has_message"] = execution["message_bytes"] is not None
        attempts = [
            dict(r)
            for r in self.conn.execute(
                "SELECT attempt_id, attempt_no, fencing_token, recipients_json, state, "
                "started_at, finished_at FROM delivery_attempts WHERE execution_id = ? "
                "ORDER BY attempt_no",
                (execution_id,),
            )
        ]
        last_attempt: dict[str, int] = {}
        for attempt in attempts:
            attempt["recipients"] = json.loads(attempt.pop("recipients_json"))
            for recipient in attempt["recipients"]:
                last_attempt[recipient] = attempt["attempt_id"]
        detail["attempts"] = attempts
        detail["recipients"] = [
            {**dict(r), "last_attempt_id": last_attempt.get(r["envelope_address"])}
            for r in self.conn.execute(
                "SELECT envelope_address, display_address, recipient_type, state, "
                "attempt_count, max_attempts, next_attempt_at, last_code, last_error, "
                "updated_at FROM execution_recipients WHERE execution_id = ? ORDER BY rowid",
                (execution_id,),
            )
        ]
        detail["events"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT occurred_at, kind, attempt_id, actor, reason, details_json "
                "FROM execution_events WHERE execution_id = ? ORDER BY event_id",
                (execution_id,),
            )
        ]
        return detail
