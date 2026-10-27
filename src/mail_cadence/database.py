import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mail_cadence.models import MAX_RETRIES_LIMIT
from mail_cadence.scheduler import TaskScheduler

BASE_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS work_schedules_meta (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    timezone TEXT NOT NULL DEFAULT 'UTC'
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS hour_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    days_of_week TEXT NOT NULL DEFAULT '*',
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
    seq INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS report_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    recipient TEXT NOT NULL,
    sender TEXT,
    subject_prefix TEXT NOT NULL DEFAULT '[MailCadence Report]',
    include_full_error_trace BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    max_retries INTEGER NOT NULL DEFAULT 3,
    priority TEXT NOT NULL DEFAULT 'normal',
    schedule_ref TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_format TEXT NOT NULL DEFAULT 'html',
    body_template_path TEXT NOT NULL,
    last_run DATETIME,
    last_status TEXT,
    last_error TEXT,
    FOREIGN KEY (schedule_ref) REFERENCES schedules(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS task_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    recipient_type TEXT NOT NULL CHECK(recipient_type IN ('to', 'cc', 'bcc')),
    email TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_context_variables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    var_name TEXT NOT NULL,
    var_value TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_recipients TEXT,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    error_details TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    executed_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_hour_windows_schedule_id ON hour_windows(schedule_id);
CREATE INDEX IF NOT EXISTS idx_tasks_schedule_ref ON tasks(schedule_ref);
CREATE INDEX IF NOT EXISTS idx_task_recipients_task_id ON task_recipients(task_id);
CREATE INDEX IF NOT EXISTS idx_task_context_task_id ON task_context_variables(task_id);
CREATE INDEX IF NOT EXISTS idx_execution_logs_task_id ON execution_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_execution_logs_status ON execution_logs(status);
"""

# Durable execution state (docs/adr/0001-execution-state-model.md). Identities, versions
# and executions are never deleted with a task or schedule: they are the audit trail.
EXECUTION_STATE_SQL = """
CREATE TABLE IF NOT EXISTS task_identities (
    uid INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    dispatch_generation INTEGER NOT NULL DEFAULT 1 CHECK (dispatch_generation >= 1),
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS task_versions (
    task_uid INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_uid, version),
    FOREIGN KEY (task_uid) REFERENCES task_identities(uid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS schedule_versions (
    schedule_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('cli', 'web', 'manual')),
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    task_outcome TEXT,
    report_outcome TEXT,
    exit_code INTEGER
);

CREATE TABLE IF NOT EXISTS task_executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid INTEGER NOT NULL,
    dispatch_generation INTEGER NOT NULL CHECK (dispatch_generation >= 1),
    window_key TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    schedule_version INTEGER,
    window_start TEXT,
    window_end TEXT,
    run_id TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'CLAIMED', 'ACTIVE', 'RETRY_WAIT', 'SUCCEEDED', 'PARTIAL', 'FAILED',
        'NEEDS_REVIEW', 'EXPIRED')),
    owner TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_expires_at TEXT,
    message_id TEXT,
    message_bytes BLOB,
    legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (task_uid, dispatch_generation, window_key),
    FOREIGN KEY (task_uid) REFERENCES task_identities(uid) ON DELETE RESTRICT,
    FOREIGN KEY (task_uid, task_version)
        REFERENCES task_versions(task_uid, version) ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES dispatch_runs(run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS execution_recipients (
    execution_id INTEGER NOT NULL,
    envelope_address TEXT NOT NULL,
    display_address TEXT NOT NULL,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('to', 'cc', 'bcc')),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'IN_FLIGHT', 'ACCEPTED', 'PERMANENT_FAILURE', 'RETRY_WAIT',
        'UNKNOWN', 'RETRY_EXHAUSTED', 'WINDOW_EXPIRED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    next_attempt_at TEXT,
    last_code INTEGER,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (execution_id, envelope_address),
    CHECK (attempt_count <= max_attempts),
    FOREIGN KEY (execution_id) REFERENCES task_executions(execution_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
    recipients_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('IN_FLIGHT', 'COMPLETED', 'UNKNOWN')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    UNIQUE (execution_id, attempt_no),
    FOREIGN KEY (execution_id) REFERENCES task_executions(execution_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS report_deliveries (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_bytes BLOB,
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'IN_FLIGHT', 'ACCEPTED', 'PERMANENT_FAILURE', 'RETRY_WAIT',
        'UNKNOWN', 'RETRY_EXHAUSTED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    next_attempt_at TEXT,
    owner TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_expires_at TEXT,
    last_code INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (attempt_count <= max_attempts),
    FOREIGN KEY (run_id) REFERENCES dispatch_runs(run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS execution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_uid INTEGER,
    execution_id INTEGER,
    attempt_id INTEGER,
    report_id INTEGER,
    actor TEXT,
    reason TEXT,
    details_json TEXT,
    FOREIGN KEY (task_uid) REFERENCES task_identities(uid) ON DELETE RESTRICT,
    FOREIGN KEY (execution_id) REFERENCES task_executions(execution_id) ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES delivery_attempts(attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY (report_id) REFERENCES report_deliveries(report_id) ON DELETE RESTRICT
);

-- One live identity per task name; retired identities keep their old name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_identities_live
    ON task_identities(task_id) WHERE deleted_at IS NULL;
-- At most one execution per task identity holds the right to send (ADR 0001, D7).
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_executions_one_active
    ON task_executions(task_uid) WHERE state IN ('CLAIMED', 'ACTIVE');
CREATE INDEX IF NOT EXISTS idx_task_executions_state ON task_executions(state);
CREATE INDEX IF NOT EXISTS idx_task_executions_run_id ON task_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_execution_recipients_state ON execution_recipients(state);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_execution_id
    ON delivery_attempts(execution_id);
CREATE INDEX IF NOT EXISTS idx_report_deliveries_state ON report_deliveries(state);
CREATE INDEX IF NOT EXISTS idx_execution_events_task_uid ON execution_events(task_uid);
CREATE INDEX IF NOT EXISTS idx_execution_events_execution_id
    ON execution_events(execution_id);
"""

SCHEMA_SQL = BASE_SCHEMA_SQL + EXECUTION_STATE_SQL


def get_connection(
    db_path: str | Path = ":memory:", check_same_thread: bool = False
) -> sqlite3.Connection:
    path_str = str(db_path)
    if path_str != ":memory:":
        parent = Path(path_str).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path_str, timeout=30.0, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    if path_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def to_utc_iso(dt: datetime) -> str:
    """The one timestamp format stored in the database: ISO 8601 in UTC with offset."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# --- Task identities and definition versions (ADR 0001, D3/D4) -------------------------
# Kept in step with the configuration tables by sync_definitions(), which every
# repository method that writes tasks or schedules calls inside its transaction.
# Definitions are read from the stored rows, not from the Pydantic models, so legacy
# rows that no longer validate still get an identity and a version.


def sync_definitions(conn: sqlite3.Connection) -> None:
    """Create or retire task identities and record new task and schedule versions.

    Runs inside the caller's transaction and never commits.
    """
    now = to_utc_iso(datetime.now(UTC))
    _sync_task_identities(conn, now)
    _sync_task_versions(conn, now)
    _sync_schedule_versions(conn, now)


def _sync_task_identities(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        """
        UPDATE task_identities SET deleted_at = ?
        WHERE deleted_at IS NULL AND task_id NOT IN (SELECT id FROM tasks)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO task_identities (task_id, created_at)
        SELECT id, ? FROM tasks
        WHERE id NOT IN (SELECT task_id FROM task_identities WHERE deleted_at IS NULL)
        """,
        (now,),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _task_definitions(conn: sqlite3.Connection) -> dict[str, str]:
    """Canonical JSON of every stored task definition, keyed by task id (3 bulk queries)."""
    recipients: dict[str, list[list[str]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT task_id, recipient_type, email FROM task_recipients ORDER BY task_id, seq, id"
    ):
        recipients[row["task_id"]].append([row["recipient_type"], row["email"]])
    context: dict[str, list[list[str]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT task_id, var_name, var_value FROM task_context_variables ORDER BY task_id, seq, id"
    ):
        context[row["task_id"]].append([row["var_name"], row["var_value"]])
    definitions: dict[str, str] = {}
    for row in conn.execute(
        "SELECT id, max_retries, priority, schedule_ref, sender, subject, body_format, "
        "body_template_path FROM tasks"
    ):
        task: dict[str, object] = dict(zip(row.keys(), tuple(row), strict=True))
        task["recipients"] = recipients[row["id"]]
        task["context_variables"] = context[row["id"]]
        definitions[row["id"]] = _canonical_json(task)
    return definitions


def _sync_task_versions(conn: sqlite3.Connection, now: str) -> None:
    latest = conn.execute(
        """
        SELECT i.uid, i.task_id, v.version, v.definition_json
        FROM task_identities AS i
        LEFT JOIN task_versions AS v
            ON v.task_uid = i.uid
           AND v.version = (SELECT MAX(version) FROM task_versions WHERE task_uid = i.uid)
        WHERE i.deleted_at IS NULL
        """
    ).fetchall()
    definitions = _task_definitions(conn)
    new_versions = [
        (row["uid"], (row["version"] or 0) + 1, definitions[row["task_id"]], now)
        for row in latest
        if row["definition_json"] != definitions[row["task_id"]]
    ]
    conn.executemany(
        "INSERT INTO task_versions (task_uid, version, definition_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        new_versions,
    )


def _schedule_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    """What makes a schedule version: its windows and the timezone, not the description."""
    meta = conn.execute("SELECT timezone FROM work_schedules_meta WHERE id = 1").fetchone()
    timezone = meta["timezone"] if meta else "UTC"
    windows: dict[str, list[list[object]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT schedule_id, start_time, end_time, days_of_week, max_concurrency, "
        "rate_limit_per_min FROM hour_windows ORDER BY schedule_id, seq, id"
    ):
        windows[row["schedule_id"]].append(list(row)[1:])
    return {
        row["id"]: _canonical_json({"timezone": timezone, "windows": windows[row["id"]]})
        for row in conn.execute("SELECT id FROM schedules")
    }


def _sync_schedule_versions(conn: sqlite3.Connection, now: str) -> None:
    """Bump a schedule's version when its fingerprint changes.

    Rows of deleted schedules stay, so a re-created schedule keeps counting upwards.
    """
    stored = {
        row["schedule_id"]: (row["version"], row["fingerprint"])
        for row in conn.execute("SELECT schedule_id, version, fingerprint FROM schedule_versions")
    }
    changed = [
        (schedule_id, stored.get(schedule_id, (0, ""))[0] + 1, fingerprint, now)
        for schedule_id, fingerprint in _schedule_fingerprints(conn).items()
        if stored.get(schedule_id, (0, ""))[1] != fingerprint
    ]
    conn.executemany(
        """
        INSERT INTO schedule_versions (schedule_id, version, fingerprint, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(schedule_id) DO UPDATE SET
            version = excluded.version,
            fingerprint = excluded.fingerprint,
            updated_at = excluded.updated_at
        """,
        changed,
    )


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# --- Migrations ------------------------------------------------------------------------
# PRAGMA user_version holds the number of migrations applied. Every migration must be
# idempotent: databases created before versioning start at 0 and run all of them, and a
# fresh database gets the full SCHEMA_SQL from migration 1 before the later ones run.

# Columns added after the first released schema: (table, column, definition).
_LEGACY_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("hour_windows", "days_of_week", "TEXT NOT NULL DEFAULT '*'"),
    ("execution_logs", "dry_run", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_base_schema(conn: sqlite3.Connection) -> None:
    """1: create missing tables and indexes, add columns missing from pre-versioning DBs."""
    conn.executescript(SCHEMA_SQL)
    for table, column, definition in _LEGACY_ADDED_COLUMNS:
        if column not in _column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Values match how older versions behaved (unknown priority -> normal, negative retries -> 0).
_TASK_FIXUPS: tuple[tuple[str, tuple[object, ...]], ...] = (
    (
        "UPDATE tasks SET priority = lower(trim(priority)) WHERE priority <> lower(trim(priority))",
        (),
    ),
    ("UPDATE tasks SET priority = 'normal' WHERE priority NOT IN ('high', 'normal', 'low')", ()),
    ("UPDATE tasks SET max_retries = 0 WHERE max_retries < 0", ()),
    ("UPDATE tasks SET max_retries = ? WHERE max_retries > ?", (MAX_RETRIES_LIMIT,) * 2),
)


def _migrate_task_validation(conn: sqlite3.Connection) -> None:
    """2: bring task rows in line with the stricter priority / max_retries validation."""
    for statement, params in _TASK_FIXUPS:
        conn.execute(statement, params)


def _migrate_executed_at_to_iso(conn: sqlite3.Connection) -> None:
    """3: SQLite CURRENT_TIMESTAMP values ('YYYY-MM-DD HH:MM:SS', UTC) -> ISO 8601 UTC."""
    if "executed_at" not in _column_names(conn, "execution_logs"):
        return
    conn.execute(
        """
        UPDATE execution_logs
        SET executed_at = replace(executed_at, ' ', 'T') || '+00:00'
        WHERE executed_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        """
    )


def _migrate_report_subject_prefix_newlines(conn: sqlite3.Connection) -> None:
    """4: CR/LF in the report subject prefix (now rejected) -> one space per line break."""
    conn.execute(
        """
        UPDATE report_settings
        SET subject_prefix = replace(replace(replace(subject_prefix,
            char(13) || char(10), ' '), char(13), ' '), char(10), ' ')
        WHERE subject_prefix LIKE '%' || char(13) || '%'
           OR subject_prefix LIKE '%' || char(10) || '%'
        """
    )


def _migrate_execution_state(conn: sqlite3.Connection) -> None:
    """5: execution-state tables (ADR 0001); identities and versions for existing rows.

    Legacy last_run/last_status are converted when the new dispatch path is enabled,
    not here: until then the old path keeps updating them.
    """
    conn.executescript(EXECUTION_STATE_SQL)
    sync_definitions(conn)


# Legacy tasks.last_status -> state of a synthetic execution (ADR 0001, D17).
# PARTIAL and FAILED rows cannot tell which recipients were accepted: review them.
_LEGACY_STATES = {"SUCCESS": "SUCCEEDED", "PARTIAL": "NEEDS_REVIEW", "FAILED": "NEEDS_REVIEW"}


def _parse_legacy_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _legacy_event(
    conn: sqlite3.Connection, kind: str, task_uid: int, reason: str, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO execution_events (occurred_at, kind, task_uid, reason)
        SELECT ?, ?, ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM execution_events WHERE kind = ? AND task_uid = ?)
        """,
        (now, kind, task_uid, reason, kind, task_uid),
    )


def _convert_legacy_row(
    conn: sqlite3.Connection, scheduler: TaskScheduler, row: sqlite3.Row, now: str
) -> None:
    state = _LEGACY_STATES.get(row["last_status"])
    if state is None:
        return
    last_run = _parse_legacy_time(row["last_run"])
    instance = scheduler.window_instance(row["schedule_ref"], last_run) if last_run else None
    if instance is None:
        reason = f"{row['last_status']} at {row['last_run']!r} matches no window occurrence"
        _legacy_event(conn, "legacy_state_dropped", row["uid"], reason, now)
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO task_executions
        (task_uid, dispatch_generation, window_key, task_version, window_start, window_end,
         state, legacy, created_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            row["uid"],
            row["dispatch_generation"],
            instance.key,
            row["version"],
            to_utc_iso(instance.start_utc),
            to_utc_iso(instance.end_utc),
            state,
            now,
            now,
        ),
    )
    reason = f"{row['last_status']} in {instance.key}"
    _legacy_event(conn, "legacy_state_converted", row["uid"], reason, now)


def _migrate_legacy_window_state(conn: sqlite3.Connection) -> None:
    """6: the old path's last_run/last_status become executions (ADR 0001, D17).

    A legacy SUCCESS blocks the window occurrence that contains last_run; PARTIAL and
    FAILED block it as NEEDS_REVIEW. Later occurrences are free. A time that matches no
    occurrence (or cannot be parsed) only leaves an event, as the old path ignored it.
    """
    # Imported here: repository imports this module.
    from mail_cadence.repository import DatabaseRepository

    try:
        scheduler = TaskScheduler(DatabaseRepository(conn).get_work_schedules())
    except ValueError:
        return  # schedules that do not validate cannot be mapped; dispatch fails anyway
    rows = conn.execute(
        """
        SELECT t.schedule_ref, t.last_run, t.last_status, i.uid, i.dispatch_generation,
               (SELECT MAX(version) FROM task_versions WHERE task_uid = i.uid) AS version
        FROM tasks AS t
        JOIN task_identities AS i ON i.task_id = t.id AND i.deleted_at IS NULL
        WHERE t.last_status IS NOT NULL
        """
    ).fetchall()
    now = to_utc_iso(datetime.now(UTC))
    for row in rows:
        _convert_legacy_row(conn, scheduler, row, now)


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migrate_base_schema,
    _migrate_task_validation,
    _migrate_executed_at_to_iso,
    _migrate_report_subject_prefix_newlines,
    _migrate_execution_state,
    _migrate_legacy_window_state,
)
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema to SCHEMA_VERSION. Cheap when already up to date.

    Raises sqlite3.DatabaseError if the database was written by a newer version.
    """
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        msg = (
            f"Database schema version {current} is newer than this version of "
            f"mail-cadence supports ({SCHEMA_VERSION}); upgrade mail-cadence."
        )
        raise sqlite3.DatabaseError(msg)
    for version in range(current + 1, SCHEMA_VERSION + 1):
        MIGRATIONS[version - 1](conn)
        conn.execute(f"PRAGMA user_version = {version:d}")
        conn.commit()
