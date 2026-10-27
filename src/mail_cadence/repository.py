import contextlib
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

from mail_cadence.database import sync_definitions, to_utc_iso
from mail_cadence.execution_store import Clock, ExecutionStoreMixin
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailConfiguration,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    TaskExecutionResult,
    TaskExecutionStatus,
    WorkSchedules,
)


class DatabaseRepository(ExecutionStoreMixin):
    """All database access. Execution-state methods come from ``ExecutionStoreMixin``.

    ``clock`` supplies the wall-clock time for leases and events (tests pass a fake).
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- WorkSchedules Operations ---

    def get_work_schedules(self) -> WorkSchedules:
        meta_row = self.conn.execute(
            "SELECT timezone FROM work_schedules_meta WHERE id = 1"
        ).fetchone()
        timezone = meta_row["timezone"] if meta_row else "UTC"

        sched_rows = self.conn.execute(
            "SELECT id, description FROM schedules ORDER BY id"
        ).fetchall()

        hw_rows = self.conn.execute(
            """
            SELECT schedule_id, start_time, end_time, days_of_week,
                   max_concurrency, rate_limit_per_min
            FROM hour_windows
            ORDER BY schedule_id, seq, id
            """
        ).fetchall()

        hw_by_schedule: dict[str, list[HourWindow]] = defaultdict(list)
        for hw in hw_rows:
            hw_by_schedule[hw["schedule_id"]].append(
                HourWindow(
                    start=hw["start_time"],
                    end=hw["end_time"],
                    days_of_week=hw["days_of_week"],
                    max_concurrency=hw["max_concurrency"],
                    rate_limit_per_min=hw["rate_limit_per_min"],
                )
            )

        schedules: list[Schedule] = [
            Schedule(
                id=s_row["id"],
                description=s_row["description"],
                allowed_hours=hw_by_schedule[s_row["id"]],
            )
            for s_row in sched_rows
        ]

        return WorkSchedules(timezone=timezone, schedules=schedules)

    def save_work_schedules(
        self, work_schedules: WorkSchedules, mode: Literal["upsert", "replace"] = "upsert"
    ) -> None:
        with self.conn:
            self._save_work_schedules(work_schedules, mode=mode)
            sync_definitions(self.conn)

    def _save_work_schedules(
        self, work_schedules: WorkSchedules, mode: Literal["upsert", "replace"] = "upsert"
    ) -> None:
        if mode == "replace":
            self.conn.execute("DELETE FROM hour_windows")
            self.conn.execute("DELETE FROM schedules")
            self.conn.execute("DELETE FROM work_schedules_meta")

        self.conn.execute(
            """
            INSERT INTO work_schedules_meta (id, timezone)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET timezone = excluded.timezone
            """,
            (work_schedules.timezone,),
        )

        for sched in work_schedules.schedules:
            self._save_schedule(sched)

    def _save_schedule(self, schedule: Schedule) -> None:
        """Upsert one schedule and replace its hour windows (no transaction of its own)."""
        self.conn.execute(
            """
            INSERT INTO schedules (id, description)
            VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET description = excluded.description
            """,
            (schedule.id, schedule.description),
        )
        self.conn.execute("DELETE FROM hour_windows WHERE schedule_id = ?", (schedule.id,))
        hw_data = [
            (
                schedule.id,
                hw.start,
                hw.end,
                hw.days_of_week,
                hw.max_concurrency,
                hw.rate_limit_per_min,
                seq,
            )
            for seq, hw in enumerate(schedule.allowed_hours)
        ]
        if hw_data:
            self.conn.executemany(
                """
                INSERT INTO hour_windows
                (schedule_id, start_time, end_time, days_of_week, max_concurrency,
                 rate_limit_per_min, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                hw_data,
            )

    # --- ReportSettings Operations ---

    def has_report_settings(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM report_settings WHERE id = 1").fetchone()
        return row is not None

    def get_report_settings(self) -> ReportSettings:
        row = self.conn.execute(
            """
            SELECT recipient, sender, subject_prefix, include_full_error_trace
            FROM report_settings WHERE id = 1
            """
        ).fetchone()

        if not row:
            return ReportSettings(
                recipient="admin@example.com",
                sender=None,
                subject_prefix="[MailCadence Report]",
                include_full_error_trace=True,
            )

        return ReportSettings(
            recipient=row["recipient"],
            sender=row["sender"],
            subject_prefix=row["subject_prefix"],
            include_full_error_trace=bool(row["include_full_error_trace"]),
        )

    def save_report_settings(self, settings: ReportSettings) -> None:
        with self.conn:
            self._save_report_settings(settings)

    def _save_report_settings(self, settings: ReportSettings) -> None:
        self.conn.execute(
            """
            INSERT INTO report_settings
            (id, recipient, sender, subject_prefix, include_full_error_trace)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                recipient = excluded.recipient,
                sender = excluded.sender,
                subject_prefix = excluded.subject_prefix,
                include_full_error_trace = excluded.include_full_error_trace
            """,
            (
                settings.recipient,
                settings.sender,
                settings.subject_prefix,
                1 if settings.include_full_error_trace else 0,
            ),
        )

    # --- MailConfiguration & MailTask Operations ---

    def get_mail_tasks(self) -> list[MailTask]:
        return self._load_tasks()

    def _load_tasks(self, task_id: str | None = None) -> list[MailTask]:
        """Load all tasks, or only ``task_id``, with three bulk queries (no N+1)."""
        params = (task_id, task_id)
        task_rows = self.conn.execute(
            """
            SELECT id, max_retries, priority, schedule_ref, sender, subject,
                   body_format, body_template_path
            FROM tasks
            WHERE (? IS NULL OR id = ?)
            ORDER BY id
            """,
            params,
        ).fetchall()
        if not task_rows:
            return []

        rec_rows = self.conn.execute(
            """
            SELECT task_id, recipient_type, email
            FROM task_recipients
            WHERE (? IS NULL OR task_id = ?)
            ORDER BY task_id, seq, id
            """,
            params,
        ).fetchall()
        recipients_by_task: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: {"to": [], "cc": [], "bcc": []}
        )
        for rec in rec_rows:
            rtype = rec["recipient_type"]
            if rtype in ("to", "cc", "bcc"):
                recipients_by_task[rec["task_id"]][rtype].append(rec["email"])

        var_rows = self.conn.execute(
            """
            SELECT task_id, var_name, var_value
            FROM task_context_variables
            WHERE (? IS NULL OR task_id = ?)
            ORDER BY task_id, seq, id
            """,
            params,
        ).fetchall()
        vars_by_task: dict[str, list[ContextVar]] = defaultdict(list)
        for v in var_rows:
            vars_by_task[v["task_id"]].append(ContextVar(name=v["var_name"], value=v["var_value"]))

        return [
            self._row_to_task(r, recipients_by_task[r["id"]], vars_by_task[r["id"]])
            for r in task_rows
        ]

    @staticmethod
    def _row_to_task(
        row: sqlite3.Row, recipients: dict[str, list[str]], context_vars: list[ContextVar]
    ) -> MailTask:
        return MailTask(
            id=row["id"],
            max_retries=row["max_retries"],
            priority=row["priority"],
            schedule_ref=row["schedule_ref"],
            sender=row["sender"],
            recipients=Recipients(to=recipients["to"], cc=recipients["cc"], bcc=recipients["bcc"]),
            subject=row["subject"],
            body_template=BodyTemplate(
                format=row["body_format"], template_path=row["body_template_path"]
            ),
            context_variables=context_vars,
        )

    def get_mail_configuration(self) -> MailConfiguration:
        report_settings = self.get_report_settings()
        tasks = self.get_mail_tasks()
        return MailConfiguration(report_settings=report_settings, tasks=tasks)

    def save_mail_configuration(
        self,
        mail_config: MailConfiguration,
        mode: Literal["upsert", "replace"] = "upsert",
    ) -> None:
        with self.conn:
            self._save_mail_configuration(mail_config, mode=mode)
            sync_definitions(self.conn)

    def _save_mail_configuration(
        self,
        mail_config: MailConfiguration,
        mode: Literal["upsert", "replace"] = "upsert",
    ) -> None:
        self._save_report_settings(mail_config.report_settings)

        if mode == "replace":
            self._delete_all_tasks()

        for task in mail_config.tasks:
            self._save_task(task, mode=mode)

    def import_configuration(
        self,
        mail_config: MailConfiguration,
        work_schedules: WorkSchedules,
        mode: Literal["upsert", "replace"] = "upsert",
    ) -> None:
        """Save schedules and tasks in a single transaction (all or nothing)."""
        with self.conn:
            if mode == "replace":
                # tasks.schedule_ref is ON DELETE RESTRICT: remove tasks before schedules.
                self._delete_all_tasks()
            self._save_work_schedules(work_schedules, mode=mode)
            self._save_mail_configuration(mail_config, mode=mode)
            # Identities follow task names, so a replace import keeps them (ADR 0001, D3).
            sync_definitions(self.conn)

    def _delete_all_tasks(self) -> None:
        self.conn.execute("DELETE FROM task_context_variables")
        self.conn.execute("DELETE FROM task_recipients")
        self.conn.execute("DELETE FROM tasks")

    def _save_task(
        self,
        task: MailTask,
        mode: Literal["upsert", "replace"] = "upsert",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO tasks
            (id, max_retries, priority, schedule_ref, sender, subject,
             body_format, body_template_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                max_retries = excluded.max_retries,
                priority = excluded.priority,
                schedule_ref = excluded.schedule_ref,
                sender = excluded.sender,
                subject = excluded.subject,
                body_format = excluded.body_format,
                body_template_path = excluded.body_template_path
            """,
            (
                task.id,
                task.max_retries,
                task.priority,
                task.schedule_ref,
                task.sender,
                task.subject,
                task.body_template.format,
                task.body_template.template_path,
            ),
        )

        if mode == "upsert":
            self.conn.execute("DELETE FROM task_recipients WHERE task_id = ?", (task.id,))
            self.conn.execute("DELETE FROM task_context_variables WHERE task_id = ?", (task.id,))

        # Insert recipients
        recipients_list = [
            (rtype, email)
            for rtype, emails in [
                ("to", task.recipients.to),
                ("cc", task.recipients.cc),
                ("bcc", task.recipients.bcc),
            ]
            for email in emails
        ]
        recipients_data = [
            (task.id, rtype, email, seq) for seq, (rtype, email) in enumerate(recipients_list)
        ]

        if recipients_data:
            self.conn.executemany(
                """
                INSERT INTO task_recipients
                (task_id, recipient_type, email, seq)
                VALUES (?, ?, ?, ?)
                """,
                recipients_data,
            )

        self._save_task_context_variables(task.id, task.context_variables)

    def _save_task_context_variables(self, task_id: str, context_vars: list[ContextVar]) -> None:
        context_vars_data = [
            (task_id, var.name, var.value, seq_var) for seq_var, var in enumerate(context_vars)
        ]
        if context_vars_data:
            self.conn.executemany(
                """
                INSERT INTO task_context_variables (task_id, var_name, var_value, seq)
                VALUES (?, ?, ?, ?)
                """,
                context_vars_data,
            )

    # --- Execution History / Logs ---

    def log_task_execution(
        self,
        result: TaskExecutionResult,
        batch_id: str | None = None,
        dry_run: bool = False,
        executed_at: datetime | None = None,
    ) -> None:
        """Append an execution log entry and update the task window state.

        Simulated executions (``dry_run=True``) are flagged in ``execution_logs`` and never
        update the task state, so they cannot mark a task as dispatched in its window.
        ``executed_at`` overrides the ``last_run`` timestamp (defaults to the current time).
        """
        failed_recipients_str = json.dumps(result.failed_recipients)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO execution_logs
                (batch_id, task_id, status, sent_count, failed_recipients,
                 duration_ms, error_details, attempt_count, dry_run, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    result.task_id,
                    result.status.value,
                    result.sent_count,
                    failed_recipients_str,
                    result.duration_ms,
                    result.error_details,
                    result.attempt_count,
                    int(dry_run),
                    to_utc_iso(datetime.now(UTC)),
                ),
            )

            if not dry_run and result.status != TaskExecutionStatus.SKIPPED_SCHEDULE:
                run_at = to_utc_iso(executed_at or datetime.now(UTC))
                self.conn.execute(
                    """
                    UPDATE tasks
                    SET last_run = ?,
                        last_status = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (run_at, result.status.value, result.error_details, result.task_id),
                )

    def get_task_execution_states(
        self,
    ) -> dict[str, tuple[datetime | None, TaskExecutionStatus | None]]:
        rows = self.conn.execute("SELECT id, last_run, last_status FROM tasks").fetchall()
        states: dict[str, tuple[datetime | None, TaskExecutionStatus | None]] = {}

        for r in rows:
            task_id = r["id"]
            raw_last_run = r["last_run"]
            raw_last_status = r["last_status"]

            last_run_dt: datetime | None = None
            if raw_last_run:
                try:
                    dt = datetime.fromisoformat(str(raw_last_run).replace("Z", "+00:00"))
                    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                    last_run_dt = dt
                except ValueError:
                    last_run_dt = None

            last_status: TaskExecutionStatus | None = None
            if raw_last_status:
                try:
                    last_status = TaskExecutionStatus(raw_last_status)
                except ValueError:
                    last_status = None

            states[task_id] = (last_run_dt, last_status)

        return states

    def get_execution_results_for_batch(self, batch_id: str) -> list[TaskExecutionResult]:
        rows = self.conn.execute(
            """
            SELECT task_id, status, sent_count, failed_recipients, duration_ms,
                   error_details, attempt_count
            FROM execution_logs
            WHERE batch_id = ?
            ORDER BY id
            """,
            (batch_id,),
        ).fetchall()

        results: list[TaskExecutionResult] = []
        for r in rows:
            failed_recipients = json.loads(r["failed_recipients"]) if r["failed_recipients"] else []
            results.append(
                TaskExecutionResult(
                    task_id=r["task_id"],
                    status=TaskExecutionStatus(r["status"]),
                    sent_count=r["sent_count"],
                    failed_recipients=failed_recipients,
                    duration_ms=r["duration_ms"],
                    error_details=r["error_details"],
                    attempt_count=r["attempt_count"],
                )
            )

        return results

    # --- Granular Task & Schedule Operations & Analytics ---

    def get_mail_task(self, task_id: str) -> MailTask | None:
        tasks = self._load_tasks(task_id)
        return tasks[0] if tasks else None

    def save_single_task(self, task: MailTask, reset_window_state: bool | None = None) -> bool:
        """Upsert a task and return whether its window state was reset.

        ``reset_window_state=None`` (default) resets ``last_run``/``last_status`` only when
        the task definition changed, so saving an unchanged task never re-enables a
        dispatch in the current window. Pass ``True``/``False`` to force either behaviour.
        A reset of an existing task also starts a new dispatch generation (ADR 0001, D6).
        """
        if reset_window_state is None:
            existing = self.get_mail_task(task.id)
            reset_window_state = existing is None or existing.model_dump() != task.model_dump()
        with self.conn:
            existed = self._task_exists(task.id)
            self._save_task(task, mode="upsert")
            sync_definitions(self.conn)
            if reset_window_state:
                self._reset_task_window_state(task.id)
                if existed:
                    self._start_dispatch_generation(task.id, reason="task_saved")
        return reset_window_state

    def reset_task_window_state(self, task_id: str) -> bool:
        """Allow a task to be dispatched again in its current window."""
        with self.conn:
            if not self._reset_task_window_state(task_id):
                return False
            self._start_dispatch_generation(task_id, reason="explicit_reset")
            return True

    def _task_exists(self, task_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row is not None

    def _start_dispatch_generation(self, task_id: str, reason: str) -> None:
        """Bump the live identity's generation and record why (no transaction of its own)."""
        self.conn.execute(
            """
            UPDATE task_identities SET dispatch_generation = dispatch_generation + 1
            WHERE task_id = ? AND deleted_at IS NULL
            """,
            (task_id,),
        )
        self.conn.execute(
            """
            INSERT INTO execution_events (occurred_at, kind, task_uid, reason, details_json)
            SELECT ?, 'generation_started', uid, ?, json_object('generation', dispatch_generation)
            FROM task_identities
            WHERE task_id = ? AND deleted_at IS NULL
            """,
            (to_utc_iso(datetime.now(UTC)), reason, task_id),
        )

    def _reset_task_window_state(self, task_id: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE tasks
            SET last_run = NULL,
                last_status = NULL,
                last_error = NULL
            WHERE id = ?
            """,
            (task_id,),
        )
        return cursor.rowcount > 0

    def delete_mail_task(self, task_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            sync_definitions(self.conn)
            return cursor.rowcount > 0

    def save_single_schedule(self, schedule: Schedule) -> None:
        with self.conn:
            self._save_schedule(schedule)
            sync_definitions(self.conn)

    def schedule_exists(self, schedule_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return row is not None

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            sync_definitions(self.conn)
            return cursor.rowcount > 0

    def get_recent_execution_logs(
        self,
        limit: int = 50,
        status_filter: str | None = None,
        task_id_filter: str | None = None,
        offset: int = 0,
        search_query: str | None = None,
    ) -> list[dict[str, Any]]:
        query = [
            "SELECT id, batch_id, task_id, status, sent_count, failed_recipients,",
            "       duration_ms, error_details, attempt_count, executed_at, dry_run",
            "FROM execution_logs",
        ]
        where, params = self._log_filter_clause(status_filter, task_id_filter, search_query)
        query.extend(where)

        query.append("ORDER BY id DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        rows = self.conn.execute(" ".join(query), params).fetchall()
        logs: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("failed_recipients"):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    d["failed_recipients"] = json.loads(d["failed_recipients"])
            logs.append(d)
        return logs

    @staticmethod
    def _log_filter_clause(
        status_filter: str | None, task_id_filter: str | None, search_query: str | None
    ) -> tuple[list[str], list[Any]]:
        """WHERE clause (as query parts) and its parameters for the execution log filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if status_filter and status_filter.strip():
            conditions.append("status = ?")
            params.append(status_filter.strip())
        if task_id_filter and task_id_filter.strip():
            conditions.append("task_id = ?")
            params.append(task_id_filter.strip())
        if search_query and search_query.strip():
            pattern = f"%{search_query.strip()}%"
            conditions.append(
                "(task_id LIKE ? OR batch_id LIKE ? OR COALESCE(error_details, '') LIKE ?)"
            )
            params.extend([pattern, pattern, pattern])
        where = ["WHERE " + " AND ".join(conditions)] if conditions else []
        return where, params

    def get_logged_task_ids(self) -> list[str]:
        """Task IDs that appear in the execution history (including deleted tasks)."""
        rows = self.conn.execute("SELECT DISTINCT task_id FROM execution_logs").fetchall()
        return [r["task_id"] for r in rows]

    def count_execution_logs(
        self,
        status_filter: str | None = None,
        task_id_filter: str | None = None,
        search_query: str | None = None,
    ) -> int:
        query = ["SELECT COUNT(*) FROM execution_logs"]
        where, params = self._log_filter_clause(status_filter, task_id_filter, search_query)
        query.extend(where)

        row = self.conn.execute(" ".join(query), params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_dashboard_stats(self) -> dict[str, Any]:
        """Aggregate execution statistics; dry-run entries are counted separately only."""
        total_tasks = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        total_schedules = self.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        total_executions = self.conn.execute(
            "SELECT COUNT(*) FROM execution_logs WHERE dry_run = 0"
        ).fetchone()[0]
        dry_run_count = self.conn.execute(
            "SELECT COUNT(*) FROM execution_logs WHERE dry_run = 1"
        ).fetchone()[0]

        status_rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM execution_logs WHERE dry_run = 0 GROUP BY status"
        ).fetchall()
        status_counts: dict[str, int] = {
            "SUCCESS": 0,
            "PARTIAL": 0,
            "FAILED": 0,
            "SKIPPED_SCHEDULE": 0,
        }
        for sr in status_rows:
            status_counts[sr["status"]] = sr["cnt"]

        tot_sent_row = self.conn.execute(
            "SELECT SUM(sent_count) FROM execution_logs WHERE dry_run = 0"
        ).fetchone()
        total_emails_sent = tot_sent_row[0] if tot_sent_row and tot_sent_row[0] is not None else 0

        avg_dur_row = self.conn.execute(
            "SELECT AVG(duration_ms) FROM execution_logs WHERE dry_run = 0"
        ).fetchone()
        avg_duration_ms = avg_dur_row[0] if avg_dur_row and avg_dur_row[0] is not None else 0.0

        return {
            "total_tasks": total_tasks,
            "total_schedules": total_schedules,
            "total_executions": total_executions,
            "dry_run_count": dry_run_count,
            "success_count": status_counts.get("SUCCESS", 0),
            "partial_count": status_counts.get("PARTIAL", 0),
            "failed_count": status_counts.get("FAILED", 0),
            "skipped_count": status_counts.get("SKIPPED_SCHEDULE", 0),
            "status_counts": status_counts,
            "total_emails_sent": total_emails_sent,
            "avg_duration_ms": round(float(avg_duration_ms), 2),
        }
