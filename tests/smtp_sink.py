"""A controllable SMTP server on 127.0.0.1 for delivery-guarantee tests.

The sink runs in its own thread with its own event loop, so the code under test can
run in the test thread, in another thread or in another process. Its log is the test
oracle: a message counts as accepted when the end of DATA arrives and the sink does not
refuse it, before the sink replies, so a message whose reply is held or dropped is
still counted.

Controls (set before the client sends the command):
- ``rcpt_codes``: reply code per recipient address for RCPT TO (default 250);
- ``mail_code``: reply to MAIL FROM (default 250);
- ``data_code``: reply to the DATA command (default 354; anything else refuses it);
- ``final_code``: reply after the message body (default 250; anything else refuses it);
- ``drop_at``: (command, n) - close the connection without replying to the n-th
  MAIL, RCPT or DATA command (DATA: before 354, so no body is sent);
- ``drop_after_data``: close the connection without replying after the n-th body;
- ``hold_at``: (command, n) - before the greeting of the n-th connection (CONNECT),
  before replying to the n-th MAIL command, or after the n-th body, wait until
  ``release()``; ``held`` is set when that point is reached.
"""

import asyncio
import contextlib
import email
import re
import threading
from dataclasses import dataclass
from email.message import Message
from types import TracebackType
from typing import Literal, Self

Command = Literal["MAIL", "RCPT", "DATA"]
HoldCommand = Literal["CONNECT", "MAIL", "DATA"]

HOLD_TIMEOUT = 60.0
_ADDRESS_RE = re.compile(r"<([^>]*)>")


def _reply(code: int) -> bytes:
    return f"{code} {'OK' if code < 400 else 'Refused by test sink'}\r\n".encode()


@dataclass(frozen=True, slots=True)
class RcptAttempt:
    session: int
    recipient: str
    code: int


@dataclass(frozen=True, slots=True)
class Delivery:
    session: int
    mail_from: str
    recipients: tuple[str, ...]
    raw: bytes

    @property
    def message(self) -> Message:
        return email.message_from_bytes(self.raw)

    @property
    def subject(self) -> str:
        return str(self.message.get("Subject", ""))


def _address(line: str) -> str:
    match = _ADDRESS_RE.search(line)
    if match:
        return match.group(1).strip()
    return line.split(":", 1)[1].strip()


class _DropConnection(Exception):
    """Close the connection without a reply."""


class _Session:
    """One SMTP connection; reports to the sink and asks it for replies."""

    def __init__(
        self,
        sink: "SMTPSink",
        number: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.sink = sink
        self.number = number
        self.reader = reader
        self.writer = writer
        self.mail_from = ""
        self.accepted: list[str] = []

    async def reply(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def run(self) -> None:
        await self.sink.pause_if_held("CONNECT")
        await self.reply(b"220 127.0.0.1 ESMTP sink\r\n")
        while line_bytes := await self.reader.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if not await self.handle(line, line.upper()):
                return

    async def handle(self, line: str, upper: str) -> bool:
        """Answer one command; False ends the session."""
        if upper.startswith(("EHLO", "HELO")):
            await self.reply(b"250-127.0.0.1\r\n250 OK\r\n")
        elif upper.startswith("MAIL FROM:"):
            await self.mail(_address(line))
        elif upper.startswith("RCPT TO:"):
            await self.rcpt(_address(line))
        elif upper == "DATA":
            return await self.data()
        elif upper.startswith("QUIT"):
            await self.reply(b"221 Bye\r\n")
            return False
        else:  # RSET, NOOP
            self.sink.log_command(self.number, upper.split(" ", 1)[0])
            self.mail_from, self.accepted = "", []
            await self.reply(b"250 OK\r\n")
        return True

    async def mail(self, sender: str) -> None:
        self.sink.on_command(self.number, "MAIL")
        await self.sink.pause_if_held("MAIL")
        self.mail_from, self.accepted = sender, []
        await self.reply(_reply(self.sink.mail_code))

    async def rcpt(self, recipient: str) -> None:
        self.sink.on_command(self.number, "RCPT")
        code = self.sink.rcpt_code(recipient, self.number)
        if code == 250:
            self.accepted.append(recipient)
        await self.reply(_reply(code))

    async def data(self) -> bool:
        self.sink.on_command(self.number, "DATA")
        if not self.accepted:
            await self.reply(_reply(554))
            return True
        if self.sink.data_code != 354:
            await self.reply(_reply(self.sink.data_code))
            return True
        await self.reply(b"354 End data with <CR><LF>.<CR><LF>\r\n")
        raw = (await self.reader.readuntil(b"\r\n.\r\n"))[:-3].replace(b"\r\n..", b"\r\n.")
        delivery = Delivery(self.number, self.mail_from, tuple(self.accepted), raw)
        self.mail_from, self.accepted = "", []
        if self.sink.final_code != 250:
            await self.reply(_reply(self.sink.final_code))
            return True
        if self.sink.record_delivery(delivery):
            return False  # drop the connection without a reply
        await self.sink.pause_if_held("DATA")
        await self.reply(b"250 2.0.0 Queued\r\n")
        return True


class SMTPSink:
    def __init__(self) -> None:
        self.rcpt_codes: dict[str, int] = {}
        self.mail_code = 250
        self.data_code = 354
        self.final_code = 250
        self.drop_at: tuple[Command, int] | None = None
        self.hold_at: tuple[HoldCommand, int] | None = None
        self.drop_after_data: int | None = None
        self.held = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {"MAIL": 0, "RCPT": 0, "DATA": 0, "session": 0}
        self._held_counts: dict[str, int] = {"CONNECT": 0, "MAIL": 0, "DATA": 0}
        self._deliveries: list[Delivery] = []
        self._rcpt_attempts: list[RcptAttempt] = []
        self._commands: list[tuple[int, str]] = []
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.port = 0

    # --- lifecycle -----------------------------------------------------------------

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=asyncio.run, args=(self._serve(),), daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("SMTP sink did not start")
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.release()
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(10)

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        async with server:
            await self._stop.wait()
            server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with self._lock:
            self._counts["session"] += 1
            number = self._counts["session"]
        try:
            await _Session(self, number, reader, writer).run()
        except (asyncio.IncompleteReadError, ConnectionError, _DropConnection):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    # --- controls --------------------------------------------------------------------

    def release(self) -> None:
        self._release.set()

    def wait_held(self, timeout: float = HOLD_TIMEOUT) -> None:
        if not self.held.wait(timeout):
            raise TimeoutError(f"SMTP sink was not reached at {self.hold_at}")

    def log_command(self, session: int, verb: str) -> None:
        with self._lock:
            self._commands.append((session, verb))

    def on_command(self, session: int, command: Command) -> None:
        """Count and log a command; raise _DropConnection if it must go unanswered."""
        with self._lock:
            self._counts[command] += 1
            self._commands.append((session, command))
            drop = self.drop_at == (command, self._counts[command])
        if drop:
            raise _DropConnection

    async def pause_if_held(self, command: HoldCommand) -> None:
        with self._lock:
            self._held_counts[command] += 1
            hold = self.hold_at == (command, self._held_counts[command])
        if hold:
            self.held.set()
            await asyncio.get_running_loop().run_in_executor(None, self._release.wait, HOLD_TIMEOUT)

    def rcpt_code(self, recipient: str, session: int) -> int:
        with self._lock:
            code = self.rcpt_codes.get(recipient, 250)
            self._rcpt_attempts.append(RcptAttempt(session, recipient, code))
        return code

    def record_delivery(self, delivery: Delivery) -> bool:
        """Log an accepted message; True if the connection must now be dropped."""
        with self._lock:
            self._deliveries.append(delivery)
            return self.drop_after_data == len(self._deliveries)

    # --- oracle ----------------------------------------------------------------------

    @property
    def deliveries(self) -> list[Delivery]:
        with self._lock:
            return list(self._deliveries)

    @property
    def rcpt_attempts(self) -> list[RcptAttempt]:
        with self._lock:
            return list(self._rcpt_attempts)

    @property
    def commands(self) -> list[tuple[int, str]]:
        with self._lock:
            return list(self._commands)

    def accepted_count(self, recipient: str) -> int:
        return sum(d.recipients.count(recipient) for d in self.deliveries)

    def rcpt_count(self, recipient: str) -> int:
        return sum(1 for a in self.rcpt_attempts if a.recipient == recipient)

    def sessions_with_mail(self) -> set[int]:
        return {a.session for a in self.rcpt_attempts}
