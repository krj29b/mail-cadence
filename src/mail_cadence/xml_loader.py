import sqlite3
from pathlib import Path
from typing import Literal, cast

import defusedxml.ElementTree as ET  # type: ignore[import-untyped]

from mail_cadence.models import MailConfiguration, WorkSchedules
from mail_cadence.repository import DatabaseRepository


class XMLValidationError(Exception):
    """Raised when XML parsing or schema/cross-reference validation fails."""


def _parse_xml_safely(file_path: str | Path) -> bytes:
    path = Path(file_path)
    if not path.is_file():
        raise XMLValidationError(f"File not found: {path}")

    try:
        # Use defusedxml to safely parse the XML file and catch entity expansion or malformed XML
        tree = ET.parse(path)
        root = tree.getroot()
        # Convert root back to bytes for pydantic-xml parsing
        raw_bytes = ET.tostring(root, encoding="utf-8")
        return cast(bytes, raw_bytes)
    except Exception as exc:
        raise XMLValidationError(f"XML security/syntax error in {path}: {exc}") from exc


def load_mail_configuration(file_path: str | Path) -> MailConfiguration:
    xml_bytes = _parse_xml_safely(file_path)
    try:
        return MailConfiguration.from_xml(xml_bytes)
    except Exception as exc:
        msg = f"Invalid MailConfiguration XML schema in {file_path}: {exc}"
        raise XMLValidationError(msg) from exc


def load_work_schedules(file_path: str | Path) -> WorkSchedules:
    xml_bytes = _parse_xml_safely(file_path)
    try:
        return WorkSchedules.from_xml(xml_bytes)
    except Exception as exc:
        msg = f"Invalid WorkSchedules XML schema in {file_path}: {exc}"
        raise XMLValidationError(msg) from exc


def validate_xml_configs(mail_config: MailConfiguration, work_schedules: WorkSchedules) -> None:
    schedule_ids = {s.id for s in work_schedules.schedules}
    missing_refs = set()

    for task in mail_config.tasks:
        if task.schedule_ref not in schedule_ids:
            missing_refs.add(f"Task '{task.id}' references unknown schedule '{task.schedule_ref}'")

    if missing_refs:
        raise XMLValidationError(
            "Cross-reference validation failed: " + "; ".join(sorted(missing_refs))
        )


def import_from_xml(
    conn: sqlite3.Connection,
    tasks_path: str | Path,
    schedules_path: str | Path,
    mode: Literal["upsert", "replace"] = "upsert",
) -> tuple[MailConfiguration, WorkSchedules]:
    mail_config = load_mail_configuration(tasks_path)
    work_schedules = load_work_schedules(schedules_path)
    validate_xml_configs(mail_config, work_schedules)

    DatabaseRepository(conn).import_configuration(mail_config, work_schedules, mode=mode)
    return mail_config, work_schedules


def export_to_xml(
    conn: sqlite3.Connection,
    tasks_path: str | Path,
    schedules_path: str | Path,
) -> tuple[MailConfiguration, WorkSchedules]:
    repo = DatabaseRepository(conn)
    work_schedules = repo.get_work_schedules()
    mail_config = repo.get_mail_configuration()

    tasks_xml_bytes = mail_config.to_xml(encoding="utf-8", pretty_print=True)
    schedules_xml_bytes = work_schedules.to_xml(encoding="utf-8", pretty_print=True)

    xml_header = b'<?xml version="1.0" encoding="UTF-8"?>\n'

    tasks_out = Path(tasks_path)
    if tasks_out.parent:
        tasks_out.parent.mkdir(parents=True, exist_ok=True)
    tasks_content = (
        tasks_xml_bytes if isinstance(tasks_xml_bytes, bytes) else tasks_xml_bytes.encode("utf-8")
    )
    tasks_out.write_bytes(xml_header + tasks_content)

    schedules_out = Path(schedules_path)
    if schedules_out.parent:
        schedules_out.parent.mkdir(parents=True, exist_ok=True)
    schedules_content = (
        schedules_xml_bytes
        if isinstance(schedules_xml_bytes, bytes)
        else schedules_xml_bytes.encode("utf-8")
    )
    schedules_out.write_bytes(xml_header + schedules_content)

    return mail_config, work_schedules
