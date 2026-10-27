# Architectural and Quality Guidelines (AGENTS.md)

This document defines absolute engineering rules for the `mail-cadence` project. Any code changes must comply with the following principles before task completion.

## 1. Mandatory Quality Gates
Before declaring code readiness, run all of the following tools. None of them may return errors or warnings:
1. `pytest` – all unit and integration tests must pass, with at least 90% coverage (`make test`).
2. `mypy src tests scripts` – full compliance with `strict = true` mode.
3. `ruff check src tests scripts` and `ruff format --check src tests scripts` – zero linter rule violations (including the `C901 <= 10` complexity limit).
4. `vulture src` – zero dead code, unused private methods, and local variables.
5. `xenon --max-absolute B --max-modules B --max-average B src` – no function or method may have a complexity rating worse than `B` (`radon cc src -a -nb` only reports and never fails).
6. `pip-audit --skip-editable` – no known vulnerabilities in dependencies.

`make quality-gate` runs all of them; CI (`.github/workflows/ci.yml`) runs the same targets, the tests on Linux and Windows with Python 3.12–3.14 and with the lowest declared dependency versions, and `scripts/smoke_test_wheel.py` against the built wheel and sdist. In the weekly run and on demand (`workflow_dispatch`) the `race-stress` job runs the race and failure tests 50 times per OS through `scripts/stress_races.py`; a test that uses several processes, threads or held SMTP sessions belongs in its `RACE_TESTS`. Warnings are errors in tests: close every SQLite connection a test opens (`with closing(get_connection(...))`, a fixture with `yield`, or `request.addfinalizer`); Python 3.13+ reports a leaked one as a `ResourceWarning`. Before calling CI-relevant work done, run the suite on Linux and on Python 3.13/3.14 too (for example in Docker), not only on the local interpreter. When raising a dependency's minimum, keep `pyproject.toml` lower bounds honest: the lowest-versions job installs exactly those.

**GitHub Actions rules** (enforced by the zizmor job; run `make workflows` locally):
* Pin every action to a full commit SHA with a `# vX.Y.Z` comment; never a tag or branch.
* Every `actions/checkout` sets `persist-credentials: false`.
* Default `permissions: contents: read`; grant more only per job (`id-token: write` only on publish jobs).
* Never interpolate `${{ ... }}` from event data into `run:` scripts; pass it through `env:`.
* `publish.yml` (releases) and `_build.yml` use no caches. Keep the file name `publish.yml`: PyPI/TestPyPI Trusted Publishing is registered for it.
* Release builds use `--no-isolation` with `.github/requirements/release.txt` (`--require-hashes`); regenerate it with the command in `release.in`.

## 2. SQLite Database Layer (`database.py`, `repository.py`, `xml_loader.py`)
* **Ban on nested `with self.conn:` blocks:** In the `sqlite3` library, exiting a `with conn:` block immediately performs a `COMMIT` across the entire connection.
  - Public methods in `DatabaseRepository` (e.g., `save_report_settings`, `save_work_schedules`, `save_mail_configuration`) open a `with self.conn:` block and delegate SQL queries to private methods (`_save_report_settings`, `_save_work_schedules`, `_save_schedule`, `_save_mail_configuration`, `_save_task`) which **do not use** `with self.conn:`. Reuse these helpers (and `_load_tasks` / `_row_to_task`, `_log_filter_clause`) instead of copying SQL between public methods.
  - Multi-step operations that must be atomic live in `DatabaseRepository` as public methods that open a single `with self.conn:` block and call the internal `_save_*` methods (e.g., `import_configuration`, used by `import_from_xml`), guaranteeing a full `ROLLBACK` on error.
  - Other modules use only the public API of `DatabaseRepository`: no calls to its private `_*` methods and no direct `repo.conn` queries.
  - Execution state (`execution_store.py`, inherited by `DatabaseRepository`; ADR 0001) uses `with self._write_transaction():` (`BEGIN IMMEDIATE` ... `COMMIT`) instead of `with self.conn:`, so two processes cannot both read "not sent" and claim. A public method opens exactly one of the two, never both and never nested; `_write_transaction` refuses to start inside an open transaction. Writes on behalf of a worker are compare-and-set on its owner and fencing token. No transaction may be open during SMTP, backoff or a rate-limiter wait.
  - Every repository method that writes tasks or schedules calls `database.sync_definitions` inside its transaction, so task identities and versions stay in step.
* **Web dispatch:** Never run a dispatch inside an HTTP request. Start it through `DispatchRunner` (`dispatch_jobs.py`), which runs it in a worker thread and guarantees a single job at a time per process; jobs open their own SQLite connection. Protection against parallel sends across processes and hosts is the execution claim in the database (ADR 0001), never `DispatchRunner` or `last_status`.
* **Scheduler purity:** `TaskScheduler` works on `WorkSchedules` models only and must not import the database layer. Callers use its public API (`window_instance`, `has_schedule`, `window_status`, `is_task_eligible`, `active_window`, `now`), never its private members. Dispatch eligibility uses `window_instance` (UTC instants, ADR 0001 D5); `window_status` only serves the panel's display.
* **Foreign Key Constraints (`ON DELETE RESTRICT`):** The `tasks` table has a foreign key `schedule_ref` with the `ON DELETE RESTRICT` clause. In `mode="replace"`, always delete rows from `task_context_variables`, `task_recipients`, and `tasks` **before** deleting rows from `schedules`.
* **Ban on N+1 queries and execution loops:**
  - When reading collections (e.g., `get_mail_tasks`, `get_work_schedules`), fetch related records via a bulk `SELECT` query and group them in memory using `collections.defaultdict`. Do not execute `SELECT` queries inside `for` loops.
  - When writing collections (recipients, context variables, hour windows), always use `self.conn.executemany(...)` instead of `self.conn.execute(...)` in a loop.

## 3. SMTP Engine and Concurrency (`mailer.py`, `scheduler.py`, `pipeline.py`)
* **Layering:** Dispatch orchestration lives only in `pipeline.py` (`run_dispatch`, `dispatch_single_task`). `cli.py` and `web.py` are thin adapters over it; `web.py` must not import `cli.py`. Keep `cli.run_dispatch_pipeline` signature-compatible (external callers and acceptance tests use it).
* **Exceptions:** Catch specific exceptions (`sqlite3.Error`, `ValidationError`, `XMLValidationError`, `OSError`, `aiosmtplib.SMTPException`, `jinja2.TemplateError`, ...). A bare `except Exception` is allowed only at a thread/job boundary (`DispatchRunner._run`) where any crash must be recorded.
* **SMTP client typing:** `AsyncMailer` works against the `mailer.SMTPClient` protocol, never `Any`; fakes in tests must match it. The protocol includes `mail`, `rcpt`, `data` and `rset`: `AsyncMailer.deliver` drives the transaction step by step so the phase of a failure is known (ADR 0001, D2). `deliver` never retries; a failure after DATA has started without a final reply is `Outcome.UNKNOWN` and is never resent automatically.
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
* **Jinja2 Templates:** Task template paths resolve only inside `template_dir`, or in the package via the explicit `builtin:` prefix; never add fallback locations, and a missing template must raise `TemplateNotFound`. `TemplateRenderer` must validate paths using the `resolved_path.relative_to(base_dir)` method (Path Traversal protection) and keep autoescape enabled via `select_autoescape(enabled_extensions=AUTOESCAPE_EXTENSIONS, default_for_string=True)` (`mailer.AUTOESCAPE_EXTENSIONS`). Note that `select_autoescape(["html", "xml"])` alone does **not** cover compound extensions such as `*.html.j2`. Task templates use `undefined=StrictUndefined` and are rendered through `TemplateRenderer.render_task_body`, which raises `EmptyBodyError` when no visible text is left; never send a body that bypasses this check. Built-in templates must handle optional variables with `default(...)` or `is defined`.
* **Dry-run:** Simulated executions must never update `tasks.last_run` / `tasks.last_status` (`log_task_execution(..., dry_run=True)`); they are flagged with `execution_logs.dry_run = 1` and excluded from dashboard statistics. A dry run never writes `dispatch_runs`, `task_executions`, `execution_recipients`, `delivery_attempts` or `report_deliveries`; it reads them through `peek_claim`.
* **Window state on edit:** `save_single_task` resets `last_run`/`last_status` only when the task definition changed (or when `reset_window_state=True`). Re-sending in the same window otherwise requires the explicit `reset_task_window_state` action. Both resets start a new dispatch generation; `last_run`/`last_status` are only a projection and never authorise a send.
* **Schema changes:** Update `SCHEMA_SQL` (fresh databases) and append an idempotent migration to `database.MIGRATIONS` (existing databases); never edit or reorder released migrations. `PRAGMA user_version` tracks the applied count. Run `init_db` when a process opens the database (web: once in `create_app`), never per request.
* **Timestamps:** Store every timestamp with `database.to_utc_iso` (ISO 8601, UTC, explicit offset); never rely on SQLite `CURRENT_TIMESTAMP`.
* **Web panel:** Without `ADMIN_USER`/`ADMIN_PASSWORD` the panel refuses access (503) unless started with `serve --insecure-no-auth` on a loopback host. State-changing requests from other origins are rejected (`verify_same_origin`). Failed logins go through `app.state.login_throttle` (`LoginThrottle`): check `retry_after` before comparing credentials, count only wrong credentials (not missing ones), compare user and password with `secrets.compare_digest` without short-circuiting.
* **Operator decisions:** `UNKNOWN` deliveries are resolved only through `DatabaseRepository.resolve_unknown` (panel: `POST /executions/{id}/resolve`), with a non-empty reason, the attempt id the operator saw (refused if a newer attempt exists) and the panel user as actor; a live owner makes it 409. Never add a path that resends an `UNKNOWN` recipient or report without such a recorded decision.
* **Web panel markup (CSP):** No inline `<script>` code, no `on*=` event-handler attributes, no `<style>`/`style=`, no CDN or other external URLs; `tests/test_web_security.py` enforces this. Behaviour goes in `web_static/*.js` via `data-on-<event>="action"` and `MailCadence.register`. Pass server data to JavaScript only through `data-*` attributes or `<script type="application/json">` with `|tojson`, never by interpolating into JavaScript; IDs in URLs use `|urlencode`. Do not relax `web.CONTENT_SECURITY_POLICY` (no `'unsafe-inline'`/`'unsafe-eval'`) or set `Referrer-Policy: no-referrer`. After template, icon or `frontend/` changes run `make assets` and commit `web_static/`.
* **Secrets and Credentials:** Passwords in `Settings` are `SecretStr`; call `get_secret_value()` only at the point of use (SMTP login, panel login), never log, render or return the value. Do not weaken the unencrypted SMTP login check (`Settings.smtp_auth_blocked`, `InsecureSMTPAuthError`) or disable TLS certificate verification. Never store real passwords or `.env` files in the repository. In tests, use exclusively local mock/sink servers on `127.0.0.1` and dummy domains (`@example.com`, `@localhost`).

## 5. Communication, Documentation & Commit Style (NO EMOJIS)
- **STRICT ZERO-EMOJI POLICY:** Never use emojis, emoticons, or decorative Unicode symbols (e.g., 🚀, ✨, 🐛, ✅, 💡) in commit messages, Pull Request titles/descriptions, code comments, log messages, CLI output, UI templates, or documentation (`README.md`, `docs/`). Use plain ASCII/UTF-8 text only.
- **Commit Messages (Conventional Commits):**
  - Format: `<type>(<scope>): <concise imperative summary>` (e.g., `fix(mailer): enforce RFC 5322 Message-ID domain alignment`).
  - Allowed types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.
  - Keep the subject line under 72 characters, lowercase after the colon, no trailing period, and zero emojis.
  - Body text (if needed) must explain *what* changed and *why* in direct, technical prose without marketing buzzwords.
- **Tone & Generated Text:**
  - Write dry, precise, senior-level engineering prose.
  - Do not use promotional language ("seamless", "blazingly fast", "powerful", "game-changer", "robust") or conversational filler in comments, docstrings, or PR descriptions.
  - In web UI templates (`web_templates/`), use Lucide SVG icons or clean typographic labels—never raw emoji characters.
