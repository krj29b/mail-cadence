[![CI](https://github.com/krj29b/mail-cadence/actions/workflows/ci.yml/badge.svg)](https://github.com/krj29b/mail-cadence/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)
[![Python versions](https://img.shields.io/pypi/pyversions/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)
[![License](https://img.shields.io/pypi/l/mail-cadence.svg)](https://github.com/TWOJ_LOGIN/mail-cadence/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/pypi/types/mail-cadence.svg)](https://pypi.org/project/mail-cadence/)

# MailCadence Engine

Production-grade, asynchronous bulk email dispatch and reporting engine built in **Python 3.12+**.

## Overview

`mail-cadence` is a high-performance email dispatch engine that utilizes SQLite as its primary database backend for runtime configurations and execution history logging, while retaining full XML support for importing and exporting configuration definitions (`tasks.xml` and `schedules.xml`). It evaluates active schedule windows across timezones, dispatches scheduled email tasks concurrently with rate limiting and exponential backoff retry resilience, and sends a final Task Completion Status Report email upon completion.

---

## Key Features

- **Python 3.12+ Standards:** Built with strict type safety, `asyncio.TaskGroup`, `StrEnum`, `pydantic v2`, and `pydantic-xml`.
- **SQLite State Persistence:** SQLite runtime database storing tasks, schedules, report settings, and detailed execution logs with Write-Ahead Logging (`WAL`) mode and busy timeout handling (`30,000 ms`).
- **Safe XML Import & Export:** Built-in `defusedxml` protection against XXE and entity expansion attacks, supporting bidirectional conversion between XML documents and SQLite data models.
- **Timezone-Aware Throttling & Idempotency:** Hourly schedule windows with `zoneinfo` time zone awareness, midnight-spanning time calculation, window-level idempotency protection (`last_status == SUCCESS`), and token bucket rate limiting.
- **Async SMTP Dispatch & Retries:** High-concurrency SMTP dispatch with `aiosmtplib`, `asyncio.Semaphore`, connection pooling, exponential backoff retries on transient errors, and `--dry-run` simulation mode.
- **Jinja2 Templating:** Templated task emails with path traversal defense and auto-escaped context variables, alongside consolidated HTML/plain-text batch summary reports.

---

## Prerequisites & Installation

### Requirements
- **Python >= 3.12**
- **pip** package manager

### Installation Instructions

Clone the repository and install `mail-cadence` along with its development dependencies:

```bash
# Install in editable mode with development dependencies
python3 -m pip install -e ".[dev]"
```

Or use the provided `Makefile`:

```bash
make install
```

---

## Configuration Reference

`mail-cadence` is configured through environment variables (or a `.env` file) for SMTP server options and database location, alongside XML or database settings. The `.env` file is read from the current directory, or from `MAIL_CADENCE_ENV_FILE` — set it explicitly for cron/systemd runs. Real environment variables take precedence over the file.

### Environment Variables (`Settings`)

Supported environment settings defined in `mail_cadence.models.Settings`:

| Variable | Default | Description |
| --- | --- | --- |
| `SMTP_HOST` | `localhost` | Hostname or IP address of the SMTP server |
| `SMTP_PORT` | `1025` | Port number for the SMTP server |
| `SMTP_USER` | *None* | Username for SMTP authentication (optional) |
| `SMTP_PASSWORD` | *None* | Password for SMTP authentication (optional). With `SMTP_USER` and `SMTP_PASSWORD` set, `SMTP_USE_TLS` or `SMTP_START_TLS` is required, see `SMTP_ALLOW_INSECURE_AUTH` |
| `SMTP_USE_TLS` | `false` | Enable direct TLS connection (`true`/`false`), usually port 465 |
| `SMTP_START_TLS` | `false` | Enable STARTTLS command upgrade (`true`/`false`), usually port 587 |
| `SMTP_ALLOW_INSECURE_AUTH` | `false` | Allow SMTP login without TLS/STARTTLS. Without it, a real run with a password but no encryption stops with exit code `1` (dry runs still work). Only for a trusted relay on the same machine or network |
| `SMTP_TIMEOUT` | `30.0` | Timeout in seconds for SMTP operations |
| `MAIL_CADENCE_DB` | `mail_cadence.db` | Path to SQLite database file |
| `MAIL_CADENCE_ENV_FILE` | `./.env` | Path to the `.env` file with these settings. When set, the file must exist. Without it, `.env` is read from the current directory (if present) |
| `MAIL_CADENCE_TEMPLATE_DIR` | `.` | Directory that task template paths are resolved against (same as `--template-dir`). Built-in templates are referenced as `builtin:<name>`, e.g. `builtin:daily_dispatch.html.j2` |
| `ADMIN_USER` | *None* | Web panel username. Without it (and `ADMIN_PASSWORD`) the panel refuses access unless started with `serve --insecure-no-auth` on a loopback host |
| `ADMIN_PASSWORD` | *None* | Web panel password |
| `ADMIN_LOGIN_MAX_FAILURES` | `10` | Wrong web panel logins allowed per client IP within the window; further attempts get `429 Too Many Requests` |
| `ADMIN_LOGIN_WINDOW_SECONDS` | `900` | Length of that window (sliding) in seconds |

### Database Upgrades

The database schema is versioned (`PRAGMA user_version`). Existing databases are upgraded automatically the first time a new version of `mail-cadence` opens them (any CLI command or `serve`); back up the `.db` file before upgrading. A database created by a newer version is refused (exit code `1`). Timestamps are stored as ISO 8601 in UTC.

### Task and Schedule Rules

- `priority` is `high`, `normal` or `low`; it only sets the start order (all eligible tasks run concurrently, limited per schedule window). `max_retries` is `0`–`10`.
- Every task needs at least one recipient (`To`, `Cc` or `Bcc`). Each entry is one address, bare or with a name (`Ann <ann@example.com>`).
- `schedule_ref` must name an existing schedule.
- Hour windows include both ends. To cover a whole day use `start="00:00" end="24:00"`; `23:59` leaves the last minute uncovered.
- Task templates are strict: a variable that is not defined (for example a typo, `{{ nme }}`) fails the task instead of rendering as empty text. Mark optional variables explicitly: `{{ region | default('everyone') }}` or `{% if region is defined %}...{% endif %}`.
- A task whose template renders no visible text (empty file, or only empty tags) fails with `Template rendering error` and nothing is sent. The panel preview shows the same error. An email made only of images therefore needs some text, for example `alt` text in a paragraph.

---

## CLI Usage Guide (`mail-cadence`)

The package provides the `mail-cadence` CLI executable (as well as `python -m mail_cadence.cli`).

### 1. Main Dispatch Command

Executes the main bulk mailer dispatch pipeline.

```bash
mail-cadence [OPTIONS]
```

#### Flags & Options
| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Optional path to tasks XML file to seed/upsert database |
| `--schedule-xml` | `-s` | `PATH` | *None* | Optional path to schedule XML file to seed/upsert database |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Path to SQLite database file (overrides `MAIL_CADENCE_DB`) |
| `--dry-run` | `-d` | `FLAG` | `false` | Execute workflow in simulation mode without sending network SMTP requests. Results are logged, but task window state is not updated |
| `--verbose` | `-v` | `FLAG` | `false` | Enable verbose debug logging output |
| `--template-dir` | | `PATH` | `.` | Directory that task template paths are resolved against (overrides `MAIL_CADENCE_TEMPLATE_DIR`). Set it explicitly when running from cron/systemd |

#### Exit Codes
- **`0`**: Successful execution or no tasks were eligible for processing.
- **`1`**: Configuration or database error encountered (e.g. invalid file format, database connection failure).
- **`2`**: One or more tasks failed or were delivered only partially (some recipients did not receive the message).
- **`3`**: The outcome of a delivery is unknown: the connection was lost after the message was sent, so the server may have accepted it. Such a delivery is never resent automatically; review it (see "Reviewing unknown deliveries" below).
- **`4`**: All executed tasks succeeded, but the status report email could not be sent.

Precedence when several apply: `3`, `1`, `2`, `4`. Until a window occurrence ends, every run in it reports the outcome of the task's execution in that occurrence again (`2` for an incomplete one, `3` for an unknown one), although nothing is sent again, so a monitoring alert stays on until the window is over or the execution is resolved.

#### Delivery guarantees

Before anything is sent, a run claims each task for its window occurrence in the database, so two runs (two timers, the CLI and the web panel, several hosts sharing the database) never send the same task at the same time. The message is rendered once and stored; every retry sends the same bytes. Each recipient's outcome is stored per attempt: a recipient accepted by the server or refused with a 5xx code is never sent the message again, a 4xx code is retried within `max_retries` (counted across runs), and no attempt starts after the window ends. A message is never sent twice automatically when its outcome is unknown. Details: [ADR 0001](docs/adr/0001-execution-state-model.md). These guarantees do not extend to the SMTP account as a whole: rate limits apply per window and per process.

#### Status report

When a run executed at least one task and report settings are configured, its report is rendered once and stored. A report that was not accepted is retried by the following runs from the same stored message, one attempt per run and at most three in total; a refusal with a 5xx code or an unknown outcome ends it. A failed report never resends task messages. Exit code `4` means that a report handled in this run (its own or an earlier one retried) is not confirmed.

Stored messages (task and report) are deleted 30 days after their delivery is over; the Message-ID, the per-recipient outcomes, the attempts and the event history are kept. A delivery that still needs review keeps its message.

#### Reviewing unknown deliveries

The web panel's **Executions** page lists every execution with its task version, window occurrence and recipient progress; "Needs review" filters the ones with an unknown outcome, and the dashboard shows how many there are. The page of an execution shows each recipient, every attempt and the event history. For a recipient in state `UNKNOWN`, check the SMTP server's log for the Message-ID shown on the page, then record a decision with a reason:

- **Delivered**: the server's log shows the message was accepted;
- **Not delivered**: the log shows it was not;
- **Send again** (check the event history first: a `late_result` event shows the server's reply that a worker received after its attempt had been recovered, and that this worker has finished the attempt): the next dispatch run sends the stored message again, if the same window occurrence is still open (once it has ended, no run picks the execution up again); first make sure the worker that made the attempt is no longer running, because nothing can stop an SMTP connection it still holds. For a manual send, use the **Run** button again instead.

Until every `UNKNOWN` recipient of an execution has a decision, the task is not sent at all: later window occurrences, a reset of the window state and the **Run** button skip it as `SKIPPED_BUSY` ("unknown outcome"), and each dispatch run that skips it exits with code `3`. A decision refers to the attempt shown on the page and is refused if a newer attempt exists. It is recorded with the panel user's name. The **Run** button on the Tasks page sends a task once per page load: clicking again reports the same send; reload the page to send it again.

#### Example Dispatch Commands
```bash
# Execute dispatch and seed XML configurations if database is empty or needs updates
mail-cadence --tasks-xml config/samples/tasks.xml --schedule-xml config/samples/schedules.xml

# Run dispatch in dry-run mode using an existing SQLite database
mail-cadence --db mail_cadence.db --dry-run --verbose
```

---

### 2. XML Import Command (`import-xml`)

Imports task definitions and schedule rules from XML configuration files into the SQLite database.

```bash
mail-cadence import-xml [OPTIONS]
```

#### Options
| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Path to tasks configuration XML file |
| `--schedule-xml` | `-s` | `PATH` | *None* | Path to schedule configuration XML file |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Target SQLite database file path |
| `--mode` | `-m` | `CHOICE` | `upsert` | Import mode: `upsert` (update/insert) or `replace` (clear and reload) |

#### Example
```bash
mail-cadence import-xml --tasks-xml config/samples/tasks.xml --schedule-xml config/samples/schedules.xml --mode upsert
```

---

### 3. XML Export Command (`export-xml`)

Exports current task definitions, schedules, and report settings from the SQLite database back into XML files.

```bash
mail-cadence export-xml [OPTIONS]
```

#### Options
| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Destination path for exported tasks XML |
| `--schedule-xml` | `-s` | `PATH` | *None* | Destination path for exported schedules XML |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Source SQLite database file path |

#### Example
```bash
mail-cadence export-xml --tasks-xml exported_tasks.xml --schedule-xml exported_schedules.xml
```

---

### 4. Web Panel (`serve`)

For a production setup (systemd units with sandboxing, a 10-minute dispatch timer, nginx with HTTPS, fail2ban), see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and the files in [`deploy/`](deploy).

```bash
mail-cadence serve --host 127.0.0.1 --port 8000
```

- Set `ADMIN_USER` and `ADMIN_PASSWORD`; without them every request gets `503`. `--insecure-no-auth` turns this off, and only works with a loopback `--host`.
- After `ADMIN_LOGIN_MAX_FAILURES` wrong logins (default 10) within `ADMIN_LOGIN_WINDOW_SECONDS` (default 15 minutes), a client IP gets `429` with a `Retry-After` header, even with the correct password, until its oldest failure is older than the window. Each block is logged as a warning (`Too many failed admin logins from <ip>`), which tools like fail2ban can watch. The counters live in memory, so they reset when the panel restarts.
- State-changing requests (`POST`, `PUT`, `DELETE`, ...) from other sites are rejected with `403`. The check compares the browser's `Origin` header with the request's `Host` header.
- The panel sends a strict `Content-Security-Policy` and other security headers, and loads no files from CDNs: scripts, styles and fonts are served by the app itself, so it also works without internet access. The template preview opens in a sandbox where scripts in a template cannot run.
- **Behind a reverse proxy** (nginx, Traefik, Caddy, ...), the proxy must pass the original `Host` header through, otherwise every save or dispatch fails with `403 Cross-site request rejected.` For nginx: `proxy_set_header Host $host;` (Caddy and Traefik keep it by default). The proxy must also send `X-Forwarded-For`: uvicorn uses it as the client IP only for proxies on `127.0.0.1`/`::1`, or those listed in the `FORWARDED_ALLOW_IPS` environment variable (e.g. `FORWARDED_ALLOW_IPS=10.0.0.5`). Otherwise every visitor appears as the proxy's IP, and the login limit blocks everyone at once. Serve the panel over HTTPS, because Basic Auth sends the password with every request.

## Development & Quality Gates

The project maintains strict code quality standards enforced via `Makefile` commands:

```bash
# Run unit and integration test suite with coverage
make test

# Run code style and linting checks (Ruff & Mypy)
make lint

# Run dead code detection (Vulture)
make dead-code

# Run code complexity analysis (Radon)
make complexity

# Run dependency vulnerability audit (pip-audit)
make audit

# Run all quality gate checks in sequence
make quality-gate

# Build the wheel and smoke-test it in a clean virtual environment
make smoke-test

# Rebuild the web panel CSS/JS/fonts after changing templates (needs Node.js)
make assets
```

The web panel's browser assets in `src/mail_cadence/web_static/` are committed, so installing or packaging `mail-cadence` never needs Node.js. After changing Tailwind classes in templates, icons (`<i data-lucide="...">`) or the versions in `frontend/package.json`, run `make assets` and commit the result; CI checks that the committed files match a fresh build.

CI (`.github/workflows/ci.yml`) runs the same checks on every push and pull request, plus tests on Linux and Windows with Python 3.12–3.14, a test run with the lowest dependency versions allowed by `pyproject.toml`, and the smoke test against the built wheel and sdist. Releases (`.github/workflows/publish.yml`) run the quality gates again, build with hash-pinned tools (`.github/requirements/release.txt`) and no caches, smoke-test, and publish to PyPI only when the release tag matches the `pyproject.toml` version; pre-releases go to TestPyPI. Workflows are checked with actionlint and [zizmor](https://docs.zizmor.sh) (`make workflows`); all actions are pinned to commit SHAs.

### Acceptance Tests for the Review Fixes

The tests in `../tests/test_required_fixes.py` (next to the review document, outside this repo) check the F2–F6 fixes. Run them from the repo root with this repo's virtual environment. They need `src` on `PYTHONPATH` and the repo path in `MAIL_CADENCE_REPO`:

```bash
MAIL_CADENCE_REPO="$PWD" PYTHONPATH=src .venv/Scripts/python -m pytest -q -p no:cacheprovider     --rootdir .. ../tests
```

On Linux/macOS use `.venv/bin/python`. In PowerShell set the variables first: `$env:MAIL_CADENCE_REPO = (Get-Location).Path; $env:PYTHONPATH = "src"`.

## When to Use `mail-cadence` (Practical Use Cases)

`mail-cadence` is designed for environments where plain `cron` scripts lack state tracking, rate control, and observability, while full distributed task queues (`Celery` + `Redis` + `PostgreSQL`) introduce unnecessary operational overhead.

* **Scheduled Infrastructure & Telemetry Digests:** Dispatch daily or shift-based operational reports (e.g., disk/log audits, security scan summaries, SCADA/IoT telemetry aggregations, or energy yield forecasts) strictly within designated time windows. Because window execution is idempotent, `mail-cadence` can be invoked repeatedly by `cron` or `systemd.timer` (e.g., every 10 minutes to wait for upstream data availability) while guaranteeing the report is sent **at most once** per active window.
* **SMTP Rate-Limit Protection (Paced Dispatch):** Standard corporate and ISP SMTP relays (such as Microsoft 365, Google Workspace, or local Postfix instances) enforce strict per-minute message caps and concurrent connection limits. `mail-cadence` paces outgoing batches using a per-window `TokenBucketRateLimiter` (`rate_limit_per_min`) and `asyncio.Semaphore` (`max_concurrency`), preventing `421`/`451` throttling errors and account lockouts.
* **Legacy System & ERP Email Gateway via XML:** Many industrial, billing, and legacy enterprise workflows export batch definitions as XML files to a shared directory rather than calling REST APIs. The `import-xml` (`upsert` / `replace`) command validates these definitions with `defusedxml` and `pydantic-xml` before syncing them into SQLite for scheduled delivery.
* **GitOps-Driven Internal Notifications:** Teams can version-control their communication schedules (`schedules.xml`) and task definitions (`tasks.xml`) in Git—enforcing business-hours-only delivery (`days_of_week="mon,tue,wed,thu,fri"`)—while operators use the built-in FastAPI + HTMX panel (`mail-cadence serve`) to preview Jinja2 templates, inspect execution logs, or trigger ad-hoc runs.
* **Zero-Broker Sidecar for Lightweight Applications:** Acts as a self-contained email subsystem backed by a single SQLite (WAL) file for on-premise or single-node deployments where data privacy and minimal RAM footprint are required.

---

## Programmatic Integration (Python API)

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

    # 2. Run the scheduler & SMTP dispatch pipeline (respects active HourWindows)
    #    (exit code: 0 = ok, 1 = configuration error, 2 = a task failed or was partial,
#     3 = outcome unknown, 4 = status report not sent)
    exit_code = await run_dispatch(DispatchOptions(db_path=DB_PATH), Settings.load())
    print(f"Dispatch finished with exit code: {exit_code}")


if __name__ == "__main__":
    asyncio.run(main())
```


---

## Architectural Deep Dive

For detailed technical specifications, database entity-relationship diagrams (ERD), scheduling algorithms, Mermaid state/sequence diagrams, and security mechanics, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
