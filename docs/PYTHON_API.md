# Programmatic Integration (Python API)

In addition to the CLI and XML workflows, external Python scripts (e.g., data pipelines, monitoring jobs, or forecasting engines) can dynamically inject or update task context variables in SQLite right before triggering the dispatch pipeline:

```python
import asyncio
from contextlib import closing
from datetime import date

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import BodyTemplate, ContextVar, MailTask, Recipients, Settings
from mail_cadence.pipeline import DispatchOptions, run_dispatch
from mail_cadence.repository import DatabaseRepository

DB_PATH = "mail_cadence.db"


def update_daily_report_task(metrics: dict[str, str]) -> None:
    """Upsert a task with fresh runtime metrics into SQLite."""
    with closing(get_connection(DB_PATH)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)

        task = MailTask(
            id="daily_system_digest",
            schedule_ref="morning_ops_window",
            priority="normal",
            max_retries=3,
            sender="ops-noreply@example.com",
            recipients=Recipients(to=["admin-team@example.com"]),
            subject=f"Daily System Digest — {date.today().isoformat()}",
            body_template=BodyTemplate(
                format="html",
                template_path="daily_digest.html.j2",
            ),
            context_variables=[
                ContextVar(name=key, value=val) for key, val in metrics.items()
            ],
        )
        # Keep "at most once per window": fresh metrics must not re-enable a dispatch
        # that already happened in the current window.
        repo.save_single_task(task, reset_window_state=False)


async def main() -> None:
    # 1. Inject dynamic values computed by an upstream pipeline
    update_daily_report_task(
        {
            "report_date": date.today().isoformat(),
            "disk_usage_pct": "68.4%",
            "anomalies_detected": "0",
        }
    )

    # 2. Run the scheduler & SMTP dispatch pipeline (respects active HourWindows).
    #    Exit codes as in the CLI, see OPERATIONS.md.
    exit_code = await run_dispatch(DispatchOptions(db_path=DB_PATH), Settings.load())
    print(f"Dispatch finished with exit code: {exit_code}")


if __name__ == "__main__":
    asyncio.run(main())
```

The exit codes are listed in [OPERATIONS.md](OPERATIONS.md#exit-codes).
