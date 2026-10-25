import contextlib
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

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


class DatabaseRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

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
            self.conn.execute(
                """
                INSERT INTO schedules (id, description)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET description = excluded.description
                """,
                (sched.id, sched.description),
            )

            if mode == "upsert":
                self.conn.execute("DELETE FROM hour_windows WHERE schedule_id = ?", (sched.id,))

            hw_data = [
                (
                    sched.id,
                    hw.start,
                    hw.end,
                    hw.days_of_week,
                    hw.max_concurrency,
                    hw.rate_limit_per_min,
                    seq,
                )
                for seq, hw in enumerate(sched.allowed_hours)
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
        task_rows = self.conn.execute(
            """
            SELECT id, max_retries, priority, schedule_ref, sender, subject,
                    body_format, body_template_path
            FROM tasks
            ORDER BY id
            """
        ).fetchall()

        if not task_rows:
            return []

        rec_rows = self.conn.execute(
            """
            SELECT task_id, recipient_type, email
            FROM task_recipients
            ORDER BY task_id, seq, id
            """
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
            ORDER BY task_id, seq, id
            """
        ).fetchall()

        vars_by_task: dict[str, list[ContextVar]] = defaultdict(list)
        for v in var_rows:
            vars_by_task[v["task_id"]].append(ContextVar(name=v["var_name"], value=v["var_value"]))

        tasks: list[MailTask] = []
        for r in task_rows:
            task_id = r["id"]
            rec_data = recipients_by_task[task_id]
            tasks.append(
                MailTask(
                    id=task_id,
                    max_retries=r["max_retries"],
                    priority=r["priority"],
                    schedule_ref=r["schedule_ref"],
                    sender=r["sender"],
                    recipients=Recipients(
                        to=rec_data["to"],
                        cc=rec_data["cc"],
                        bcc=rec_data["bcc"],
                    ),
                    subject=r["subject"],
                    body_template=BodyTemplate(
                        format=r["body_format"], template_path=r["body_template_path"]
                    ),
                    context_variables=vars_by_task[task_id],
                )
            )

        return tasks

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

    def _save_mail_configuration(
        self,
        mail_config: MailConfiguration,
        mode: Literal["upsert", "replace"] = "upsert",
    ) -> None:
        self._save_report_settings(mail_config.report_settings)

        if mode == "replace":
            self.conn.execute("DELETE FROM task_context_variables")
            self.conn.execute("DELETE FROM task_recipients")
            self.conn.execute("DELETE FROM tasks")

        for task in mail_config.tasks:
            self._save_task(task, mode=mode)

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
        update_state: bool = True,
        executed_at: datetime | None = None,
    ) -> None:
        """Append an execution log entry and, unless disabled, update the task window state.

        Pass ``update_state=False`` for simulated (dry-run) executions so they never
        mark a task as dispatched in its active window. ``executed_at`` overrides the
        ``last_run`` timestamp (defaults to the current time).
        """
        failed_recipients_str = json.dumps(result.failed_recipients)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO execution_logs
                (batch_id, task_id, status, sent_count, failed_recipients,
                 duration_ms, error_details, attempt_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

            if update_state and result.status != TaskExecutionStatus.SKIPPED_SCHEDULE:
                run_at = executed_at or datetime.now(UTC)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=UTC)
                now_utc_str = run_at.astimezone(UTC).isoformat()
                self.conn.execute(
                    """
                    UPDATE tasks
                    SET last_run = ?,
                        last_status = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (now_utc_str, result.status.value, result.error_details, result.task_id),
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
        r = self.conn.execute(
            """
            SELECT id, max_retries, priority, schedule_ref, sender, subject,
                   body_format, body_template_path
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

        if not r:
            return None

        rec_rows = self.conn.execute(
            """
            SELECT recipient_type, email
            FROM task_recipients
            WHERE task_id = ?
            ORDER BY seq, id
            """,
            (task_id,),
        ).fetchall()

        rec_data: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
        for rec in rec_rows:
            rtype = rec["recipient_type"]
            if rtype in rec_data:
                rec_data[rtype].append(rec["email"])

        var_rows = self.conn.execute(
            """
            SELECT var_name, var_value
            FROM task_context_variables
            WHERE task_id = ?
            ORDER BY seq, id
            """,
            (task_id,),
        ).fetchall()

        context_vars = [ContextVar(name=v["var_name"], value=v["var_value"]) for v in var_rows]

        return MailTask(
            id=r["id"],
            max_retries=r["max_retries"],
            priority=r["priority"],
            schedule_ref=r["schedule_ref"],
            sender=r["sender"],
            recipients=Recipients(
                to=rec_data["to"],
                cc=rec_data["cc"],
                bcc=rec_data["bcc"],
            ),
            subject=r["subject"],
            body_template=BodyTemplate(
                format=r["body_format"], template_path=r["body_template_path"]
            ),
            context_variables=context_vars,
        )

    def save_single_task(self, task: MailTask) -> None:
        with self.conn:
            self._save_task(task, mode="upsert")
            self.conn.execute(
                """
                UPDATE tasks
                SET last_run = NULL,
                    last_status = NULL,
                    last_error = NULL
                WHERE id = ?
                """,
                (task.id,),
            )

    def delete_mail_task(self, task_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def save_single_schedule(self, schedule: Schedule) -> None:
        with self.conn:
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

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
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
            "       duration_ms, error_details, attempt_count, executed_at",
            "FROM execution_logs",
        ]
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

        if conditions:
            query.append("WHERE " + " AND ".join(conditions))

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

    def count_execution_logs(
        self,
        status_filter: str | None = None,
        task_id_filter: str | None = None,
        search_query: str | None = None,
    ) -> int:
        query = ["SELECT COUNT(*) FROM execution_logs"]
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

        if conditions:
            query.append("WHERE " + " AND ".join(conditions))

        row = self.conn.execute(" ".join(query), params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_dashboard_stats(self) -> dict[str, Any]:
        total_tasks = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        total_schedules = self.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        total_executions = self.conn.execute("SELECT COUNT(*) FROM execution_logs").fetchone()[0]

        status_rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM execution_logs GROUP BY status"
        ).fetchall()
        status_counts: dict[str, int] = {
            "SUCCESS": 0,
            "PARTIAL": 0,
            "FAILED": 0,
            "SKIPPED_SCHEDULE": 0,
        }
        for sr in status_rows:
            status_counts[sr["status"]] = sr["cnt"]

        tot_sent_row = self.conn.execute("SELECT SUM(sent_count) FROM execution_logs").fetchone()
        total_emails_sent = tot_sent_row[0] if tot_sent_row and tot_sent_row[0] is not None else 0

        avg_dur_row = self.conn.execute("SELECT AVG(duration_ms) FROM execution_logs").fetchone()
        avg_duration_ms = avg_dur_row[0] if avg_dur_row and avg_dur_row[0] is not None else 0.0

        return {
            "total_tasks": total_tasks,
            "total_schedules": total_schedules,
            "total_executions": total_executions,
            "success_count": status_counts.get("SUCCESS", 0),
            "partial_count": status_counts.get("PARTIAL", 0),
            "failed_count": status_counts.get("FAILED", 0),
            "skipped_count": status_counts.get("SKIPPED_SCHEDULE", 0),
            "status_counts": status_counts,
            "total_emails_sent": total_emails_sent,
            "avg_duration_ms": round(float(avg_duration_ms), 2),
        }
