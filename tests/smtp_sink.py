"""A controllable SMTP server on 127.0.0.1 for delivery-guarantee tests.

The sink runs in its own thread with its own event loop, so the code under test can
run in the test thread, in another thread or in another process. Its log is the test
oracle: a message counts as accepted when the end of DATA arrives, before the sink
replies, so a message whose reply is held or dropped is still counted.

Controls:
- ``rcpt_codes``: reply code per recipient address for RCPT TO (default 250);
- ``hold_at``: (command, n) - stop before replying to the n-th MAIL or DATA command
  until ``release()`` is called; ``held`` is set when that point is reached;
- ``drop_after_data``: close the connection without replying to the n-th DATA.
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

HoldCommand = Literal["MAIL", "DATA"]

HOLD_TIMEOUT = 60.0
_ADDRESS_RE = re.compile(r"<([^>]*)>")
_REPLIES: dict[int, bytes] = {
    250: b"250 2.1.5 OK\r\n",
    450: b"450 4.2.0 Mailbox busy, try again later\r\n",
    550: b"550 5.1.1 No such user\r\n",
}


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
    message: Message

    @property
    def subject(self) -> str:
        return str(self.message.get("Subject", ""))


def _address(line: str) -> str:
    match = _ADDRESS_RE.search(line)
    if match:
        return match.group(1).strip()
    return line.split(":", 1)[1].strip()


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
            await self.sink.pause_if_held("MAIL")
            self.mail_from, self.accepted = _address(line), []
            await self.reply(b"250 2.1.0 OK\r\n")
        elif upper.startswith("RCPT TO:"):
            await self.rcpt(_address(line))
        elif upper == "DATA":
            return await self.data()
        elif upper.startswith("QUIT"):
            await self.reply(b"221 Bye\r\n")
            return False
        else:  # RSET, NOOP
            self.mail_from, self.accepted = "", []
            await self.reply(b"250 OK\r\n")
        return True

    async def rcpt(self, recipient: str) -> None:
        code = self.sink.rcpt_code(recipient, self.number)
        if code == 250:
            self.accepted.append(recipient)
        await self.reply(_REPLIES[code])

    async def data(self) -> bool:
        if not self.accepted:
            await self.reply(b"554 5.5.1 No valid recipients\r\n")
            return True
        await self.reply(b"354 End data with <CR><LF>.<CR><LF>\r\n")
        raw = (await self.reader.readuntil(b"\r\n.\r\n"))[:-3].replace(b"\r\n..", b"\r\n.")
        delivery = Delivery(
            session=self.number,
            mail_from=self.mail_from,
            recipients=tuple(self.accepted),
            message=email.message_from_bytes(raw),
        )
        if self.sink.record_delivery(delivery):
            return False  # drop the connection without a reply
        await self.sink.pause_if_held("DATA")
        self.mail_from, self.accepted = "", []
        await self.reply(b"250 2.0.0 Queued\r\n")
        return True


class SMTPSink:
    def __init__(self) -> None:
        self.rcpt_codes: dict[str, int] = {}
        self.hold_at: tuple[HoldCommand, int] | None = None
        self.drop_after_data: int | None = None
        self.held = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {"MAIL": 0, "DATA": 0, "session": 0}
        self._deliveries: list[Delivery] = []
        self._rcpt_attempts: list[RcptAttempt] = []
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
        except (asyncio.IncompleteReadError, ConnectionError):
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

    async def pause_if_held(self, command: HoldCommand) -> None:
        with self._lock:
            self._counts[command] += 1
            hold = self.hold_at == (command, self._counts[command])
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

    def accepted_count(self, recipient: str) -> int:
        return sum(d.recipients.count(recipient) for d in self.deliveries)

    def rcpt_count(self, recipient: str) -> int:
        return sum(1 for a in self.rcpt_attempts if a.recipient == recipient)

    def sessions_with_mail(self) -> set[int]:
        return {a.session for a in self.rcpt_attempts}
