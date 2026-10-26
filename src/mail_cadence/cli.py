import asyncio
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import ValidationError

from mail_cadence import __version__
from mail_cadence.database import get_connection, init_db
from mail_cadence.models import Settings
from mail_cadence.pipeline import DispatchOptions, run_dispatch
from mail_cadence.xml_loader import (
    XMLValidationError,
    export_to_xml,
    import_from_xml,
)

app = typer.Typer(
    name="mail-cadence",
    help="Asynchronous MailCadence email dispatch and reporting engine",
    invoke_without_command=True,
)


TEMPLATE_DIR_HELP = (
    "Directory that task template paths are resolved against "
    "(built-in templates use the 'builtin:' prefix)"
)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


DEFAULT_DB = "mail_cadence.db"


def _resolve_db_path(db: str, settings: Settings | None = None) -> str:
    """The --db default defers to MAIL_CADENCE_DB from the .env file."""
    if db == DEFAULT_DB:
        return (settings or Settings.load()).mail_cadence_db
    return db


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mail-cadence {__version__}")
        raise typer.Exit(code=0)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def run_dispatch_pipeline(
    tasks_xml: Path | None = None,
    schedule_xml: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    ref_dt: datetime | None = None,
    db_path: str = DEFAULT_DB,
    template_dir: str | Path = ".",
) -> int:
    """CLI-flavoured entry point: sets up logging and loads settings, then runs the pipeline.

    Library code can call ``mail_cadence.pipeline.run_dispatch`` directly.
    """
    setup_logging(verbose)
    settings = Settings.load()
    options = DispatchOptions(
        db_path=_resolve_db_path(db_path, settings),
        template_dir=template_dir,
        dry_run=dry_run,
        ref_dt=ref_dt,
        tasks_xml=tasks_xml,
        schedule_xml=schedule_xml,
    )
    return await run_dispatch(options, settings)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
    tasks_xml: Annotated[
        Path | None,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Path to tasks configuration XML file to seed database",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    schedule_xml: Annotated[
        Path | None,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Path to hourly work schedules XML file to seed database",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = DEFAULT_DB,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-d",
            help="Execute without sending real network SMTP requests",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose debug logging",
        ),
    ] = False,
    template_dir: Annotated[
        str,
        typer.Option(
            "--template-dir",
            envvar="MAIL_CADENCE_TEMPLATE_DIR",
            help=TEMPLATE_DIR_HELP,
        ),
    ] = ".",
) -> None:
    if ctx.invoked_subcommand is None:
        exit_code = asyncio.run(
            run_dispatch_pipeline(
                tasks_xml=tasks_xml,
                schedule_xml=schedule_xml,
                dry_run=dry_run,
                verbose=verbose,
                db_path=db,
                template_dir=template_dir,
            )
        )
        raise typer.Exit(code=exit_code)


@app.command("import-xml")
def cli_import_xml(
    tasks_xml: Annotated[
        Path,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Path to tasks configuration XML file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    schedule_xml: Annotated[
        Path,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Path to hourly work schedules XML file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = DEFAULT_DB,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            "-m",
            help="Import mode: 'upsert' (update existing) or 'replace' (wipe and replace)",
        ),
    ] = "upsert",
) -> None:
    """Import tasks and schedules XML into the SQLite database."""
    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)
        import_mode: Literal["upsert", "replace"] = (
            "replace" if mode.lower() == "replace" else "upsert"
        )

        try:
            import_from_xml(conn, tasks_xml, schedule_xml, mode=import_mode)
            typer.echo(
                f"Successfully imported {tasks_xml} and {schedule_xml} into "
                f"{resolved_db} ({import_mode})."
            )
        except XMLValidationError as exc:
            typer.echo(f"XML Validation Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except (sqlite3.Error, ValidationError) as exc:
            typer.echo(f"Error importing XML: {exc}", err=True)
            raise typer.Exit(code=1) from exc


@app.command("export-xml")
def cli_export_xml(
    tasks_xml: Annotated[
        Path,
        typer.Option(
            "--tasks-xml",
            "-t",
            help="Target path for tasks configuration XML file",
        ),
    ],
    schedule_xml: Annotated[
        Path,
        typer.Option(
            "--schedule-xml",
            "-s",
            help="Target path for hourly work schedules XML file",
        ),
    ],
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = DEFAULT_DB,
) -> None:
    """Export current SQLite database configuration to XML files."""
    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)

        try:
            export_to_xml(conn, tasks_xml, schedule_xml)
            typer.echo(
                f"Successfully exported database {resolved_db} to {tasks_xml} and {schedule_xml}."
            )
        except (sqlite3.Error, OSError, ValidationError) as exc:
            typer.echo(f"Error exporting XML: {exc}", err=True)
            raise typer.Exit(code=1) from exc


@app.command("serve")
def cli_serve(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            "-h",
            help="Host address to bind web server",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port to bind web server",
        ),
    ] = 8000,
    db: Annotated[
        str,
        typer.Option(
            "--db",
            "-b",
            envvar="MAIL_CADENCE_DB",
            help="Path to SQLite database file",
        ),
    ] = DEFAULT_DB,
    template_dir: Annotated[
        str,
        typer.Option(
            "--template-dir",
            "-t",
            envvar="MAIL_CADENCE_TEMPLATE_DIR",
            help=TEMPLATE_DIR_HELP,
        ),
    ] = ".",
    insecure_no_auth: Annotated[
        bool,
        typer.Option(
            "--insecure-no-auth",
            help="Allow access without ADMIN_USER/ADMIN_PASSWORD (only with a loopback --host)",
        ),
    ] = False,
) -> None:
    """Launch the web administration and observability panel."""
    import uvicorn

    from mail_cadence.web import create_app

    if insecure_no_auth and host not in _LOOPBACK_HOSTS:
        typer.echo(
            "--insecure-no-auth is only allowed with --host 127.0.0.1, ::1 or localhost.",
            err=True,
        )
        raise typer.Exit(code=1)

    setup_logging()
    resolved_db = _resolve_db_path(db)
    with closing(get_connection(resolved_db)) as conn:
        init_db(conn)

    web_app = create_app(
        db_path=resolved_db,
        template_dir=template_dir,
        allow_insecure_no_auth=insecure_no_auth,
    )
    uvicorn.run(web_app, host=host, port=port)


if __name__ == "__main__":
    app()
