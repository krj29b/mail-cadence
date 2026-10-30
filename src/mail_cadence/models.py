import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from email.utils import getaddresses
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_xml import BaseXmlModel, attr, element, wrapped

ENV_FILE_VAR = "MAIL_CADENCE_ENV_FILE"


def resolve_env_file() -> Path:
    """Return the settings file: $MAIL_CADENCE_ENV_FILE if set, else ./.env.

    An explicitly configured file must exist; the default ./.env is optional.
    """
    custom = os.environ.get(ENV_FILE_VAR, "").strip()
    if not custom:
        return Path(".env")
    path = Path(custom).expanduser()
    if not path.is_file():
        msg = f"{ENV_FILE_VAR} points to a missing file: {path}"
        raise FileNotFoundError(msg)
    return path


class EmailPriority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_ORDER: dict[str, int] = {
    EmailPriority.HIGH: 0,
    EmailPriority.NORMAL: 1,
    EmailPriority.LOW: 2,
}

MAX_RETRIES_LIMIT = 10
END_OF_DAY = "24:00"


class Outcome(StrEnum):
    """What one SMTP attempt proved for one recipient (ADR 0001, D1/D2)."""

    ACCEPTED = "ACCEPTED"  # 250 after the end of DATA
    PERMANENT = "PERMANENT"  # certain 5xx refusal
    TRANSIENT = "TRANSIENT"  # certain non-delivery: 4xx or failure before DATA
    UNKNOWN = "UNKNOWN"  # the connection failed after DATA: the message may be accepted


@dataclass(frozen=True, slots=True)
class RecipientResult:
    outcome: Outcome
    code: int | None = None
    message: str | None = None


SendKind = Literal["window", "manual"]


@dataclass(frozen=True, slots=True)
class SendContext:
    """What a task message is sent for; templates see it as ``cadence``.

    Built once per execution, when it is claimed, and stored with it, so every render
    of the execution (also after a takeover) uses the same values. ``window_start`` and
    ``window_end`` are aware datetimes in ``timezone``; ``date`` is the local day the
    window occurrence started on, or the local day of the claim when the schedule has
    no window.
    """

    task_id: str
    kind: SendKind
    timezone: str
    window_start: datetime | None
    window_end: datetime | None
    date: date

    @classmethod
    def build(
        cls,
        task_id: str,
        kind: SendKind,
        timezone: str,
        window: tuple[datetime, datetime] | None,
        at: datetime,
    ) -> Self:
        """``window`` is the occurrence as UTC instants; ``at`` the moment of the claim."""
        tz = ZoneInfo(timezone)
        if window is None:
            return cls(task_id, kind, timezone, None, None, at.astimezone(tz).date())
        start, end = (instant.astimezone(tz) for instant in window)
        return cls(task_id, kind, timezone, start, end, start.date())


class TaskExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    # Some recipients may or may not have the message; never resent automatically.
    UNKNOWN = "UNKNOWN"
    SKIPPED_SCHEDULE = "SKIPPED_SCHEDULE"
    # Another worker owns the task right now (ADR 0001, D7), or an earlier delivery of
    # the task has an unknown outcome that an operator has not resolved yet (D12).
    SKIPPED_BUSY = "SKIPPED_BUSY"


def _check_no_newlines(value: str | None, field_name: str) -> None:
    if value is not None and ("\r" in value or "\n" in value):
        msg = (
            f"Field '{field_name}' contains illegal newline characters (SMTP injection prevention)"
        )
        raise ValueError(msg)


_ADDR_SPEC_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+$")


def _check_email_address(value: str | None, field_name: str) -> None:
    """Accept one address, bare (``a@example.com``) or with a name (``Ann <a@example.com>``)."""
    if value is None:
        return
    _check_no_newlines(value, field_name)
    parsed = getaddresses([value])
    if len(parsed) != 1 or not _ADDR_SPEC_RE.match(parsed[0][1]):
        msg = f"Field '{field_name}' contains an invalid email address: {value!r}"
        raise ValueError(msg)


class ReportSettings(BaseXmlModel, tag="ReportSettings"):
    recipient: str = element(tag="Recipient")
    sender: str | None = element(tag="Sender", default=None)
    subject_prefix: str = element(tag="SubjectPrefix", default="[MailCadence Report]")
    include_full_error_trace: bool = element(tag="IncludeFullErrorTrace", default=True)

    @model_validator(mode="after")
    def validate_addresses(self) -> Self:
        _check_email_address(self.recipient, "recipient")
        _check_email_address(self.sender or None, "sender")
        _check_no_newlines(self.subject_prefix, "subject_prefix")
        return self


class Recipients(BaseXmlModel, tag="Recipients"):
    to: list[str] = element(tag="To", default_factory=list)
    cc: list[str] = element(tag="Cc", default_factory=list)
    bcc: list[str] = element(tag="Bcc", default_factory=list)

    @model_validator(mode="after")
    def validate_addresses(self) -> Self:
        for field_name, addr_list in [("to", self.to), ("cc", self.cc), ("bcc", self.bcc)]:
            for addr in addr_list:
                _check_email_address(addr, field_name)
        return self

    @property
    def all_recipients(self) -> list[str]:
        return self.to + self.cc + self.bcc


class BodyTemplate(BaseXmlModel, tag="BodyTemplate"):
    format: str = attr(default="html")
    template_path: str


class ContextVar(BaseXmlModel, tag="Var"):
    name: str = attr()
    value: str


class MailTask(BaseXmlModel, tag="MailTask"):
    id: str = attr()
    max_retries: int = attr(default=3, ge=0, le=MAX_RETRIES_LIMIT)
    priority: EmailPriority = attr(default=EmailPriority.NORMAL)
    schedule_ref: str = attr()
    sender: str = element(tag="Sender")
    recipients: Recipients = element(tag="Recipients")
    subject: str = element(tag="Subject")
    body_template: BodyTemplate = element(tag="BodyTemplate")
    context_variables: list[ContextVar] = wrapped(
        "ContextVariables", element(tag="Var", default_factory=list), default_factory=list
    )

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        # An empty sender falls back to SMTP_USER at dispatch time.
        _check_email_address(self.sender or None, "sender")
        _check_no_newlines(self.subject, "subject")
        if not self.recipients.all_recipients:
            raise ValueError(f"Task '{self.id}' has no recipients (To, Cc or Bcc)")
        return self

    def get_context_dict(self) -> dict[str, str]:
        return {var.name: var.value for var in self.context_variables}


class MailConfiguration(BaseXmlModel, tag="MailConfiguration"):
    report_settings: ReportSettings = element(tag="ReportSettings")
    tasks: list[MailTask] = wrapped("Tasks", element(tag="MailTask", default_factory=list))


@lru_cache(maxsize=1024)
def parse_time_str(time_str: str) -> time:
    if not isinstance(time_str, str):
        raise ValueError(f"Time value must be a string, got {type(time_str)}")
    parts = time_str.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time format '{time_str}'. Expected 'HH:MM' or 'HH:MM:SS'.")
    try:
        hour, minute, *seconds = (int(part) for part in parts)
        return time(hour, minute, seconds[0] if seconds else 0)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid time string '{time_str}': {exc}") from exc


VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def parse_window_end(time_str: str) -> time:
    """Parse a window end; ``24:00`` means the end of the day (inclusive up to 23:59:59.999999)."""
    if isinstance(time_str, str) and time_str.strip() in (END_OF_DAY, f"{END_OF_DAY}:00"):
        return time.max
    return parse_time_str(time_str)


class HourWindow(BaseXmlModel, tag="HourWindow"):
    """Allowed sending window; both ends are inclusive. Use end="24:00" for the end of day."""

    start: str = attr()
    end: str = attr()
    days_of_week: str = attr(default="*")
    max_concurrency: int = attr(default=1)
    rate_limit_per_min: int = attr(default=60)

    @field_validator("days_of_week", mode="before")
    @classmethod
    def validate_days_of_week(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(f"days_of_week must be a string, got {type(v)}")
        v_clean = v.strip().lower()
        if v_clean == "*":
            return "*"
        tokens = [t.strip() for t in v_clean.split(",") if t.strip()]
        if not tokens:
            raise ValueError("days_of_week cannot be empty")
        for token in tokens:
            if token not in VALID_WEEKDAYS:
                valid_list = sorted(VALID_WEEKDAYS)
                msg = f"Invalid weekday abbreviation '{token}'. Must be one of {valid_list} or '*'"
                raise ValueError(msg)
        return ",".join(tokens)

    @model_validator(mode="after")
    def validate_hour_window(self) -> Self:
        parse_time_str(self.start)
        parse_window_end(self.end)
        if self.max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {self.max_concurrency}")
        if self.rate_limit_per_min < 0:
            raise ValueError(f"rate_limit_per_min must be >= 0, got {self.rate_limit_per_min}")
        return self

    @cached_property
    def start_time(self) -> time:
        return parse_time_str(self.start)

    @cached_property
    def end_time(self) -> time:
        return parse_window_end(self.end)


class Schedule(BaseXmlModel, tag="Schedule"):
    id: str = attr()
    description: str | None = attr(default=None)
    allowed_hours: list[HourWindow] = wrapped(
        "AllowedHours", element(tag="HourWindow", default_factory=list)
    )


class WorkSchedules(BaseXmlModel, tag="WorkSchedules"):
    timezone: str = attr(default="UTC")
    schedules: list[Schedule] = element(tag="Schedule", default_factory=list)

    @model_validator(mode="after")
    def validate_timezone(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid timezone '{self.timezone}': {exc}") from exc
        return self


class TaskExecutionResult(BaseModel):
    task_id: str
    status: TaskExecutionStatus
    sent_count: int = 0
    failed_recipients: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    error_details: str | None = None
    attempt_count: int = 1


class BatchSummaryReport(BaseModel):
    total_tasks: int
    successful_tasks: int
    partial_tasks: int
    failed_tasks: int
    skipped_tasks: int
    total_duration_ms: float
    start_time: datetime
    end_time: datetime
    results: list[TaskExecutionResult]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    # SecretStr: shown as '**********' in repr, logs and dumps; read with get_secret_value().
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = False
    smtp_start_tls: bool = False
    # Allow SMTP login without TLS/STARTTLS (only for a trusted local relay).
    smtp_allow_insecure_auth: bool = False
    smtp_timeout: float = 30.0
    mail_cadence_db: str = "mail_cadence.db"
    message_retention_days: int = Field(default=30, ge=1, le=3650)

    admin_user: str | None = None
    admin_password: SecretStr | None = None
    # Wrong web panel logins allowed per client IP within the window (then 429).
    admin_login_max_failures: int = Field(default=10, ge=1)
    admin_login_window_seconds: float = Field(default=900.0, gt=0)
    log_format: Literal["text", "json"] = "text"

    @field_validator("log_format", mode="before")
    @classmethod
    def normalize_log_format(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_clean = v.strip().lower()
            if v_clean in ("text", "json"):
                return v_clean
        return v

    @property
    def smtp_auth_unencrypted(self) -> bool:
        """SMTP login is configured, but neither TLS nor STARTTLS: the password is sent in clear."""
        has_credentials = bool(self.smtp_user and self.smtp_password)
        return has_credentials and not (self.smtp_use_tls or self.smtp_start_tls)

    @property
    def smtp_auth_blocked(self) -> bool:
        """Unencrypted SMTP login that was not explicitly allowed; real sends are refused."""
        return self.smtp_auth_unencrypted and not self.smtp_allow_insecure_auth

    @classmethod
    def load(cls) -> Self:
        # `_env_file` is pydantic-settings' runtime override; the mypy plugin does not model it.
        return cls(_env_file=resolve_env_file())  # type: ignore[call-arg]
