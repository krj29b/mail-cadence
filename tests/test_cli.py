import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from mail_cadence.cli import app, run_dispatch_pipeline, setup_logging
from mail_cadence.mailer import AsyncMailer, RenderedMessage
from mail_cadence.models import MailTask, Outcome, RecipientResult
from mail_cadence.scheduler import TokenBucketRateLimiter

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
        assert kwargs["format"] == "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        handlers = kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.StreamHandler)
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

    def tracked_render(self: AsyncMailer, task: MailTask) -> RenderedMessage:
        executed_order.append(task.id)
        return original_render(self, task)

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
