# Recipes must be indented with tabs.
.PHONY: install test lint dead-code complexity audit quality-gate smoke-test assets workflows run-dry clean

PYTHON ?= python
CODE_DIRS = src tests scripts
COV_MIN ?= 90

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest --cov=mail_cadence --cov-report=term-missing --cov-fail-under=$(COV_MIN)

lint:
	$(PYTHON) -m ruff check $(CODE_DIRS)
	$(PYTHON) -m ruff format --check $(CODE_DIRS)
	$(PYTHON) -m mypy $(CODE_DIRS)

dead-code:
	$(PYTHON) -m vulture src

# radon only reports; xenon fails the build when anything is worse than B.
complexity:
	$(PYTHON) -m radon cc src -a -nb --total-average
	$(PYTHON) -m xenon --max-absolute B --max-modules B --max-average B src

audit:
	$(PYTHON) -m pip_audit --skip-editable

quality-gate: lint dead-code complexity audit test

# Build the distributions and smoke-test them in throwaway virtual environments
# (Linux/macOS paths; CI runs the same script on Windows too).
smoke-test:
	rm -rf dist
	$(PYTHON) -m build
	set -e; \
	VERSION=$$($(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'); \
	SMOKE_DIR="$${TMPDIR:-/tmp}/mail-cadence-smoke"; \
	rm -rf "$$SMOKE_DIR"; \
	mkdir -p "$$SMOKE_DIR"; \
	for dist in dist/*.whl dist/*.tar.gz; do \
		venv_dir="$$SMOKE_DIR/venv-$$(basename "$$dist")"; \
		$(PYTHON) -m venv "$$venv_dir"; \
		"$$venv_dir/bin/python" -m pip install -q "$$dist" httpx; \
		(cd "$$SMOKE_DIR" && "$$venv_dir/bin/python" -I "$(CURDIR)/scripts/smoke_test_wheel.py" --repo "$(CURDIR)" --expect-version "$$VERSION"); \
	done; \
	rm -rf "$$SMOKE_DIR"

# Rebuild the web panel assets in src/mail_cadence/web_static (needs Node.js; the result
# is committed, so installing or packaging mail-cadence never needs Node).
assets:
	cd frontend && npm ci --ignore-scripts --no-audit --no-fund && npm run build

# Lint and security-audit the GitHub Actions workflows (same versions as CI; needs uv).
workflows:
	uvx --from actionlint-py actionlint .github/workflows/*.yml
	uvx zizmor@1.31.1 --offline .github/workflows

run-dry:
	$(PYTHON) -m mail_cadence.cli \
		--tasks-xml config/samples/tasks.xml \
		--schedule-xml config/samples/schedules.xml \
		--dry-run

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage dist build *.egg-info
