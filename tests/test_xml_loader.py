from pathlib import Path

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.models import (
    BodyTemplate,
    MailConfiguration,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.xml_loader import (
    XMLValidationError,
    _parse_xml_safely,
    export_to_xml,
    import_from_xml,
    load_mail_configuration,
    load_work_schedules,
    validate_xml_configs,
)


def test_load_sample_xml_files(tmp_path: Path) -> None:
    tasks_path = "config/samples/tasks.xml"
    schedules_path = "config/samples/schedules.xml"

    mail_config = load_mail_configuration(tasks_path)
    schedules = load_work_schedules(schedules_path)

    assert len(mail_config.tasks) == 2
    assert len(schedules.schedules) == 3

    # Validate cross references without errors
    validate_xml_configs(mail_config, schedules)


def test_missing_file() -> None:
    with pytest.raises(XMLValidationError, match="File not found"):
        load_mail_configuration("non_existent.xml")


def test_parse_xml_safely_missing_file() -> None:
    fake_path = "non_existent_file.xml"
    with pytest.raises(XMLValidationError, match=f"File not found: {fake_path}"):
        _parse_xml_safely(fake_path)

    fake_path_obj = Path("non_existent_path_obj.xml")
    with pytest.raises(XMLValidationError, match=f"File not found: {fake_path_obj}"):
        _parse_xml_safely(fake_path_obj)


def test_invalid_schedule_reference(tmp_path: Path) -> None:
    tasks_xml = tmp_path / "tasks.xml"
    schedules_xml = tmp_path / "schedules.xml"

    tasks_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
  </ReportSettings>
  <Tasks>
    <MailTask id="TASK-001" schedule_ref="SCHED-UNKNOWN">
      <Sender>noreply@example.com</Sender>
      <Recipients><To>user@example.com</To></Recipients>
      <Subject>Test</Subject>
      <BodyTemplate format="html">tpl.j2</BodyTemplate>
    </MailTask>
  </Tasks>
</MailConfiguration>""")

    schedules_xml.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-KNOWN">
    <AllowedHours>
      <HourWindow start="00:00" end="23:59"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    mail_config = load_mail_configuration(tasks_xml)
    schedules = load_work_schedules(schedules_xml)

    with pytest.raises(XMLValidationError, match="references unknown schedule 'SCHED-UNKNOWN'"):
        validate_xml_configs(mail_config, schedules)


def test_import_and_export_xml(tmp_path: Path) -> None:
    conn = get_connection(":memory:")
    init_db(conn)

    tasks_path = "config/samples/tasks.xml"
    schedules_path = "config/samples/schedules.xml"

    import_from_xml(conn, tasks_path, schedules_path)

    repo = DatabaseRepository(conn)
    db_config = repo.get_mail_configuration()
    db_schedules = repo.get_work_schedules()

    assert len(db_config.tasks) == 2
    assert len(db_schedules.schedules) == 3

    out_tasks = tmp_path / "out_tasks.xml"
    out_schedules = tmp_path / "out_schedules.xml"

    export_to_xml(conn, out_tasks, out_schedules)

    reloaded_config = load_mail_configuration(out_tasks)
    reloaded_schedules = load_work_schedules(out_schedules)

    assert len(reloaded_config.tasks) == 2
    assert len(reloaded_schedules.schedules) == 3


def test_import_from_xml_replace_rollback_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = get_connection(":memory:")
    init_db(conn)

    tasks_path = "config/samples/tasks.xml"
    schedules_path = "config/samples/schedules.xml"

    # Initial import to populate database
    import_from_xml(conn, tasks_path, schedules_path)

    repo = DatabaseRepository(conn)
    orig_config = repo.get_mail_configuration()
    orig_schedules = repo.get_work_schedules()
    assert len(orig_config.tasks) == 2
    assert len(orig_schedules.schedules) == 3

    # Monkeypatch DatabaseRepository._save_task to raise an exception
    def mock_save_task(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Simulated failure during task save")

    monkeypatch.setattr(DatabaseRepository, "_save_task", mock_save_task)

    with pytest.raises(RuntimeError, match="Simulated failure during task save"):
        import_from_xml(conn, tasks_path, schedules_path, mode="replace")

    # Verify rollback: database state remains unchanged
    after_config = repo.get_mail_configuration()
    after_schedules = repo.get_work_schedules()

    assert len(after_config.tasks) == 2
    assert len(after_schedules.schedules) == 3
    assert [t.id for t in after_config.tasks] == [t.id for t in orig_config.tasks]
    assert [s.id for s in after_schedules.schedules] == [s.id for s in orig_schedules.schedules]


def test_load_work_schedules_invalid_time_or_timezone(tmp_path: Path) -> None:
    schedules_invalid_time = tmp_path / "sched_invalid_time.xml"
    schedules_invalid_time.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-01">
    <AllowedHours>
      <HourWindow start="25:00" end="23:59"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    with pytest.raises(XMLValidationError):
        load_work_schedules(schedules_invalid_time)

    schedules_invalid_tz = tmp_path / "sched_invalid_tz.xml"
    schedules_invalid_tz.write_bytes(b"""<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="Mars/Phobos">
  <Schedule id="SCHED-01">
    <AllowedHours>
      <HourWindow start="00:00" end="23:59"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>""")

    with pytest.raises(XMLValidationError):
        load_work_schedules(schedules_invalid_tz)


def test_validate_xml_configs_empty_schedules_and_tasks() -> None:
    # 1. Empty schedules with tasks referencing schedule -> raises XMLValidationError
    mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="admin@example.com"),
        tasks=[
            MailTask(
                id="TASK-001",
                schedule_ref="SCHED-1",
                sender="sender@example.com",
                recipients=Recipients(to=["user@example.com"]),
                subject="Test",
                body_template=BodyTemplate(template_path="tpl.j2"),
            )
        ],
    )
    empty_schedules = WorkSchedules(timezone="UTC", schedules=[])

    with pytest.raises(XMLValidationError, match="references unknown schedule 'SCHED-1'"):
        validate_xml_configs(mail_config, empty_schedules)

    # 2. Empty schedules and empty tasks -> passes without error
    empty_mail_config = MailConfiguration(
        report_settings=ReportSettings(recipient="admin@example.com"),
        tasks=[],
    )
    validate_xml_configs(empty_mail_config, empty_schedules)

    # 3. Non-empty schedules and empty tasks -> passes without error
    schedules_with_items = WorkSchedules(
        timezone="UTC",
        schedules=[Schedule(id="SCHED-1", allowed_hours=[])],
    )
    validate_xml_configs(empty_mail_config, schedules_with_items)
