# ADR 0001: Durable execution state for task dispatch

- Status: Accepted (2026-10-27)
- Date: 2026-10-27
- Scope: `database.py`, `repository.py`, `scheduler.py`, `pipeline.py`, `mailer.py`, `reporter.py`, `cli.py`, `web.py`
- Source: `plan/gpt6_audyt_01.md` (implementation plan for the execution-state audit)
- Reproductions: `tests/test_delivery_guarantees.py`

## 1. Context

Whether a task may be sent is decided from two columns, `tasks.last_run` and `tasks.last_status`. A batch reads them once at the start (`pipeline._load_config`) and writes them only after SMTP has finished (`DatabaseRepository.log_task_execution`). The retry budget and the per-recipient outcome exist only in memory, inside one `AsyncMailer.dispatch_task` call. This causes the following defects, each reproduced by a test marked `xfail(strict=True)`:

| Defect | Cause in the code | Test |
|---|---|---|
| Two processes send the same task in the same window. | No claim. `DispatchRunner` serialises jobs only inside one web process; a cron run and the panel do not share it. | `test_t01_concurrent_processes_send_once` |
| A later run in the same window sends to every recipient again, including accepted ones and ones refused with 5xx. | `TaskScheduler._is_dispatched_in_window` blocks the window only after `SUCCESS`; `PARTIAL` and `FAILED` leave it open. | `test_t03_...`, `test_t04_...` |
| The retry budget restarts with every run. | `max_retries` is counted in a local variable. | `test_t05_...` |
| A lost reply after DATA causes a duplicate. | `SMTPServerDisconnected` and `OSError` are treated as transient and the whole message is sent again. | `test_lost_reply_after_data_is_not_resent` |
| The result of an old definition marks an edited or re-created task as sent. | `UPDATE tasks ... WHERE id = ?` runs after SMTP and hits whatever row has that text id now. | `test_t06_...`, `test_t08_...` |
| A failed status report is never retried. | The report has no stored state; the next run sends a report only if it executed a task. | `test_t13_...` |

Fixed separately (commit `9413421`): dry-run skips in the statistics, exit code for `PARTIAL` and for a failed report, CR/LF in `ReportSettings.subject_prefix`.

## 2. Decisions

### D1. What success means

A recipient is `ACCEPTED` only when the server answers 250 to the end of DATA and had accepted that recipient at RCPT TO. A 250 to RCPT TO alone is not a delivery. Delivery to the mailbox is outside our knowledge.

SQLite and SMTP cannot share a transaction, so exactly-once delivery is impossible. **The policy is at-most-once whenever the outcome is ambiguous**: such recipients go to `UNKNOWN` and are never retried automatically. An operator resolves them (D12).

### D2. SMTP attempt and protocol phase

`AsyncMailer` stops using `send_message` for task mail and drives the transaction itself with `mail()`, `rcpt()` and `data()`. All three exist in every supported `aiosmtplib` version (>= 3.0.1). They are added to the `mailer.SMTPClient` protocol, and test fakes must implement them. The attempt returns a typed result per recipient, not a dictionary:

| Where the attempt ended | Outcome |
|---|---|
| Connect, EHLO, auth, MAIL FROM: reply code or network error | Certain, nothing sent: 4xx or network error -> `RETRY_WAIT`, 5xx -> `PERMANENT_FAILURE` for all recipients. |
| RCPT TO per recipient | Certain per recipient: 4xx -> `RETRY_WAIT`, 5xx -> `PERMANENT_FAILURE`. |
| Reply to the DATA command (354 expected) | Certain, nothing sent: code classified as above. |
| After the message body was sent: final reply 250 | `ACCEPTED` for the recipients accepted at RCPT. |
| After the message body was sent: final reply 4xx or 5xx | Certain, not accepted: classified as above. |
| After the message body was sent: timeout, disconnect, no reply | `UNKNOWN` for the recipients accepted at RCPT. |

The message is serialised once (D7). Bcc addresses are only in the envelope, never in the headers; `build_email_message` already sets no Bcc header.

*Amended in stage 4* (`AsyncMailer.deliver`, `mailer._Attempt`):

- A refusal at connect or login (for example 535) says nothing about the recipients: every recipient is `TRANSIENT`, not `PERMANENT`. Otherwise a wrong password would end deliveries for good.
- `aiosmtplib.SMTP.data()` sends the DATA command and the body in one call. A lost connection or a timeout inside it cannot be placed before or after the body, so it is `UNKNOWN`. A reply code from inside it (refused DATA command, refused body) is certain, as in the table.
- A pooled connection is dropped after any error, not only after a disconnect: a timed-out session may still hold a late reply. A dropped client is always closed (`SMTPClient.close`): after the server ends the connection, aiosmtplib 3.0.1 leaves the transport open, so only calling `quit()` on connected clients leaked a socket.
- The message is stored as `email.policy.SMTP` with `cte_type="7bit"` (8-bit parts become base64), so the same bytes pass any server, with or without 8BITMIME. The envelope sender is the bare address of the stored `From` header.

Phase transitions are not written to the database during an attempt. If the process dies during an attempt, the stored state is `IN_FLIGHT` without a result, and recovery treats it as `UNKNOWN` (D10). One write per phase would allow some of these cases to be retried safely; it is rejected as a cost with small benefit.

### D3. Task identity

*Amended in stage 2:* identities live in a separate table instead of a rebuilt `tasks` table. Rebuilding a table that other tables reference by foreign key is risky in SQLite and is not needed.

`task_identities(uid, task_id, dispatch_generation, created_at, deleted_at)` holds the stable identity. The text `id` stays the user-visible name of a live task; a partial unique index allows one live identity per name. Deleting a task retires its identity (`deleted_at`), and creating a task with the same `id` gives a new `uid`, so the new task is never linked to the old executions. Identity rows are never deleted. Executions, versions and events reference `uid`, not `tasks`, so the history survives deletion (I10).

`database.sync_definitions` keeps identities in step with `tasks`. Every repository method that writes tasks or schedules calls it at the end of its transaction. Identities follow task names, so an XML import in `replace` mode keeps the identity of every task it configures again, although it deletes and re-inserts the rows.

### D4. Definition and schedule versions

- `task_versions(task_uid, version, definition_json, created_at)`. `definition_json` is the canonical JSON of the stored rows (task columns, recipients and context variables in order), not of the Pydantic model, so legacy rows that no longer validate still get a version. A new version is written when it differs from the latest one; saving an unchanged task writes nothing.
- `schedule_versions(schedule_id, version, fingerprint, updated_at)`. The fingerprint is the schedule's windows plus the timezone; the description is not part of it. A change bumps the version, and a timezone change bumps every schedule. Rows stay when a schedule is deleted, so a re-created schedule keeps counting upwards.
- An execution pins `task_version` and `schedule_version`.

### D5. Window instance

`TaskScheduler` gets a public, pure method that returns an immutable `WindowInstance(schedule_id, window_seq, start_utc, end_utc, timezone)`. The instance key is `"{schedule_id}@{start_utc ISO 8601}"`. The start instant identifies the occurrence; the end is pinned on the execution. Editing a window's end time keeps the key. Editing its start time creates a new instance, which can send again on the same day; that is accepted for an explicit schedule change and shown in the UI.

Window bounds:

- Closed bounds `[start, end]` stay, as today.
- Overlapping windows: the first one in configuration order wins, as today.
- Bounds are local wall-clock times converted with `fold=0`. A start inside a spring-forward gap maps to the first instant after the gap (zoneinfo behaviour for `fold=0`). On a fall-back day, an ambiguous bound means its first occurrence.
- Activity is decided by comparing instants (`start_utc <= now <= end_utc`), not wall-clock times. During the repeated hour, a wall-clock time outside the instance is not active. This changes `window_status` only around DST transitions; `test_t12_*` (stage 2) pins the expected UTC instants.

### D6. Dispatch generation and reset

Each task identity has a `dispatch_generation`:

| Action | Version | Generation |
|---|---|---|
| Save without changes | same | same |
| Change with the default reset (`reset_window_state=None` or `True`) | +1 | +1 |
| Change with `reset_window_state=False` | +1 | same, the current instance stays blocked |
| `reset_task_window_state` (explicit permission to resend) | same | +1, reason stored |
| Next window instance | - | - (a new instance key needs no reset) |

When the block on the current instance comes from an older version, the UI shows "version N sent, version M blocked in this window", never "sent".

### D7. Executions and the claim

New tables (exact SQL is part of stage 2):

| Table | Content |
|---|---|
| `dispatch_runs` | Run id, source (`cli`, `web`, `manual`), dry_run, start, end, task outcome, report outcome. |
| `task_executions` | `UNIQUE(task_uid, dispatch_generation, window_key)`; pinned versions; window start and end; state; owner; fencing token; lease expiry; Message-ID; serialised message (`BLOB`) or NULL after retention. |
| `execution_recipients` | Execution, envelope address, display form, state, attempt count, `next_attempt_at`, last SMTP code and text. |
| `delivery_attempts` | Execution, attempt number, fencing token, recipient set (JSON), start, end, outcome, error. |
| `execution_events` | State changes, operator actions, resets, with reason and ids. |
| `report_deliveries` | Run, serialised report message, Message-ID, state, attempts, `next_attempt_at`. |

- A manual send uses `window_key = "manual:{idempotency_key}"`. The web panel creates the key when it renders the button, so a repeated HTTP request returns the existing execution (T09).
- At most one active execution per task identity: a partial unique index on `task_executions(task_uid) WHERE state IN ('CLAIMED', 'ACTIVE')`. It covers every generation, manual sends and an old worker.
- The rendered message and its Message-ID are stored when the execution is claimed, before the first attempt. Retries send the stored bytes, so a template change between attempts has no effect (T16).

**Transactions.** Every state change is a short transaction opened with `BEGIN IMMEDIATE` through one repository helper, `_write_transaction()`, which commits or rolls back. Python's `sqlite3` would otherwise start a deferred transaction, and two readers would both decide to send. Rules:

- A public repository method opens either `with self.conn:` (existing methods) or `with self._write_transaction():` (claim and state changes), never both, never nested. AGENTS.md section 2 is amended accordingly.
- No transaction is open during SMTP, backoff, a semaphore or a rate-limiter wait (I8). `test_t18_*` (stage 3) proves it with a write from another process during a held SMTP session.
- `pipeline.py` and `web.py` use only public repository methods: `claim_execution`, `start_attempt`, `record_attempt_result`, `heartbeat`, `recover_abandoned`, `resolve_unknown`.

**Claim in a batch.** Inside one immediate transaction: insert the execution or find the existing one for the key; take ownership if it is unowned or its lease has expired and no attempt has started. A conflict on the partial index means another worker owns the task: the batch logs the task as `SKIPPED_BUSY` and continues. That is not an error of the batch (D11).

### D8. Lease and fencing

- Owner: a random id per run. Fencing token: an integer that increments on every change of owner.
- Lease: `lease_expires_at = now + 5 min`, renewed every 60 s by a heartbeat task of the run. All timestamps use `to_utc_iso`; durations use `time.monotonic()`.
- Every write of a result includes `WHERE owner = ? AND fencing_token = ?`. A late worker cannot overwrite the new owner's state.
- An expired lease with no attempt started: the execution can be taken over.
- An expired lease with an attempt `IN_FLIGHT`: recovery moves those recipients to `UNKNOWN` and the execution to `NEEDS_REVIEW`. There is no automatic resend. Fencing protects the database, not the old SMTP connection.
- A wall-clock jump can expire a lease, but it only leads to `NEEDS_REVIEW`, never to a resend.

### D9. Recipient states and the retry budget

```
PENDING -> IN_FLIGHT -> ACCEPTED | PERMANENT_FAILURE | RETRY_WAIT | UNKNOWN
RETRY_WAIT -> IN_FLIGHT        (before the window end, after next_attempt_at, budget left, owner valid)
RETRY_WAIT -> RETRY_EXHAUSTED  (budget used)
RETRY_WAIT | PENDING -> WINDOW_EXPIRED  (window ended before a safe attempt)
```

- The budget is `max_retries + 1` attempts per recipient, taken from the pinned version and counted in the database. A restart does not reset it (I12).
- Backoff stays `0.5 * 2^(n-1)` s inside a run; `next_attempt_at` is stored. A recipient left in `RETRY_WAIT` by a dead process is picked up by the next run while the window is open.
- No attempt starts after `end_utc` of the instance. The check runs again after waiting for the semaphore, the rate limiter and the backoff (I11).
- Envelope deduplication: the key is the local part unchanged plus `@` plus the lower-cased domain. A recipient listed in To and Cc is one envelope recipient. The headers are not changed.
- The execution state is derived from the recipient states: `ACTIVE`, `RETRY_WAIT`, `SUCCEEDED`, `PARTIAL`, `FAILED`, `NEEDS_REVIEW` (any `UNKNOWN`), `EXPIRED`. `PARTIAL` never authorises sending to the whole list.

### D10. Recovery

At the start of every non-dry run, `recover_abandoned()` moves `IN_FLIGHT` recipients of executions with an expired lease to `UNKNOWN` and records an event. A failed write after SMTP (for example `database is locked`) leaves the recipients `IN_FLIGHT`. The run reports exit code 3 and the next recovery makes them `UNKNOWN`. Cancelling the `TaskGroup` follows the same rule.

### D11. Exit codes

| Code | Meaning | Priority |
|---|---|---|
| 0 | Everything required is confirmed, including "no eligible task". | - |
| 3 | At least one recipient or report is `UNKNOWN`, or a result could not be stored. | 1 (highest) |
| 1 | Configuration, database or infrastructure error. | 2 |
| 2 | Incomplete delivery: `PARTIAL`, `FAILED`, `RETRY_EXHAUSTED`, `WINDOW_EXPIRED`. | 3 |
| 4 | Tasks confirmed, but the required report is not. | 4 |

A busy task in a batch is `SKIPPED_BUSY` and does not change the code. The web panel answers a manual send with 202 and the existing execution id for a repeated key, and with 409 and the id of the active execution for another key. The CLI has no single-task command, so code 5 from the plan is not introduced.

### D12. Operator resolution of UNKNOWN

The panel lists `NEEDS_REVIEW` executions with their attempts, the owner and its last heartbeat. Resolving needs authentication, same-origin, the attempt id and a mandatory reason. It is allowed only when the lease has expired or the execution is finished. The choices are:

- `accepted`: the server's logs prove delivery;
- `failed`: proof of no delivery;
- `retry`: the recipient goes back to `PENDING` with a fresh budget. The operator must confirm that the old worker is stopped, because fencing cannot stop an open SMTP connection.

Every choice writes an event. Secrets are never shown.

### D13. Dry run

A dry run never writes `task_executions`, `execution_recipients`, `delivery_attempts` or `report_deliveries`. It reads them to report what would happen: send, busy or blocked. Its logs carry `dry_run = 1`, and the report is only logged, never sent. Importing XML together with `--dry-run` still changes the configuration; README states that the import is not part of the simulation.

### D14. Status report

The report message is rendered once per run and stored in `report_deliveries` with its Message-ID. Its failure does not affect task messages. The next run (dry or not, with or without eligible tasks) retries unconfirmed reports first, from the stored bytes, under the same rules as D2 and D9 with the report's own budget (3 attempts). An `UNKNOWN` report is not retried automatically.

### D15. Compatibility projection

`tasks.last_run`, `last_status` and `last_error` stay as a read-only projection for the UI and the XML export. Only an execution of the task's current version updates them, and they no longer authorise sending. `cli.run_dispatch_pipeline` keeps its signature. `ref_dt` evaluates the schedule only; timestamps of events and leases always use the real clock.

### D16. Retention

The serialised message (task or report) is set to NULL 30 days after its execution reaches a final state. The value is a constant for now and can become a setting later. Recipient states, attempts and events are the audit trail and are kept. Passwords and `Settings` are never stored.

### D17. Migration and rollout

1. Append migrations; `SCHEMA_SQL` describes the new tables. Released migrations are not changed.
2. Every task gets a `uid` and version 1 from its current definition; every schedule gets version 1. *Done in migration 5 (stage 2).*

Steps 3 to 5 run in the migration of the release that enables the new dispatch path, not in migration 5: until then the old path keeps updating `last_run` and `last_status`, and a conversion made earlier would be out of date.

3. A legacy `SUCCESS` creates a synthetic `SUCCEEDED` execution (`legacy = 1`, no recipients) for the window instance that contains `last_run`. The current instance stays blocked, later ones are free.
4. A legacy `PARTIAL` or `FAILED` creates a synthetic `NEEDS_REVIEW` execution for that instance only. The old data cannot tell which recipients were accepted.
5. An unparsable `last_run` creates no execution and writes an event. The current code also treats it as "never sent".
6. Tests start from every released schema version (0 to the current one), check that a repeated `init_db` is a no-op, and compare a fresh schema with a migrated one (tables, indexes, foreign keys, unique constraints).
7. Rollout: stop every timer and process, back up the database with the SQLite backup API (`sqlite3 db ".backup copy.db"`), never copy a live WAL database file, then upgrade. An older version refuses the newer schema (the existing `init_db` check). A rollback to the backup must be reconciled with messages accepted after the backup.

The old authorisation through `last_status` and the new claim never run side by side: one release switches over completely.

### D18. Limits of the guarantees

Rate limits and concurrency apply per window instance and per process. They are not a limit for the whole SMTP account; no distributed limiter is added. The documentation says so.

## 3. Consequences

- AGENTS.md changes: section 2 (the `_write_transaction` rule; `DispatchRunner` is no longer the only protection against parallel runs); section 3 (exit codes 0 to 4; `SMTPClient` has `mail`, `rcpt` and `data`); section 4 (a dry run never writes execution tables).
- The `SMTPClient` protocol and test fakes grow. `send_message` stays only if the report keeps using it; under D14 it does not.
- More writes per task: claim, one per attempt start, one per attempt result. That is acceptable for the expected volumes (tens to hundreds of tasks per run).
- `window_status` changes behaviour around DST (D5).

## 4. Rejected alternatives

- Keep `last_status` and treat `PARTIAL` and `FAILED` as "sent": it hides failures and cannot retry only the failed recipients.
- A file lock or `DispatchRunner` across processes: it does not survive a crash cleanly and does not cover several hosts sharing a database.
- An external queue or another database: out of scope (plan section 1).
- Automatic retry after a lost reply: it trades a missing message for a duplicate without telling anyone.

## 5. Verification

- Stage 1 (this ADR): reproductions in `tests/test_delivery_guarantees.py`, with an SMTP sink in `tests/smtp_sink.py` whose log is the oracle, and separate processes in `tests/dispatch_worker.py` (spawn start method, also on Windows).
- Stage 2 (schema): `tests/test_execution_schema.py`. A database from every released version 1 to 4 (frozen schema in `tests/fixtures/schema_v4.sql`) migrates to the same shape as a fresh one; migration 5 is idempotent and resumes after a crash; identities, versions and generations follow D3, D4 and D6; SQLite itself rejects a second active execution, a missing version and invalid recipient rows (T17). The tables are not used for dispatch yet.
- Stage 3 (repository): `execution_store.py`, inherited by `DatabaseRepository`, with `claim_execution`, `start_attempt`, `record_attempt_result`, `next_attempt_at`, `release_execution`, `heartbeat`, `recover_abandoned`, `resolve_unknown`, `start_run` and `finish_run`. `tests/test_execution_store.py` covers claim and fencing, the persistent retry budget, window expiry, UNKNOWN handling, recovery, operator resolution and pinned versions with a fake clock; six processes claiming eight tasks at once get exactly one winner per task (T01 without SMTP); another connection can write between `start_attempt` and `record_attempt_result` (T18 without SMTP); a failing write leaves no partial state (T17). Five local mutations (no token check, accepted recipients retried, no busy check, late result after recovery, no window-end check) each fail at least one test. Not wired into the pipeline yet.
- Stage 4 (mailer): `AsyncMailer.render_task_message` (snapshot with Message-ID) and `AsyncMailer.deliver` (one transaction through `mail`, `rcpt`, `data`, `rset`, typed `RecipientResult` per recipient, no retries of its own); `DatabaseRepository.pin_message` stores the snapshot once per execution. `tests/test_mailer_attempts.py` drives a real TCP session against the sink for every row of the D2 table: accepted, refused per recipient, all refused (RSET, session reused), refused sender, refused DATA command, refused body, unreachable server, lost connection before DATA (transient), lost connection or timeout after the body (unknown, the sink shows the message was accepted). Five local mutations each fail at least one test. `dispatch_task` and the pipeline still use the old path.
- Later stages add T02, T07, T09 to T12 and T14 to T18 from the plan, the failure matrix (plan 7.3), the reference transition model (plan 7.4) and mutation checks. Each stage removes the `xfail` markers it fixes.
- A stage is done when `make quality-gate` passes and the lowest-dependency job is green on Linux and Windows.
