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
  - Public methods in `DatabaseRepository` (e.g., `save_report_settings`, `save_work_schedules`, `save_mail_configuration`) open a `with self.conn:` block and delegate SQL queries to private methods (`_save_report_settings`, `_save_work_schedules`, `_save_mail_configuration`, `_save_task`) which **do not use** `with self.conn:`.
  - Complex cross-module operations (e.g., `import_from_xml` in `xml_loader.py`) must open a single `with conn:` block at the highest level and call internal `_save_*` methods, guaranteeing a full `ROLLBACK` on error.
* **Foreign Key Constraints (`ON DELETE RESTRICT`):** The `tasks` table has a foreign key `schedule_ref` with the `ON DELETE RESTRICT` clause. In `mode="replace"`, always delete rows from `task_context_variables`, `task_recipients`, and `tasks` **before** deleting rows from `schedules`.
* **Ban on N+1 queries and execution loops:**
  - When reading collections (e.g., `get_mail_tasks`, `get_work_schedules`), fetch related records via a bulk `SELECT` query and group them in memory using `collections.defaultdict`. Do not execute `SELECT` queries inside `for` loops.
  - When writing collections (recipients, context variables, hour windows), always use `self.conn.executemany(...)` instead of `self.conn.execute(...)` in a loop.

## 3. SMTP Engine and Concurrency (`mailer.py`, `scheduler.py`, `cli.py`)
* **DRY Principle in `AsyncMailer`:** Management of the SMTP connection lifecycle (`_custom_smtp_client`, connection pool `self._pool`, single-use connection) must be implemented in a single place (e.g., via `@contextlib.asynccontextmanager`), without duplicating `try...except` blocks and `send_message` calls.
* **SMTP Error Classification and Retry:**
  - Error codes in the range `500 <= code < 600` are permanent errors and **are not subject** to retries.
  - Upon partial delivery (when some recipients receive the message while others return an error in the `errors` dictionary), filter out recipients with a permanent `5xx` error immediately and retry sending (`pending_recipients`) exclusively for recipients with transient errors (`4xx`).
* **Time and Clocks:**
  - For measuring task execution duration (`duration_ms`) and in `TokenBucketRateLimiter`, use exclusively a monotonic clock (`time.monotonic()`).
  - Execution timestamps (`last_run`, `start_time`, `end_time`) must always be stored as timezone-aware `datetime` objects (`datetime.now(UTC)` or `zoneinfo.ZoneInfo`).

## 4. Security (`models.py`, `xml_loader.py`, `mailer.py`)
* **XML Parsing:** Every XML file before being passed to `pydantic-xml` must be validated via `defusedxml.ElementTree` to protect against XXE and entity expansion attacks.
* **SMTP Header Injection Protection:** All fields going into message headers (`sender`, `recipient`, `subject`, `to`, `cc`, `bcc`) must be validated for the absence of `\r` and `\n` characters (`_check_no_newlines`).
* **Jinja2 Templates:** `TemplateRenderer` must validate paths using the `resolved_path.relative_to(base_dir)` method (Path Traversal protection) and keep autoescape enabled via `select_autoescape(enabled_extensions=AUTOESCAPE_EXTENSIONS, default_for_string=True)` (`mailer.AUTOESCAPE_EXTENSIONS`). Note that `select_autoescape(["html", "xml"])` alone does **not** cover compound extensions such as `*.html.j2`.
* **Dry-run:** Simulated executions must never update `tasks.last_run` / `tasks.last_status` (`log_task_execution(..., update_state=False)`).
* **Web panel:** Without `ADMIN_USER`/`ADMIN_PASSWORD` the panel refuses access (503) unless started with `serve --insecure-no-auth` on a loopback host. State-changing requests from other origins are rejected (`verify_same_origin`).
* **Secrets and Credentials:** Never store real passwords or `.env` files in the repository. In tests, use exclusively local mock/sink servers on `127.0.0.1` and dummy domains (`@example.com`, `@localhost`).

## 5. Strict Anti-Tampering & Test Authenticity Rules:
* DO NOT delete, modify, or weaken any existing test functions or `assert` statements in `tests/` unless an API signature explicitly changed.
* DO NOT mock `DatabaseRepository`, `TaskScheduler`, `TemplateRenderer`, or SQLite connections. Only external I/O (`aiosmtplib.SMTP`) may be mocked.
* Every new test must verify real state transitions (actual SQLite rows via `DatabaseRepository`, real rendered HTML/DOM elements, or actual parsed `EmailMessage` MIME parts)—never just `status_code == 200`.
* Do not introduce test-only branches or hardcoded test IDs inside `src/mail_cadence/`.
* Verify via `git diff tests/` before finishing that no existing assertions were removed.
