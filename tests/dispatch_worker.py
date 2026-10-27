"""Work run in separate processes (multiprocessing, spawn start method).

Spawn starts the base interpreter directly, so ``Process.kill()`` ends the process
that holds the database and the SMTP connection. ``subprocess`` with a venv
``python.exe`` on Windows would only kill the launcher and leave the worker running.
"""

import asyncio
import json
import multiprocessing
import sys
from contextlib import closing
from datetime import datetime
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Barrier
from pathlib import Path

from mail_cadence.database import get_connection
from mail_cadence.execution_store import ClaimRequest
from mail_cadence.models import Settings
from mail_cadence.pipeline import DispatchOptions, run_dispatch
from mail_cadence.repository import DatabaseRepository

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


def claim_tasks(
    db_path: str, owner: str, task_ids: list[str], barrier: Barrier, out_path: str
) -> None:
    """Claim every task once, right after all workers pass the barrier; write the statuses."""
    with closing(get_connection(db_path)) as conn:
        repo = DatabaseRepository(conn)
        barrier.wait(PROCESS_TIMEOUT)
        statuses = {
            task_id: repo.claim_execution(
                ClaimRequest(task_id=task_id, window_key="W", run_id="run-1", owner=owner)
            ).status.value
            for task_id in task_ids
        }
    Path(out_path).write_text(json.dumps(statuses), encoding="utf-8")


def spawn_claimers(db_path: Path, task_orders: list[list[str]], out_dir: Path) -> list[BaseProcess]:
    """One process per task order; all start claiming at the same moment."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(task_orders))
    procs: list[BaseProcess] = [
        ctx.Process(
            target=claim_tasks,
            args=(str(db_path), f"worker-{n}", order, barrier, str(out_dir / f"worker-{n}.json")),
            daemon=True,
        )
        for n, order in enumerate(task_orders)
    ]
    for proc in procs:
        proc.start()
    return procs
