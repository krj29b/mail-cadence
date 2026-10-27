from pathlib import Path

import pytest

from mail_cadence.database import get_connection, init_db
from mail_cadence.repository import DatabaseRepository
from mail_cadence.xml_loader import (
    export_to_xml,
    import_from_xml,
    load_mail_configuration,
    load_work_schedules,
)


def test_xml_sqlite_round_trip(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    initial_tasks_xml = Path("config/samples/tasks.xml")
    initial_schedules_xml = Path("config/samples/schedules.xml")

    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    request.addfinalizer(conn.close)
    init_db(conn)

    # 1. XML -> SQLite Import
    mail_config, work_schedules = import_from_xml(conn, initial_tasks_xml, initial_schedules_xml)
    assert len(mail_config.tasks) == 2
    assert len(work_schedules.schedules) == 3

    repo = DatabaseRepository(conn)
    db_config = repo.get_mail_configuration()
    db_schedules = repo.get_work_schedules()

    assert len(db_config.tasks) == 2
    assert len(db_schedules.schedules) == 3

    # 2. Modify in DB
    # Add a new task to DB
    task1 = db_config.tasks[0]
    task1.subject = "Modified Subject In DB"

    repo.save_mail_configuration(db_config)

    # 3. SQLite Export -> XML
    exported_tasks_xml = tmp_path / "exported_tasks.xml"
    exported_schedules_xml = tmp_path / "exported_schedules.xml"

    export_to_xml(conn, exported_tasks_xml, exported_schedules_xml)

    # 4. Re-import from exported XML into new DB
    new_db_path = tmp_path / "reimported.db"
    new_conn = get_connection(new_db_path)
    request.addfinalizer(new_conn.close)
    init_db(new_conn)

    reimported_config, reimported_schedules = import_from_xml(
        new_conn, exported_tasks_xml, exported_schedules_xml
    )

    assert len(reimported_config.tasks) == 2
    assert reimported_config.tasks[0].subject == "Modified Subject In DB"
    assert len(reimported_schedules.schedules) == 3


def test_cli_import_and_export_subcommands(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from mail_cadence.cli import app

    runner = CliRunner()
    db_file = tmp_path / "cli_test.db"
    tasks_xml = Path("config/samples/tasks.xml")
    schedules_xml = Path("config/samples/schedules.xml")

    # Test import-xml subcommand
    import_res = runner.invoke(
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
    assert import_res.exit_code == 0
    assert "Successfully imported" in import_res.output

    # Test export-xml subcommand
    export_tasks = tmp_path / "cli_export_tasks.xml"
    export_schedules = tmp_path / "cli_export_schedules.xml"

    export_res = runner.invoke(
        app,
        [
            "export-xml",
            "--tasks-xml",
            str(export_tasks),
            "--schedule-xml",
            str(export_schedules),
            "--db",
            str(db_file),
        ],
    )
    assert export_res.exit_code == 0
    assert "Successfully exported" in export_res.output

    # Verify exported file content exists
    reloaded_tasks = load_mail_configuration(export_tasks)
    reloaded_schedules = load_work_schedules(export_schedules)
    assert len(reloaded_tasks.tasks) == 2
    assert len(reloaded_schedules.schedules) == 3
