"""Broken task templates must fail the task, never send an empty or incomplete email."""

from pathlib import Path

import jinja2
import pytest

from mail_cadence.mailer import AsyncMailer, EmptyBodyError, TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    MailTask,
    Recipients,
    Settings,
    TaskExecutionStatus,
)


def _task(template: str, fmt: str = "html", **variables: str) -> MailTask:
    return MailTask(
        id="T1",
        schedule_ref="S1",
        sender="noreply@example.com",
        recipients=Recipients(to=["to@example.com"]),
        subject="Subject",
        body_template=BodyTemplate(format=fmt, template_path=template),
        context_variables=[ContextVar(name=k, value=v) for k, v in variables.items()],
    )


@pytest.mark.parametrize(
    ("content", "fmt"),
    [
        ("<p>Hello {{ nme }}</p>", "html"),  # misspelled variable
        ("", "html"),  # empty file
        ("   \n  ", "text"),  # whitespace only
        ("<p></p><div>  </div>", "html"),  # markup without visible text
        ("<p>{{ name </p>", "html"),  # syntax error
    ],
)
@pytest.mark.asyncio
async def test_broken_template_fails_task_without_sending(
    tmp_path: Path, content: str, fmt: str
) -> None:
    (tmp_path / "t.j2").write_text(content, encoding="utf-8")
    mailer = AsyncMailer(Settings(), renderer=TemplateRenderer(tmp_path), dry_run=True)
    sent: list[object] = []

    async def fake_send(**kwargs: object) -> tuple[bool, str | None, dict[str, object]]:
        sent.append(kwargs)
        return True, None, {}

    mailer.send_single_message = fake_send  # type: ignore[method-assign,assignment]
    result = await mailer.dispatch_task(_task("t.j2", fmt=fmt, name="Ann"))

    assert result.status == TaskExecutionStatus.FAILED
    assert result.error_details is not None
    assert result.error_details.startswith("Template rendering error:")
    assert sent == []


def test_undefined_variable_raises(tmp_path: Path) -> None:
    (tmp_path / "t.html.j2").write_text("<p>{{ nme }}</p>", encoding="utf-8")
    with pytest.raises(jinja2.UndefinedError):
        TemplateRenderer(tmp_path).render_task_body(_task("t.html.j2", name="Ann"))


def test_empty_body_raises(tmp_path: Path) -> None:
    (tmp_path / "t.html.j2").write_text("<p>{{ name }}</p>", encoding="utf-8")
    with pytest.raises(EmptyBodyError, match="rendered an empty body"):
        TemplateRenderer(tmp_path).render_task_body(_task("t.html.j2", name=""))


def test_optional_variable_with_default_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "t.html.j2").write_text(
        "<p>Hi {{ title | default('there') }}{% if extra is defined %} {{ extra }}{% endif %}</p>",
        encoding="utf-8",
    )
    body = TemplateRenderer(tmp_path).render_task_body(_task("t.html.j2"))
    assert body == "<p>Hi there</p>"


def test_builtin_daily_dispatch_renders_without_optional_vars() -> None:
    body = TemplateRenderer().render_task_body(_task("builtin:daily_dispatch.html.j2"))
    assert "Valued Customer" in body
