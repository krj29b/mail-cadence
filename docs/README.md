# Documentation

| Document | For | Contents |
| --- | --- | --- |
| [CONFIGURATION.md](CONFIGURATION.md) | Everyone | Environment variables, database upgrades, rules for tasks, schedules and templates |
| [CLI.md](CLI.md) | Everyone | Every `mail-cadence` command and option, the web panel (`serve`) and its security behaviour |
| [OPERATIONS.md](OPERATIONS.md) | Operators | Exit codes, monitoring with `mail-cadence status`, delivery guarantees, status report retries, retention, reviewing unknown deliveries |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Administrators | Production setup: systemd units, dispatch timer, nginx with HTTPS, fail2ban, backups and upgrades |
| [PYTHON_API.md](PYTHON_API.md) | Integrators | Updating tasks and running a dispatch from Python |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Developers | Modules, database schema and ERD, XML mapping, validation rules, scheduling and dispatch internals |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Contributors | Development install, quality gates, CI and releases |
| [adr/0001-execution-state-model.md](adr/0001-execution-state-model.md) | Developers | Design of the durable execution state: claims, attempts, unknown outcomes |
| [adr/0001-validation-report.md](adr/0001-validation-report.md) | Developers | Evidence that the execution state model works as designed |

The engineering rules for changes are in [AGENTS.md](../AGENTS.md).
