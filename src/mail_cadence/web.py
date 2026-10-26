import json
import logging
import math
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Generator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jinja2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from mail_cadence import __version__
from mail_cadence.database import get_connection, init_db
from mail_cadence.dispatch_jobs import DispatchBusyError, DispatchJob, DispatchRunner, JobWork
from mail_cadence.login_throttle import LoginThrottle
from mail_cadence.mailer import (
    INTERNAL_TEMPLATES,
    TemplateRenderer,
    list_builtin_templates,
)
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    EmailPriority,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    Settings,
    WorkSchedules,
)
from mail_cadence.pipeline import DispatchOptions, dispatch_single_task, run_dispatch
from mail_cadence.repository import DatabaseRepository
from mail_cadence.scheduler import TaskScheduler

logger = logging.getLogger(__name__)

COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Warsaw",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Kyiv",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Australia/Sydney",
)

_VALID_TEMPLATE_EXTENSIONS: frozenset[str] = frozenset({".j2", ".html", ".htm", ".txt"})
_IGNORED_TEMPLATE_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "web_templates",
        "tests",
        "docs",
        "config",
        "build",
        "dist",
    }
)

security = HTTPBasic(auto_error=False)


def _get_settings(request: Request) -> Settings:
    custom: Settings | None = getattr(request.app.state, "custom_settings", None)
    if custom is not None:
        return custom
    loaded = Settings.load()
    request.app.state.settings = loaded
    return loaded


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    settings = _get_settings(request)
    admin_user = settings.admin_user
    admin_password = settings.admin_password

    if not admin_user or not admin_password:
        if getattr(request.app.state, "allow_insecure_no_auth", False):
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Admin panel credentials are not configured. Set ADMIN_USER and ADMIN_PASSWORD."
            ),
        )

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )
    if not credentials:
        # The browser's first request has no credentials; it is not a failed login.
        raise unauthorized

    throttle: LoginThrottle = request.app.state.login_throttle
    client = _client_address(request)
    # Checked before the password, so a blocked client cannot learn if a guess was right.
    _raise_if_throttled(throttle, client)
    if not _credentials_match(credentials, admin_user, admin_password):
        if throttle.record_failure(client):
            logger.warning(
                f"Too many failed admin logins from {client}: blocked for up to "
                f"{throttle.window_seconds:.0f}s"
            )
        raise unauthorized
    throttle.reset(client)


def _client_address(request: Request) -> str:
    # Behind a reverse proxy uvicorn sets this from X-Forwarded-For, but only for proxies
    # listed in FORWARDED_ALLOW_IPS (default: 127.0.0.1 and ::1).
    return request.client.host if request.client else "unknown"


def _raise_if_throttled(throttle: LoginThrottle, client: str) -> None:
    retry_after = throttle.retry_after(client)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def _credentials_match(
    credentials: HTTPBasicCredentials, admin_user: str, admin_password: str
) -> bool:
    # Both comparisons always run (constant time, no short-circuit on the user name).
    user_ok = secrets.compare_digest(credentials.username.encode(), admin_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), admin_password.encode())
    return user_ok and pass_ok


_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def verify_same_origin(request: Request) -> None:
    """Reject state-changing requests sent by other sites (CSRF protection).

    Browsers attach Basic Auth credentials automatically, so cross-site requests are
    refused based on the ``Sec-Fetch-Site`` and ``Origin`` headers.
    """
    if request.method not in _UNSAFE_METHODS:
        return
    forbidden = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cross-site request rejected.",
    )
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in ("same-origin", "none"):
        raise forbidden
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc.lower() != host.lower():
            raise forbidden


def _is_valid_task_template(path: Path, base_path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in _VALID_TEMPLATE_EXTENSIONS:
        return False
    if path.name in INTERNAL_TEMPLATES:
        return False
    rel_parts = path.relative_to(base_path).parts
    return not any(p.startswith((".", "_")) or p in _IGNORED_TEMPLATE_DIRS for p in rel_parts)


def _discover_available_templates(template_dir: str | Path) -> list[str]:
    """List templates usable by tasks: files in template_dir, then the built-in ones."""
    base_path = Path(template_dir)
    templates_found: list[str] = []
    if base_path.is_dir():
        templates_found = sorted(
            path.relative_to(base_path).as_posix()
            for path in base_path.rglob("*")
            if _is_valid_task_template(path, base_path)
        )
    return templates_found + list_builtin_templates()


def _get_tasks_window_status(
    repo: DatabaseRepository,
    tasks: list[MailTask],
    work_schedules: WorkSchedules,
) -> dict[str, dict[str, Any]]:
    scheduler = TaskScheduler(work_schedules)
    states = repo.get_task_execution_states()
    ref_dt = scheduler.now()
    statuses: dict[str, dict[str, Any]] = {}

    for task in tasks:
        last_run, last_status = states.get(task.id, (None, None))
        window_status = scheduler.window_status(
            task, reference_dt=ref_dt, last_run=last_run, last_status=last_status
        )
        has_window = window_status.window is not None
        status_key = (
            "dispatched_in_window"
            if window_status.dispatched_in_window
            else ("eligible_now" if has_window else "waiting_window")
        )

        statuses[task.id] = {
            "status_key": status_key,
            "has_active_window": has_window,
            "dispatched_in_window": window_status.dispatched_in_window,
            "eligible_now": window_status.eligible,
            "last_status": last_status.value if last_status else "NEVER_RUN",
            "last_executed_at": last_run.isoformat() if last_run else None,
        }

    return statuses


def _get_tz_info(tz_name: str) -> dict[str, str]:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
        tz_name = "UTC"

    now = datetime.now(tz)
    local_time_str = now.strftime("%H:%M:%S")
    abbrev = now.strftime("%Z")
    offset_str = now.strftime("%z")
    expected_offset_len = 5
    is_standard_offset = len(offset_str) == expected_offset_len
    formatted_offset = (
        f"UTC{offset_str[:3]}:{offset_str[3:]}" if is_standard_offset else f"UTC{offset_str}"
    )
    return {
        "timezone": tz_name,
        "local_time": local_time_str,
        "abbrev": abbrev,
        "formatted_offset": formatted_offset,
        "display": f"{local_time_str} ({abbrev}, {formatted_offset})",
    }


def _get_schedule_statuses(work_schedules: WorkSchedules) -> dict[str, bool]:
    scheduler = TaskScheduler(work_schedules)
    ref_dt = scheduler.now()
    return {
        sched.id: scheduler.active_window(sched.id, ref_dt) is not None
        for sched in work_schedules.schedules
    }


def _normalize_filter_string(val: str | None) -> str | None:
    if not val or not val.strip():
        return None
    return val.strip()


def _normalize_page_size(page_size: int) -> int:
    allowed_page_sizes = (10, 25, 50, 100)
    if page_size in allowed_page_sizes:
        return page_size
    if page_size < 1:
        return 25
    return min(page_size, 200)


def _get_all_known_task_ids(repo: DatabaseRepository) -> list[str]:
    configured = [t.id for t in repo.get_mail_tasks()]
    return sorted(set(configured) | set(repo.get_logged_task_ids()))


@dataclass(frozen=True, slots=True)
class LogFilters:
    """Paging and filter query parameters shared by the log views."""

    page: int = 1
    page_size: int = 25
    status_filter: str | None = None
    task_id: str | None = None
    q: str | None = None


def get_log_filters(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    task_id: str | None = Query(None),
    q: str | None = Query(None),
) -> LogFilters:
    return LogFilters(page, page_size, status_filter, task_id, q)


def _build_logs_pagination_context(repo: DatabaseRepository, filters: LogFilters) -> dict[str, Any]:
    page = filters.page
    norm_page_size = _normalize_page_size(filters.page_size)
    s_filter = _normalize_filter_string(filters.status_filter)
    t_filter = _normalize_filter_string(filters.task_id)
    q_filter = _normalize_filter_string(filters.q)

    total_logs = repo.count_execution_logs(
        status_filter=s_filter,
        task_id_filter=t_filter,
        search_query=q_filter,
    )
    total_pages = max(1, math.ceil(total_logs / norm_page_size))
    norm_page = min(max(1, page), total_pages)

    offset = (norm_page - 1) * norm_page_size
    logs = repo.get_recent_execution_logs(
        limit=norm_page_size,
        status_filter=s_filter,
        task_id_filter=t_filter,
        offset=offset,
        search_query=q_filter,
    )

    all_task_ids = _get_all_known_task_ids(repo)
    start_idx = 0 if total_logs == 0 else offset + 1
    end_idx = 0 if total_logs == 0 else min(offset + norm_page_size, total_logs)
    page_range = list(range(max(1, norm_page - 2), min(total_pages, norm_page + 2) + 1))

    return {
        "logs": logs,
        "page": norm_page,
        "page_size": norm_page_size,
        "total_logs": total_logs,
        "total_pages": total_pages,
        "start_idx": start_idx,
        "end_idx": end_idx,
        "status_filter": s_filter or "",
        "task_id_filter": t_filter or "",
        "search_query": q_filter or "",
        "task_ids": all_task_ids,
        "page_range": page_range,
    }


PACKAGE_TEMPLATES_DIR = Path(__file__).parent / "web_templates"
PACKAGE_STATIC_DIR = Path(__file__).parent / "web_static"
STATIC_PREFIX = "/static"
templates = Jinja2Templates(directory=str(PACKAGE_TEMPLATES_DIR))
templates.env.globals["asset_version"] = __version__

# Every panel response. Scripts, styles and fonts come only from /static: no inline
# JavaScript, no event-handler attributes, no CDNs.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Rendered task templates (preview) run in a sandbox with an opaque origin: no scripts,
# no access to the panel. Inline styles and remote images are allowed because emails use them.
PREVIEW_CONTENT_SECURITY_POLICY = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline' https:; "
    "img-src data: https:; font-src data: https:; frame-ancestors 'none'"
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not "no-referrer": with it browsers send "Origin: null" on form posts, which the
    # same-origin check (correctly) rejects.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


async def add_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Set security headers on every response; a route may set its own CSP (preview)."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Static assets are revalidated (ETag); pages and API data are never stored.
    is_static = request.url.path.startswith(STATIC_PREFIX + "/")
    response.headers.setdefault("Cache-Control", "no-cache" if is_static else "no-store")
    return response


def format_utc_timestamp(value: object) -> str:
    """Show a stored ISO 8601 timestamp as 'YYYY-MM-DD HH:MM:SS UTC' (raw text if unparsable)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["utc_time"] = format_utc_timestamp
router = APIRouter()


def get_repo(request: Request) -> Generator[DatabaseRepository, None, None]:
    db_path = getattr(request.app.state, "db_path", "mail_cadence.db")
    with closing(get_connection(db_path)) as conn:
        yield DatabaseRepository(conn)


def parse_email_list(raw_input: str) -> list[str]:
    if not raw_input:
        return []
    cleaned = raw_input.replace("\r\n", "\n").replace("\r", "\n")
    items = [item.strip() for line in cleaned.split("\n") for item in line.split(",")]
    return [item for item in items if item]


def parse_context_vars(raw_json: str) -> list[ContextVar]:
    if not raw_json or not raw_json.strip():
        return []
    data = json.loads(raw_json)
    if isinstance(data, list):
        return [ContextVar(name=item["name"], value=str(item["value"])) for item in data]
    if isinstance(data, dict):
        return [ContextVar(name=k, value=str(v)) for k, v in data.items()]
    return []


def parse_hour_windows(raw_json: str) -> list[HourWindow]:
    if not raw_json or not raw_json.strip():
        return []
    data = json.loads(raw_json)
    if isinstance(data, list):
        return [
            HourWindow(
                start=item["start"],
                end=item["end"],
                days_of_week=item.get("days_of_week", "*"),
                max_concurrency=int(item.get("max_concurrency", 1)),
                rate_limit_per_min=int(item.get("rate_limit_per_min", 60)),
            )
            for item in data
        ]
    return []


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    filters: LogFilters = Depends(get_log_filters),
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    logs_ctx = _build_logs_pagination_context(repo, filters)
    if (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-Target") == "logs-table-container"
    ):
        return templates.TemplateResponse(
            request=request,
            name="_logs_table.html",
            context={"request": request, **logs_ctx},
        )

    stats = repo.get_dashboard_stats()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "stats": stats,
            "job": _get_runner(request).current,
            **logs_ctx,
        },
    )


@router.get("/dashboard/logs", response_class=HTMLResponse)
def get_dashboard_logs(
    request: Request,
    filters: LogFilters = Depends(get_log_filters),
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    logs_ctx = _build_logs_pagination_context(repo, filters)
    return templates.TemplateResponse(
        request=request,
        name="_logs_table.html",
        context={"request": request, **logs_ctx},
    )


@router.get("/api/tasks/{task_id}", response_class=JSONResponse)
def get_api_task(
    task_id: str,
    repo: DatabaseRepository = Depends(get_repo),
) -> dict[str, Any]:
    task = repo.get_mail_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    task_dict = task.model_dump(mode="json")
    task_dict["body_format"] = task.body_template.format
    task_dict["body_template_path"] = task.body_template.template_path
    task_dict["to"] = task.recipients.to
    task_dict["cc"] = task.recipients.cc
    task_dict["bcc"] = task.recipients.bcc
    return task_dict


@router.get("/tasks", response_class=HTMLResponse)
def list_tasks(
    request: Request,
    edit: str | None = Query(None),
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    edit_task = repo.get_mail_task(edit) if edit else None
    return _render_tasks_page(request, repo, edit_task=edit_task)


def _parse_priority(raw: str) -> EmailPriority:
    try:
        return EmailPriority(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(p.value for p in EmailPriority)
        raise ValueError(f"Invalid priority '{raw}'; must be one of: {allowed}") from None


def _build_task_from_form_dict(form_dict: dict[str, Any], default_sender: str = "") -> MailTask:
    task_id = str(form_dict.get("id", ""))
    schedule_ref = str(form_dict.get("schedule_ref", ""))
    sender_raw = str(form_dict.get("sender", ""))
    sender = sender_raw if sender_raw.strip() else default_sender
    priority = _parse_priority(str(form_dict.get("priority", "normal")))
    max_retries = int(form_dict.get("max_retries", 3))
    subject = str(form_dict.get("subject", ""))
    body_format = str(form_dict.get("body_format", "html"))
    body_template_path = str(form_dict.get("body_template_path", ""))

    to_list = parse_email_list(str(form_dict.get("to", "")))
    cc_list = parse_email_list(str(form_dict.get("cc", "")))
    bcc_list = parse_email_list(str(form_dict.get("bcc", "")))

    ctx_vars_raw = str(form_dict.get("context_variables", ""))
    context_vars = parse_context_vars(ctx_vars_raw)

    return MailTask(
        id=task_id,
        max_retries=max_retries,
        priority=priority,
        schedule_ref=schedule_ref,
        sender=sender,
        recipients=Recipients(to=to_list, cc=cc_list, bcc=bcc_list),
        subject=subject,
        body_template=BodyTemplate(
            format=body_format,
            template_path=body_template_path,
        ),
        context_variables=context_vars,
    )


@router.post("/tasks", response_class=HTMLResponse)
async def save_task(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    default_sender = _get_settings(request).smtp_user or ""
    try:
        form_data = await request.form()
        form_dict = {k: v for k, v in form_data.items()}
        task = _build_task_from_form_dict(form_dict, default_sender=default_sender)
        if not repo.schedule_exists(task.schedule_ref):
            raise ValueError(f"Schedule '{task.schedule_ref}' does not exist")
        was_reset = repo.save_single_task(task)
    except (ValidationError, ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation Error: {exc}",
        ) from exc

    message = f"Task '{task.id}' saved successfully."
    if not was_reset:
        message += " No changes detected; dispatch state in the current window was kept."
    return _render_tasks_page(request, repo, message)


@router.post("/tasks/{task_id}/reset-window", response_class=HTMLResponse)
def reset_task_window(
    task_id: str,
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    if not repo.reset_task_window_state(task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    return _render_tasks_page(
        request,
        repo,
        f"Task '{task_id}' may be dispatched again in its current window.",
    )


def _render_tasks_page(
    request: Request,
    repo: DatabaseRepository,
    message: str | None = None,
    edit_task: MailTask | None = None,
) -> Response:
    tasks = repo.get_mail_tasks()
    work_schedules = repo.get_work_schedules()
    template_dir = getattr(request.app.state, "template_dir", ".")
    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            "active_page": "tasks",
            "tasks": tasks,
            "schedules": work_schedules.schedules,
            "default_sender": _get_settings(request).smtp_user or "",
            "available_templates": _discover_available_templates(template_dir),
            "task_statuses": _get_tasks_window_status(repo, tasks, work_schedules),
            "edit_task": edit_task,
            "message": message,
        },
    )


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: str,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    deleted = repo.delete_mail_task(task_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    return HTMLResponse("")


@router.get("/tasks/{task_id}/preview")
def preview_task(
    task_id: str,
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    task = repo.get_mail_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )

    template_dir = getattr(request.app.state, "template_dir", ".")
    renderer = TemplateRenderer(template_dir=template_dir)

    try:
        rendered = renderer.render_task_body(task)
    except (ValueError, jinja2.TemplateNotFound, jinja2.TemplateError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template error: {exc}",
        ) from exc

    headers = {"Content-Security-Policy": PREVIEW_CONTENT_SECURITY_POLICY}
    if task.body_template.format.lower() == "html":
        return HTMLResponse(content=rendered, headers=headers)
    return Response(content=rendered, media_type="text/plain", headers=headers)


@router.get("/api/schedules/{schedule_id}", response_class=JSONResponse)
def get_api_schedule(
    schedule_id: str,
    repo: DatabaseRepository = Depends(get_repo),
) -> dict[str, Any]:
    work_schedules = repo.get_work_schedules()
    sched = next((s for s in work_schedules.schedules if s.id == schedule_id), None)
    if not sched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule '{schedule_id}' not found",
        )
    return sched.model_dump(mode="json")


@router.get("/schedules", response_class=HTMLResponse)
def list_schedules(
    request: Request,
    edit: str | None = Query(None),
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    return _render_schedules_page(request, repo, edit_id=edit)


def _render_schedules_page(
    request: Request,
    repo: DatabaseRepository,
    message: str | None = None,
    edit_id: str | None = None,
) -> Response:
    work_schedules = repo.get_work_schedules()
    edit_schedule: Schedule | None = None
    if edit_id:
        edit_schedule = next((s for s in work_schedules.schedules if s.id == edit_id), None)
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            "active_page": "schedules",
            "work_schedules": work_schedules,
            "common_timezones": COMMON_TIMEZONES,
            "tz_info": _get_tz_info(work_schedules.timezone),
            "schedule_statuses": _get_schedule_statuses(work_schedules),
            "edit_schedule": edit_schedule,
            "message": message,
        },
    )


@router.post("/schedules", response_class=HTMLResponse)
async def save_schedule(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    form = await request.form()
    schedule_id = str(form.get("id", ""))
    desc = form.get("description")
    description = str(desc) if desc is not None and str(desc).strip() else None
    allowed_hours_json = str(form.get("allowed_hours_json", ""))

    try:
        allowed_hours = parse_hour_windows(allowed_hours_json)
        sched = Schedule(
            id=schedule_id,
            description=description,
            allowed_hours=allowed_hours,
        )
        repo.save_single_schedule(sched)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation Error: {exc}",
        ) from exc

    return _render_schedules_page(request, repo, f"Schedule '{schedule_id}' saved successfully.")


@router.post("/schedules/timezone", response_class=HTMLResponse)
async def update_timezone(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    form = await request.form()
    tz_val = str(form.get("timezone", "UTC"))

    try:
        work_schedules = repo.get_work_schedules()
        updated_ws = WorkSchedules(timezone=tz_val, schedules=work_schedules.schedules)
        repo.save_work_schedules(updated_ws)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation Error: {exc}",
        ) from exc

    return _render_schedules_page(request, repo, f"Timezone updated to '{tz_val}'.")


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: str,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    try:
        deleted = repo.delete_schedule(schedule_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Schedule '{schedule_id}' not found",
            )
        return HTMLResponse("")
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete schedule '{schedule_id}': referenced by existing tasks.",
        ) from exc


@router.get("/settings", response_class=HTMLResponse)
def view_settings(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    return _render_settings_page(request, repo)


def _render_settings_page(
    request: Request, repo: DatabaseRepository, message: str | None = None
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": repo.get_report_settings(),
            "smtp_settings": _get_settings(request),
            "message": message,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    form = await request.form()
    recipient = str(form.get("recipient", ""))
    sender_raw = form.get("sender")
    sender = str(sender_raw) if sender_raw and str(sender_raw).strip() else None
    subject_prefix = str(form.get("subject_prefix", "[MailCadence Report]"))
    include_full_error_trace = form.get("include_full_error_trace") in ("true", "1", "on", True)

    try:
        settings = ReportSettings(
            recipient=recipient,
            sender=sender,
            subject_prefix=subject_prefix,
            include_full_error_trace=include_full_error_trace,
        )
        repo.save_report_settings(settings)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation Error: {exc}",
        ) from exc

    return _render_settings_page(request, repo, "Report settings updated successfully.")


@router.get("/api/stats", response_class=JSONResponse)
def api_stats(
    repo: DatabaseRepository = Depends(get_repo),
) -> dict[str, Any]:
    return repo.get_dashboard_stats()


@router.get("/api/logs", response_class=JSONResponse)
def api_logs(
    limit: int = Query(50, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    task_id_filter: str | None = Query(None, alias="task_id"),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
    repo: DatabaseRepository = Depends(get_repo),
) -> list[dict[str, Any]]:
    return repo.get_recent_execution_logs(
        limit=limit,
        status_filter=status_filter,
        task_id_filter=task_id_filter,
        offset=offset,
        search_query=q,
    )


@router.post("/dispatch/run", response_class=HTMLResponse)
async def trigger_dispatch_run(
    request: Request,
    filters: LogFilters = Depends(get_log_filters),
) -> Response:
    form = await request.form()
    dry_run_raw = form.get("dry_run", request.query_params.get("dry_run", "false"))
    dry_run = str(dry_run_raw).lower() in ("true", "1", "on", "yes")

    db_path: str = request.app.state.db_path
    options = DispatchOptions(
        db_path=db_path, template_dir=request.app.state.template_dir, dry_run=dry_run
    )
    settings = _get_settings(request)

    async def work(job: DispatchJob) -> None:
        job.exit_code = await run_dispatch(options, settings)

    job = _start_job(request, DispatchJob(kind="batch", dry_run=dry_run), work)

    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        stats = repo.get_dashboard_stats()
        logs_ctx = _build_logs_pagination_context(repo, filters)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        status_code=status.HTTP_202_ACCEPTED,
        context={
            "request": request,
            "active_page": "dashboard",
            "stats": stats,
            "job": job,
            "message": f"Dispatch started (dry_run={dry_run}).",
            **logs_ctx,
        },
    )


@router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
def run_single_task(
    task_id: str,
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    if repo.get_mail_task(task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )

    db_path: str = request.app.state.db_path
    template_dir: str = request.app.state.template_dir
    settings = _get_settings(request)

    async def work(job: DispatchJob) -> None:
        job.result = await dispatch_single_task(db_path, task_id, settings, template_dir)

    job = _start_job(request, DispatchJob(kind="task", task_id=task_id), work)
    return _render_dispatch_status(request, job, status_code=status.HTTP_202_ACCEPTED)


@router.get("/dispatch/status", response_class=HTMLResponse)
def get_dispatch_status(request: Request) -> Response:
    """HTML status fragment; it keeps polling itself while a job is running."""
    return _render_dispatch_status(request, _get_runner(request).current)


@router.get("/api/dispatch/status", response_class=JSONResponse)
def get_api_dispatch_status(request: Request) -> dict[str, Any]:
    job = _get_runner(request).current
    return job.to_dict() if job else {"state": "idle"}


def _get_runner(request: Request) -> DispatchRunner:
    runner: DispatchRunner = request.app.state.dispatch_runner
    return runner


def _start_job(request: Request, job: DispatchJob, work: JobWork) -> DispatchJob:
    try:
        return _get_runner(request).start(job, work)
    except DispatchBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _render_dispatch_status(
    request: Request, job: DispatchJob | None, status_code: int = status.HTTP_200_OK
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="_dispatch_status.html",
        status_code=status_code,
        context={"request": request, "job": job},
    )


def create_app(
    db_path: str | Path = "mail_cadence.db",
    template_dir: str | Path = ".",
    settings: Settings | None = None,
    allow_insecure_no_auth: bool = False,
) -> FastAPI:
    """Build the admin panel app.

    Without ADMIN_USER/ADMIN_PASSWORD every request is refused with 503, unless
    ``allow_insecure_no_auth`` is set explicitly (intended for local use only).
    """
    app = FastAPI(
        title="MailCadence Admin Panel",
        description="Lightweight web administration and observability panel",
        version=__version__,
        dependencies=[Depends(verify_credentials), Depends(verify_same_origin)],
    )
    # Create or migrate the schema once at startup, not on every request.
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
    app.state.allow_insecure_no_auth = allow_insecure_no_auth
    app.state.db_path = str(db_path)
    app.state.template_dir = str(template_dir)
    app.state.custom_settings = settings
    app.state.settings = settings or Settings.load()
    app.state.dispatch_runner = DispatchRunner()
    app.state.login_throttle = LoginThrottle(
        max_failures=app.state.settings.admin_login_max_failures,
        window_seconds=app.state.settings.admin_login_window_seconds,
    )
    app.middleware("http")(add_security_headers)
    # Static assets are public (no credentials needed): they hold no data.
    app.mount(STATIC_PREFIX, StaticFiles(directory=PACKAGE_STATIC_DIR), name="static")
    app.include_router(router)
    return app
