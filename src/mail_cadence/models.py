from datetime import datetime, time
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_xml import BaseXmlModel, attr, element, wrapped

_PROJECT_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"

PRIORITY_ORDER: dict[str, int] = {"high": 0, "normal": 1, "low": 2}


class TaskExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED_SCHEDULE = "SKIPPED_SCHEDULE"


def _check_no_newlines(value: str | None, field_name: str) -> None:
    if value is not None and ("\r" in value or "\n" in value):
        msg = (
            f"Field '{field_name}' contains illegal newline characters (SMTP injection prevention)"
        )
        raise ValueError(msg)


class ReportSettings(BaseXmlModel, tag="ReportSettings"):
    recipient: str = element(tag="Recipient")
    sender: str | None = element(tag="Sender", default=None)
    subject_prefix: str = element(tag="SubjectPrefix", default="[MailCadence Report]")
    include_full_error_trace: bool = element(tag="IncludeFullErrorTrace", default=True)

    @model_validator(mode="after")
    def validate_no_newlines(self) -> Self:
        _check_no_newlines(self.recipient, "recipient")
        _check_no_newlines(self.sender, "sender")
        return self


class Recipients(BaseXmlModel, tag="Recipients"):
    to: list[str] = element(tag="To", default_factory=list)
    cc: list[str] = element(tag="Cc", default_factory=list)
    bcc: list[str] = element(tag="Bcc", default_factory=list)

    @model_validator(mode="after")
    def validate_no_newlines(self) -> Self:
        for field_name, addr_list in [("to", self.to), ("cc", self.cc), ("bcc", self.bcc)]:
            for addr in addr_list:
                _check_no_newlines(addr, field_name)
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
    max_retries: int = attr(default=3)
    priority: str = attr(default="normal")
    schedule_ref: str = attr()
    sender: str = element(tag="Sender")
    recipients: Recipients = element(tag="Recipients")
    subject: str = element(tag="Subject")
    body_template: BodyTemplate = element(tag="BodyTemplate")
    context_variables: list[ContextVar] = wrapped(
        "ContextVariables", element(tag="Var", default_factory=list), default_factory=list
    )

    @model_validator(mode="after")
    def validate_no_newlines(self) -> Self:
        _check_no_newlines(self.sender, "sender")
        _check_no_newlines(self.subject, "subject")
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
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour, minute, second)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid time string '{time_str}': {exc}") from exc


VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class HourWindow(BaseXmlModel, tag="HourWindow"):
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
        parse_time_str(self.end)
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
        return parse_time_str(self.end)


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
        except Exception as exc:
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
        env_file=(_PROJECT_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = False
    smtp_start_tls: bool = False
    smtp_timeout: float = 30.0
    mail_cadence_db: str = "mail_cadence.db"

    admin_user: str | None = None
    admin_password: str | None = None

    @classmethod
    def load(cls) -> Self:
        return cls()
