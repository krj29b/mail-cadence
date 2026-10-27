"""One phase-aware SMTP attempt (ADR 0001, D2) against the local sink.

Every recipient gets a certain outcome unless the connection fails after DATA has
started; then it is UNKNOWN and must never be retried automatically. The sink's log
shows what was really accepted.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import aiosmtplib
import pytest

from mail_cadence.mailer import (
    AsyncMailer,
    RenderedMessage,
    TemplateRenderer,
    _Attempt,
    envelope_sender,
)
from mail_cadence.models import (
    BodyTemplate,
    MailTask,
    Outcome,
    RecipientResult,
    Recipients,
    Settings,
)
from smtp_sink import SMTPSink

A = "a@example.com"
B = "b@example.com"
C = "c@example.com"


@pytest.fixture
def sink() -> Iterator[SMTPSink]:
    with SMTPSink() as server:
        yield server


def _mailer(
    tmp_path: Path, port: int, *, timeout: float = 10.0, dry_run: bool = False
) -> AsyncMailer:
    (tmp_path / "body.html.j2").write_text(
        "<p>Zażółć gęślą jaźń, {{ name }}.</p>\n.\n<p>Line with a dot above.</p>",
        encoding="utf-8",
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, smtp_host="127.0.0.1", smtp_port=port, smtp_timeout=timeout
    )
    return AsyncMailer(settings, renderer=TemplateRenderer(tmp_path), dry_run=dry_run)


def _message(mailer: AsyncMailer) -> RenderedMessage:
    task = MailTask(
        id="T1",
        schedule_ref="S1",
        sender="Zespół Raportów <cadence@example.com>",
        subject="Raport dzienny: zażółć",
        recipients=Recipients(to=[A, B], bcc=[C]),
        body_template=BodyTemplate(template_path="body.html.j2"),
        context_variables=[{"name": "name", "value": "Łucja"}],  # type: ignore[list-item]
    )
    return mailer.render_task_message(task)


def _outcomes(results: dict[str, RecipientResult]) -> dict[str, tuple[Outcome, int | None]]:
    return {r: (res.outcome, res.code) for r, res in results.items()}


# --- Snapshot --------------------------------------------------------------------------


def test_snapshot_is_7bit_with_its_message_id_and_without_bcc(tmp_path: Path) -> None:
    mailer = _mailer(tmp_path, 1)
    message = _message(mailer)

    head = message.data.split(b"\r\n\r\n", 1)[0].decode("ascii")
    assert message.data.isascii()
    assert b"\n" not in message.data.replace(b"\r\n", b"")
    assert f"Message-ID: {message.message_id}" in head
    assert "Bcc" not in head
    assert C.encode() not in message.data
    assert envelope_sender(message.data) == "cadence@example.com"


async def test_dry_run_reports_acceptance_without_connecting(tmp_path: Path) -> None:
    mailer = _mailer(tmp_path, 1, dry_run=True)
    results = await mailer.deliver(_message(mailer).data, [A])
    assert _outcomes(results) == {A: (Outcome.ACCEPTED, None)}


# --- Certain outcomes ------------------------------------------------------------------


async def test_all_recipients_accepted(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    message = _message(mailer)

    results = await mailer.deliver(message.data, [A, B, C])

    assert _outcomes(results) == {r: (Outcome.ACCEPTED, 250) for r in (A, B, C)}
    [delivery] = sink.deliveries
    assert (delivery.mail_from, delivery.recipients) == ("cadence@example.com", (A, B, C))
    assert delivery.raw.rstrip(b"\r\n") == message.data.rstrip(b"\r\n")


async def test_refused_recipients_get_their_own_outcome(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    sink.rcpt_codes.update({B: 450, C: 550})

    results = await mailer.deliver(_message(mailer).data, [A, B, C])

    assert _outcomes(results) == {
        A: (Outcome.ACCEPTED, 250),
        B: (Outcome.TRANSIENT, 450),
        C: (Outcome.PERMANENT, 550),
    }
    assert [d.recipients for d in sink.deliveries] == [(A,)]


async def test_all_refused_sends_no_data_and_keeps_the_connection(
    tmp_path: Path, sink: SMTPSink
) -> None:
    mailer = _mailer(tmp_path, sink.port)
    data = _message(mailer).data
    sink.rcpt_codes.update({A: 550, B: 450})

    async with mailer:
        refused = await mailer.deliver(data, [A, B])
        sink.rcpt_codes.clear()
        accepted = await mailer.deliver(data, [A])

    assert _outcomes(refused) == {A: (Outcome.PERMANENT, 550), B: (Outcome.TRANSIENT, 450)}
    assert _outcomes(accepted) == {A: (Outcome.ACCEPTED, 250)}
    assert [verb for _, verb in sink.commands] == [
        "MAIL",
        "RCPT",
        "RCPT",
        "RSET",
        "MAIL",
        "RCPT",
        "DATA",
    ]
    assert {session for session, _ in sink.commands} == {1}, "the pooled session is reused"


@pytest.mark.parametrize(("code", "outcome"), [(550, Outcome.PERMANENT), (451, Outcome.TRANSIENT)])
async def test_refused_sender_decides_for_everyone(
    tmp_path: Path, sink: SMTPSink, code: int, outcome: Outcome
) -> None:
    mailer = _mailer(tmp_path, sink.port)
    sink.mail_code = code

    results = await mailer.deliver(_message(mailer).data, [A, B])

    assert _outcomes(results) == {A: (outcome, code), B: (outcome, code)}
    assert sink.rcpt_attempts == []


async def test_refused_data_command_is_certain(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    sink.data_code = 451
    sink.rcpt_codes[B] = 550

    results = await mailer.deliver(_message(mailer).data, [A, B])

    assert _outcomes(results) == {A: (Outcome.TRANSIENT, 451), B: (Outcome.PERMANENT, 550)}
    assert sink.deliveries == []


async def test_refused_message_body_is_certain(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    sink.final_code = 554

    results = await mailer.deliver(_message(mailer).data, [A])

    assert _outcomes(results) == {A: (Outcome.PERMANENT, 554)}
    assert sink.deliveries == []


async def test_unreachable_server_is_transient(tmp_path: Path) -> None:
    with SMTPSink() as closed:
        port = closed.port
    mailer = _mailer(tmp_path, port, timeout=2.0)

    results = await mailer.deliver(_message(mailer).data, [A, B])

    assert {r.outcome for r in results.values()} == {Outcome.TRANSIENT}


async def test_connection_lost_before_data_is_transient(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    sink.drop_at = ("RCPT", 2)

    results = await mailer.deliver(_message(mailer).data, [A, B])

    assert {r: res.outcome for r, res in results.items()} == {
        A: Outcome.TRANSIENT,
        B: Outcome.TRANSIENT,
    }
    assert sink.deliveries == []


def test_login_refusal_says_nothing_about_recipients() -> None:
    attempt = _Attempt((A,))
    attempt.fail(aiosmtplib.SMTPAuthenticationError(535, "Authentication failed"))
    assert _outcomes(attempt.results) == {A: (Outcome.TRANSIENT, None)}


# --- Unknown outcomes ------------------------------------------------------------------


async def test_connection_lost_after_the_body_is_unknown(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    data = _message(mailer).data
    sink.rcpt_codes[B] = 550
    sink.drop_after_data = 1

    async with mailer:
        results = await mailer.deliver(data, [A, B])
        await mailer.deliver(data, [C])

    assert _outcomes(results) == {A: (Outcome.UNKNOWN, None), B: (Outcome.PERMANENT, 550)}
    assert [d.recipients for d in sink.deliveries] == [(A,), (C,)], "A was in fact accepted"
    assert [s for s, verb in sink.commands if verb == "MAIL"] == [1, 2], "broken session dropped"


async def test_timeout_after_the_body_is_unknown(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port, timeout=0.5)
    sink.hold_at = ("DATA", 1)

    results = await mailer.deliver(_message(mailer).data, [A])
    sink.release()

    assert {r.outcome for r in results.values()} == {Outcome.UNKNOWN}
    assert len(sink.deliveries) == 1


async def test_connection_lost_at_the_data_command_is_treated_as_unknown(
    tmp_path: Path, sink: SMTPSink
) -> None:
    """aiosmtplib sends DATA and the body in one call, so a lost connection inside it
    cannot be placed before or after the body: the conservative answer is UNKNOWN."""
    mailer = _mailer(tmp_path, sink.port)
    sink.drop_at = ("DATA", 1)

    results = await mailer.deliver(_message(mailer).data, [A])

    assert {r.outcome for r in results.values()} == {Outcome.UNKNOWN}
    assert sink.deliveries == []


async def test_retries_send_the_same_bytes(tmp_path: Path, sink: SMTPSink) -> None:
    mailer = _mailer(tmp_path, sink.port)
    data = _message(mailer).data
    sink.rcpt_codes[B] = 450

    await mailer.deliver(data, [A, B])
    sink.rcpt_codes.clear()
    await mailer.deliver(data, [B])

    first, second = sink.deliveries
    assert first.raw == second.raw
    assert first.message["Message-ID"] == second.message["Message-ID"]


async def test_session_with_a_late_reply_is_not_reused(tmp_path: Path, sink: SMTPSink) -> None:
    """A timed-out session may still deliver the late reply; reusing it would pair
    that reply with the next command."""
    mailer = _mailer(tmp_path, sink.port, timeout=0.5)
    data = _message(mailer).data
    sink.hold_at = ("MAIL", 1)

    async with mailer:
        first = await mailer.deliver(data, [A])
        sink.release()
        second = await mailer.deliver(data, [B])

    assert {r.outcome for r in first.values()} == {Outcome.TRANSIENT}
    assert _outcomes(second) == {B: (Outcome.ACCEPTED, 250)}
    assert [s for s, verb in sink.commands if verb == "MAIL"] == [1, 2]


class _StuckClient:
    """Stays 'connected' after a failed MAIL, like a session with an unread reply."""

    created: ClassVar[list["_StuckClient"]] = []

    def __init__(self) -> None:
        self.is_connected = False
        self.mails = 0
        _StuckClient.created.append(self)

    async def connect(self) -> None:
        self.is_connected = True

    async def mail(self, sender: str, /) -> aiosmtplib.SMTPResponse:
        self.mails += 1
        if len(_StuckClient.created) == 1:
            raise aiosmtplib.SMTPTimeoutError("timed out")
        return aiosmtplib.SMTPResponse(250, "OK")

    async def rcpt(self, recipient: str, /) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "OK")

    async def data(self, message: bytes, /) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "Queued")

    async def rset(self) -> aiosmtplib.SMTPResponse:
        return aiosmtplib.SMTPResponse(250, "OK")

    async def quit(self) -> None:
        self.is_connected = False

    def close(self) -> None:
        self.is_connected = False


async def test_pool_drops_a_client_after_any_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailer = _mailer(tmp_path, 1)
    data = _message(mailer).data
    _StuckClient.created = []
    monkeypatch.setattr(mailer, "_get_smtp_connection", _StuckClient)

    async with mailer:
        first = await mailer.deliver(data, [A])
        second = await mailer.deliver(data, [A])

    assert {r.outcome for r in first.values()} == {Outcome.TRANSIENT}
    assert {r.outcome for r in second.values()} == {Outcome.ACCEPTED}
    assert len(_StuckClient.created) == 2, "the failed client was not reused"
    assert not _StuckClient.created[0].is_connected
