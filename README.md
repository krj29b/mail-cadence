[![CI](https://github.com/krj29b/mail-cadence/actions/workflows/ci.yml/badge.svg)](https://github.com/krj29b/mail-cadence/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)
[![Python versions](https://img.shields.io/pypi/pyversions/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)
[![License](https://img.shields.io/pypi/l/mail-cadence.svg)](https://github.com/krj29b/mail-cadence/blob/master/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/pypi/types/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)

# MailCadence Engine

Production-grade, asynchronous bulk email dispatch and reporting engine built in **Python 3.12+**.

## Overview

`mail-cadence` is a high-performance email dispatch engine that utilizes SQLite as its primary database backend for runtime configurations and execution history logging, while retaining full XML support for importing and exporting configuration definitions (`tasks.xml` and `schedules.xml`). It evaluates active schedule windows across timezones, dispatches scheduled email tasks concurrently with rate limiting and exponential backoff retry resilience, and sends a final Task Completion Status Report email upon completion.

## Key Features

- **Python 3.12+ Standards:** Built with strict type safety, `asyncio.TaskGroup`, `StrEnum`, `pydantic v2`, and `pydantic-xml`.
- **SQLite State Persistence:** SQLite runtime database storing tasks, schedules, report settings, and detailed execution logs with Write-Ahead Logging (`WAL`) mode and busy timeout handling (`30,000 ms`).
- **Safe XML Import & Export:** Built-in `defusedxml` protection against XXE and entity expansion attacks, supporting bidirectional conversion between XML documents and SQLite data models.
- **Timezone-Aware Throttling & Idempotency:** Hourly schedule windows with `zoneinfo` time zone awareness, midnight-spanning time calculation, a durable claim per task and window occurrence (one automatic send per window occurrence until the task is changed or reset, also across processes on one host; see [Delivery Guarantees](https://github.com/krj29b/mail-cadence/blob/master/docs/OPERATIONS.md#delivery-guarantees)), and token bucket rate limiting.
- **Async SMTP Dispatch & Retries:** High-concurrency SMTP dispatch with `aiosmtplib`, `asyncio.Semaphore`, connection pooling, per-recipient retries of transient errors, and `--dry-run` simulation mode. A delivery with an unknown outcome is never resent automatically.
- **Jinja2 Templating:** Templated task emails with path traversal defense and auto-escaped context variables, alongside consolidated HTML/plain-text batch summary reports.
- **Web Panel:** FastAPI + HTMX panel for tasks, schedules, template previews, execution history and operator review.

## Installation

Requires **Python >= 3.12**.

```bash
python3 -m pip install mail-cadence
```

For development (editable install, quality gates), see [docs/DEVELOPMENT.md](https://github.com/krj29b/mail-cadence/blob/master/docs/DEVELOPMENT.md).

## Quick Start

The sample configuration lives in [`config/samples/`](https://github.com/krj29b/mail-cadence/tree/master/config/samples) of the repository and uses the built-in templates.

```bash
# 1. Load tasks and schedules into a new database (mail_cadence.db)
mail-cadence import-xml --tasks-xml config/samples/tasks.xml --schedule-xml config/samples/schedules.xml

# 2. See what would be sent now, without sending anything
mail-cadence --dry-run --verbose

# 3. Send: set SMTP_HOST, SMTP_PORT and, for a login, SMTP_USER/SMTP_PASSWORD with TLS
mail-cadence

# 4. Open the web panel on http://127.0.0.1:8000
ADMIN_USER=admin ADMIN_PASSWORD='change-me' mail-cadence serve
```

Run step 3 from cron or a systemd timer, more often than the shortest window lasts: each run sends the tasks whose window is open and skips a task already sent in the current window occurrence, however often the command runs. Saving a changed task, **Allow Resend** and the **Run** button in the panel can send a task again; the exact guarantees and their limits are in [Delivery Guarantees](https://github.com/krj29b/mail-cadence/blob/master/docs/OPERATIONS.md#delivery-guarantees). The exit code tells the result (`0` ok, `1` configuration error, `2` incomplete delivery, `3` unknown outcome to review, `4` report not sent).

## Documentation

| Document | Contents |
| --- | --- |
| [Configuration](https://github.com/krj29b/mail-cadence/blob/master/docs/CONFIGURATION.md) | Environment variables, database upgrades, rules for tasks, schedules and templates |
| [CLI reference](https://github.com/krj29b/mail-cadence/blob/master/docs/CLI.md) | Every command and option, the web panel and its security behaviour |
| [Operations](https://github.com/krj29b/mail-cadence/blob/master/docs/OPERATIONS.md) | Exit codes, monitoring with `mail-cadence status`, delivery guarantees, status report retries, reviewing unknown deliveries |
| [Deployment](https://github.com/krj29b/mail-cadence/blob/master/docs/DEPLOYMENT.md) | systemd units, dispatch timer, nginx with HTTPS, fail2ban, backups |
| [Python API](https://github.com/krj29b/mail-cadence/blob/master/docs/PYTHON_API.md) | Updating tasks and running a dispatch from Python |
| [Architecture](https://github.com/krj29b/mail-cadence/blob/master/docs/ARCHITECTURE.md) | Modules, database schema, XML mapping, validation rules, internals |
| [Development](https://github.com/krj29b/mail-cadence/blob/master/docs/DEVELOPMENT.md) | Quality gates, CI and releases |

All documents are listed in [docs/README.md](https://github.com/krj29b/mail-cadence/blob/master/docs/README.md).

## When to Use `mail-cadence`

`mail-cadence` is designed for environments where plain `cron` scripts lack state tracking, rate control, and observability, while full distributed task queues (`Celery` + `Redis` + `PostgreSQL`) introduce unnecessary operational overhead.

* **Scheduled Infrastructure & Telemetry Digests:** Dispatch daily or shift-based operational reports (e.g., disk/log audits, security scan summaries, SCADA/IoT telemetry aggregations, or energy yield forecasts) within designated time windows. Each window occurrence is claimed in the database, so `mail-cadence` can run every few minutes from `cron` or a `systemd.timer` and a task is still sent once per window occurrence. The timer checks the schedule, not the source system: the data and templates must be ready when the window opens. A report that cannot be rendered fails for that window occurrence and is not retried when the data arrives later; edit the task or use **Allow Resend** to send it again.
* **SMTP Rate-Limit Protection (Paced Dispatch):** Standard corporate and ISP SMTP relays (such as Microsoft 365, Google Workspace, or local Postfix instances) enforce strict per-minute message caps and concurrent connection limits. `mail-cadence` paces delivery attempts per window occurrence using a `TokenBucketRateLimiter` (`rate_limit_per_min`) and an `asyncio.Semaphore` (`max_concurrency`). This lowers the risk of `421`/`451` throttling, but it is not a limit for the whole SMTP account: several windows, concurrent processes and other applications using the same account add up.
* **Legacy System & ERP Email Gateway via XML:** Many industrial, billing, and legacy enterprise workflows export batch definitions as XML files to a shared directory rather than calling REST APIs. The `import-xml` (`upsert` / `replace`) command validates these definitions with `defusedxml` and `pydantic-xml` before syncing them into SQLite for scheduled delivery.
* **GitOps-Driven Internal Notifications:** Teams can version-control their communication schedules (`schedules.xml`) and task definitions (`tasks.xml`) in Git (automatic sends start only on the listed days and hours, e.g. `days_of_week="mon,tue,wed,thu,fri"`; manual runs and status reports are not bound to windows) while operators use the built-in FastAPI + HTMX panel (`mail-cadence serve`) to preview Jinja2 templates, inspect execution logs, or trigger ad-hoc runs.
* **Zero-Broker Sidecar for Lightweight Applications:** Acts as a self-contained email subsystem backed by a single SQLite (WAL) file for on-premise or single-node deployments where data privacy and minimal RAM footprint are required.

## License

MIT, see [LICENSE](https://github.com/krj29b/mail-cadence/blob/master/LICENSE).
