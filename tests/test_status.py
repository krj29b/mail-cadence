"""``mail-cadence status``: rules, output formats, read-only access and real dispatch state."""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mail_cadence.cli import app
from mail_cadence.database import MIGRATIONS, SCHEMA_VERSION, get_connection, init_db
from mail_cadence.health import (
    HealthSnapshot,
    Severity,
    Thresholds,
    check,
    evaluate,
    format_age,
    parse_duration,
)
from mail_cadence.models import (
    BodyTemplate,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.pipeline import DispatchOptions, dispatch_single_task, run_dispatch
from mail_cadence.repository import DatabaseRepository
from smtp_sink import SMTPSink

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
DEFAULTS = Thresholds()
A = "a@example.com"
B = "b@example.com"
REPORT_TO = "ops@example.com"
REF_DT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)  # inside the 06:00-18:00 UTC window

runner = CliRunner()


# --- Durations and thresholds ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("2H", timedelta(hours=2)),
        (" 1d ", timedelta(days=1)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "m", "1.5h", "-1h", "0m", "2w", "1h30m"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (119, "119s"), (120, "2m"), (7199, "119m"), (7200, "2h"), (172800, "2d")],
)
def test_format_age(seconds: int, expected: str) -> None:
    assert format_age(timedelta(seconds=seconds)) == expected


def test_warning_age_longer_than_critical_age_is_rejected() -> None:
    with pytest.raises(ValueError, match="--warning-age"):
        Thresholds.parse("3h", "1h", "24h")


# --- Rules -----------------------------------------------------------------------------


def _snapshot(*, age: timedelta | None = timedelta(minutes=5), **counts: int) -> HealthSnapshot:
    finished = NOW - age if age is not None else None
    return HealthSnapshot(last_run_finished_at=finished, **counts)


def test_recent_run_without_problems_is_ok() -> None:
    report = evaluate(_snapshot(), DEFAULTS, NOW)

    assert report.severity == Severity.OK
    assert report.findings == ()
    assert report.status_line().startswith("MAIL-CADENCE OK - last run 5m ago | ")


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(minutes=30), Severity.OK),
        (timedelta(minutes=31), Severity.WARNING),
        (timedelta(hours=2), Severity.WARNING),
        (timedelta(hours=2, seconds=1), Severity.CRITICAL),
        (None, Severity.CRITICAL),
    ],
)
def test_age_of_the_last_run(age: timedelta | None, expected: Severity) -> None:
    report = evaluate(_snapshot(age=age), DEFAULTS, NOW)

    assert report.severity == expected
    assert [f.check for f in report.findings] == ([] if expected == Severity.OK else ["last_run"])


@pytest.mark.parametrize(
    ("counter", "expected", "text"),
    [
        ("needs_review", Severity.CRITICAL, "2 executions awaiting operator review"),
        ("reports_unknown", Severity.CRITICAL, "2 status reports with unknown outcome"),
        ("reports_pending", Severity.WARNING, "2 status reports waiting for a retry"),
        ("reports_failed", Severity.WARNING, "2 status reports not delivered in the last 24h"),
        ("executions_failed", Severity.WARNING, "2 executions failed, partial or expired"),
        ("stalled_runs", Severity.WARNING, "2 dispatch runs started over 2h ago"),
    ],
)
def test_each_counter_has_its_severity(counter: str, expected: Severity, text: str) -> None:
    changes: dict[str, Any] = {counter: 2}
    report = evaluate(replace(_snapshot(), **changes), DEFAULTS, NOW)

    assert report.severity == expected
    assert [f.check for f in report.findings] == [counter]
    assert text in report.status_line()


def test_singular_wording() -> None:
    line = evaluate(_snapshot(needs_review=1), DEFAULTS, NOW).status_line()
    assert "1 execution awaiting operator review" in line


def test_worst_finding_decides_and_comes_first() -> None:
    snapshot = _snapshot(age=timedelta(minutes=45), reports_pending=1, needs_review=1)
    report = evaluate(snapshot, DEFAULTS, NOW)

    assert report.severity == Severity.CRITICAL
    assert [f.severity for f in report.findings] == [
        Severity.CRITICAL,
        Severity.WARNING,
        Severity.WARNING,
    ]
    assert report.status_line().startswith(
        "MAIL-CADENCE CRITICAL - 1 execution awaiting operator review; last run finished 45m ago"
    )


def test_perfdata_and_json() -> None:
    report = evaluate(_snapshot(age=timedelta(seconds=240), reports_failed=3), DEFAULTS, NOW)

    perfdata = report.status_line().split(" | ", 1)[1].split()
    assert "reports_failed=3;;;0" in perfdata
    assert perfdata[-1] == "last_run_age=240s;1800;7200;0"

    data = json.loads(json.dumps(report.as_dict()))
    assert data["status"] == "WARNING"
    assert data["exit_code"] == 1
    assert data["findings"] == [
        {
            "severity": "WARNING",
            "check": "reports_failed",
            "message": "3 status reports not delivered in the last 24h",
        }
    ]
    assert data["metrics"]["reports_failed"] == 3
    assert data["metrics"]["last_run_age_seconds"] == 240
    assert data["thresholds"] == {
        "warning_age_seconds": 1800,
        "critical_age_seconds": 7200,
        "since_seconds": 86400,
    }


def test_never_finished_run_has_undetermined_age() -> None:
    report = evaluate(_snapshot(age=None), DEFAULTS, NOW)

    assert report.status_line().endswith("last_run_age=U;1800;7200;0")
    assert report.as_dict()["metrics"]["last_run_age_seconds"] is None


# --- Reading the database --------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_database_is_unknown_and_not_created(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    report = check(db_path, DEFAULTS, NOW)

    assert report.severity == Severity.UNKNOWN
    assert "cannot read database" in report.status_line()
    assert not db_path.exists()


def test_file_that_is_not_a_database_is_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "junk.db"
    db_path.write_bytes(b"not a database" * 100)

    assert check(db_path, DEFAULTS, NOW).severity == Severity.UNKNOWN


def test_older_schema_is_unknown_and_not_migrated(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with closing(get_connection(db_path)) as conn:
        for version, migrate in enumerate(MIGRATIONS[:4], start=1):
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {version:d}")
        conn.commit()
    before = _digest(db_path)

    report = check(db_path, DEFAULTS, NOW)

    assert report.severity == Severity.UNKNOWN
    assert "older than" in report.status_line()
    assert _digest(db_path) == before
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_newer_schema_is_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "new.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1:d}")
        conn.commit()

    report = check(db_path, DEFAULTS, NOW)

    assert report.severity == Severity.UNKNOWN
    assert "upgrade mail-cadence" in report.status_line()


def test_stalled_run_counts_only_within_since(tmp_path: Path) -> None:
    db_path = tmp_path / "stalled.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        DatabaseRepository(conn, clock=lambda: NOW - timedelta(hours=3)).start_run("r1", "cli")
        DatabaseRepository(conn, clock=lambda: NOW - timedelta(days=2)).start_run("r2", "cli")
        DatabaseRepository(conn, clock=lambda: NOW - timedelta(minutes=5)).start_run("r3", "cli")

    report = check(db_path, DEFAULTS, NOW)

    assert report.snapshot is not None
    assert report.snapshot.stalled_runs == 1  # r2 is older than --since, r3 still running


# --- Real dispatch state ---------------------------------------------------------------


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _prepare(tmp_path: Path, *, report: bool = False, max_retries: int = 0) -> Path:
    (tmp_path / "body.html.j2").write_text("<p>Hello</p>", encoding="utf-8")
    db_path = tmp_path / "cadence.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
        repo = DatabaseRepository(conn)
        window = HourWindow(start="06:00", end="18:00")
        repo.save_work_schedules(
            WorkSchedules(timezone="UTC", schedules=[Schedule(id="S1", allowed_hours=[window])])
        )
        repo.save_single_task(
            MailTask(
                id="T1",
                schedule_ref="S1",
                sender="cadence@example.com",
                subject="Subject",
                max_retries=max_retries,
                recipients=Recipients(to=[A, B]),
                body_template=BodyTemplate(template_path="body.html.j2"),
            )
        )
        if report:
            repo.save_report_settings(ReportSettings(recipient=REPORT_TO))
    return db_path


def _settings(port: int) -> Settings:
    return Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]


def _dispatch(db_path: Path, port: int, *, dry_run: bool = False) -> int:
    options = DispatchOptions(
        db_path=db_path, template_dir=db_path.parent, dry_run=dry_run, ref_dt=REF_DT
    )
    return asyncio.run(run_dispatch(options, _settings(port)))


def _checks(db_path: Path) -> dict[str, Severity]:
    report = check(db_path, DEFAULTS)
    return {f.check: f.severity for f in report.findings}


def test_successful_run_is_ok(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, report=True)
    assert _dispatch(db_path, sink.port) == 0

    assert check(db_path, DEFAULTS).severity == Severity.OK


def test_unknown_delivery_is_critical(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.drop_after_data = 1  # connection lost after the body: outcome unknown
    _dispatch(db_path, sink.port)

    assert _checks(db_path) == {"needs_review": Severity.CRITICAL}


def test_refused_task_is_a_warning(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    sink.rcpt_codes.update({A: 550, B: 550})
    _dispatch(db_path, sink.port)

    assert _checks(db_path) == {"executions_failed": Severity.WARNING}


@pytest.mark.parametrize(("code", "counter"), [(450, "reports_pending"), (550, "reports_failed")])
def test_undelivered_report_is_a_warning(
    tmp_path: Path, sink: SMTPSink, code: int, counter: str
) -> None:
    db_path = _prepare(tmp_path, report=True)
    sink.rcpt_codes[REPORT_TO] = code
    _dispatch(db_path, sink.port)

    assert _checks(db_path) == {counter: Severity.WARNING}


def test_dry_runs_and_manual_sends_are_not_dispatch_runs(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    _dispatch(db_path, sink.port, dry_run=True)
    asyncio.run(dispatch_single_task(db_path, "T1", _settings(sink.port), db_path.parent))

    assert _checks(db_path) == {"last_run": Severity.CRITICAL}


def test_status_leaves_the_database_file_unchanged(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path, report=True)
    _dispatch(db_path, sink.port)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _digest(db_path)

    check(db_path, DEFAULTS)

    assert _digest(db_path) == before


# --- Command line ----------------------------------------------------------------------


def test_cli_status_exit_code_and_line(tmp_path: Path, sink: SMTPSink) -> None:
    db_path = _prepare(tmp_path)
    _dispatch(db_path, sink.port)

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.output.startswith("MAIL-CADENCE OK - last run ")


def test_cli_status_json(tmp_path: Path) -> None:
    db_path = _prepare(tmp_path)

    result = runner.invoke(app, ["status", "--db", str(db_path), "--json"])

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["status"] == "CRITICAL"
    assert data["findings"][0]["check"] == "last_run"


def test_cli_status_invalid_option_is_unknown(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(tmp_path / "x.db"), "--since", "5x"])

    assert result.exit_code == 3
    assert result.output.startswith("MAIL-CADENCE UNKNOWN - invalid duration '5x'")
    assert not (tmp_path / "x.db").exists()
