# Architectural and Quality Guidelines (AGENTS.md)

This document defines absolute engineering rules for the `mail-cadence` project. Any code changes must comply with the following principles before task completion.

## 1. Mandatory Quality Gates
Before declaring code readiness, run all of the following tools. None of them may return errors or warnings:
1. `pytest` – all unit and integration tests must pass.
2. `mypy src tests` – full compliance with `strict = true` mode.
3. `ruff check src tests` and `ruff format --check src tests` – zero linter rule violations (including the `C901 <= 10` complexity limit).
4. `vulture src` – zero dead code, unused private methods, and local variables.
5. `radon cc src -a -nb` – no function or method may have a complexity rating worse than `B`.
6. `pip-audit` – no known vulnerabilities in dependencies.

## 2. SQLite Database Layer (`database.py`, `repository.py`, `xml_loader.py`)
* **Ban on nested `with self.conn:` blocks:** In the `sqlite3` library, exiting a `with conn:` block immediately performs a `COMMIT` across the entire connection.
  - Public methods in `DatabaseRepository` (e.g., `save_report_settings`, `save_work_schedules`, `save_mail_configuration`) open a `with self.conn:` block and delegate SQL queries to private methods (`_save_report_settings`, `_save_work_schedules`, `_save_schedule`, `_save_mail_configuration`, `_save_task`) which **do not use** `with self.conn:`. Reuse these helpers (and `_load_tasks` / `_row_to_task`, `_log_filter_clause`) instead of copying SQL between public methods.
  - Multi-step operations that must be atomic live in `DatabaseRepository` as public methods that open a single `with self.conn:` block and call the internal `_save_*` methods (e.g., `import_configuration`, used by `import_from_xml`), guaranteeing a full `ROLLBACK` on error.
  - Other modules use only the public API of `DatabaseRepository`: no calls to its private `_*` methods and no direct `repo.conn` queries.
* **Web dispatch:** Never run a dispatch inside an HTTP request. Start it through `DispatchRunner` (`dispatch_jobs.py`), which runs it in a worker thread and guarantees a single job at a time; jobs open their own SQLite connection.
* **Scheduler purity:** `TaskScheduler` works on `WorkSchedules` models only and must not import the database layer. Callers use its public API (`window_status`, `is_task_eligible`, `active_window`, `now`), never its private members.
* **Foreign Key Constraints (`ON DELETE RESTRICT`):** The `tasks` table has a foreign key `schedule_ref` with the `ON DELETE RESTRICT` clause. In `mode="replace"`, always delete rows from `task_context_variables`, `task_recipients`, and `tasks` **before** deleting rows from `schedules`.
* **Ban on N+1 queries and execution loops:**
  - When reading collections (e.g., `get_mail_tasks`, `get_work_schedules`), fetch related records via a bulk `SELECT` query and group them in memory using `collections.defaultdict`. Do not execute `SELECT` queries inside `for` loops.
  - When writing collections (recipients, context variables, hour windows), always use `self.conn.executemany(...)` instead of `self.conn.execute(...)` in a loop.

## 3. SMTP Engine and Concurrency (`mailer.py`, `scheduler.py`, `pipeline.py`)
* **Layering:** Dispatch orchestration lives only in `pipeline.py` (`run_dispatch`, `dispatch_single_task`). `cli.py` and `web.py` are thin adapters over it; `web.py` must not import `cli.py`. Keep `cli.run_dispatch_pipeline` signature-compatible (external callers and acceptance tests use it).
* **Exceptions:** Catch specific exceptions (`sqlite3.Error`, `ValidationError`, `XMLValidationError`, `OSError`, `aiosmtplib.SMTPException`, `jinja2.TemplateError`, ...). A bare `except Exception` is allowed only at a thread/job boundary (`DispatchRunner._run`) where any crash must be recorded.
* **SMTP client typing:** `AsyncMailer` works against the `mailer.SMTPClient` protocol, never `Any`; fakes in tests must match it.
* **Lint exemptions:** Do not add `per-file-ignores` for complexity rules (`C901`, `PLR0912`, `PLR0915`); split the function instead. The only source exemptions are `B008` in `web.py` (FastAPI `Depends`/`Query`) and the argument-count rules in `cli.py` (Typer commands).
* **Email construction:** Every outgoing message (tasks and the status report) is an `email.message.EmailMessage` built with `mailer.new_email_message` (standard headers) and `set_html_body`; do not use `email.mime.*` classes.
* **DRY Principle in `AsyncMailer`:** Management of the SMTP connection lifecycle (`_custom_smtp_client`, connection pool `self._pool`, single-use connection) must be implemented in a single place (e.g., via `@contextlib.asynccontextmanager`), without duplicating `try...except` blocks and `send_message` calls.
* **SMTP Error Classification and Retry:**
  - Error codes in the range `500 <= code < 600` are permanent errors and **are not subject** to retries.
  - Upon partial delivery (when some recipients receive the message while others return an error in the `errors` dictionary), filter out recipients with a permanent `5xx` error immediately and retry sending (`pending_recipients`) exclusively for recipients with transient errors (`4xx`).
* **Time and Clocks:**
  - For measuring task execution duration (`duration_ms`) and in `TokenBucketRateLimiter`, use exclusively a monotonic clock (`time.monotonic()`).
  - Execution timestamps (`last_run`, `start_time`, `end_time`) must always be stored as timezone-aware `datetime` objects (`datetime.now(UTC)` or `zoneinfo.ZoneInfo`).

## 4. Security (`models.py`, `xml_loader.py`, `mailer.py`)
* **XML Parsing:** Every XML file before being passed to `pydantic-xml` must be validated via `defusedxml.ElementTree` to protect against XXE and entity expansion attacks.
* **SMTP Header Injection Protection:** All fields going into message headers (`sender`, `recipient`, `subject`, `to`, `cc`, `bcc`) must be validated for the absence of `\r` and `\n` characters (`_check_no_newlines`); address fields use `_check_email_address`.
* **Model validation:** Constraints live in the Pydantic models (`EmailPriority`, `max_retries` `0..MAX_RETRIES_LIMIT`, at least one recipient, `HourWindow.end` up to `24:00`). When tightening a constraint that old rows could violate, add a new migration to `database.MIGRATIONS` that fixes existing rows so old databases keep loading. Web endpoints turn validation and integrity errors into `400`, never `500`.
* **Jinja2 Templates:** Task template paths resolve only inside `template_dir`, or in the package via the explicit `builtin:` prefix; never add fallback locations, and a missing template must raise `TemplateNotFound`. `TemplateRenderer` must validate paths using the `resolved_path.relative_to(base_dir)` method (Path Traversal protection) and keep autoescape enabled via `select_autoescape(enabled_extensions=AUTOESCAPE_EXTENSIONS, default_for_string=True)` (`mailer.AUTOESCAPE_EXTENSIONS`). Note that `select_autoescape(["html", "xml"])` alone does **not** cover compound extensions such as `*.html.j2`.
* **Dry-run:** Simulated executions must never update `tasks.last_run` / `tasks.last_status` (`log_task_execution(..., dry_run=True)`); they are flagged with `execution_logs.dry_run = 1` and excluded from dashboard statistics.
* **Window state on edit:** `save_single_task` resets `last_run`/`last_status` only when the task definition changed (or when `reset_window_state=True`). Re-sending in the same window otherwise requires the explicit `reset_task_window_state` action.
* **Schema changes:** Update `SCHEMA_SQL` (fresh databases) and append an idempotent migration to `database.MIGRATIONS` (existing databases); never edit or reorder released migrations. `PRAGMA user_version` tracks the applied count. Run `init_db` when a process opens the database (web: once in `create_app`), never per request.
* **Timestamps:** Store every timestamp with `database.to_utc_iso` (ISO 8601, UTC, explicit offset); never rely on SQLite `CURRENT_TIMESTAMP`.
* **Web panel:** Without `ADMIN_USER`/`ADMIN_PASSWORD` the panel refuses access (503) unless started with `serve --insecure-no-auth` on a loopback host. State-changing requests from other origins are rejected (`verify_same_origin`).
* **Secrets and Credentials:** Never store real passwords or `.env` files in the repository. In tests, use exclusively local mock/sink servers on `127.0.0.1` and dummy domains (`@example.com`, `@localhost`).
