"""Background dispatch jobs for the web panel.

Each job runs in its own worker thread with its own event loop and SQLite connection,
so a long batch never blocks the web server or hits HTTP/proxy timeouts. Only one job
runs at a time: ``DispatchRunner.start`` checks and claims the slot atomically.

The runner is per process. With several server processes (e.g. uvicorn --workers N)
each process has its own slot; run the panel with a single worker.
"""

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from mail_cadence.models import TaskExecutionResult

logger = logging.getLogger(__name__)

JobKind = Literal["batch", "task"]
JobState = Literal["running", "finished", "error"]
JobWork = Callable[["DispatchJob"], Coroutine[Any, Any, None]]


class DispatchBusyError(RuntimeError):
    """Raised when a dispatch job is started while another one is running."""


@dataclass
class DispatchJob:
    kind: JobKind
    dry_run: bool = False
    task_id: str | None = None
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    exit_code: int | None = None
    result: TaskExecutionResult | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "dry_run": self.dry_run,
            "task_id": self.task_id,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exit_code": self.exit_code,
            "result": self.result.model_dump(mode="json") if self.result else None,
            "error": self.error,
        }


class DispatchRunner:
    """Runs at most one dispatch job at a time in a worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: DispatchJob | None = None

    @property
    def current(self) -> DispatchJob | None:
        """The running job, or the last finished one."""
        return self._current

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running_locked()

    def _is_running_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job: DispatchJob, work: JobWork) -> DispatchJob:
        """Start ``work(job)`` in a worker thread; raise DispatchBusyError if one is running."""
        with self._lock:
            if self._is_running_locked():
                raise DispatchBusyError("A dispatch batch is already running.")
            self._current = job
            # Non-daemon: on shutdown the interpreter waits for the batch to finish.
            self._thread = threading.Thread(
                target=self._run, args=(job, work), name=f"dispatch-{job.job_id}"
            )
            self._thread.start()
        return job

    def wait(self, timeout: float | None = None) -> DispatchJob | None:
        """Block until the current job finishes (mainly for tests and shutdown)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._current

    @staticmethod
    def _run(job: DispatchJob, work: JobWork) -> None:
        try:
            asyncio.run(work(job))
            final_state: JobState = "finished"
        except Exception as exc:  # any crash must end up in the job status
            logger.exception(f"Dispatch job {job.job_id} crashed")
            job.error = f"{type(exc).__name__}: {exc}"
            final_state = "error"
        job.finished_at = datetime.now(UTC)
        job.state = final_state
