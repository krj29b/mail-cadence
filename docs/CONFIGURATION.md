# Configuration

`mail-cadence` is configured through environment variables (or a `.env` file) for SMTP server options and database location, alongside XML or database settings. The `.env` file is read from the current directory, or from `MAIL_CADENCE_ENV_FILE` — set it explicitly for cron/systemd runs. Real environment variables take precedence over the file.

## Environment Variables (`Settings`)

Supported environment settings defined in `mail_cadence.models.Settings`:

| Variable | Default | Description |
| --- | --- | --- |
| `SMTP_HOST` | `localhost` | Hostname or IP address of the SMTP server |
| `SMTP_PORT` | `1025` | Port number for the SMTP server |
| `SMTP_USER` | *None* | Username for SMTP authentication (optional) |
| `SMTP_PASSWORD` | *None* | Password for SMTP authentication (optional). With `SMTP_USER` and `SMTP_PASSWORD` set, `SMTP_USE_TLS` or `SMTP_START_TLS` is required, see `SMTP_ALLOW_INSECURE_AUTH` |
| `SMTP_USE_TLS` | `false` | Enable direct TLS connection (`true`/`false`), usually port 465 |
| `SMTP_START_TLS` | `false` | Enable STARTTLS command upgrade (`true`/`false`), usually port 587 |
| `SMTP_ALLOW_INSECURE_AUTH` | `false` | Allow SMTP login without TLS/STARTTLS. Without it, a real run with a password but no encryption stops with exit code `1` (dry runs still work). Only for a trusted relay on the same machine or network |
| `SMTP_TIMEOUT` | `30.0` | Timeout in seconds for SMTP operations |
| `MAIL_CADENCE_DB` | `mail_cadence.db` | Path to SQLite database file, on a local disk of the host that runs every mail-cadence process (not a network share: WAL mode does not work there) |
| `MESSAGE_RETENTION_DAYS` | `30` | Retention period in days for stored task and report message contents (`1`–`3650`) |
| `MAIL_CADENCE_ENV_FILE` | `./.env` | Path to the `.env` file with these settings. When set, the file must exist. Without it, `.env` is read from the current directory (if present) |
| `MAIL_CADENCE_TEMPLATE_DIR` | `.` | Directory that task template paths are resolved against (same as `--template-dir`). Built-in templates are referenced as `builtin:<name>`, e.g. `builtin:daily_dispatch.html.j2` |
| `ADMIN_USER` | *None* | Web panel username. Without it (and `ADMIN_PASSWORD`) the panel refuses access unless started with `serve --insecure-no-auth` on a loopback host |
| `ADMIN_PASSWORD` | *None* | Web panel password |
| `ADMIN_LOGIN_MAX_FAILURES` | `10` | Wrong web panel logins allowed per client IP within the window; further attempts get `429 Too Many Requests` |
| `ADMIN_LOGIN_WINDOW_SECONDS` | `900` | Length of that window (sliding) in seconds |
| `LOG_FORMAT` | `text` | Log format on stdout (`text` or `json`) |

## Database Upgrades

The database schema is versioned (`PRAGMA user_version`). Existing databases are upgraded automatically the first time a new version of `mail-cadence` opens them (any CLI command or `serve`); back up the `.db` file before upgrading. A database created by a newer version is refused (exit code `1`). Timestamps are stored as ISO 8601 in UTC.

## Task and Schedule Rules

- `priority` is `high`, `normal` or `low`; it only sets the start order (all eligible tasks run concurrently, limited per schedule window). `max_retries` is `0`–`10`.
- Every task needs at least one recipient (`To`, `Cc` or `Bcc`). Each entry is one address, bare or with a name (`Ann <ann@example.com>`).
- `schedule_ref` must name an existing schedule.
- Hour windows include both ends. To cover a whole day use `start="00:00" end="24:00"`; `23:59` leaves the last minute uncovered.
- Task templates are strict: a variable that is not defined (for example a typo, `{{ nme }}`) fails the task instead of rendering as empty text. Mark optional variables explicitly: `{{ region | default('everyone') }}` or `{% if region is defined %}...{% endif %}`.
- A task whose template renders no visible text (empty file, or only empty tags) fails with `Template rendering error` and nothing is sent. The panel preview shows the same error. An email made only of images therefore needs some text, for example `alt` text in a paragraph.

The XML format of tasks and schedules and the full list of validation rules are in [ARCHITECTURE.md](ARCHITECTURE.md#validation-rules); [`config/samples/`](../config/samples) holds a working example.

## Send Variables (`cadence`)

Every task template, and a subject with `render_subject` (below), sees the variable `cadence`. It describes what the message is sent for:

| Variable | Value |
| --- | --- |
| `cadence.date` | The local day, in the schedules' timezone, on which the window occurrence started. A window `22:00`-`02:00` that started on 28 October gives `2026-10-28`, also at 01:00 on the 29th. |
| `cadence.window_start`, `cadence.window_end` | Start and end of that occurrence as datetimes in the schedules' timezone. `None` only when the task's schedule has no window. |
| `cadence.kind` | `window` for a scheduled send, `manual` for the **Run** button. |
| `cadence.task_id`, `cadence.timezone` | The task id and the schedules' timezone name. |

- A scheduled send uses the occurrence the run is in. A manual send (**Run**) and the panel's message page use the open occurrence, or else the one that started last: a nightly report sent by hand in the morning carries the night's date, a weekly window its last occurrence. A schedule without windows gives no window and the day of the send.
- The values are stored with the execution when it is claimed. Retries, a takeover after a crash and the dry run render with the same values; the execution page shows them ("Sent for ..."). Executions claimed before version 0.10 have none stored and get values derived from their window and claim time.
- A task variable named `cadence` shadows it.

Filters for dates (each accepts a date or a datetime and returns the same type):

| Filter | Result |
| --- | --- |
| `date(fmt="%Y-%m-%d")` | `strftime` text, for example `{{ cadence.date \| date("%d.%m.%Y") }}` |
| `add_days(n)` | `n` days later (negative: earlier); a datetime keeps its local time |
| `add_months(n)` | `n` months later; the day is clamped to the end of the month (31 March minus one month is 28 or 29 February) |
| `month_start` | The first day of the month |

Examples: yesterday `{{ cadence.date | add_days(-1) | date("%d.%m.%Y") }}`; the previous month `{{ cadence.date | month_start | add_months(-1) | date("%m.%Y") }}`. Month and day names (`%B`, `%A`) follow the locale of the process, so prefer numeric formats.

### Subject Templates

- Set `render_subject="true"` on `<MailTask>` (panel: **Subject is a template**) to render the subject with the same variables and filters, for example `<Subject>Daily report {{ cadence.date | add_days(-1) | date("%d.%m.%Y") }}</Subject>`. Without it the subject is sent as written, also when it contains `{{`.
- The subject is rendered in a sandbox and is not HTML-escaped (`&` stays `&`). It must render to one non-empty line of at most 255 characters; otherwise, and for an undefined variable, the task fails with a template rendering error and nothing is sent. The panel's message page (**Preview** in the task list) shows the rendered subject or the error.
- Turning the flag on or off changes the task definition, like any other edit.
