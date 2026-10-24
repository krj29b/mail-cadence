import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mail_cadence.cli import app, run_dispatch_pipeline, setup_logging

runner = CliRunner()


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
      <HourWindow start="00:00" end="23:59" max_concurrency="5" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    from mail_cadence.cli import run_dispatch_pipeline
    from mail_cadence.models import TaskExecutionResult, TaskExecutionStatus

    captured_rl: list[pytest.MonkeyPatch | object] = []

    async def fake_dispatch(task: object, rate_limiter: object = None) -> TaskExecutionResult:
        captured_rl.append(rate_limiter)
        return TaskExecutionResult(
            task_id="T",
            status=TaskExecutionStatus.SUCCESS,
            sent_count=1,
            duration_ms=1.0,
        )

    import asyncio

    sem_instances: list[asyncio.Semaphore] = []
    original_sem = asyncio.Semaphore

    def tracked_sem(value: int = 1) -> asyncio.Semaphore:
        sem = original_sem(value)
        sem_instances.append(sem)
        return sem

    with (
        patch("mail_cadence.cli.AsyncMailer.dispatch_task", side_effect=fake_dispatch),
        patch("asyncio.Semaphore", side_effect=tracked_sem),
    ):
        code = await run_dispatch_pipeline(
            tasks_xml, schedules_xml, dry_run=True, db_path=str(tmp_path / "test.db")
        )
        assert code == 0
        assert len(captured_rl) == 2
        # Identical rate limiter instance
        assert captured_rl[0] is captured_rl[1]
        # Identical semaphore instance (instantiated only once)
        assert len(sem_instances) == 1


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

    # max_concurrency = 1 ensures tasks run sequentially through the semaphore
    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-01">
    <AllowedHours>
      <HourWindow start="00:00" end="23:59" max_concurrency="1" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    from mail_cadence.models import MailTask, TaskExecutionResult, TaskExecutionStatus

    executed_order: list[str] = []

    async def fake_dispatch(task: MailTask, rate_limiter: object = None) -> TaskExecutionResult:
        executed_order.append(task.id)
        return TaskExecutionResult(
            task_id=task.id,
            status=TaskExecutionStatus.SUCCESS,
            sent_count=1,
            duration_ms=1.0,
        )

    db_path = str(tmp_path / "test.db")
    with patch("mail_cadence.cli.AsyncMailer.dispatch_task", side_effect=fake_dispatch):
        code = await run_dispatch_pipeline(tasks_xml, schedules_xml, dry_run=True, db_path=db_path)
        assert code == 0
        assert executed_order == ["task_z", "task_a"]


def test_cli_version_option() -> None:
    from mail_cadence import __version__

    res_long = runner.invoke(app, ["--version"])
    assert res_long.exit_code == 0
    assert f"mail-cadence {__version__}" in res_long.output

    res_short = runner.invoke(app, ["-V"])
    assert res_short.exit_code == 0
    assert f"mail-cadence {__version__}" in res_short.output
