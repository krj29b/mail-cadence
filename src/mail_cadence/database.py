import sqlite3
from pathlib import Path

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
    executed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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


# Columns added after the first schema version: (table, column, definition).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("hour_windows", "days_of_week", "TEXT NOT NULL DEFAULT '*'"),
    ("execution_logs", "dry_run", "INTEGER NOT NULL DEFAULT 0"),
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    for table, column, definition in _ADDED_COLUMNS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
