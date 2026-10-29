"""Smoke test for an installed mail-cadence distribution (wheel or sdist).

Run it with the interpreter of a clean virtual environment where only the built package
(plus httpx for the web check) is installed, from a directory outside the repository:

    python -I scripts/smoke_test_wheel.py --repo . [--expect-version 1.2.3]

It checks that:
- the package is imported from site-packages, not from the source tree;
- every file under src/mail_cadence is present in the installed package (catches files
  missing from the wheel's artifact list);
- the console script works: --version, import-xml and a dry-run dispatch of the samples;
- the status command reads the database (no real run yet, so CRITICAL, exit code 2);
- every sample task renders with its (built-in) template;
- the web panel starts and serves its pages.
"""

import argparse
import json
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import closing
from pathlib import Path

SKIP_PARTS = {"__pycache__"}


def _fail(message: str) -> None:
    print(f"SMOKE TEST FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _run_cli(*args: str, expected_code: int = 0) -> str:
    exe = Path(sysconfig.get_path("scripts")) / "mail-cadence"
    command = [str(exe), *args]
    print("$", " ".join(command))
    proc = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != expected_code:
        _fail(f"{' '.join(args)} exited with {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout + proc.stderr


def check_installed_location(repo: Path) -> Path:
    import mail_cadence

    package_dir = Path(mail_cadence.__file__).resolve().parent
    if package_dir.is_relative_to(repo.resolve()):
        _fail(f"mail_cadence was imported from the source tree: {package_dir}")
    print(f"OK  installed package: {package_dir}")
    return package_dir


def check_packaged_files(repo: Path, package_dir: Path) -> None:
    source_dir = repo / "src" / "mail_cadence"
    expected = {
        path.relative_to(source_dir)
        for path in source_dir.rglob("*")
        if path.is_file() and not SKIP_PARTS.intersection(path.parts) and path.suffix != ".pyc"
    }
    missing = sorted(str(p) for p in expected if not (package_dir / p).is_file())
    if missing:
        _fail("files missing from the installed package:\n  " + "\n  ".join(missing))
    print(f"OK  all {len(expected)} source files are packaged")


def check_version(expected: str | None) -> None:
    output = _run_cli("--version")
    if expected and expected not in output:
        _fail(f"--version printed {output.strip()!r}, expected {expected!r}")
    print(f"OK  {output.strip()}")


def check_samples(repo: Path, workdir: Path) -> Path:
    samples = repo / "config" / "samples"
    db = workdir / "smoke.db"
    tasks_xml, schedules_xml = str(samples / "tasks.xml"), str(samples / "schedules.xml")
    _run_cli("import-xml", "-t", tasks_xml, "-s", schedules_xml, "--db", str(db))
    _run_cli("--db", str(db), "--dry-run")
    print("OK  import-xml and dry-run dispatch of the samples")

    from mail_cadence.database import get_connection
    from mail_cadence.mailer import TemplateRenderer
    from mail_cadence.repository import DatabaseRepository

    with closing(get_connection(db)) as conn:
        tasks = DatabaseRepository(conn).get_mail_tasks()
    if not tasks:
        _fail("no tasks were imported from the samples")
    renderer = TemplateRenderer(template_dir=samples)
    for task in tasks:
        renderer.render_task_body(task)
    print(f"OK  {len(tasks)} sample task templates render")
    return db


def check_status(db: Path) -> None:
    output = _run_cli("status", "--db", str(db), "--json", expected_code=2)
    result = json.loads(output)
    if result["status"] != "CRITICAL" or result["findings"][0]["check"] != "last_run":
        _fail(f"unexpected status output: {output}")
    print("OK  status reads the database (CRITICAL: no dispatch run yet)")


def check_web_panel(db: Path) -> None:
    from fastapi.testclient import TestClient

    from mail_cadence.models import Settings
    from mail_cadence.web import create_app

    app = create_app(db_path=db, settings=Settings(), allow_insecure_no_auth=True)
    client = TestClient(app)
    for page in ("/", "/tasks", "/schedules", "/settings"):
        response = client.get(page)
        if response.status_code != 200:  # noqa: PLR2004
            _fail(f"GET {page} returned {response.status_code}")
    print("OK  web panel pages render")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="repository root")
    parser.add_argument("--expect-version", help="version that --version must print")
    args = parser.parse_args()
    repo: Path = args.repo.resolve()

    package_dir = check_installed_location(repo)
    check_packaged_files(repo, package_dir)
    check_version(args.expect_version)
    with tempfile.TemporaryDirectory() as tmp:
        db = check_samples(repo, Path(tmp))
        check_status(db)
        check_web_panel(db)
    print("Smoke test passed.")


if __name__ == "__main__":
    main()
