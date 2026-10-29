"""Health check for monitoring: ``mail-cadence status``.

Follows the Nagios plugin conventions (exit code 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN,
one status line with performance data), so Nagios, Icinga and Zabbix can run it
directly. The database is opened read-only: the check never creates, migrates or
writes it.
"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any

from mail_cadence.database import (
    SCHEMA_VERSION,
    get_readonly_connection,
    schema_version,
    to_utc_iso,
)
from mail_cadence.repository import DatabaseRepository


class Severity(IntEnum):
    """Nagios plugin states; the value is the exit code."""

    OK = 0
    WARNING = 1
    CRITICAL = 2
    UNKNOWN = 3


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    """``90s``, ``30m``, ``2h`` or ``1d``; raises ValueError otherwise."""
    match = _DURATION_RE.match(text.lower())
    if match is None:
        msg = f"invalid duration {text!r}: use a number with s, m, h or d (e.g. 30m)"
        raise ValueError(msg)
    value = timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})
    if value <= timedelta(0):
        msg = f"invalid duration {text!r}: must be greater than zero"
        raise ValueError(msg)
    return value


# (unit length in seconds, suffix): a unit is used up to twice the next smaller one.
_AGE_UNITS: tuple[tuple[int, str], ...] = ((86400, "d"), (3600, "h"), (60, "m"))


def format_age(age: timedelta) -> str:
    """Short age for humans: 90s, 45m, 26h, 3d."""
    seconds = int(age.total_seconds())
    for unit, suffix in _AGE_UNITS:
        if seconds >= 2 * unit:
            return f"{seconds // unit}{suffix}"
    return f"{seconds}s"


@dataclass(frozen=True, slots=True)
class Thresholds:
    warning_age: timedelta = timedelta(minutes=30)
    critical_age: timedelta = timedelta(hours=2)
    since: timedelta = timedelta(hours=24)

    @classmethod
    def parse(cls, warning_age: str, critical_age: str, since: str) -> "Thresholds":
        thresholds = cls(
            parse_duration(warning_age), parse_duration(critical_age), parse_duration(since)
        )
        if thresholds.warning_age > thresholds.critical_age:
            msg = "--warning-age must not be longer than --critical-age"
            raise ValueError(msg)
        return thresholds


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """What the database says; ``last_run_finished_at`` is None when no run finished."""

    needs_review: int = 0
    executions_failed: int = 0
    reports_pending: int = 0
    reports_unknown: int = 0
    reports_failed: int = 0
    stalled_runs: int = 0
    last_run_finished_at: datetime | None = None

    @classmethod
    def from_counts(cls, counts: dict[str, Any]) -> "HealthSnapshot":
        finished = counts["last_run_finished_at"]
        return cls(
            needs_review=int(counts["needs_review"]),
            executions_failed=int(counts["executions_failed"]),
            reports_pending=int(counts["reports_pending"]),
            reports_unknown=int(counts["reports_unknown"]),
            reports_failed=int(counts["reports_failed"]),
            stalled_runs=int(counts["stalled_runs"]),
            last_run_finished_at=datetime.fromisoformat(finished) if finished else None,
        )


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    check: str
    message: str


# Counter, severity, message; {n} is the counter, {since} and {stalled} the thresholds.
_COUNT_RULES: tuple[tuple[str, Severity, str], ...] = (
    ("needs_review", Severity.CRITICAL, "{n} awaiting operator review"),
    ("reports_unknown", Severity.CRITICAL, "{n} with unknown outcome in the last {since}"),
    ("reports_pending", Severity.WARNING, "{n} waiting for a retry"),
    ("reports_failed", Severity.WARNING, "{n} not delivered in the last {since}"),
    ("executions_failed", Severity.WARNING, "{n} failed, partial or expired in the last {since}"),
    ("stalled_runs", Severity.WARNING, "{n} started over {stalled} ago and never finished"),
)
_NOUNS = {
    "needs_review": "execution",
    "executions_failed": "execution",
    "reports_unknown": "status report",
    "reports_pending": "status report",
    "reports_failed": "status report",
    "stalled_runs": "dispatch run",
}
_PERF_COUNTERS = tuple(rule[0] for rule in _COUNT_RULES)


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@dataclass(frozen=True, slots=True)
class HealthReport:
    severity: Severity
    findings: tuple[Finding, ...]
    checked_at: datetime
    snapshot: HealthSnapshot | None = None
    thresholds: Thresholds | None = None

    def last_run_age(self) -> timedelta | None:
        if self.snapshot is None or self.snapshot.last_run_finished_at is None:
            return None
        return max(timedelta(0), self.checked_at - self.snapshot.last_run_finished_at)

    def status_line(self) -> str:
        """One line in Nagios plugin format: ``MAIL-CADENCE STATE - text | perfdata``."""
        parts = [f.message for f in self.findings]
        age = self.last_run_age()
        if age is not None and not any(f.check == "last_run" for f in self.findings):
            parts.append(f"last run {format_age(age)} ago")
        line = f"MAIL-CADENCE {self.severity.name} - {'; '.join(parts) or 'no data'}"
        perfdata = self.perfdata()
        return f"{line} | {perfdata}" if perfdata else line

    def perfdata(self) -> str:
        if self.snapshot is None or self.thresholds is None:
            return ""
        fields = [f"{name}={getattr(self.snapshot, name)};;;0" for name in _PERF_COUNTERS]
        age = self.last_run_age()
        value = f"{int(age.total_seconds())}s" if age is not None else "U"
        warn = int(self.thresholds.warning_age.total_seconds())
        crit = int(self.thresholds.critical_age.total_seconds())
        fields.append(f"last_run_age={value};{warn};{crit};0")
        return " ".join(fields)

    def as_dict(self) -> dict[str, Any]:
        """The JSON output of ``mail-cadence status --json``."""
        result: dict[str, Any] = {
            "status": self.severity.name,
            "exit_code": int(self.severity),
            "checked_at": to_utc_iso(self.checked_at),
            "findings": [
                {"severity": f.severity.name, "check": f.check, "message": f.message}
                for f in self.findings
            ],
        }
        if self.snapshot is not None and self.thresholds is not None:
            age = self.last_run_age()
            finished = self.snapshot.last_run_finished_at
            result["metrics"] = {name: getattr(self.snapshot, name) for name in _PERF_COUNTERS}
            result["metrics"]["last_run_finished_at"] = to_utc_iso(finished) if finished else None
            result["metrics"]["last_run_age_seconds"] = (
                int(age.total_seconds()) if age is not None else None
            )
            result["thresholds"] = {
                "warning_age_seconds": int(self.thresholds.warning_age.total_seconds()),
                "critical_age_seconds": int(self.thresholds.critical_age.total_seconds()),
                "since_seconds": int(self.thresholds.since.total_seconds()),
            }
        return result


def _last_run_finding(
    snapshot: HealthSnapshot, thresholds: Thresholds, now: datetime
) -> list[Finding]:
    finished = snapshot.last_run_finished_at
    if finished is None:
        return [Finding(Severity.CRITICAL, "last_run", "no dispatch run has finished yet")]
    age = now - finished
    for limit, severity in (
        (thresholds.critical_age, Severity.CRITICAL),
        (thresholds.warning_age, Severity.WARNING),
    ):
        if age > limit:
            text = f"last run finished {format_age(age)} ago (limit {format_age(limit)})"
            return [Finding(severity, "last_run", text)]
    return []


def _count_findings(snapshot: HealthSnapshot, thresholds: Thresholds) -> list[Finding]:
    findings = []
    for name, severity, template in _COUNT_RULES:
        n = getattr(snapshot, name)
        if n:
            message = template.format(
                n=_counted(n, _NOUNS[name]),
                since=format_age(thresholds.since),
                stalled=format_age(thresholds.critical_age),
            )
            findings.append(Finding(severity, name, message))
    return findings


def evaluate(snapshot: HealthSnapshot, thresholds: Thresholds, now: datetime) -> HealthReport:
    """Apply the rules; the worst finding decides the state, CRITICAL findings first."""
    findings = _last_run_finding(snapshot, thresholds, now) + _count_findings(snapshot, thresholds)
    findings.sort(key=lambda f: f.severity, reverse=True)
    severity = max((f.severity for f in findings), default=Severity.OK)
    return HealthReport(severity, tuple(findings), now, snapshot, thresholds)


def unknown(message: str, now: datetime) -> HealthReport:
    """The check itself could not run (bad options, unreadable database, other schema)."""
    return HealthReport(Severity.UNKNOWN, (Finding(Severity.UNKNOWN, "check", message),), now)


def _schema_problem(version: int) -> str | None:
    if version > SCHEMA_VERSION:
        return (
            f"database schema version {version} is newer than this mail-cadence supports "
            f"({SCHEMA_VERSION}); upgrade mail-cadence"
        )
    if version < SCHEMA_VERSION:
        return (
            f"database schema version {version} is older than {SCHEMA_VERSION}; run "
            "mail-cadence once (dispatch, import-xml or serve) to upgrade it"
        )
    return None


def check(db_path: str | Path, thresholds: Thresholds, now: datetime | None = None) -> HealthReport:
    """Read the database at ``db_path`` and evaluate it; never writes to it."""
    now = now or datetime.now(UTC)
    try:
        with closing(get_readonly_connection(db_path)) as conn:
            problem = _schema_problem(schema_version(conn))
            if problem is not None:
                return unknown(problem, now)
            counts = DatabaseRepository(conn).health_counts(
                since=to_utc_iso(now - thresholds.since),
                stalled_before=to_utc_iso(now - thresholds.critical_age),
            )
    except sqlite3.Error as exc:
        return unknown(f"cannot read database {db_path}: {exc}", now)
    return evaluate(HealthSnapshot.from_counts(counts), thresholds, now)
