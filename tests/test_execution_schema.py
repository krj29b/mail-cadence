"""Execution-state schema (ADR 0001): migration 5, identities, versions and constraints.

Checks use direct SQL queries, independent of the code that writes the rows.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mail_cadence.database import SCHEMA_VERSION, get_connection, init_db, schema_version
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailConfiguration,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository

SCHEMA_V4 = (Path(__file__).parent / "fixtures" / "schema_v4.sql").read_text(encoding="utf-8")
NOW = "2026-10-27T10:00:00+00:00"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(get_connection(tmp_path / "cadence.db")) as connection:
        init_db(connection)
        yield connection


def _task(task_id: str = "T1", *, subject: str = "Hello", schedule: str = "S1") -> MailTask:
    return MailTask(
        id=task_id,
        schedule_ref=schedule,
        sender="cadence@example.com",
        subject=subject,
        recipients=Recipients(to=["a@example.com"]),
        body_template=BodyTemplate(template_path="builtin:x.html.j2"),
    )


def _schedules(*windows: HourWindow, timezone: str = "UTC") -> WorkSchedules:
    allowed = list(windows) or [HourWindow(start="06:00", end="18:00")]
    return WorkSchedules(timezone=timezone, schedules=[Schedule(id="S1", allowed_hours=allowed)])


def _repo_with_task(conn: sqlite3.Connection) -> DatabaseRepository:
    repo = DatabaseRepository(conn)
    repo.save_work_schedules(_schedules())
    repo.save_single_task(_task())
    return repo


def _identity(conn: sqlite3.Connection, task_id: str = "T1") -> sqlite3.Row:
    row = conn.execute(
        "SELECT uid, dispatch_generation FROM task_identities "
        "WHERE task_id = ? AND deleted_at IS NULL",
        (task_id,),
    ).fetchone()
    assert isinstance(row, sqlite3.Row)
    return row


def _versions(conn: sqlite3.Connection, uid: int) -> list[int]:
    rows = conn.execute(
        "SELECT version FROM task_versions WHERE task_uid = ? ORDER BY version", (uid,)
    )
    return [r[0] for r in rows]


def _schedule_version(conn: sqlite3.Connection, schedule_id: str = "S1") -> int:
    row = conn.execute(
        "SELECT version FROM schedule_versions WHERE schedule_id = ?", (schedule_id,)
    ).fetchone()
    return int(row[0])


# --- Migration -------------------------------------------------------------------------


def _schema_shape(conn: sqlite3.Connection) -> dict[str, Any]:
    """Columns, foreign keys and indexes per table; column order does not matter."""
    shape: dict[str, Any] = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        columns = {tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})")}
        fks = {tuple(r)[2:] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
        indexes = {}
        for idx in conn.execute(f"PRAGMA index_list({table})"):
            name, unique, partial = idx[1], idx[2], idx[4]
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})"))
            indexes[name] = (unique, partial, cols)
        shape[table] = (columns, fks, indexes)
    partial_sql = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql LIKE '%WHERE%'"
    ).fetchall()
    shape["__partial_indexes__"] = {(r[0], " ".join(r[1].split())) for r in partial_sql}
    return shape


def _released_v4(path: Path, version: int = 4) -> None:
    """A database as version ``version`` (1..4) left it, with typical rows."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA_V4)
        conn.execute("INSERT INTO work_schedules_meta (id, timezone) VALUES (1, 'UTC')")
        conn.execute("INSERT INTO schedules (id) VALUES ('S1')")
        conn.execute(
            "INSERT INTO hour_windows (schedule_id, start_time, end_time) "
            "VALUES ('S1', '06:00', '18:00')"
        )
        for task_id in ("T1", "T2"):
            conn.execute(
                "INSERT INTO tasks (id, schedule_ref, sender, subject, body_template_path, "
                "last_run, last_status) VALUES (?, 'S1', 'a@example.com', 's', 'x.html.j2', "
                "?, 'SUCCESS')",
                (task_id, NOW),
            )
        # T2 has no recipients: invalid for the models, but it must still migrate.
        conn.execute(
            "INSERT INTO task_recipients (task_id, recipient_type, email) "
            "VALUES ('T1', 'to', 'b@example.com')"
        )
        conn.execute(f"PRAGMA user_version = {version}")


@pytest.mark.parametrize("start_version", [1, 2, 3, 4])
def test_migrated_schema_matches_a_fresh_one(tmp_path: Path, start_version: int) -> None:
    _released_v4(tmp_path / "old.db", start_version)
    with (
        closing(get_connection(tmp_path / "old.db")) as old,
        closing(get_connection(tmp_path / "new.db")) as new,
    ):
        init_db(old)
        init_db(new)
        assert schema_version(old) == schema_version(new) == SCHEMA_VERSION
        assert _schema_shape(old) == _schema_shape(new)


def test_migration_gives_existing_tasks_and_schedules_version_one(tmp_path: Path) -> None:
    _released_v4(tmp_path / "old.db")
    with closing(get_connection(tmp_path / "old.db")) as conn:
        init_db(conn)
        identities = conn.execute(
            "SELECT task_id, dispatch_generation, deleted_at FROM task_identities ORDER BY task_id"
        ).fetchall()
        versions = conn.execute(
            "SELECT i.task_id, v.version FROM task_versions v "
            "JOIN task_identities i ON i.uid = v.task_uid ORDER BY i.task_id"
        ).fetchall()
        legacy_state = conn.execute("SELECT last_run, last_status FROM tasks WHERE id = 'T1'")
        executions = conn.execute("SELECT COUNT(*) FROM task_executions").fetchone()[0]

        assert [tuple(r) for r in identities] == [("T1", 1, None), ("T2", 1, None)]
        assert [tuple(r) for r in versions] == [("T1", 1), ("T2", 1)]
        assert _schedule_version(conn) == 1
        assert tuple(legacy_state.fetchone()) == (NOW, "SUCCESS")
        assert executions == 0, "legacy state is converted only when the new path is enabled"


def test_repeated_init_db_changes_nothing(tmp_path: Path) -> None:
    _released_v4(tmp_path / "old.db")
    with closing(get_connection(tmp_path / "old.db")) as conn:
        init_db(conn)
        before = conn.execute("SELECT COUNT(*) FROM task_versions").fetchone()[0]
        init_db(conn)
        assert conn.execute("SELECT COUNT(*) FROM task_versions").fetchone()[0] == before


def test_interrupted_migration_resumes_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _released_v4(tmp_path / "old.db")

    def crash(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("simulated crash")

    with closing(get_connection(tmp_path / "old.db")) as conn:
        with monkeypatch.context() as patch:
            patch.setattr("mail_cadence.database.sync_definitions", crash)
            with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
                init_db(conn)
        conn.rollback()
        assert schema_version(conn) == SCHEMA_VERSION - 1

        init_db(conn)
        rows = conn.execute("SELECT task_id FROM task_identities ORDER BY task_id").fetchall()
        assert schema_version(conn) == SCHEMA_VERSION
        assert [r[0] for r in rows] == ["T1", "T2"]


# --- Identities, versions and generations ----------------------------------------------


def test_saving_an_unchanged_task_creates_no_version(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    before = _identity(conn)
    repo.save_single_task(_task())
    after = _identity(conn)
    assert tuple(after) == tuple(before)
    assert _versions(conn, after["uid"]) == [1]


def test_changed_task_gets_new_version_and_generation(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    uid = _identity(conn)["uid"]

    assert repo.save_single_task(_task(subject="Changed")) is True

    identity = _identity(conn)
    events = conn.execute(
        "SELECT kind, reason FROM execution_events WHERE task_uid = ?", (uid,)
    ).fetchall()
    assert identity["uid"] == uid
    assert identity["dispatch_generation"] == 2
    assert _versions(conn, uid) == [1, 2]
    assert [tuple(e) for e in events] == [("generation_started", "task_saved")]


def test_change_without_reset_keeps_generation(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    repo.save_single_task(_task(subject="Changed"), reset_window_state=False)
    identity = _identity(conn)
    assert identity["dispatch_generation"] == 1
    assert _versions(conn, identity["uid"]) == [1, 2]


def test_explicit_reset_starts_generation_without_version(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    assert repo.reset_task_window_state("T1") is True
    assert repo.reset_task_window_state("NOPE") is False
    identity = _identity(conn)
    reasons = [r[0] for r in conn.execute("SELECT reason FROM execution_events")]
    assert identity["dispatch_generation"] == 2
    assert _versions(conn, identity["uid"]) == [1]
    assert reasons == ["explicit_reset"]


def test_recreated_task_is_a_new_identity(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    old_uid = _identity(conn)["uid"]

    repo.delete_mail_task("T1")
    retired = conn.execute(
        "SELECT deleted_at FROM task_identities WHERE uid = ?", (old_uid,)
    ).fetchone()
    repo.save_single_task(_task())

    new = _identity(conn)
    assert retired[0] is not None
    assert new["uid"] != old_uid
    assert new["dispatch_generation"] == 1
    assert _versions(conn, old_uid) == [1], "the old identity keeps its history"
    assert _versions(conn, new["uid"]) == [1]


def test_replace_import_keeps_identities_of_surviving_tasks(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    repo.save_single_task(_task("T2"))
    uid = _identity(conn)["uid"]
    report = ReportSettings(recipient="admin@example.com")

    repo.import_configuration(
        MailConfiguration(report_settings=report, tasks=[_task()]), _schedules(), mode="replace"
    )

    live = conn.execute("SELECT task_id FROM task_identities WHERE deleted_at IS NULL")
    assert _identity(conn)["uid"] == uid
    assert _versions(conn, uid) == [1]
    assert [r[0] for r in live] == ["T1"], "T2 is no longer configured"


def test_schedule_version_follows_windows_and_timezone(conn: sqlite3.Connection) -> None:
    repo = DatabaseRepository(conn)
    repo.save_work_schedules(_schedules())
    assert _schedule_version(conn) == 1

    repo.save_single_schedule(
        Schedule(
            id="S1",
            description="only the description",
            allowed_hours=[HourWindow(start="06:00", end="18:00")],
        )
    )
    assert _schedule_version(conn) == 1

    repo.save_work_schedules(_schedules(HourWindow(start="07:00", end="18:00")))
    assert _schedule_version(conn) == 2

    repo.save_work_schedules(
        _schedules(HourWindow(start="07:00", end="18:00"), timezone="Europe/Warsaw")
    )
    assert _schedule_version(conn) == 3


# --- Constraints (T17: invalid rows are rejected by SQLite itself) ----------------------


def _execution(
    conn: sqlite3.Connection, uid: int, *, window_key: str = "S1@2026-10-27T06:00:00+00:00"
) -> int:
    cursor = conn.execute(
        "INSERT INTO task_executions (task_uid, dispatch_generation, window_key, "
        "task_version, state, created_at) VALUES (?, 1, ?, 1, 'CLAIMED', ?)",
        (uid, window_key, NOW),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_only_one_execution_per_task_may_hold_the_right_to_send(
    conn: sqlite3.Connection,
) -> None:
    _repo_with_task(conn)
    uid = _identity(conn)["uid"]
    first = _execution(conn, uid)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _execution(conn, uid, window_key="manual:key-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _execution(conn, uid)

    conn.execute("UPDATE task_executions SET state = 'SUCCEEDED' WHERE execution_id = ?", (first,))
    _execution(conn, uid, window_key="manual:key-1")


def test_execution_must_reference_an_existing_version(conn: sqlite3.Connection) -> None:
    _repo_with_task(conn)
    uid = _identity(conn)["uid"]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO task_executions (task_uid, dispatch_generation, window_key, "
            "task_version, state, created_at) VALUES (?, 1, 'k', 99, 'CLAIMED', ?)",
            (uid, NOW),
        )


@pytest.mark.parametrize(
    ("state", "attempts", "max_attempts"),
    [("SENT", 0, 1), ("PENDING", 2, 1), ("PENDING", -1, 1), ("PENDING", 0, 0)],
)
def test_recipient_rows_are_checked(
    conn: sqlite3.Connection, state: str, attempts: int, max_attempts: int
) -> None:
    _repo_with_task(conn)
    execution_id = _execution(conn, _identity(conn)["uid"])
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO execution_recipients (execution_id, envelope_address, "
            "display_address, recipient_type, state, attempt_count, max_attempts, updated_at) "
            "VALUES (?, 'a@example.com', 'a@example.com', 'to', ?, ?, ?, ?)",
            (execution_id, state, attempts, max_attempts, NOW),
        )


def test_task_with_history_cannot_lose_its_identity_row(conn: sqlite3.Connection) -> None:
    repo = _repo_with_task(conn)
    uid = _identity(conn)["uid"]
    _execution(conn, uid)
    conn.commit()

    assert repo.delete_mail_task("T1") is True
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM task_identities WHERE uid = ?", (uid,))
