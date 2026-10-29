"""Run the race and failure tests of ADR 0001 many times (plan 7.4: at least 50 runs).

Every iteration is a fresh pytest process over the tests that use several processes,
threads or held SMTP sessions. All iterations run even after a failure, so every
conflict leaves a trace: the output of each failed iteration is printed, and a summary
goes to stdout and, on GitHub Actions, to the job summary.

    python scripts/stress_races.py --iterations 10 --label "windows shard 1"

Exits 1 if any iteration failed.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RACE_TESTS = (
    "tests/test_delivery_guarantees.py::test_t01_concurrent_processes_send_once",
    "tests/test_delivery_guarantees.py::test_t02_cli_and_two_panels_send_once",
    "tests/test_delivery_guarantees.py::test_t03_restart_never_resends_to_accepted_recipient",
    "tests/test_delivery_guarantees.py::test_t06_old_version_result_does_not_complete_edited_task",
    "tests/test_delivery_guarantees.py::test_t08_recreated_task_is_a_new_identity",
    "tests/test_delivery_guarantees.py::test_t18_configuration_can_be_saved_while_smtp_is_in_progress",
    "tests/test_execution_store.py::test_concurrent_processes_claim_each_task_once",
    "tests/test_failure_matrix.py",
    "tests/test_mailer_attempts.py::test_timeout_after_the_body_is_unknown",
    "tests/test_mailer_attempts.py::test_session_with_a_late_reply_is_not_reused",
    "tests/test_mailer_attempts.py::test_pooled_session_closed_on_idle_timeout_is_replaced",
    "tests/test_dispatch_jobs.py::test_only_one_of_many_concurrent_starts_wins",
    "tests/test_web.py::test_web_dispatch_lock_conflict",
    "tests/test_web.py::test_write_waiting_for_the_lock_does_not_stall_other_requests",
)
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
OUTPUT_TAIL_LINES = 80


@dataclass
class Summary:
    label: str
    iterations: int = 0
    seconds: list[float] = field(default_factory=list)
    failures: dict[int, list[str]] = field(default_factory=dict)

    def markdown(self) -> str:
        lines = [
            f"### Race stress: {self.label}",
            "",
            f"- iterations: {self.iterations}, failed: {len(self.failures)}",
            f"- seconds per iteration: min {min(self.seconds):.1f}, max {max(self.seconds):.1f}",
        ]
        lines += [
            f"- iteration {n}: {', '.join(nodes) or 'pytest error'}"
            for n, nodes in sorted(self.failures.items())
        ]
        return "\n".join(lines) + "\n"


def run_iteration(number: int, summary: Summary) -> None:
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE", *RACE_TESTS]
    started = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    elapsed = time.monotonic() - started
    summary.iterations += 1
    summary.seconds.append(elapsed)
    if proc.returncode == 0:
        print(f"iteration {number}: ok ({elapsed:.1f} s)", flush=True)
        return
    failed = _FAILED_RE.findall(proc.stdout)
    summary.failures[number] = failed
    print(f"iteration {number}: FAILED ({elapsed:.1f} s): {failed}", flush=True)
    tail = (proc.stdout + proc.stderr).splitlines()[-OUTPUT_TAIL_LINES:]
    print("\n".join(f"    {line}" for line in tail), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--label", default=sys.platform)
    args = parser.parse_args()

    summary = Summary(args.label)
    for number in range(1, args.iterations + 1):
        run_iteration(number, summary)

    report = summary.markdown()
    print(report)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
