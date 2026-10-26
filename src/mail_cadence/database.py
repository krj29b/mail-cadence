import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mail_cadence.models import MAX_RETRIES_LIMIT

SCHEMA_SQL = """
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


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migrate_base_schema,
    _migrate_task_validation,
    _migrate_executed_at_to_iso,
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
