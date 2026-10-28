"""Broken task templates must fail the task, never send an empty or incomplete email."""

from pathlib import Path

import jinja2
import pytest

from mail_cadence.mailer import RENDER_ERRORS, AsyncMailer, EmptyBodyError, TemplateRenderer
from mail_cadence.models import (
    BodyTemplate,
    ContextVar,
    MailTask,
    Recipients,
    Settings,
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
def test_broken_template_raises_a_render_error(tmp_path: Path, content: str, fmt: str) -> None:
    """Real runs abort the execution and dry runs fail the task on these errors."""
    (tmp_path / "t.j2").write_text(content, encoding="utf-8")
    mailer = AsyncMailer(Settings(), renderer=TemplateRenderer(tmp_path))
    with pytest.raises(RENDER_ERRORS):
        mailer.render_task_message(_task("t.j2", fmt=fmt, name="Ann"))


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
