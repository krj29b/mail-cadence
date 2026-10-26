import asyncio
import contextlib
import html
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from email.message import EmailMessage, Message
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

from mail_cadence.models import MailTask, Settings, TaskExecutionResult, TaskExecutionStatus
from mail_cadence.scheduler import TokenBucketRateLimiter

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

# Errors that can come out of rendering a task template and building its message.
RENDER_ERRORS: tuple[type[Exception], ...] = (TemplateError, ValueError, TypeError, OSError)
# Errors a broken connection raises on close; quitting is best effort.
SMTP_CLOSE_ERRORS: tuple[type[Exception], ...] = (aiosmtplib.SMTPException, OSError)


class SMTPClient(Protocol):
    """The part of ``aiosmtplib.SMTP`` that AsyncMailer uses; tests pass fakes that match it."""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> object: ...

    async def send_message(
        self, message: Message, /, *, sender: str, recipients: Sequence[str]
    ) -> tuple[dict[str, SMTPResponse], str]: ...

    async def quit(self) -> object: ...


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


def _try_int(val: Any) -> int | None:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


INSECURE_SMTP_AUTH_MESSAGE = (
    "SMTP_USER and SMTP_PASSWORD are set, but neither SMTP_USE_TLS nor SMTP_START_TLS is "
    "enabled, so the password would be sent unencrypted. Enable TLS or STARTTLS, or set "
    "SMTP_ALLOW_INSECURE_AUTH=true for a trusted local relay."
)


class InsecureSMTPAuthError(ValueError):
    """SMTP login without TLS/STARTTLS was configured but not explicitly allowed."""


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
                    await self._quit_if_connected(client)
                except asyncio.QueueEmpty:
                    break
            self._pool = None

    @staticmethod
    async def _quit_if_connected(client: SMTPClient) -> None:
        if client.is_connected:
            with contextlib.suppress(*SMTP_CLOSE_ERRORS):
                await client.quit()

    @staticmethod
    def _extract_smtp_code(error_val: Any) -> int | None:
        if hasattr(error_val, "code"):
            res = _try_int(error_val.code)
            if res is not None:
                return res

        if isinstance(error_val, tuple) and error_val:
            res = _try_int(error_val[0])
            if res is not None:
                return res

        if isinstance(error_val, dict):
            code_val = error_val.get("code")
            if code_val is None:
                code_val = error_val.get("smtp_code")
            if code_val is not None:
                res = _try_int(code_val)
                if res is not None:
                    return res

        return _try_int(error_val)

    @classmethod
    def _is_permanent_smtp_error(cls, errors: dict[str, Any]) -> bool:
        code = cls._extract_smtp_code(errors)
        return code is not None and SMTP_PERMANENT_ERROR_MIN <= code < SMTP_PERMANENT_ERROR_MAX

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

    def _get_pooled_or_new_client(self) -> SMTPClient:
        if self._pool is not None:
            with contextlib.suppress(asyncio.QueueEmpty):
                return self._pool.get_nowait()
        return self._get_smtp_connection()

    async def _release_pooled_client(self, client: SMTPClient, keep_client: bool) -> None:
        # A client that lost its connection is never returned to the pool.
        if keep_client and self._pool is not None and client.is_connected:
            self._pool.put_nowait(client)
        else:
            await self._quit_if_connected(client)

    @contextlib.asynccontextmanager
    async def _acquire_smtp_client(self) -> AsyncIterator[SMTPClient]:
        if self._custom_smtp_client is not None:
            client = self._custom_smtp_client
            await self._ensure_connected(client)
            yield client
            return

        if self._in_context and self._pool is not None:
            client = self._get_pooled_or_new_client()
            keep_client = True
            try:
                await self._ensure_connected(client)
                yield client
            except aiosmtplib.SMTPServerDisconnected:
                keep_client = False
                raise
            finally:
                await self._release_pooled_client(client, keep_client)
            return

        client = self._get_smtp_connection()
        try:
            await self._ensure_connected(client)
            yield client
        finally:
            await self._quit_if_connected(client)

    @staticmethod
    def _smtp_exception_result(
        exc: aiosmtplib.SMTPException,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
            # All recipients refused: report per-recipient codes so 5xx/4xx are classified
            # the same way as on partial delivery.
            refused: dict[str, Any] = {r.recipient: r for r in exc.recipients}
            return False, f"All recipients refused: {list(refused.keys())}", refused
        return False, f"SMTP Exception: {exc}", {}

    async def send_single_message(
        self,
        msg: EmailMessage,
        sender: str,
        recipients: list[str],
    ) -> tuple[bool, str | None, dict[str, Any]]:
        if self.dry_run:
            logger.info(
                f"[DRY-RUN] Would send email from {sender} to {recipients} subject={msg['Subject']}"
            )
            return True, None, {}

        try:
            async with self._acquire_smtp_client() as client:
                res = await client.send_message(msg, sender=sender, recipients=recipients)
                errors = self._refused_recipients(res)

                if errors:
                    return True, f"Partial delivery errors: {list(errors.keys())}", errors
                return True, None, {}
        except aiosmtplib.SMTPResponseException as exc:
            return False, f"SMTP error {exc.code}: {exc.message}", {"smtp_code": exc.code}
        except aiosmtplib.SMTPException as exc:
            return self._smtp_exception_result(exc)
        except OSError as exc:  # refused/reset connections and timeouts
            return False, f"Network/Connection error: {exc}", {}

    @staticmethod
    def _refused_recipients(send_result: object) -> dict[str, Any]:
        """Per-recipient errors from ``send_message``'s ``(errors, message)`` result."""
        match send_result:
            case (dict() as errors, _):
                return errors
            case _:
                return {}

    def _resolve_sender(self, sender: str) -> str:
        if sender:
            return sender
        return self.settings.smtp_user or ""

    @classmethod
    def _partition_partial_errors(
        cls,
        pending_recipients: list[str],
        errors: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str]]:
        failed_in_attempt = list(errors.keys())
        succeeded = [r for r in pending_recipients if r not in failed_in_attempt]
        perm_failed: list[str] = []
        next_pending: list[str] = []
        for recipient in failed_in_attempt:
            code = cls._extract_smtp_code(errors[recipient])
            if code is not None and SMTP_PERMANENT_ERROR_MIN <= code < SMTP_PERMANENT_ERROR_MAX:
                perm_failed.append(recipient)
            else:
                next_pending.append(recipient)
        return succeeded, perm_failed, next_pending

    @classmethod
    def _classify_attempt(
        cls,
        success: bool,
        errors: dict[str, Any],
        pending_recipients: list[str],
    ) -> tuple[list[str], list[str], list[str]]:
        """Split the attempted recipients into (succeeded, permanently failed, still pending)."""
        if success and not errors:
            return list(pending_recipients), [], []
        if errors and "smtp_code" not in errors:
            # Per-recipient errors (partial delivery or all recipients refused).
            return cls._partition_partial_errors(pending_recipients, errors)
        if cls._is_permanent_smtp_error(errors):
            return [], list(pending_recipients), []
        return [], [], list(pending_recipients)

    async def _execute_delivery_attempts(
        self,
        task: MailTask,
        msg: EmailMessage,
        sender_addr: str,
        all_recipients: list[str],
        rate_limiter: TokenBucketRateLimiter | None,
    ) -> tuple[int, list[str], list[str], str | None]:
        attempts = 0
        max_attempts = task.max_retries + 1
        last_error: str | None = None
        pending_recipients = list(all_recipients)
        successful_recipients: list[str] = []
        permanently_failed_recipients: list[str] = []

        while attempts < max_attempts and pending_recipients:
            attempts += 1
            if rate_limiter:
                await rate_limiter.acquire()

            success, error_msg, errors = await self.send_single_message(
                msg=msg,
                sender=sender_addr,
                recipients=pending_recipients,
            )

            last_error = error_msg
            if not success:
                logger.warning(
                    f"Task {task.id} attempt {attempts}/{max_attempts} failed: {error_msg}"
                )
            succeeded, perm_failed, pending_recipients = self._classify_attempt(
                success, errors, pending_recipients
            )
            successful_recipients.extend(succeeded)
            permanently_failed_recipients.extend(perm_failed)

            if attempts < max_attempts and pending_recipients:
                backoff = 0.5 * (2 ** (attempts - 1))
                await asyncio.sleep(backoff)

        failed_recipients = permanently_failed_recipients + pending_recipients
        return attempts, successful_recipients, failed_recipients, last_error

    async def dispatch_task(
        self,
        task: MailTask,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> TaskExecutionResult:
        start_time = time.monotonic()
        all_recipients = task.recipients.all_recipients
        sender_addr = self._resolve_sender(task.sender)

        if not all_recipients:
            # MailTask validation rejects this; guard against unvalidated (model_construct) tasks.
            return TaskExecutionResult(
                task_id=task.id,
                status=TaskExecutionStatus.FAILED,
                sent_count=0,
                duration_ms=0.0,
                error_details="No recipients defined",
            )

        try:
            body_content = self.renderer.render_task_body(task)
            msg = build_email_message(
                task,
                body_content,
                default_sender=self.settings.smtp_user,
            )
        except RENDER_ERRORS as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            logger.error(f"Failed to render template for task {task.id}: {exc}")
            return TaskExecutionResult(
                task_id=task.id,
                status=TaskExecutionStatus.FAILED,
                sent_count=0,
                failed_recipients=all_recipients,
                duration_ms=duration_ms,
                error_details=f"Template rendering error: {exc}",
            )

        (
            attempts,
            successful_recipients,
            failed_recipients,
            last_error,
        ) = await self._execute_delivery_attempts(
            task, msg, sender_addr, all_recipients, rate_limiter
        )
        duration_ms = (time.monotonic() - start_time) * 1000.0

        sent_count = len(successful_recipients)
        if sent_count == len(all_recipients):
            status = TaskExecutionStatus.SUCCESS
            last_error = None
            failed_recipients = []
        elif sent_count > 0:
            status = TaskExecutionStatus.PARTIAL
        else:
            status = TaskExecutionStatus.FAILED

        return TaskExecutionResult(
            task_id=task.id,
            status=status,
            sent_count=sent_count,
            failed_recipients=failed_recipients,
            duration_ms=duration_ms,
            attempt_count=attempts,
            error_details=last_error,
        )
