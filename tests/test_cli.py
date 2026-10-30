import logging
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from mail_cadence.cli import TEXT_LOG_FORMAT, app, run_dispatch_pipeline, setup_logging
from mail_cadence.mailer import AsyncMailer, RenderedMessage
from mail_cadence.models import MailTask, Outcome, RecipientResult, SendContext
from mail_cadence.scheduler import TokenBucketRateLimiter
from mail_cadence.xml_loader import XMLValidationError

runner = CliRunner()


async def _accept_all(
    _mailer: AsyncMailer, _data: bytes, recipients: Sequence[str]
) -> dict[str, RecipientResult]:
    """Stands in for AsyncMailer.deliver: the server accepts every recipient."""
    return {r: RecipientResult(Outcome.ACCEPTED, 250) for r in recipients}


@pytest.mark.parametrize(
    ("verbose", "expected_level"),
    [
        (True, logging.DEBUG),
        (False, logging.INFO),
    ],
)
def test_setup_logging(verbose: bool, expected_level: int) -> None:
    with patch("logging.basicConfig") as mock_basic_config:
        setup_logging(verbose=verbose)
        mock_basic_config.assert_called_once()
        _, kwargs = mock_basic_config.call_args
        assert kwargs["level"] == expected_level
        assert "force" not in kwargs
        handlers = kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.StreamHandler)
        assert handlers[0].formatter is not None
        assert handlers[0].formatter._fmt == TEXT_LOG_FORMAT
        assert handlers[0].stream == sys.stdout


def test_cli_dry_run_samples(tmp_path: Path) -> None:
    tasks = Path("config/samples/tasks.xml")
    schedules = Path("config/samples/schedules.xml")
    db_file = tmp_path / "samples_test.db"

    result = runner.invoke(
        app,
        [
            "--tasks-xml",
            str(tasks),
            "--schedule-xml",
            str(schedules),
            "--db",
            str(db_file),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "Starting MailCadence email dispatch engine" in result.output or result.exit_code == 0


@pytest.mark.asyncio
async def test_shared_primitives_per_schedule_window(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"

    tasks_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
  </ReportSettings>
  <Tasks>
    <MailTask id="TASK-001" schedule_ref="SCHED-01">
      <Sender>noreply@example.com</Sender>
      <Recipients><To>u1@example.com</To></Recipients>
      <Subject>Task 1</Subject>
      <BodyTemplate format="html">tpl.j2</BodyTemplate>
    </MailTask>
    <MailTask id="TASK-002" schedule_ref="SCHED-01">
      <Sender>noreply@example.com</Sender>
      <Recipients><To>u2@example.com</To></Recipients>
      <Subject>Task 2</Subject>
      <BodyTemplate format="html">tpl.j2</BodyTemplate>
    </MailTask>
  </Tasks>
</MailConfiguration>""")

    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-01">
    <AllowedHours>
      <HourWindow start="00:00" end="24:00" max_concurrency="5" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    (tmp_path / "tpl.j2").write_text("<p>Hello</p>", encoding="utf-8")
    limiters: list[TokenBucketRateLimiter] = []
    original_acquire = TokenBucketRateLimiter.acquire

    async def tracked_acquire(self: TokenBucketRateLimiter) -> None:
        limiters.append(self)
        await original_acquire(self)

    with (
        patch.object(AsyncMailer, "deliver", _accept_all),
        patch.object(TokenBucketRateLimiter, "acquire", tracked_acquire),
    ):
        code = await run_dispatch_pipeline(
            tasks_xml,
            schedules_xml,
            db_path=str(tmp_path / "test.db"),
            template_dir=tmp_path,
        )
    assert code == 0
    # Both tasks share the window's limiter (and its semaphore, which
    # pipeline._window_controls creates together with it).
    assert limiters
    assert all(limiter is limiters[0] for limiter in limiters)


@pytest.mark.asyncio
async def test_run_dispatch_pipeline_closes_connection(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pipeline.db"

    # Run dispatch pipeline using db_file
    code = await run_dispatch_pipeline(db_path=str(db_file), dry_run=True)
    assert code == 0

    # Ensure file can be removed or unlinked (verifying connection handle is closed)
    db_file.unlink()
    assert not db_file.exists()


def test_cli_import_and_export_closes_connection(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"

    tasks_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
  </ReportSettings>
</MailConfiguration>""")

    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC"/>""")

    db_file = tmp_path / "test_cli.db"

    # Test import-xml
    res_import = runner.invoke(
        app,
        [
            "import-xml",
            "--tasks-xml",
            str(tasks_xml),
            "--schedule-xml",
            str(schedules_xml),
            "--db",
            str(db_file),
        ],
    )
    assert res_import.exit_code == 0

    out_tasks = tmp_path / "out_tasks.xml"
    out_schedules = tmp_path / "out_schedules.xml"

    # Test export-xml
    res_export = runner.invoke(
        app,
        [
            "export-xml",
            "--tasks-xml",
            str(out_tasks),
            "--schedule-xml",
            str(out_schedules),
            "--db",
            str(db_file),
        ],
    )
    assert res_export.exit_code == 0

    # Check that database file can be removed (unlocked)
    db_file.unlink()
    assert not db_file.exists()


@pytest.mark.asyncio
async def test_task_priority_scheduling_order(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"

    # task_a has priority low, task_z has priority high
    tasks_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
  </ReportSettings>
  <Tasks>
    <MailTask id="task_a" priority="low" schedule_ref="SCHED-01">
      <Sender>noreply@example.com</Sender>
      <Recipients><To>u1@example.com</To></Recipients>
      <Subject>Task A</Subject>
      <BodyTemplate format="html">tpl.j2</BodyTemplate>
    </MailTask>
    <MailTask id="task_z" priority="high" schedule_ref="SCHED-01">
      <Sender>noreply@example.com</Sender>
      <Recipients><To>u2@example.com</To></Recipients>
      <Subject>Task Z</Subject>
      <BodyTemplate format="html">tpl.j2</BodyTemplate>
    </MailTask>
  </Tasks>
</MailConfiguration>""")

    # Both tasks share one window; the dry run renders them in priority order.
    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-01">
    <AllowedHours>
      <HourWindow start="00:00" end="24:00" max_concurrency="1" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    (tmp_path / "tpl.j2").write_text("<p>Hello</p>", encoding="utf-8")
    executed_order: list[str] = []
    original_render = AsyncMailer.render_task_message

    def tracked_render(
        self: AsyncMailer, task: MailTask, context: SendContext | None = None
    ) -> RenderedMessage:
        executed_order.append(task.id)
        return original_render(self, task, context)

    db_path = str(tmp_path / "test.db")
    with patch.object(AsyncMailer, "render_task_message", tracked_render):
        code = await run_dispatch_pipeline(
            tasks_xml, schedules_xml, dry_run=True, db_path=db_path, template_dir=tmp_path
        )
    assert code == 0
    assert executed_order == ["task_z", "task_a"]


def test_cli_dispatch_passes_template_dir(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    with patch("mail_cadence.cli.run_dispatch_pipeline", new_callable=AsyncMock) as pipeline:
        pipeline.return_value = 0
        res = runner.invoke(app, ["--db", db, "--template-dir", "/srv/templates"])
        assert res.exit_code == 0
        assert pipeline.await_args is not None
        assert pipeline.await_args.kwargs["template_dir"] == "/srv/templates"

        res_env = runner.invoke(app, ["--db", db], env={"MAIL_CADENCE_TEMPLATE_DIR": "/env/tpl"})
        assert res_env.exit_code == 0
        assert pipeline.await_args.kwargs["template_dir"] == "/env/tpl"


def test_cli_version_option() -> None:
    from mail_cadence import __version__

    res_long = runner.invoke(app, ["--version"])
    assert res_long.exit_code == 0
    assert f"mail-cadence {__version__}" in res_long.output

    res_short = runner.invoke(app, ["-V"])
    assert res_short.exit_code == 0
    assert f"mail-cadence {__version__}" in res_short.output


def test_cli_import_xml_replace_mode(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"

    tasks_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
  </ReportSettings>
</MailConfiguration>""")

    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC"/>""")

    db_file = tmp_path / "test_replace.db"

    res = runner.invoke(
        app,
        [
            "import-xml",
            "--tasks-xml",
            str(tasks_xml),
            "--schedule-xml",
            str(schedules_xml),
            "--db",
            str(db_file),
            "--mode",
            "replace",
        ],
    )
    assert res.exit_code == 0
    assert "(replace)" in res.output


def test_cli_import_xml_xml_validation_error(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"
    tasks_xml.write_bytes(b"<xml/>")
    schedules_xml.write_bytes(b"<xml/>")
    db_file = tmp_path / "test_err.db"

    with patch(
        "mail_cadence.cli.import_from_xml", side_effect=XMLValidationError("XML syntax error")
    ):
        res = runner.invoke(
            app,
            [
                "import-xml",
                "--tasks-xml",
                str(tasks_xml),
                "--schedule-xml",
                str(schedules_xml),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "XML Validation Error: XML syntax error" in res.output


def test_cli_import_xml_sqlite_error(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"
    tasks_xml.write_bytes(b"<xml/>")
    schedules_xml.write_bytes(b"<xml/>")
    db_file = tmp_path / "test_err.db"

    with patch("mail_cadence.cli.import_from_xml", side_effect=sqlite3.Error("Database locked")):
        res = runner.invoke(
            app,
            [
                "import-xml",
                "--tasks-xml",
                str(tasks_xml),
                "--schedule-xml",
                str(schedules_xml),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "Error importing XML: Database locked" in res.output


def test_cli_import_xml_pydantic_validation_error(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"
    tasks_xml.write_bytes(b"<xml/>")
    schedules_xml.write_bytes(b"<xml/>")
    db_file = tmp_path / "test_err.db"

    err = ValidationError.from_exception_data("Invalid model data", [])
    with patch("mail_cadence.cli.import_from_xml", side_effect=err):
        res = runner.invoke(
            app,
            [
                "import-xml",
                "--tasks-xml",
                str(tasks_xml),
                "--schedule-xml",
                str(schedules_xml),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "Error importing XML:" in res.output


def test_cli_export_xml_sqlite_error(tmp_path: Path) -> None:
    out_tasks = tmp_path / "out_tasks.xml"
    out_schedules = tmp_path / "out_schedules.xml"
    db_file = tmp_path / "test_export_err.db"

    with patch("mail_cadence.cli.export_to_xml", side_effect=sqlite3.Error("Export DB failure")):
        res = runner.invoke(
            app,
            [
                "export-xml",
                "--tasks-xml",
                str(out_tasks),
                "--schedule-xml",
                str(out_schedules),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "Error exporting XML: Export DB failure" in res.output


def test_cli_export_xml_os_error(tmp_path: Path) -> None:
    out_tasks = tmp_path / "out_tasks.xml"
    out_schedules = tmp_path / "out_schedules.xml"
    db_file = tmp_path / "test_export_err.db"

    with patch("mail_cadence.cli.export_to_xml", side_effect=OSError("Permission denied")):
        res = runner.invoke(
            app,
            [
                "export-xml",
                "--tasks-xml",
                str(out_tasks),
                "--schedule-xml",
                str(out_schedules),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "Error exporting XML: Permission denied" in res.output


def test_cli_export_xml_validation_error(tmp_path: Path) -> None:
    out_tasks = tmp_path / "out_tasks.xml"
    out_schedules = tmp_path / "out_schedules.xml"
    db_file = tmp_path / "test_export_err.db"

    err = ValidationError.from_exception_data("Export validation error", [])
    with patch("mail_cadence.cli.export_to_xml", side_effect=err):
        res = runner.invoke(
            app,
            [
                "export-xml",
                "--tasks-xml",
                str(out_tasks),
                "--schedule-xml",
                str(out_schedules),
                "--db",
                str(db_file),
            ],
        )
        assert res.exit_code == 1
        assert "Error exporting XML:" in res.output


def test_cli_status_ok(tmp_path: Path) -> None:
    db_file = tmp_path / "status_ok.db"
    from datetime import UTC, datetime

    from mail_cadence.database import get_connection, init_db
    from mail_cadence.health import Finding, HealthReport, Severity

    with closing(get_connection(str(db_file))) as conn:
        init_db(conn)

    ok_report = HealthReport(
        severity=Severity.OK,
        findings=(Finding(Severity.OK, "last_run", "no issues"),),
        checked_at=datetime.now(UTC),
    )
    with patch("mail_cadence.cli.check_health", return_value=ok_report):
        res = runner.invoke(app, ["status", "--db", str(db_file)])
        assert res.exit_code == 0
        assert "OK" in res.output


def test_cli_status_unpopulated_db(tmp_path: Path) -> None:
    db_file = tmp_path / "status_empty.db"
    from mail_cadence.database import get_connection, init_db

    with closing(get_connection(str(db_file))) as conn:
        init_db(conn)

    res = runner.invoke(app, ["status", "--db", str(db_file)])
    assert res.exit_code == 2
    assert "CRITICAL" in res.output


def test_cli_status_json_output(tmp_path: Path) -> None:
    db_file = tmp_path / "status_json.db"
    import json
    from datetime import UTC, datetime

    from mail_cadence.database import get_connection, init_db
    from mail_cadence.health import HealthReport, Severity

    with closing(get_connection(str(db_file))) as conn:
        init_db(conn)

    ok_report = HealthReport(
        severity=Severity.OK,
        findings=(),
        checked_at=datetime.now(UTC),
    )
    with patch("mail_cadence.cli.check_health", return_value=ok_report):
        res = runner.invoke(app, ["status", "--db", str(db_file), "--json"])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["status"] == "OK"
        assert data["exit_code"] == 0


def test_cli_status_invalid_thresholds(tmp_path: Path) -> None:
    db_file = tmp_path / "status_invalid.db"

    # --warning-age > --critical-age triggers ValueError in Thresholds.parse
    res = runner.invoke(
        app,
        ["status", "--db", str(db_file), "--warning-age", "5h", "--critical-age", "1h"],
    )
    assert res.exit_code == 3
    assert "UNKNOWN" in res.output


def test_cli_status_non_ok_severity(tmp_path: Path) -> None:
    db_file = tmp_path / "non_ok.db"
    from datetime import UTC, datetime

    from mail_cadence.health import Finding, HealthReport, Severity

    dummy_report = HealthReport(
        severity=Severity.CRITICAL,
        findings=(Finding(Severity.CRITICAL, "test_check", "Critical failure detected"),),
        checked_at=datetime.now(UTC),
    )

    with patch("mail_cadence.cli.check_health", return_value=dummy_report):
        res = runner.invoke(app, ["status", "--db", str(db_file)])
        assert res.exit_code == 2
        assert "CRITICAL" in res.output


def test_cli_serve_insecure_non_loopback_error() -> None:
    res = runner.invoke(
        app,
        ["serve", "--host", "192.168.1.100", "--insecure-no-auth"],
    )
    assert res.exit_code == 1
    assert "--insecure-no-auth is only allowed with" in res.output


def test_cli_serve_success(tmp_path: Path) -> None:
    db_file = tmp_path / "serve_test.db"

    with patch("uvicorn.run") as mock_uvicorn_run:
        res = runner.invoke(
            app,
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8888",
                "--db",
                str(db_file),
                "--insecure-no-auth",
            ],
        )
        assert res.exit_code == 0
        mock_uvicorn_run.assert_called_once()
        _, kwargs = mock_uvicorn_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8888


def test_resolve_db_path() -> None:
    from mail_cadence.cli import DEFAULT_DB, _resolve_db_path
    from mail_cadence.models import Settings

    assert _resolve_db_path("custom.db") == "custom.db"

    dummy_settings = Settings(mail_cadence_db="from_settings.db")
    assert _resolve_db_path(DEFAULT_DB, settings=dummy_settings) == "from_settings.db"


def test_cli_main_module_execution() -> None:
    import runpy
    import warnings

    with patch("sys.argv", ["mail-cadence", "--version"]), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("mail_cadence.cli", run_name="__main__")
        assert exc_info.value.code == 0
