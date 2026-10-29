# CLI Reference (`mail-cadence`)

The package provides the `mail-cadence` CLI executable (as well as `python -m mail_cadence.cli`). `mail-cadence --version` (`-V`) prints the installed version.

## 1. Main Dispatch Command

Executes the main bulk mailer dispatch pipeline.

```bash
mail-cadence [OPTIONS]
```

### Flags & Options

| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Optional path to tasks XML file to seed/upsert database |
| `--schedule-xml` | `-s` | `PATH` | *None* | Optional path to schedule XML file to seed/upsert database |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Path to SQLite database file (overrides `MAIL_CADENCE_DB`) |
| `--dry-run` | `-d` | `FLAG` | `false` | Execute workflow in simulation mode without sending network SMTP requests. Results are logged, but task window state is not updated |
| `--verbose` | `-v` | `FLAG` | `false` | Enable verbose debug logging output |
| `--template-dir` | | `PATH` | `.` | Directory that task template paths are resolved against (overrides `MAIL_CADENCE_TEMPLATE_DIR`). Set it explicitly when running from cron/systemd |

`--tasks-xml` and `--schedule-xml` import the files into the database before the run, also together with `--dry-run`: the import changes the stored configuration and is not part of the simulation.

The exit codes (`0` to `4`) and what an operator does about each are described in [OPERATIONS.md](OPERATIONS.md#exit-codes).

### Examples

```bash
# Execute dispatch and seed XML configurations if database is empty or needs updates
mail-cadence --tasks-xml config/samples/tasks.xml --schedule-xml config/samples/schedules.xml

# Run dispatch in dry-run mode using an existing SQLite database
mail-cadence --db mail_cadence.db --dry-run --verbose
```

## 2. XML Import Command (`import-xml`)

Imports task definitions and schedule rules from XML configuration files into the SQLite database.

```bash
mail-cadence import-xml [OPTIONS]
```

| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Path to tasks configuration XML file |
| `--schedule-xml` | `-s` | `PATH` | *None* | Path to schedule configuration XML file |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Target SQLite database file path |
| `--mode` | `-m` | `CHOICE` | `upsert` | Import mode: `upsert` (update/insert) or `replace` (clear and reload) |

```bash
mail-cadence import-xml --tasks-xml config/samples/tasks.xml --schedule-xml config/samples/schedules.xml --mode upsert
```

## 3. XML Export Command (`export-xml`)

Exports current task definitions, schedules, and report settings from the SQLite database back into XML files.

```bash
mail-cadence export-xml [OPTIONS]
```

| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--tasks-xml` | `-t` | `PATH` | *None* | Destination path for exported tasks XML |
| `--schedule-xml` | `-s` | `PATH` | *None* | Destination path for exported schedules XML |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Source SQLite database file path |

```bash
mail-cadence export-xml --tasks-xml exported_tasks.xml --schedule-xml exported_schedules.xml
```

## 4. Web Panel (`serve`)

For a production setup (systemd units with sandboxing, a 10-minute dispatch timer, nginx with HTTPS, fail2ban), see [DEPLOYMENT.md](DEPLOYMENT.md) and the files in [`deploy/`](../deploy).

```bash
mail-cadence serve --host 127.0.0.1 --port 8000
```

| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--host` | `-h` | `TEXT` | `127.0.0.1` | Host address to bind the web server |
| `--port` | `-p` | `INTEGER` | `8000` | Port to bind the web server |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | Path to SQLite database file (overrides `MAIL_CADENCE_DB`) |
| `--template-dir` | `-t` | `PATH` | `.` | Directory that task template paths are resolved against (overrides `MAIL_CADENCE_TEMPLATE_DIR`) |
| `--insecure-no-auth` | | `FLAG` | `false` | Allow access without `ADMIN_USER`/`ADMIN_PASSWORD`; only with a loopback `--host` |

- Set `ADMIN_USER` and `ADMIN_PASSWORD`; without them every request gets `503`. `--insecure-no-auth` turns this off, and only works with a loopback `--host`.
- After `ADMIN_LOGIN_MAX_FAILURES` wrong logins (default 10) within `ADMIN_LOGIN_WINDOW_SECONDS` (default 15 minutes), a client IP gets `429` with a `Retry-After` header, even with the correct password, until its oldest failure is older than the window. Each block is logged as a warning (`Too many failed admin logins from <ip>`), which tools like fail2ban can watch. The counters live in memory, so they reset when the panel restarts.
- State-changing requests (`POST`, `PUT`, `DELETE`, ...) from other sites are rejected with `403`. The check compares the browser's `Origin` header with the request's `Host` header.
- The panel sends a strict `Content-Security-Policy` and other security headers, and loads no files from CDNs: scripts, styles and fonts are served by the app itself, so it also works without internet access. The template preview opens in a sandbox where scripts in a template cannot run.
- **Behind a reverse proxy** (nginx, Traefik, Caddy, ...), the proxy must pass the original `Host` header through, otherwise every save or dispatch fails with `403 Cross-site request rejected.` For nginx: `proxy_set_header Host $host;` (Caddy and Traefik keep it by default). The proxy must also send `X-Forwarded-For`: uvicorn uses it as the client IP only for proxies on `127.0.0.1`/`::1`, or those listed in the `FORWARDED_ALLOW_IPS` environment variable (e.g. `FORWARDED_ALLOW_IPS=10.0.0.5`). Otherwise every visitor appears as the proxy's IP, and the login limit blocks everyone at once. Serve the panel over HTTPS, because Basic Auth sends the password with every request.

## 5. Monitoring (`status`)

Checks the database for a monitoring system and prints one status line in the Nagios plugin format. It only reads the database: it never creates, migrates or writes it.

```bash
mail-cadence status [OPTIONS]
```

| Option | Short | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--db` | `-b` | `PATH` | `mail_cadence.db` | SQLite database file (overrides `MAIL_CADENCE_DB`) |
| `--warning-age` | | `DURATION` | `30m` | WARNING when the last finished dispatch run is older |
| `--critical-age` | | `DURATION` | `2h` | CRITICAL when the last finished dispatch run is older; also the age at which an unfinished run counts as stalled |
| `--since` | | `DURATION` | `24h` | How far back failed executions, undelivered reports and stalled runs count |
| `--json` | | `FLAG` | `false` | Print a JSON object (findings, counters, thresholds) instead of the status line |

A duration is a whole number with `s`, `m`, `h` or `d` (`90s`, `30m`, `2h`, `1d`). Exit codes follow the Nagios plugin conventions, not those of the dispatch command: `0` OK, `1` WARNING, `2` CRITICAL, `3` UNKNOWN. The checks are described in [OPERATIONS.md](OPERATIONS.md#monitoring).

```bash
$ mail-cadence status --db /var/lib/mail-cadence/mail_cadence.db
MAIL-CADENCE CRITICAL - 1 execution awaiting operator review; last run 4m ago | needs_review=1;;;0 reports_unknown=0;;;0 reports_pending=0;;;0 reports_failed=0;;;0 executions_failed=0;;;0 stalled_runs=0;;;0 last_run_age=240s;1800;7200;0
$ echo $?
2
```
