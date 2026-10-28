import asyncio
import contextlib
import email.policy
import html
import logging
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import aiosmtplib
from aiosmtplib import SMTPResponse
from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PrefixLoader,
    StrictUndefined,
    TemplateError,
    TemplateNotFound,
    select_autoescape,
)

from mail_cadence.models import (
    MailTask,
    Outcome,
    RecipientResult,
    Settings,
)

logger = logging.getLogger(__name__)

PACKAGE_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Templates shipped with the package are referenced as "builtin:<file name>".
BUILTIN_TEMPLATE_PREFIX = "builtin:"
# Templates used internally by the status report; never usable as task templates.
INTERNAL_TEMPLATES: frozenset[str] = frozenset({"report_email.html.j2", "report_email.txt.j2"})

# select_autoescape() only matches the end of the file name, so compound
# extensions such as ".html.j2" must be listed explicitly.
AUTOESCAPE_EXTENSIONS: tuple[str, ...] = ("html", "htm", "xml", "html.j2", "htm.j2", "xml.j2")

SMTP_PERMANENT_ERROR_MIN = 500
SMTP_PERMANENT_ERROR_MAX = 600
# "Service not available, closing transmission channel", also sent on an idle timeout.
SMTP_SERVICE_CLOSING = 421

# Errors that can come out of rendering a task template and building its message.
RENDER_ERRORS: tuple[type[Exception], ...] = (TemplateError, ValueError, TypeError, OSError)
# Errors a broken connection raises on close; quitting is best effort.
SMTP_CLOSE_ERRORS: tuple[type[Exception], ...] = (aiosmtplib.SMTPException, OSError)

# Stored task messages are 7-bit clean (8-bit parts become base64), CRLF line endings,
# so the same bytes go through any server, with or without 8BITMIME.
SNAPSHOT_POLICY = email.policy.SMTP.clone(cte_type="7bit")


class SMTPClient(Protocol):
    """The part of ``aiosmtplib.SMTP`` that AsyncMailer uses; tests pass fakes that match it."""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> object: ...

    async def quit(self) -> object: ...

    def close(self) -> None: ...

    # One SMTP transaction step by step, so the protocol phase of a failure is known.
    async def mail(self, sender: str, /) -> SMTPResponse: ...

    async def rcpt(self, recipient: str, /) -> SMTPResponse: ...

    async def data(self, message: bytes, /) -> SMTPResponse: ...

    async def rset(self) -> SMTPResponse: ...


def list_builtin_templates() -> list[str]:
    """Return the task templates shipped with the package, as "builtin:" references."""
    if not PACKAGE_TEMPLATES_DIR.is_dir():
        return []
    return sorted(
        f"{BUILTIN_TEMPLATE_PREFIX}{path.name}"
        for path in PACKAGE_TEMPLATES_DIR.glob("*.j2")
        if path.is_file() and path.name not in INTERNAL_TEMPLATES
    )


class TemplateRenderer:
    """Render task templates.

    Resolution rules:
    - ``builtin:<name>`` loads a template shipped with the package;
    - any other path is resolved inside ``template_dir`` only (no fallbacks);
    - a missing template raises ``jinja2.TemplateNotFound``.

    Task templates use ``StrictUndefined``: a misspelled or missing variable raises
    ``jinja2.UndefinedError`` instead of rendering as an empty string. Optional
    variables need ``{{ name | default('...') }}`` or ``{% if name is defined %}``.
    """

    def __init__(self, template_dir: str | Path = ".") -> None:
        self.template_dir = Path(template_dir)
        builtin_loader = PrefixLoader(
            {"builtin": FileSystemLoader(str(PACKAGE_TEMPLATES_DIR))}, delimiter=":"
        )
        self.env = Environment(
            loader=ChoiceLoader([builtin_loader, FileSystemLoader(str(self.template_dir))]),
            undefined=StrictUndefined,
            autoescape=select_autoescape(
                enabled_extensions=AUTOESCAPE_EXTENSIONS, default_for_string=True
            ),
        )

    def _resolve_loader_name(self, template_path: str) -> str:
        if template_path.startswith(BUILTIN_TEMPLATE_PREFIX):
            if template_path not in list_builtin_templates():
                raise TemplateNotFound(template_path)
            return template_path

        tpl_path = Path(template_path)
        base_dir = self.template_dir.resolve()
        resolved_path = (base_dir / tpl_path).resolve()
        try:
            rel_path = resolved_path.relative_to(base_dir)
        except ValueError as exc:
            msg = f"Access denied for template path outside base directory: {template_path}"
            raise ValueError(msg) from exc
        if not resolved_path.is_file():
            raise TemplateNotFound(template_path)
        return rel_path.as_posix()

    def render(self, template_path: str, context: dict[str, Any]) -> str:
        template = self.env.get_template(self._resolve_loader_name(template_path))
        return template.render(**context)

    def render_task_body(self, task: MailTask) -> str:
        """Render a task's body; raises EmptyBodyError if it contains no visible text."""
        body = self.render(task.body_template.template_path, task.get_context_dict())
        is_html = task.body_template.format.lower() == "html"
        visible_text = _html_to_plain_text(body) if is_html else body
        if not visible_text.strip():
            msg = f"Template '{task.body_template.template_path}' rendered an empty body"
            raise EmptyBodyError(msg)
        return body


class EmptyBodyError(ValueError):
    """A task template rendered no visible text; such a message is never sent."""


def _html_to_plain_text(html_str: str) -> str:
    text = re.sub(r"(?is)<(style|script)\b[^>]*>.*?</\1>", "", html_str)
    text = re.sub(r"(?i)<(?:br\s*/?|/p|/div|/li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _extract_sender_domain(sender: str | None) -> str:
    if not sender:
        return "localhost"
    _, addr = parseaddr(sender)
    if not addr:
        addr = sender
    if "@" in addr:
        domain = addr.rsplit("@", 1)[1].strip()
        if domain:
            return domain
    return "localhost"


def new_email_message(
    subject: str, sender: str, to: Sequence[str], cc: Sequence[str] = ()
) -> EmailMessage:
    """Message with the standard headers: Subject, From, To, Cc, Date and Message-ID."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_extract_sender_domain(sender))
    return msg


def set_html_body(msg: EmailMessage, html_body: str, plain_body: str | None = None) -> None:
    """Set a multipart/alternative body; the plain part is derived from HTML if omitted."""
    plain = plain_body if plain_body is not None else _html_to_plain_text(html_body)
    msg.set_content(plain, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")


def build_email_message(
    task: MailTask,
    body_content: str,
    default_sender: str | None = None,
) -> EmailMessage:
    msg = new_email_message(
        subject=task.subject,
        sender=task.sender or default_sender or "",
        to=task.recipients.to,
        cc=task.recipients.cc,
    )
    if task.body_template.format.lower() == "html":
        set_html_body(msg, body_content)
    else:
        msg.set_content(body_content, subtype="plain", charset="utf-8")
    return msg


INSECURE_SMTP_AUTH_MESSAGE = (
    "SMTP_USER and SMTP_PASSWORD are set, but neither SMTP_USE_TLS nor SMTP_START_TLS is "
    "enabled, so the password would be sent unencrypted. Enable TLS or STARTTLS, or set "
    "SMTP_ALLOW_INSECURE_AUTH=true for a trusted local relay."
)


class InsecureSMTPAuthError(ValueError):
    """SMTP login without TLS/STARTTLS was configured but not explicitly allowed."""


# --- Durable delivery: message snapshot and one phase-aware attempt (ADR 0001, D2) ------


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A task message rendered once and stored before the first attempt."""

    message_id: str
    data: bytes


def envelope_sender(data: bytes, fallback: str = "") -> str:
    """The MAIL FROM address: the bare address of the stored message's From header."""
    header = BytesHeaderParser(policy=email.policy.default).parsebytes(data).get("From", "")
    return parseaddr(str(header))[1] or fallback


def _refusal(code: int) -> Outcome:
    is_permanent = SMTP_PERMANENT_ERROR_MIN <= code < SMTP_PERMANENT_ERROR_MAX
    return Outcome.PERMANENT if is_permanent else Outcome.TRANSIENT


def _closed_session(exc: Exception) -> bool:
    """The server had closed the session: no reply at all, or 421 (closing channel)."""
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return exc.code == SMTP_SERVICE_CLOSING
    return isinstance(exc, aiosmtplib.SMTPServerDisconnected | ConnectionError)


@dataclass(slots=True)
class _Attempt:
    """Per-recipient results of one attempt and how far the SMTP transaction got."""

    recipients: tuple[str, ...]
    phase: str = "connect"
    accepted: list[str] = field(default_factory=list)
    results: dict[str, RecipientResult] = field(default_factory=dict)

    def finish_remaining(self, outcome: Outcome, code: int | None, message: str) -> None:
        for recipient in self.recipients:
            self.results.setdefault(recipient, RecipientResult(outcome, code, message))

    def fail(self, exc: Exception) -> None:
        """Record a failure that ended the transaction for every recipient left.

        A reply code is certain, except at connect/login, which says nothing about the
        recipients. Without a reply, the outcome is unknown only once DATA has started.
        """
        if isinstance(exc, aiosmtplib.SMTPResponseException) and self.phase != "connect":
            self.finish_remaining(_refusal(exc.code), exc.code, exc.message)
            return
        outcome = Outcome.UNKNOWN if self.phase == "data" else Outcome.TRANSIENT
        self.finish_remaining(outcome, None, f"{type(exc).__name__}: {exc}")


class AsyncMailer:
    def __init__(
        self,
        settings: Settings,
        renderer: TemplateRenderer | None = None,
        dry_run: bool = False,
        smtp_client: SMTPClient | None = None,
    ) -> None:
        if not dry_run and smtp_client is None:
            # Dry runs and injected clients never log in to a real server.
            if settings.smtp_auth_blocked:
                raise InsecureSMTPAuthError(INSECURE_SMTP_AUTH_MESSAGE)
            if settings.smtp_auth_unencrypted:
                logger.warning(
                    "SMTP password is sent without TLS/STARTTLS (SMTP_ALLOW_INSECURE_AUTH=true)."
                )
        self.settings = settings
        self.renderer = renderer or TemplateRenderer()
        self.dry_run = dry_run
        self._custom_smtp_client = smtp_client
        self._in_context = False
        self._pool: asyncio.Queue[SMTPClient] | None = None

    async def __aenter__(self) -> "AsyncMailer":
        self._in_context = True
        self._pool = asyncio.Queue()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self._in_context = False
        if self._pool is not None:
            while not self._pool.empty():
                try:
                    client = self._pool.get_nowait()
                    await self._close_client(client)
                except asyncio.QueueEmpty:
                    break
            self._pool = None

    @staticmethod
    async def _close_client(client: SMTPClient) -> None:
        """QUIT if still connected, then always close the transport.

        After the server drops the connection, older aiosmtplib versions leave the
        transport open; closing it here frees the socket in every case.
        """
        if client.is_connected:
            with contextlib.suppress(*SMTP_CLOSE_ERRORS):
                await client.quit()
        client.close()

    def _get_smtp_connection(self) -> SMTPClient:
        if self._custom_smtp_client is not None:
            return self._custom_smtp_client

        return aiosmtplib.SMTP(
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_user or None,
            password=password.get_secret_value()
            if (password := self.settings.smtp_password)
            else None,
            use_tls=self.settings.smtp_use_tls,
            start_tls=self.settings.smtp_start_tls,
            timeout=self.settings.smtp_timeout,
        )

    @staticmethod
    async def _ensure_connected(client: SMTPClient) -> None:
        if not client.is_connected:
            await client.connect()

    def _take_pooled_client(self) -> SMTPClient | None:
        if self._pool is not None:
            with contextlib.suppress(asyncio.QueueEmpty):
                return self._pool.get_nowait()
        return None

    async def _release_pooled_client(self, client: SMTPClient, keep_client: bool) -> None:
        # A client that lost its connection is never returned to the pool.
        if keep_client and self._pool is not None and client.is_connected:
            self._pool.put_nowait(client)
        else:
            await self._close_client(client)

    @contextlib.asynccontextmanager
    async def _acquire_smtp_client(
        self, *, reuse: bool = True
    ) -> AsyncIterator[tuple[SMTPClient, bool]]:
        """A connected client and whether it is a pooled session that was already open.

        ``reuse=False`` skips the pool and opens a new connection.
        """
        if self._custom_smtp_client is not None:
            client = self._custom_smtp_client
            await self._ensure_connected(client)
            yield client, False
            return

        if self._in_context and self._pool is not None:
            pooled = self._take_pooled_client() if reuse else None
            client = pooled if pooled is not None else self._get_smtp_connection()
            reused = pooled is not None and pooled.is_connected
            keep_client = True
            try:
                await self._ensure_connected(client)
                yield client, reused
            except BaseException:
                # Any failure may leave the session mid-transaction: never reuse it.
                keep_client = False
                raise
            finally:
                await self._release_pooled_client(client, keep_client)
            return

        client = self._get_smtp_connection()
        try:
            await self._ensure_connected(client)
            yield client, False
        finally:
            await self._close_client(client)

    def render_task_message(self, task: MailTask) -> RenderedMessage:
        """Render and serialise a task message; raises one of RENDER_ERRORS on failure."""
        body = self.renderer.render_task_body(task)
        msg = build_email_message(task, body, default_sender=self.settings.smtp_user)
        return RenderedMessage(str(msg["Message-ID"]), msg.as_bytes(policy=SNAPSHOT_POLICY))

    async def deliver(self, data: bytes, recipients: Sequence[str]) -> dict[str, RecipientResult]:
        """Send stored message bytes to ``recipients`` in one SMTP transaction.

        Never retries a delivery and never raises for SMTP or network errors: every
        recipient gets an outcome. A failure after DATA has started without a final reply
        is UNKNOWN. A pooled session that the server had already closed when MAIL FROM
        was sent is replaced by a new connection once; nothing was sent on it.
        """
        if self.dry_run:
            attempt = _Attempt(tuple(recipients))
            logger.info(f"[DRY-RUN] Would deliver a stored message to {list(recipients)}")
            attempt.finish_remaining(Outcome.ACCEPTED, None, "dry run")
            return attempt.results
        sender = envelope_sender(data, fallback=self.settings.smtp_user or "")
        return await self._attempt(sender, data, recipients, reuse=True)

    async def _attempt(
        self, sender: str, data: bytes, recipients: Sequence[str], *, reuse: bool
    ) -> dict[str, RecipientResult]:
        attempt = _Attempt(tuple(recipients))
        reused = False
        try:
            async with self._acquire_smtp_client(reuse=reuse) as (client, reused):
                await self._transact(client, sender, data, attempt)
        except (aiosmtplib.SMTPException, OSError) as exc:
            if reused and attempt.phase == "mail" and _closed_session(exc):
                logger.info(f"Pooled SMTP session was closed by the server ({exc}); reconnecting.")
                return await self._attempt(sender, data, recipients, reuse=False)
            attempt.fail(exc)
        return attempt.results

    @staticmethod
    async def _transact(client: SMTPClient, sender: str, data: bytes, attempt: _Attempt) -> None:
        attempt.phase = "mail"
        await client.mail(sender)
        attempt.phase = "rcpt"
        for recipient in attempt.recipients:
            try:
                await client.rcpt(recipient)
            except aiosmtplib.SMTPRecipientRefused as exc:
                attempt.results[recipient] = RecipientResult(
                    _refusal(exc.code), exc.code, exc.message
                )
            else:
                attempt.accepted.append(recipient)
        if not attempt.accepted:
            await client.rset()
            return
        attempt.phase = "data"
        response = await client.data(data)
        for recipient in attempt.accepted:
            attempt.results[recipient] = RecipientResult(
                Outcome.ACCEPTED, response.code, response.message
            )
