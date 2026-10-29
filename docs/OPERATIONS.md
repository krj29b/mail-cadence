# Operations: Exit Codes, Delivery Guarantees and Unknown Deliveries

This page is for the operator who runs `mail-cadence` from cron or a systemd timer and watches its results. The design behind it is [ADR 0001](adr/0001-execution-state-model.md).

## Exit Codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Successful execution, or no tasks were eligible for processing. | Nothing. |
| `1` | Configuration or database error (e.g. invalid file format, database connection failure, SMTP login without TLS). | Read the log; fix the configuration or the database. |
| `2` | One or more tasks failed or were delivered only partially (some recipients did not receive the message). | Check the task in the panel; nothing is resent to recipients that accepted the message. |
| `3` | The outcome of a delivery is unknown: the connection was lost after the message was sent, so the server may have accepted it. | Review it, see [Reviewing Unknown Deliveries](#reviewing-unknown-deliveries). The task sends nothing until then. |
| `4` | All executed tasks succeeded, but the status report email could not be sent. | Check the report in the panel (**Executions**); later runs retry it. |

Precedence when several apply: `3`, `1`, `2`, `4`. Until a window occurrence ends, every run in it reports the outcome of the task's execution in that occurrence again (`2` for an incomplete one, `3` for an unknown one), although nothing is sent again, so a monitoring alert stays on until the window is over or the execution is resolved. A task blocked by an unresolved unknown outcome makes every run report `3` until the decision is recorded.

## Monitoring

`mail-cadence status` reports the state of the database to Nagios, Icinga, Zabbix or a script ([options](CLI.md#5-monitoring-status)). The dispatch exit code tells the result of one run; `status` tells whether anything needs attention now, whichever run caused it, and can be checked at any time.

| State (exit code) | Condition |
| --- | --- |
| CRITICAL (`2`) | An execution awaits operator review: a delivery has an unknown outcome and the task sends nothing until it is resolved (see [Reviewing Unknown Deliveries](#reviewing-unknown-deliveries)). |
| CRITICAL (`2`) | No dispatch run has finished within `--critical-age` (default 2h), or none has finished yet. |
| CRITICAL (`2`) | A status report ended with an unknown outcome within `--since` (default 24h). |
| WARNING (`1`) | The last dispatch run finished more than `--warning-age` ago (default 30m). |
| WARNING (`1`) | A status report waits for a retry by the next run. |
| WARNING (`1`) | A status report was refused or used up its attempts within `--since`. |
| WARNING (`1`) | A task execution failed, was delivered only partially, or expired at the window end within `--since`. |
| WARNING (`1`) | A dispatch run started more than `--critical-age` ago (and within `--since`) and never finished: the process was killed or crashed. |
| UNKNOWN (`3`) | The database cannot be read, its schema is older or newer than this version, or an option is invalid. |
| OK (`0`) | None of the above. |

Notes:

- Only real dispatch runs from the CLI, a timer or the panel's batch dispatch count as runs; dry runs and the **Run** button do not. Choose `--warning-age` and `--critical-age` above the timer interval plus the longest batch.
- A report with an unknown outcome cannot be resolved in the panel; it stops raising an alert once it is older than `--since`.
- The check opens the database read-only. In WAL mode SQLite still needs to create or open the `-shm` and `-wal` files next to the database, so run it as the user that owns the database (e.g. `sudo -u mail-cadence`), not as a user with read access to the database file only.
- Options it does not know are rejected by the command-line parser with exit code `2` before the check runs.

Nagios or Icinga (`check_by_ssh` or NRPE on the host that stores the database):

```
command[check_mail_cadence]=/usr/bin/sudo -u mail-cadence /opt/mail-cadence/venv/bin/mail-cadence status --db /var/lib/mail-cadence/mail_cadence.db
```

## Delivery Guarantees

Before anything is sent, a run claims each task for its window occurrence in the database, so two runs (two timers, the CLI and the web panel, processes on one host sharing the database) never send the same task at the same time. The message is rendered once and stored; every retry sends the same bytes. Each recipient's outcome is stored per attempt: a recipient accepted by the server or refused with a 5xx code is never sent the message again, a 4xx code is retried within `max_retries` (counted across runs), and no attempt starts after the window ends. A message is never sent twice automatically when its outcome is unknown. These guarantees hold for processes on the host that stores the database file, which must be on a local disk: SQLite in WAL mode does not work on a network file system (NFS, SMB), so never point runs on several hosts at a shared database. They do not extend to the SMTP account as a whole: rate limits apply per window and per process.

What "once" covers and what it does not:

- A task is sent automatically at most once per dispatch generation and window occurrence. Saving a changed task or **Allow Resend** starts a new generation, so the task can be sent again in the same window occurrence. The **Run** button ignores the window (see [Manual Sends](#manual-sends)).
- "Accepted" means the SMTP server accepted the message. Whether it reaches the mailbox is outside the application.
- It is not exactly-once between the database and the SMTP server: when the connection breaks after the message was sent, the outcome is unknown and nothing is sent again until an operator decides (see [Reviewing Unknown Deliveries](#reviewing-unknown-deliveries)).
- A message whose template cannot be rendered (missing template or variable) ends the execution without any SMTP traffic. Later runs in the same window occurrence do not retry it when the data appears; edit the task or use **Allow Resend**. Make the data and templates ready before the window opens.
- Windows limit when automatic attempts start, not when the SMTP exchange ends. Status reports are sent at the end of a run, independent of any window.

## Status Report

When a run executed at least one task and report settings are configured, its report is rendered once and stored. A report that was not accepted is retried by the following runs from the same stored message, even after the last task was deleted, one attempt per run and at most three in total; a refusal with a 5xx code or an unknown outcome ends it. A failed report never resends task messages. Exit code `4` means that a report handled in this run (its own or an earlier one retried) is not confirmed.

## Retention

Stored messages (task and report) are deleted 30 days after their delivery is over; the Message-ID, the per-recipient outcomes, the attempts and the event history are kept. A delivery that still needs review keeps its message.

## Reviewing Unknown Deliveries

The web panel's **Executions** page lists every execution with its task version, window occurrence and recipient progress; "Needs review" filters the ones with an unknown outcome, and the dashboard shows how many there are. The page of an execution shows each recipient, every attempt and the event history. For a recipient in state `UNKNOWN`, check the SMTP server's log for the Message-ID shown on the page, then record a decision with a reason:

- **Delivered**: the server's log shows the message was accepted;
- **Not delivered**: the log shows it was not;
- **Send again** (check the event history first: a `late_result` event shows the server's reply that a worker received after its attempt had been recovered, and that this worker has finished the attempt): the next dispatch run sends the stored message again, if the same window occurrence is still open (once it has ended, no run picks the execution up again); first make sure the worker that made the attempt is no longer running, because nothing can stop an SMTP connection it still holds. For a manual send, use the **Run** button again instead.

Until every `UNKNOWN` recipient of an execution has a decision, the task is not sent at all: later window occurrences, a reset of the window state and the **Run** button skip it as `SKIPPED_BUSY` ("unknown outcome"), and each dispatch run that skips it exits with code `3`. A decision refers to the attempt shown on the page and is refused if a newer attempt exists. It is recorded with the panel user's name.

## Manual Sends

The **Run** button on the Tasks page sends a task once per page load: clicking again reports the same send; reload the page to send it again. A manual send ignores the schedule window, but not another worker sending the same task or an unresolved unknown outcome.
## Log Shipping (Loki / ELK)

Setting `LOG_FORMAT=json` configures log output as single-line JSON objects on standard output for ingestion by log shippers such as Grafana Loki, Fluentbit, or Elastic Vector. Each record includes `timestamp` (ISO 8601 in UTC), `level`, `logger`, `message`, and `exception` (formatted traceback present only when an exception occurred).
