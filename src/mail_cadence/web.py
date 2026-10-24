import asyncio
import json
import secrets
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Any

import jinja2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from mail_cadence.cli import run_dispatch_pipeline
from mail_cadence.database import get_connection, init_db
from mail_cadence.mailer import AsyncMailer, TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    HourWindow,
    MailTask,
    Recipients,
    ReportSettings,
    Schedule,
    Settings,
    TaskExecutionStatus,
    WorkSchedules,
)
from mail_cadence.repository import DatabaseRepository
from mail_cadence.scheduler import TaskScheduler, get_active_window

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

    if admin_user and admin_password:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(
            credentials.username.encode("utf-8"),
            admin_user.encode("utf-8"),
        )
        pass_ok = secrets.compare_digest(
            credentials.password.encode("utf-8"),
            admin_password.encode("utf-8"),
        )
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )


def _discover_available_templates(template_dir: str | Path) -> list[str]:
    base_path = Path(template_dir)
    if not base_path.exists() or not base_path.is_dir():
        return []
    templates_found: list[str] = []
    valid_extensions = {".j2", ".html", ".htm", ".txt"}
    for path in base_path.rglob("*"):
        if path.is_file() and path.suffix.lower() in valid_extensions:
            rel_parts = path.relative_to(base_path).parts
            if any(
                p.startswith(".") or p in ("__pycache__", "node_modules", "venv") for p in rel_parts
            ):
                continue
            templates_found.append(path.relative_to(base_path).as_posix())
    return sorted(templates_found)


def _is_dispatched_in_window(
    scheduler: TaskScheduler,
    window: Any,
    last_run: Any,
    last_status: Any,
    ref_dt: Any,
) -> bool:
    if window is None or last_run is None or last_status != TaskExecutionStatus.SUCCESS:
        return False
    w_start, w_end = scheduler._get_window_bounds(window, ref_dt)
    last_run_tz = scheduler._normalize_dt(last_run)
    return w_start <= last_run_tz <= w_end


def _get_tasks_window_status(
    repo: DatabaseRepository,
    tasks: list[MailTask],
    work_schedules: WorkSchedules,
) -> dict[str, dict[str, Any]]:
    scheduler = TaskScheduler(work_schedules)
    states = repo.get_task_execution_states()
    schedule_map = {s.id: s for s in work_schedules.schedules}
    ref_dt = scheduler._normalize_dt(None)
    statuses: dict[str, dict[str, Any]] = {}

    for task in tasks:
        last_run, last_status = states.get(task.id, (None, None))
        schedule = schedule_map.get(task.schedule_ref)
        window = get_active_window(schedule, ref_dt) if schedule else None
        eligible, _ = scheduler.is_task_eligible(
            task, reference_dt=ref_dt, last_run=last_run, last_status=last_status
        )

        dispatched_in_window = _is_dispatched_in_window(
            scheduler, window, last_run, last_status, ref_dt
        )
        status_key = (
            "dispatched_in_window"
            if dispatched_in_window
            else ("eligible_now" if window is not None else "waiting_window")
        )

        statuses[task.id] = {
            "status_key": status_key,
            "has_active_window": window is not None,
            "dispatched_in_window": dispatched_in_window,
            "eligible_now": eligible,
            "last_status": last_status.value if last_status else "NEVER_RUN",
            "last_executed_at": last_run.isoformat() if last_run else None,
        }

    return statuses


PACKAGE_TEMPLATES_DIR = Path(__file__).parent / "web_templates"
templates = Jinja2Templates(directory=str(PACKAGE_TEMPLATES_DIR))
router = APIRouter()


def get_repo(request: Request) -> Generator[DatabaseRepository, None, None]:
    db_path = getattr(request.app.state, "db_path", "mail_cadence.db")
    with closing(get_connection(db_path)) as conn:
        init_db(conn)
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
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    stats = repo.get_dashboard_stats()
    logs = repo.get_recent_execution_logs(limit=50)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"active_page": "dashboard", "stats": stats, "logs": logs},
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
    tasks = repo.get_mail_tasks()
    work_schedules = repo.get_work_schedules()
    schedules = work_schedules.schedules
    default_sender = _get_settings(request).smtp_user or ""
    template_dir = getattr(request.app.state, "template_dir", ".")
    available_templates = _discover_available_templates(template_dir)
    task_statuses = _get_tasks_window_status(repo, tasks, work_schedules)

    edit_task: MailTask | None = None
    if edit:
        edit_task = repo.get_mail_task(edit)

    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            "active_page": "tasks",
            "tasks": tasks,
            "schedules": schedules,
            "default_sender": default_sender,
            "available_templates": available_templates,
            "task_statuses": task_statuses,
            "edit_task": edit_task,
        },
    )


def _build_task_from_form_dict(form_dict: dict[str, Any], default_sender: str = "") -> MailTask:
    task_id = str(form_dict.get("id", ""))
    schedule_ref = str(form_dict.get("schedule_ref", ""))
    sender_raw = str(form_dict.get("sender", ""))
    sender = sender_raw if sender_raw.strip() else default_sender
    priority = str(form_dict.get("priority", "normal"))
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
        repo.save_single_task(task)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation Error: {exc}",
        ) from exc

    tasks = repo.get_mail_tasks()
    work_schedules = repo.get_work_schedules()
    schedules = work_schedules.schedules
    template_dir = getattr(request.app.state, "template_dir", ".")
    available_templates = _discover_available_templates(template_dir)
    task_statuses = _get_tasks_window_status(repo, tasks, work_schedules)

    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            "active_page": "tasks",
            "tasks": tasks,
            "schedules": schedules,
            "default_sender": default_sender,
            "available_templates": available_templates,
            "task_statuses": task_statuses,
            "message": f"Task '{task.id}' saved successfully.",
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
        rendered = renderer.render(
            task.body_template.template_path,
            task.get_context_dict(),
        )
    except (ValueError, jinja2.TemplateNotFound, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template error: {exc}",
        ) from exc

    if task.body_template.format.lower() == "html":
        return HTMLResponse(content=rendered)
    return Response(content=rendered, media_type="text/plain")


@router.get("/schedules", response_class=HTMLResponse)
def list_schedules(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    work_schedules = repo.get_work_schedules()
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={"active_page": "schedules", "work_schedules": work_schedules},
    )


@router.post("/schedules", response_class=HTMLResponse)
async def save_schedule(
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    form = await request.form()
    schedule_id = str(form.get("id", ""))
    desc = form.get("description")
    description = str(desc) if desc is not None else None
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

    work_schedules = repo.get_work_schedules()
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            "active_page": "schedules",
            "work_schedules": work_schedules,
            "message": f"Schedule '{schedule_id}' saved successfully.",
        },
    )


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

    work_schedules = repo.get_work_schedules()
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            "active_page": "schedules",
            "work_schedules": work_schedules,
            "message": f"Timezone updated to '{tz_val}'.",
        },
    )


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
    report_settings = repo.get_report_settings()
    smtp_settings = _get_settings(request)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": report_settings,
            "smtp_settings": smtp_settings,
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

    report_settings = repo.get_report_settings()
    smtp_settings = _get_settings(request)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": report_settings,
            "smtp_settings": smtp_settings,
            "message": "Report settings updated successfully.",
        },
    )


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
    repo: DatabaseRepository = Depends(get_repo),
) -> list[dict[str, Any]]:
    return repo.get_recent_execution_logs(
        limit=limit,
        status_filter=status_filter,
        task_id_filter=task_id_filter,
    )


@router.post("/dispatch/run", response_class=HTMLResponse)
async def trigger_dispatch_run(
    request: Request,
) -> Response:
    lock: asyncio.Lock = request.app.state.dispatch_lock
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A dispatch batch is already running.",
        )

    form = await request.form()
    dry_run_raw = form.get("dry_run", request.query_params.get("dry_run", "false"))
    dry_run = str(dry_run_raw).lower() in ("true", "1", "on", "yes")

    async with lock:
        db_path = getattr(request.app.state, "db_path", "mail_cadence.db")
        await run_dispatch_pipeline(db_path=db_path, dry_run=dry_run)

        with closing(get_connection(db_path)) as conn:
            repo = DatabaseRepository(conn)
            stats = repo.get_dashboard_stats()
            logs = repo.get_recent_execution_logs(limit=50)

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "active_page": "dashboard",
                "stats": stats,
                "logs": logs,
                "message": f"Manual dispatch completed (dry_run={dry_run}).",
            },
        )


@router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
async def run_single_task(
    task_id: str,
    request: Request,
    repo: DatabaseRepository = Depends(get_repo),
) -> Response:
    lock: asyncio.Lock = request.app.state.dispatch_lock
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A dispatch batch is already running.",
        )

    task = repo.get_mail_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )

    async with lock:
        settings = _get_settings(request)
        template_dir = getattr(request.app.state, "template_dir", ".")
        renderer = TemplateRenderer(template_dir=template_dir)
        mailer = AsyncMailer(settings=settings, renderer=renderer)

        async with mailer:
            res = await mailer.dispatch_task(task)

        repo.log_task_execution(res)

        badge_class = (
            "badge-success"
            if res.status == TaskExecutionStatus.SUCCESS
            else "badge-failed"
            if res.status == TaskExecutionStatus.FAILED
            else "badge-partial"
        )

        content = (
            f'<div class="alert-success">'
            f"Task '<strong>{task_id}</strong>' executed: "
            f'<span class="{badge_class}">{res.status.value}</span> '
            f"(Sent: {res.sent_count})"
            f"</div>"
        )
        return HTMLResponse(content=content)


def create_app(
    db_path: str | Path = "mail_cadence.db",
    template_dir: str | Path = ".",
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI(
        title="MailCadence Admin Panel",
        description="Lightweight web administration and observability panel",
        version="0.2.0",
        dependencies=[Depends(verify_credentials)],
    )
    app.state.db_path = str(db_path)
    app.state.template_dir = str(template_dir)
    app.state.custom_settings = settings
    app.state.settings = settings or Settings.load()
    app.state.dispatch_lock = asyncio.Lock()
    app.include_router(router)
    return app
