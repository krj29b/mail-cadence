# MailCadence Architecture & Technical Documentation

Comprehensive, engineering-grade architectural specifications, database schema, data flows, and security controls for the `mail-cadence` execution engine.

---

## 1. System Overview & Component Architecture

### Core Modules & Responsibilities

`mail-cadence` is a high-throughput, asynchronous bulk email dispatch engine written in Python 3.12+. It utilizes SQLite for local state persistence and runtime execution tracking, while retaining XML support for schema-validated configuration import and export.

| Module | Location | Primary Responsibilities |
| --- | --- | --- |
| **Dispatch Pipeline** | `src/mail_cadence/pipeline.py` | The single dispatch orchestration used by every entry point: `run_dispatch(DispatchOptions, Settings)` (XML seeding, schedule evaluation, concurrent execution, logging, status report) and `dispatch_single_task` (manual run of one task). Opens its own SQLite connection. |
| **CLI** | `src/mail_cadence/cli.py` | Thin Typer adapter (`dispatch`, `import-xml`, `export-xml`, `serve`): argument parsing, logging setup, settings and `--db` resolution. `run_dispatch_pipeline(...)` is kept as a backward-compatible wrapper over `pipeline.run_dispatch`. |
| **Web Panel** | `src/mail_cadence/web.py` | Thin FastAPI/HTMX adapter. Starts `pipeline` functions in background jobs (`dispatch_jobs.py`); it does not import `cli.py`. |
| **XML Loader & Exporter** | `src/mail_cadence/xml_loader.py` | Secure XML parsing via `defusedxml`, schema validation using `pydantic-xml` models (`MailConfiguration`, `WorkSchedules`), and bidirectional conversion between XML documents and SQLite data models. |
| **Database & Connection Manager** | `src/mail_cadence/database.py` | SQLite connection pooling initialization, database schema DDL execution, journal mode (`WAL`), busy timeout settings, and foreign key enforcements. |
| **Database Repository** | `src/mail_cadence/repository.py` | Data Access Object (DAO) providing transactional database operations (`upsert`, `replace`, state recovery, execution logging). Uses top-level transaction blocks (`with self.conn:`) to maintain single-statement and batch atomicity. |
| **Task Scheduler** | `src/mail_cadence/scheduler.py` | Timezone-aware schedule window evaluation (`zoneinfo`), active execution window calculation (including midnight-spanning window handling), window-level idempotency checking, and token bucket rate-limiting resource management. |
| **Async Mailer & Renderer** | `src/mail_cadence/mailer.py` | Async email dispatch engine using `aiosmtplib`, `asyncio.TaskGroup`, `asyncio.Semaphore`, and `TokenBucketRateLimiter`. Handles Jinja2 template rendering (`TemplateRenderer`), MIME message construction, connection pooling (`AsyncMailer._pool`), and exponential backoff retry logic. |
| **Status Reporter** | `src/mail_cadence/reporter.py` | Aggregates execution results into `BatchSummaryReport` and generates HTML/plain-text summary reports dispatched to operators upon task batch completion. |
| **Domain & Data Models** | `src/mail_cadence/models.py` | Pydantic v2 and `pydantic-xml` models, custom data types (`EmailPriority`, `TaskStatus`, `RecipientType`), string header validators, and Pydantic field transformations. |

---

### Component Architecture Diagram

The following C4 component diagram illustrates component boundaries and data flows across CLI, storage, scheduling, dispatching, and reporting layers:

```mermaid
flowchart TB
    subgraph CLI ["Entry Points & Pipeline (cli.py, web.py -> pipeline.py)"]
        CLI_Entry["Typer CLI / Web Panel
-> pipeline.run_dispatch"]
        Config_Settings["Pydantic Settings (.env)"]
    end

    subgraph Parsing ["XML Layer (xml_loader.py)"]
        Defused_XML["defusedxml Parser"]
        Pydantic_XML["pydantic-xml Models"]
    end

    subgraph Storage ["Persistence Layer (database.py & repository.py)"]
        SQLite_DB[("SQLite Database\n(WAL Mode, busy_timeout=30s)")]
        Repo["DatabaseRepository"]
    end

    subgraph Scheduling ["Scheduling & Throttling (scheduler.py)"]
        Scheduler["TaskScheduler\n(ZoneInfo Timezone)"]
        Rate_Limiter["TokenBucketRateLimiter"]
        Sem_Manager["asyncio.Semaphore"]
    end

    subgraph Dispatch ["Dispatch Engine (mailer.py)"]
        Async_Mailer["AsyncMailer"]
        Task_Group["asyncio.TaskGroup"]
        Jinja_Renderer["TemplateRenderer\n(Jinja2 Auto-Escape)"]
        SMTP_Pool["SMTP Connection Pool\n(aiosmtplib)"]
    end

    subgraph Reporting ["Reporting Module (reporter.py)"]
        Report_Gen["ReportGenerator"]
        Batch_Summary["BatchSummaryReport"]
    end

    CLI_Entry --> Config_Settings
    CLI_Entry --> Defused_XML
    Defused_XML --> Pydantic_XML
    Pydantic_XML --> Repo
    Repo <--> SQLite_DB

    CLI_Entry --> Scheduler
    CLI_Entry --> Repo
    Scheduler --> Rate_Limiter
    Scheduler --> Sem_Manager

    CLI_Entry --> Async_Mailer
    Async_Mailer --> Task_Group
    Task_Group --> Sem_Manager
    Task_Group --> Rate_Limiter
    Task_Group --> Jinja_Renderer
    Task_Group --> SMTP_Pool
    Task_Group --> Repo

    CLI_Entry --> Report_Gen
    Report_Gen --> Batch_Summary
    Batch_Summary --> Async_Mailer

    SMTP_Pool -->|"SMTP (TLS / STARTTLS)"| Target_MTA[("Remote SMTP Server / MTA")]
```

---

## 2. Database Schema & Entity-Relationship Diagram (ERD)

### SQLite Pragma & Concurrency Configuration

`mail-cadence` configures SQLite for high concurrency, durability, and strict integrity via `database.py`:

- **Write-Ahead Logging (`PRAGMA journal_mode = WAL;`)**: Configured for file-backed databases to allow concurrent readers without blocking write transactions.
- **Busy Timeout (`PRAGMA busy_timeout = 30000;`)**: Wait up to 30,000 ms (30 seconds) on lock contention before raising `sqlite3.OperationalError`.
- **Foreign Key Enforcement (`PRAGMA foreign_keys = ON;`)**: Explicitly enabled per connection to guarantee referential integrity and execute cascade rules.
- **Transaction Atomicity Rules**: Public methods in `DatabaseRepository` execute SQL queries within top-level `with self.conn:` context managers. Private helper methods (`_save_task`, `_save_work_schedules`, etc.) omit `with self.conn:` context managers, allowing them to execute within the parent transaction without triggering premature commits.

---

### Entity-Relationship Diagram (ERD)

```mermaid
erDiagram
    work_schedules_meta {
        INTEGER id PK "CHECK(id = 1)"
        TEXT timezone NOT NULL "DEFAULT 'UTC'"
    }

    schedules {
        TEXT id PK
        TEXT description
    }

    hour_windows {
        INTEGER id PK "AUTOINCREMENT"
        TEXT schedule_id FK "REFERENCES schedules(id) ON DELETE CASCADE"
        TEXT start_time NOT NULL
        TEXT end_time NOT NULL
        INTEGER max_concurrency NOT NULL "DEFAULT 1"
        INTEGER rate_limit_per_min NOT NULL "DEFAULT 60"
        INTEGER seq NOT NULL "DEFAULT 0"
    }

    report_settings {
        INTEGER id PK "CHECK(id = 1)"
        TEXT recipient NOT NULL
        TEXT sender
        TEXT subject_prefix NOT NULL "DEFAULT '[MailCadence Report]'"
        BOOLEAN include_full_error_trace NOT NULL "DEFAULT 1"
    }

    tasks {
        TEXT id PK
        INTEGER max_retries NOT NULL "DEFAULT 3"
        TEXT priority NOT NULL "DEFAULT 'normal'"
        TEXT schedule_ref FK "REFERENCES schedules(id) ON DELETE RESTRICT"
        TEXT sender NOT NULL
        TEXT subject NOT NULL
        TEXT body_format NOT NULL "DEFAULT 'html'"
        TEXT body_template_path NOT NULL
        DATETIME last_run
        TEXT last_status
        TEXT last_error
    }

    task_recipients {
        INTEGER id PK "AUTOINCREMENT"
        TEXT task_id FK "REFERENCES tasks(id) ON DELETE CASCADE"
        TEXT recipient_type NOT NULL "CHECK(recipient_type IN ('to', 'cc', 'bcc'))"
        TEXT email NOT NULL
        INTEGER seq NOT NULL "DEFAULT 0"
    }

    task_context_variables {
        INTEGER id PK "AUTOINCREMENT"
        TEXT task_id FK "REFERENCES tasks(id) ON DELETE CASCADE"
        TEXT var_name NOT NULL
        TEXT var_value NOT NULL
        INTEGER seq NOT NULL "DEFAULT 0"
    }

    execution_logs {
        INTEGER id PK "AUTOINCREMENT"
        TEXT batch_id
        TEXT task_id NOT NULL
        TEXT status NOT NULL
        INTEGER sent_count NOT NULL "DEFAULT 0"
        TEXT failed_recipients
        REAL duration_ms NOT NULL "DEFAULT 0.0"
        TEXT error_details
        INTEGER attempt_count NOT NULL "DEFAULT 1"
        DATETIME executed_at NOT NULL "DEFAULT CURRENT_TIMESTAMP"
        INTEGER dry_run NOT NULL "DEFAULT 0"
    }

    schedules ||--o{ hour_windows : "has (ON DELETE CASCADE)"
    schedules ||--o{ tasks : "referenced by (ON DELETE RESTRICT)"
    tasks ||--o{ task_recipients : "contains (ON DELETE CASCADE)"
    tasks ||--o{ task_context_variables : "contains (ON DELETE CASCADE)"
    tasks ||--o{ execution_logs : "logs history"
```

---

## 3. Domain & XML Data Models (`models.py` & `xml_loader.py`)

### Pydantic & `pydantic-xml` Model Mapping

Data structures map directly between XML elements, Pydantic objects, and SQLite relational tables:

| Pydantic / XML Model | XML Root / Tag | Target SQLite Table(s) | Parsing & Serialization Notes |
| --- | --- | --- | --- |
| `MailConfiguration` | `<MailConfiguration>` | Multi-table container | Root object containing `ReportSettings` and list of `MailTask` instances. |
| `ReportSettings` | `<ReportSettings>` | `report_settings` | Single-row singleton table (`id = 1`). |
| `MailTask` | `<MailTask>` | `tasks`, `task_recipients`, `task_context_variables` | XML attributes: `id`, `max_retries`, `priority`, `schedule_ref`. Elements: `Sender`, `Recipients` (`To`, `Cc`, `Bcc`), `Subject`, `BodyTemplate`, `ContextVariables` (`Var`). |
| `WorkSchedules` | `<WorkSchedules>` | `work_schedules_meta`, `schedules` | Root container with `timezone` attribute mapping to `work_schedules_meta.timezone` (`id = 1`). |
| `Schedule` | `<Schedule>` | `schedules` | XML attribute: `id`, `description`. Contains list of `HourWindow` objects. |
| `HourWindow` | `<HourWindow>` | `hour_windows` | Attributes: `start`, `end`, `days_of_week`, `max_concurrency`, `rate_limit_per_min`. Both ends are inclusive; `end="24:00"` means the end of the day. FK to `schedules.id` (`ON DELETE CASCADE`). |

### Validation Rules

| Field | Rule |
| --- | --- |
| `MailTask.priority` | `EmailPriority`: `high`, `normal` or `low` (case-insensitive on input). |
| `MailTask.max_retries` | Integer `0..10` (`MAX_RETRIES_LIMIT`); attempts = `max_retries + 1`. |
| `MailTask` recipients | At least one address across `To`, `Cc` and `Bcc`. |
| Addresses (`Sender`, `To`, `Cc`, `Bcc`, report `Recipient`/`Sender`) | Exactly one address per entry, bare (`a@example.com`) or with a display name (`Ann <a@example.com>`). An empty task `Sender` falls back to `SMTP_USER`. |
| `MailTask.schedule_ref` | Must reference an existing schedule: XML import fails cross-reference validation, the panel returns `400`. |
| `HourWindow.end` | `HH:MM[:SS]` or `24:00`. A window `00:00`-`23:59` leaves the last minute of the day uncovered; use `24:00`. |

`init_db` also normalizes rows written by older versions: unknown priorities become `normal`, and `max_retries` is clamped to `0..10`.

---

### Seeding Modes: `upsert` vs `replace`

When importing XML files using `DatabaseRepository.save_mail_configuration` and `DatabaseRepository.save_work_schedules`:

1. **`upsert` Mode (Default)**:
   - Updates existing records or inserts new records based on primary key matching (`tasks.id`, `schedules.id`).
   - For `schedules`: replaces child `hour_windows` for matched schedules.
   - For `tasks`: updates task metadata and replaces corresponding `task_recipients` and `task_context_variables`.
2. **`replace` Mode**:
   - Completely clears the target configuration tables prior to insertion (`DELETE FROM tasks`, `DELETE FROM schedules`, etc.).
   - Re-populates the database strictly from the provided XML files.

---

### Complete XML Examples

#### `tasks.xml`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>ops-alerts@example.com</Recipient>
    <Sender>mailer-daemon@example.com</Sender>
    <SubjectPrefix>[MailCadence Status Report]</SubjectPrefix>
    <IncludeFullErrorTrace>true</IncludeFullErrorTrace>
  </ReportSettings>
  <Tasks>
    <MailTask id="TASK-001" max_retries="3" priority="high" schedule_ref="SCHED-MORNING">
      <Sender>noreply@example.com</Sender>
      <Recipients>
        <To>client.alpha@example.com</To>
        <To>client.beta@example.com</To>
        <Cc>account.manager@example.com</Cc>
        <Bcc>audit@example.com</Bcc>
      </Recipients>
      <Subject>Daily Operational Dispatch - EMEA</Subject>
      <BodyTemplate format="html">builtin:daily_dispatch.html.j2</BodyTemplate>
      <ContextVariables>
        <Var name="region">EMEA</Var>
        <Var name="report_date">2026-10-17</Var>
      </ContextVariables>
    </MailTask>
  </Tasks>
</MailConfiguration>
```

#### `schedules.xml`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="Europe/Warsaw">
  <Schedule id="SCHED-MORNING" description="Morning operational window">
    <AllowedHours>
      <HourWindow start="06:00" end="12:00" max_concurrency="5" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
  <Schedule id="SCHED-NIGHT" description="Overnight operational window">
    <AllowedHours>
      <HourWindow start="22:00" end="04:00" max_concurrency="2" rate_limit_per_min="30"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>
```

---

## 4. Scheduling, Idempotency & Rate Limiting (`scheduler.py`)

### Timezone-Aware Eligibility Evaluation

`TaskScheduler` converts system time to the defined IANA timezone (`zoneinfo.ZoneInfo`) configured in `work_schedules_meta.timezone`:

1. Retrieves current timestamp in target timezone `reference_dt = datetime.now(self.timezone)`.
2. Locates `Schedule` matching `task.schedule_ref`.
3. Checks if `reference_dt.time()` falls within any defined `HourWindow` for that schedule.

---

### Midnight-Spanning Execution Windows

For hour windows where `start_time > end_time` (e.g., `22:00` to `04:00`):

- If `reference_dt.time() >= start_time` (`23:00`):
  - `window_start_dt` = `ref_date 22:00`
  - `window_end_dt` = `(ref_date + 1 day) 04:00`
- If `reference_dt.time() < start_time` (`02:00`):
  - `window_start_dt` = `(ref_date - 1 day) 22:00`
  - `window_end_dt` = `ref_date 04:00`

---

### Window-Level Idempotency Protection

To prevent duplicate dispatches during an active schedule window:

- The engine evaluates `task.last_run` and `task.last_status`.
- If `task.last_status == TaskStatus.SUCCESS` and `window_start_dt <= task.last_run < window_end_dt`:
  - `is_task_eligible()` returns `False`.
  - The task is skipped with status `SKIPPED_SCHEDULE`.
- If `last_run` falls outside the active window instance or `last_status != SUCCESS` (e.g. `FAILED` or `PARTIAL`), the task is marked eligible for re-execution.
- Dry-run executions are written to `execution_logs` only (flagged with `dry_run = 1` and excluded from dashboard statistics); they never update `last_run` / `last_status`, so they cannot block a real dispatch.
- Saving a task (web panel or `save_single_task`) resets `last_run` / `last_status` only when the task definition changed. The panel's "Allow Resend" action (`POST /tasks/{id}/reset-window`) explicitly re-enables a dispatch in the current window.
- The status report is sent only when at least one task was executed (success, partial or failed) and `report_settings` is configured.

---

### Rate Limiting & Concurrency Architecture

`TokenBucketRateLimiter` enforces rate limits per minute using `time.monotonic()`:

- **Token Bucket Refill**: Tokens fill continuously at rate `rate_limit_per_min / 60.0` tokens/sec up to capacity `rate_limit_per_min`.
- **Resource Key Sharing**: The pipeline (`pipeline._partition_eligible_tasks`) groups concurrency semaphores (`asyncio.Semaphore(window.max_concurrency)`) and rate limiters using a composite key: `(schedule_ref, window.start, window.end)`. All tasks sharing the same schedule window share the exact same semaphore and token bucket instance.

---

## 5. Dispatch Pipeline & SMTP Retry Flow (`pipeline.py`, `mailer.py`, `reporter.py`)

### Execution Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    actor CLI as CLI / Operator
    participant Runner as Engine Runner (pipeline.py)
    participant Repo as DatabaseRepository
    participant DB as SQLite DB
    participant Sched as TaskScheduler
    participant Mailer as AsyncMailer
    participant SMTP as Remote SMTP Server
    participant Reporter as ReportGenerator

    CLI->>Runner: run_dispatch(DispatchOptions, Settings)
    Runner->>Repo: init_db (column migrations + data fix-ups)
    opt XML Seeding Provided (--tasks-xml / --schedule-xml)
        Runner->>Repo: import_from_xml(mode="upsert")
        Repo->>DB: import_configuration (single transaction)
    end

    Runner->>Repo: get_mail_configuration(), get_work_schedules(), get_task_execution_states()
    Repo-->>Runner: MailConfiguration, WorkSchedules, last_run / last_status per task

    loop For each task
        Runner->>Sched: window_status(task, ref_dt, last_run, last_status)
        alt Eligible (active window, not yet dispatched in it)
            Runner->>Runner: Reuse or create Semaphore + TokenBucketRateLimiter for (schedule_ref, start, end)
        else Not eligible
            Runner->>Repo: log_task_execution(SKIPPED_SCHEDULE)
        end
    end

    Runner->>Runner: Sort eligible tasks by priority (high, normal, low), then id
    Note over Runner,Mailer: Priority only sets the start order. All eligible tasks run in one asyncio.TaskGroup, limited per window by the shared Semaphore and rate limiter.

    par One coroutine per eligible task (asyncio.TaskGroup)
        Runner->>Runner: Acquire window Semaphore
        Runner->>Mailer: dispatch_task(task, rate_limiter)
        Mailer->>Mailer: Render Jinja2 template & build EmailMessage

        loop Delivery attempts (1 to max_retries + 1)
            Mailer->>Mailer: rate_limiter.acquire()
            Mailer->>Mailer: Acquire SMTP client (_acquire_smtp_client)
            Mailer->>SMTP: send_message(msg, pending_recipients)
            alt Success (2xx for all pending recipients)
                SMTP-->>Mailer: 250 OK
            else Transient error (4xx / connection error)
                SMTP-->>Mailer: 4xx Error / Connection Error
                Mailer->>Mailer: Backoff delay: 0.5 * 2^(attempt - 1), retry 4xx recipients only
            else Permanent error (5xx)
                SMTP-->>Mailer: 5xx Error
                Mailer->>Mailer: Drop 5xx recipients, no retry for them
            end
        end

        Mailer-->>Runner: TaskExecutionResult (SUCCESS / PARTIAL / FAILED)
        Runner->>Repo: log_task_execution(result, batch_id, dry_run)
        Repo->>DB: INSERT execution_logs (+ UPDATE tasks state unless dry run)
    end

    Runner->>Reporter: aggregate_summary_report(results, start, end)
    Reporter-->>Runner: BatchSummaryReport
    opt At least one task executed and report_settings configured
        Runner->>Reporter: send_report(report, report_settings, mailer)
        Reporter->>SMTP: Send status report email
    end
    Runner-->>CLI: Complete execution (Exit Code 0, 1, or 2)
```

---

### Task Status State Diagram

```mermaid
stateDiagram-v2
    [*] --> PENDING : Pipeline Initiated

    PENDING --> SKIPPED_SCHEDULE : Schedule Window Inactive / Idempotency Hit
    PENDING --> DISPATCHING : Active Window & Task Selected

    DISPATCHING --> SUCCESS : All Recipients Delivered (2xx)
    DISPATCHING --> PARTIAL : Partial Recipients Succeeded (Some 5xx / 4xx Exhausted)
    DISPATCHING --> FAILED : All Recipients Failed / Permanent Error / Retries Exhausted

    SKIPPED_SCHEDULE --> [*]
    SUCCESS --> [*]
    PARTIAL --> [*]
    FAILED --> [*]
```

---

## 6. Security Architecture

`mail-cadence` implements defense-in-depth security measures across input processing, file handling, template rendering, and SMTP header construction:

### 1. XXE & Entity Expansion Protection (`defusedxml`)
- All XML inputs (`tasks.xml`, `schedules.xml`) are parsed via `defusedxml.ElementTree.fromstring` prior to passing DOM nodes into `pydantic-xml`.
- Prevents XML External Entity (XXE) attacks, billion laughs entity expansion, and DTD retrieval attacks.

### 2. Template Resolution & Path Traversal Prevention (`TemplateRenderer`)
- `builtin:<name>` (e.g. `builtin:daily_dispatch.html.j2`) loads a template shipped in the package's `templates/` directory. The internal report templates (`report_email.*`) cannot be used as task templates.
- Any other `<BodyTemplate>` path is resolved only inside the template directory (`--template-dir` / `MAIL_CADENCE_TEMPLATE_DIR`, default: current directory). There are no fallback locations; a missing file raises `jinja2.TemplateNotFound` and the task fails.
- The resolved path must reside within the template directory (`resolved_path.relative_to(base_dir)`). Attempting to reference files outside it raises a `ValueError`.

### 3. Cross-Site Scripting (XSS) Prevention
- Jinja2 environments (task renderer and status report) use `select_autoescape(enabled_extensions=mailer.AUTOESCAPE_EXTENSIONS, default_for_string=True)`, which enables autoescape for `html`, `htm`, `xml` and the compound `html.j2`, `htm.j2`, `xml.j2` extensions, and for templates rendered from strings.
- Ensures context variables and SMTP error details rendered into HTML emails are auto-escaped. `*.txt.j2` templates are not escaped.

### 5. Web Panel Dispatch Jobs (`dispatch_jobs.py`)
- `POST /dispatch/run` and `POST /tasks/{id}/run` start a background job and return `202` immediately. Each job runs in its own worker thread with a separate event loop and SQLite connection, so long batches never block the web server or exceed proxy timeouts.
- `DispatchRunner.start` checks for a running job and claims the slot atomically; a concurrent request gets `409`.
- Progress: `GET /dispatch/status` (HTML fragment, polls itself every 2 s while running) and `GET /api/dispatch/status` (JSON).
- The slot is per process: run `serve` with a single worker. It does not coordinate with dispatches started from cron/systemd; those rely on window-level idempotency.

### 6. Web Panel Access Control
- Without `ADMIN_USER` and `ADMIN_PASSWORD` every request is refused with `503`. `mail-cadence serve --insecure-no-auth` opts out, and is only accepted with a loopback `--host`.
- State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) are rejected with `403` when `Sec-Fetch-Site` is not `same-origin`/`none` or when `Origin` does not match `Host` (CSRF protection for Basic Auth).

### 4. SMTP Header Injection Prevention
- Header string fields (`sender`, `recipient`, `subject`, `to`, `cc`, `bcc`) are validated using `_check_no_newlines` Pydantic validators in `models.py`; address fields additionally go through `_check_email_address` (one parseable address per entry).
- Rejects inputs containing CR (`\r`) or LF (`\n`) characters, preventing SMTP command injection and unauthorized header injection.
