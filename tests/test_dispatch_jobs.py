import asyncio
import threading

import pytest

from mail_cadence.dispatch_jobs import DispatchBusyError, DispatchJob, DispatchRunner


def test_only_one_of_many_concurrent_starts_wins() -> None:
    runner = DispatchRunner()
    release = threading.Event()
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    async def blocking_work(_job: DispatchJob) -> None:
        await asyncio.to_thread(release.wait, 30)

    def try_start() -> None:
        barrier.wait()
        try:
            runner.start(DispatchJob(kind="batch"), blocking_work)
            outcome = "started"
        except DispatchBusyError:
            outcome = "busy"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=try_start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) == ["busy"] * 7 + ["started"]
    assert runner.is_running()

    release.set()
    job = runner.wait(timeout=30)
    assert job is not None
    assert job.state == "finished"
    assert job.finished_at is not None
    assert not runner.is_running()


def test_runner_accepts_new_job_after_previous_finished() -> None:
    runner = DispatchRunner()

    async def set_exit_code(job: DispatchJob) -> None:
        job.exit_code = 0

    first = runner.start(DispatchJob(kind="batch"), set_exit_code)
    runner.wait(timeout=30)
    second = runner.start(DispatchJob(kind="batch", dry_run=True), set_exit_code)
    runner.wait(timeout=30)

    assert first.job_id != second.job_id
    assert runner.current is second
    assert second.to_dict()["exit_code"] == 0
    assert second.to_dict()["dry_run"] is True


def test_crashing_job_is_reported_as_error() -> None:
    runner = DispatchRunner()

    async def crash(_job: DispatchJob) -> None:
        raise RuntimeError("smtp exploded")

    runner.start(DispatchJob(kind="task", task_id="T1"), crash)
    job = runner.wait(timeout=30)

    assert job is not None
    assert job.state == "error"
    assert job.error == "RuntimeError: smtp exploded"
    assert not runner.is_running()


def test_wait_without_job_returns_none() -> None:
    assert DispatchRunner().wait(timeout=1) is None


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_job_to_dict_is_json_ready(kind: str) -> None:
    job = DispatchJob(kind=kind)  # type: ignore[arg-type]
    data = job.to_dict()
    assert data["state"] == "running"
    assert data["finished_at"] is None
    assert isinstance(data["started_at"], str)
