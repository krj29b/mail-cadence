## Summary

<!-- Briefly describe what this PR changes and why. -->

Fixes #<!-- issue number, if applicable -->

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation / CI / Refactoring

## Quality Gates Checklist

- [ ] `pytest` passes with >= 90% coverage (`make test`)
- [ ] `mypy src tests scripts` passes with zero errors (`make lint`)
- [ ] `ruff check src tests scripts` and `ruff format --check src tests scripts` pass (`make lint`)
- [ ] `vulture src` reports no dead code (`make dead-code`)
- [ ] `xenon --max-absolute B --max-modules B --max-average B src` passes (`make complexity`)
- [ ] If SQLite schema or model constraints changed: updated `SCHEMA_SQL` and appended an idempotent migration to `database.MIGRATIONS`
- [ ] If web templates, icons, or `frontend/` changed: ran `make assets` and committed `src/mail_cadence/web_static/`
