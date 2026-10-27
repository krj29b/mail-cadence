"""Run one dispatch batch in a separate process (multiprocessing, spawn start method).

Spawn starts the base interpreter directly, so ``Process.kill()`` ends the process
that holds the database and the SMTP connection. ``subprocess`` with a venv
``python.exe`` on Windows would only kill the launcher and leave the worker running.
"""

import asyncio
import multiprocessing
import sys
from datetime import datetime
from multiprocessing.process import BaseProcess
from pathlib import Path

from mail_cadence.models import Settings
from mail_cadence.pipeline import DispatchOptions, run_dispatch

PROCESS_TIMEOUT = 60.0


def run_batch(db_path: str, template_dir: str, port: int, ref_dt: str) -> None:
    settings = Settings(_env_file=None, smtp_host="127.0.0.1", smtp_port=port)  # type: ignore[call-arg]
    options = DispatchOptions(
        db_path=db_path, template_dir=template_dir, ref_dt=datetime.fromisoformat(ref_dt)
    )
    sys.exit(asyncio.run(run_dispatch(options, settings)))


def spawn_batch(db_path: Path, port: int, ref_dt: datetime) -> BaseProcess:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=run_batch,
        args=(str(db_path), str(db_path.parent), port, ref_dt.isoformat()),
        daemon=True,
    )
    proc.start()
    return proc


def stop(proc: BaseProcess) -> None:
    """Kill the process if it still runs, then reap it."""
    if proc.is_alive():
        proc.kill()
    proc.join(PROCESS_TIMEOUT)
    proc.close()
