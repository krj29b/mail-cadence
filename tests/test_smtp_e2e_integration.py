import asyncio
import email
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest

from mail_cadence.cli import run_dispatch_pipeline
from mail_cadence.database import get_connection
from mail_cadence.models import TaskExecutionStatus
from mail_cadence.repository import DatabaseRepository


@dataclass
class ReceivedMessage:
    mail_from: str
    rcpt_to: list[str]
    message: Message
    raw_data: bytes


class SMTPServerController:
    def __init__(self, port: int = 0) -> None:
        self.port: int = port
        self.received_messages: list[ReceivedMessage] = []

    def clear(self) -> None:
        self.received_messages.clear()


def parse_email_address(line: str) -> str:
    start = line.find("<")
    end = line.rfind(">")
    if start != -1 and end != -1 and end > start:
        return line[start + 1 : end].strip()
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


async def handle_smtp_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    controller: SMTPServerController,
) -> None:
    current_mail_from: str = ""
    current_rcpt_to: list[str] = []

    writer.write(b"220 127.0.0.1 ESMTP Test Server Ready\r\n")
    await writer.drain()

    try:
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            line_upper = line.upper()

            if line_upper.startswith(("EHLO", "HELO")):
                writer.write(b"250-127.0.0.1\r\n250 OK\r\n")
                await writer.drain()
            elif line_upper.startswith("MAIL FROM:"):
                current_mail_from = parse_email_address(line)
                writer.write(b"250 OK\r\n")
                await writer.drain()
            elif line_upper.startswith("RCPT TO:"):
                current_rcpt_to.append(parse_email_address(line))
                writer.write(b"250 OK\r\n")
                await writer.drain()
            elif line_upper == "DATA":
                writer.write(b"354 Start mail input; end with <CR><LF>.<CR><LF>\r\n")
                await writer.drain()

                raw_body = await reader.readuntil(b"\r\n.\r\n")
                data_bytes = raw_body[:-3]  # Strip trailing .\r\n
                data_bytes = data_bytes.replace(b"\r\n..", b"\r\n.")
                if data_bytes.startswith(b".."):
                    data_bytes = data_bytes[1:]

                parsed_msg = email.message_from_bytes(data_bytes)
                controller.received_messages.append(
                    ReceivedMessage(
                        mail_from=current_mail_from,
                        rcpt_to=list(current_rcpt_to),
                        message=parsed_msg,
                        raw_data=data_bytes,
                    )
                )
                current_mail_from = ""
                current_rcpt_to = []

                writer.write(b"250 OK\r\n")
                await writer.drain()
            elif line_upper.startswith("RSET"):
                current_mail_from = ""
                current_rcpt_to = []
                writer.write(b"250 OK\r\n")
                await writer.drain()
            elif line_upper.startswith("NOOP"):
                writer.write(b"250 OK\r\n")
                await writer.drain()
            elif line_upper.startswith("QUIT"):
                writer.write(b"221 Bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"500 Command unrecognized\r\n")
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
async def smtp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[SMTPServerController, None]:
    controller = SMTPServerController(port=0)

    server = await asyncio.start_server(
        lambda r, w: handle_smtp_session(r, w, controller),
        "127.0.0.1",
        0,
    )
    sockets = server.sockets
    assert sockets is not None
    assert len(sockets) > 0
    port = sockets[0].getsockname()[1]
    controller.port = port

    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(port))
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.setenv("SMTP_START_TLS", "false")

    async with server:
        yield controller
        server.close()
        await server.wait_closed()


def get_message_body_str(msg: Message) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    parts.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


@pytest.mark.asyncio
async def test_full_dispatch_pipeline_real_smtp(
    tmp_path: Path, smtp_server: SMTPServerController
) -> None:
    # 1. Setup Jinja2 template in templates/ relative path
    template_relative_path = "templates/e2e_test_template.html.j2"
    template_file = Path(template_relative_path)
    template_file.parent.mkdir(parents=True, exist_ok=True)
    template_file.write_text("<h1>Witaj {{ user_name }}</h1>", encoding="utf-8")  # noqa: ASYNC240

    # Also save template copy in tmp_path
    (tmp_path / "e2e_test_template.html.j2").write_text(
        "<h1>Witaj {{ user_name }}</h1>", encoding="utf-8"
    )

    # 2. Setup schedules.xml in tmp_path
    schedules_xml = tmp_path / "schedules.xml"
    schedules_xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<WorkSchedules timezone="UTC">
  <Schedule id="SCHED-E2E" description="E2E test schedule">
    <AllowedHours>
      <HourWindow start="00:00" end="23:59" max_concurrency="5" rate_limit_per_min="60"/>
    </AllowedHours>
  </Schedule>
</WorkSchedules>
""",
        encoding="utf-8",
    )

    # 3. Setup tasks.xml in tmp_path
    tasks_xml = tmp_path / "tasks.xml"
    tasks_xml.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<MailConfiguration>
  <ReportSettings>
    <Recipient>admin@example.com</Recipient>
    <Sender>reports@example.com</Sender>
    <SubjectPrefix>[E2E Report]</SubjectPrefix>
    <IncludeFullErrorTrace>true</IncludeFullErrorTrace>
  </ReportSettings>
  <Tasks>
    <MailTask id="TASK-E2E-001" max_retries="3" priority="high" schedule_ref="SCHED-E2E">
      <Sender>sender@example.com</Sender>
      <Recipients>
        <To>to1@example.com</To>
        <To>to2@example.com</To>
        <Cc>cc1@example.com</Cc>
        <Bcc>bcc1@example.com</Bcc>
      </Recipients>
      <Subject>E2E Test Email</Subject>
      <BodyTemplate format="html">{template_relative_path}</BodyTemplate>
      <ContextVariables>
        <Var name="user_name">Jan Kowalski</Var>
      </ContextVariables>
    </MailTask>
  </Tasks>
</MailConfiguration>
""",
        encoding="utf-8",
    )

    db_path = tmp_path / "test_e2e.db"
    ref_dt = datetime.now(UTC)

    try:
        # First Run: Full dispatch pipeline
        code = await run_dispatch_pipeline(
            tasks_xml=tasks_xml,
            schedule_xml=schedules_xml,
            dry_run=False,
            verbose=True,
            ref_dt=ref_dt,
            db_path=str(db_path),
        )

        # Assertions for Scenario 1
        assert code == 0

        # SMTP server should receive 2 messages: 1 task email + 1 report email
        assert len(smtp_server.received_messages) == 2

        # First message: Task Email
        task_msg = smtp_server.received_messages[0]
        assert task_msg.mail_from == "sender@example.com"
        assert set(task_msg.rcpt_to) == {
            "to1@example.com",
            "to2@example.com",
            "cc1@example.com",
            "bcc1@example.com",
        }
        assert "to1@example.com" in task_msg.message["To"]
        assert "to2@example.com" in task_msg.message["To"]
        assert "cc1@example.com" in task_msg.message["Cc"]
        task_body = get_message_body_str(task_msg.message)
        assert "<h1>Witaj Jan Kowalski</h1>" in task_body

        # Second message: Report Email
        report_msg = smtp_server.received_messages[1]
        assert report_msg.mail_from == "reports@example.com"
        assert report_msg.rcpt_to == ["admin@example.com"]
        assert report_msg.message["To"] == "admin@example.com"
        assert report_msg.message["Subject"].startswith("[E2E Report] Task Execution Summary")
        report_body = get_message_body_str(report_msg.message)
        assert "Successful: 1" in report_body
        assert "Total Tasks: 1" in report_body

        # Check SQLite DB state
        conn = get_connection(db_path)
        repo = DatabaseRepository(conn)

        # Check tasks table
        task_states = repo.get_task_execution_states()
        assert "TASK-E2E-001" in task_states
        last_run, last_status = task_states["TASK-E2E-001"]
        assert last_status == TaskExecutionStatus.SUCCESS
        assert last_run is not None

        # Check execution_logs table
        logs = conn.execute(
            "SELECT task_id, status, sent_count FROM execution_logs WHERE task_id = ?",
            ("TASK-E2E-001",),
        ).fetchall()
        assert len(logs) == 1
        log_row = logs[0]
        assert log_row["status"] == TaskExecutionStatus.SUCCESS.value
        assert log_row["sent_count"] == 4  # 2 To + 1 Cc + 1 Bcc

        # Scenario 2: Idempotency check in same window
        smtp_server.clear()

        code2 = await run_dispatch_pipeline(
            tasks_xml=tasks_xml,
            schedule_xml=schedules_xml,
            dry_run=False,
            verbose=True,
            ref_dt=ref_dt,
            db_path=str(db_path),
        )

        assert code2 == 0

        # No duplicate task email, and no report either: nothing was executed in this batch
        assert len(smtp_server.received_messages) == 0

        # Check execution_logs table now has a SKIPPED_SCHEDULE log
        all_logs = conn.execute(
            "SELECT status FROM execution_logs WHERE task_id = ? ORDER BY id DESC",
            ("TASK-E2E-001",),
        ).fetchall()
        assert len(all_logs) == 2
        assert all_logs[0]["status"] == TaskExecutionStatus.SKIPPED_SCHEDULE.value

        conn.close()
    finally:
        if template_file.exists():  # noqa: ASYNC240
            template_file.unlink()  # noqa: ASYNC240
