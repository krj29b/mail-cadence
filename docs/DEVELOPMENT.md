# Development and Quality Gates

Clone the repository and install `mail-cadence` in editable mode with the development dependencies:

```bash
python3 -m pip install -e ".[dev]"
# or
make install
```

The engineering rules every change must follow are in [AGENTS.md](../AGENTS.md).

## Make Targets

```bash
# Run unit and integration test suite with coverage
make test

# Run code style and linting checks (Ruff & Mypy)
make lint

# Run dead code detection (Vulture)
make dead-code

# Run code complexity analysis (Radon)
make complexity

# Run dependency vulnerability audit (pip-audit)
make audit

# Run all quality gate checks in sequence
make quality-gate

# Build the wheel and smoke-test it in a clean virtual environment
make smoke-test

# Rebuild the web panel CSS/JS/fonts after changing templates (needs Node.js)
make assets
```

The web panel's browser assets in `src/mail_cadence/web_static/` are committed, so installing or packaging `mail-cadence` never needs Node.js. After changing Tailwind classes in templates, icons (`<i data-lucide="...">`) or the versions in `frontend/package.json`, run `make assets` and commit the result; CI checks that the committed files match a fresh build.

## Continuous Integration and Releases

CI (`.github/workflows/ci.yml`) runs the same checks on every push and pull request, plus tests on Linux and Windows with Python 3.12–3.14, a test run with the lowest dependency versions allowed by `pyproject.toml`, and the smoke test against the built wheel and sdist. Releases (`.github/workflows/publish.yml`) run the quality gates again, build with hash-pinned tools (`.github/requirements/release.txt`) and no caches, smoke-test, and publish to PyPI only when the release tag matches the `pyproject.toml` version; pre-releases go to TestPyPI. Workflows are checked with actionlint and [zizmor](https://docs.zizmor.sh) (`make workflows`); all actions are pinned to commit SHAs.

The race and failure tests of ADR 0001 run 50 times per OS in the weekly `race-stress` job (and on demand through `workflow_dispatch`), via `scripts/stress_races.py`.

## Acceptance Tests for the Review Fixes

The tests in `../tests/test_required_fixes.py` (next to the review document, outside this repo) check the F2–F6 fixes. Run them from the repo root with this repo's virtual environment. They need `src` on `PYTHONPATH` and the repo path in `MAIL_CADENCE_REPO`:

```bash
MAIL_CADENCE_REPO="$PWD" PYTHONPATH=src .venv/Scripts/python -m pytest -q -p no:cacheprovider     --rootdir .. ../tests
```

On Linux/macOS use `.venv/bin/python`. In PowerShell set the variables first: `$env:MAIL_CADENCE_REPO = (Get-Location).Path; $env:PYTHONPATH = "src"`.
