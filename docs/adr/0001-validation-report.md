# Validation report: ADR 0001 (durable execution state)

- Date: 2026-10-27
- Code: `master` at `e71bbac`, plus the T02 and T18 tests added with this report and the CI fixes of section 2a
- Plan: `plan/gpt6_audyt_01.md`, section 9 ("Warunki zakończenia")
- Decision record: [0001-execution-state-model.md](0001-execution-state-model.md)

This report lists what was run, with which versions, what came out, and where each guarantee is enforced and tested. Line coverage is listed only because the quality gate requires it; it is not evidence of any delivery guarantee. The evidence is the tests named below, whose oracle is the SMTP sink's log of accepted messages and direct SQL queries, and the local mutations that each of them catches.

## 1. Environment

Local runs: Windows 11 (10.0.26200), Python 3.12.11, 3.13.13 and 3.14.4, SQLite 3.50.4; Linux in Docker (`python:3.12-slim`, `3.13-slim`, `3.14-slim`).

| Tool or library | Current | Lowest declared (lowest-versions run) |
|---|---|---|
| pytest | 9.1.1 | 8.3.0 |
| pytest-asyncio | 1.4.1 | 1.0.0 |
| pytest-cov | 7.1.0 | 5.0.0 |
| hypothesis | 6.170.12 | 6.100.0 |
| mypy | 2.4.0 | - |
| ruff | 0.17.2 | - |
| vulture | 2.16 | - |
| xenon / radon | 0.9.3 / 6.0.1 | - |
| pip-audit | 2.10.1 | - |
| actionlint / zizmor | 1.7.12 / 1.31.1 | - |
| aiosmtplib | 5.1.3 | 3.0.1 |
| fastapi / starlette | 0.162.0 / 1.10.0 | 0.115.0 / 0.38.6 |
| pydantic | 2.14.1 | 2.8.0 |
| httpx2 / httpx | 2.16.0 / - | - / 0.27.0 |

## 2. Commands and exit codes

| Command | Exit code | Result |
|---|---|---|
| `python -m pytest --cov=mail_cadence --cov-fail-under=90` | 0 | 424 passed, 0 xfail, warnings are errors; coverage 95.9% |
| lowest versions: `uv pip install --resolution lowest-direct -e ".[dev]"`, then `pytest -W default::DeprecationWarning` | 0 | 424 passed, 3 deprecation warnings from the oldest fastapi/typer |
| `mypy src tests scripts` (strict) | 0 | no issues in 53 files |
| `ruff check src tests scripts` | 0 | all checks passed |
| `ruff format --check src tests scripts` | 0 | 53 files formatted |
| `vulture src` | 0 | no dead code |
| `xenon --max-absolute B --max-modules B --max-average B src` | 0 | average complexity A (2.81) over 373 blocks |
| `pip-audit --skip-editable` | 0 | no known vulnerabilities |
| `actionlint .github/workflows/ci.yml`, `zizmor --offline .github/workflows` | 0 | no findings |
| `python scripts/stress_races.py --iterations 50` | 0 | 50 of 50 iterations passed (18.1-50.7 s each) |

## 2a. CI matrix

GitHub Actions failed from `bc07318` to `4b4fa41`; only Windows with Python 3.12, the configuration used locally, passed. Two causes, both in tests:

- Python 3.13 reports an unclosed `sqlite3.Connection` as a `ResourceWarning`, and since `9413421` warnings are errors. 23 older tests in `test_database.py`, `test_repository.py`, `test_roundtrip.py`, `test_window_state_dry_run_settings.py` and `test_xml_loader.py` never closed their connection. They now close it through fixtures or finalisers. The leaks were attributed to their tests by a local plugin that runs `gc.collect()` after every test (not committed).
- On Linux the multi-process claim test lost its `multiprocessing` barrier: the parent let it be garbage-collected, which unlinks the named POSIX semaphore before the spawned children open it (`FileNotFoundError`). `tests/dispatch_worker.claimers` now keeps it until the processes are reaped.

Every job of the matrix was then reproduced locally with exit code 0:

| CI job | Reproduced on | Result |
|---|---|---|
| Tests, Linux, Python 3.12 (with coverage) | Docker `python:3.12-slim` | 424 passed, coverage 95.9% |
| Tests, Linux, Python 3.13 / 3.14 | Docker `python:3.13-slim` / `3.14-slim` | 424 passed each |
| Tests, Windows, Python 3.12 / 3.13 / 3.14 | local venvs | 424 passed each; on 3.13 also with `gc.collect()` after every test |
| Lowest declared versions | Docker `python:3.12-slim`, `uv --resolution lowest-direct` | 424 passed |
| Build & smoke test (wheel and sdist) | Docker `python:3.12-slim`, hash-pinned build tools | `twine check` passed, both smoke tests passed |

The GitHub runs of the fixed commit are still to be checked in the Actions tab. The `race-stress` job runs only weekly and on demand and has not run on GitHub yet.

## 3. Invariants

| ID | Requirement (plan 2.2) | Enforced by | Tests |
|---|---|---|---|
| I1 | one active owner per execution key | partial unique index `idx_task_executions_one_active`; `claim_execution` in one `BEGIN IMMEDIATE` | T01, T02, `test_concurrent_processes_claim_each_task_once`, model invariant "one owner" |
| I2 | an accepted recipient is never attempted again | `start_attempt` selects only `PENDING`/`RETRY_WAIT` | T03, `test_accepted_and_refused_recipients_are_never_attempted_again`, model |
| I3 | a 5xx recipient is never attempted again | as I2; `PERMANENT_FAILURE` is final | T04, `test_refused_recipients_get_their_own_outcome` |
| I4 | an UNKNOWN recipient is never attempted automatically | `_settle` turns in-flight into `UNKNOWN`; only `resolve_unknown` changes it | lost reply, failure matrix (5 rows), `test_report_with_unknown_outcome_is_never_resent` |
| I5 | a result updates only its execution and version | compare-and-set on owner, token and attempt; projection condition in `log_task_execution` | T06, `test_old_version_result_does_not_update_the_projection`, `test_an_attempt_result_is_recorded_once` |
| I6 | an old version's completion never completes the new one | task versions, dispatch generations, identities | T06, T08, `test_execution_keeps_its_version_and_generations_do_not_overlap` |
| I7 | atomic reservation and transitions; no SMTP after a conflict | `_write_transaction`; claim before any render or SMTP | T01, T02, `test_failed_operation_leaves_no_partial_write` (T17) |
| I8 | no transaction open during network, backoff or limiter | short transactions; `_write_transaction` refuses to nest | T18 (pipeline), `test_no_transaction_stays_open_between_calls` |
| I9 | dry runs change no real state | `peek_claim`; no writes to execution or report tables | T10, `test_dry_run_stores_no_report`, `test_dry_run_skips_are_flagged_and_excluded_from_stats` |
| I10 | results survive edit and deletion | identities are retired, never deleted; `ON DELETE RESTRICT` | T08, `test_task_with_history_cannot_lose_its_identity_row`, `test_recreated_task_is_a_new_identity` |
| I11 | no new attempt after the window end | `start_attempt` checks `window_end` after every wait | T11 (pipeline and repository) |
| I12 | the retry budget is durable across runs | `attempt_count` and `max_attempts` per recipient | T05, `test_retry_budget_survives_a_new_owner`, model invariant "budget" |

## 4. Scenarios T01-T18

| ID | Test |
|---|---|
| T01 | `test_delivery_guarantees.py::test_t01_concurrent_processes_send_once` |
| T02 | `test_delivery_guarantees.py::test_t02_cli_and_two_panels_send_once` |
| T03 | `test_delivery_guarantees.py::test_t03_restart_never_resends_to_accepted_recipient` |
| T04 | `test_delivery_guarantees.py::test_t04_next_run_skips_accepted_and_permanently_refused` |
| T05 | `test_delivery_guarantees.py::test_t05_retry_budget_is_shared_across_runs` |
| T06 | `test_delivery_guarantees.py::test_t06_old_version_result_does_not_complete_edited_task` |
| T07 | `test_execution_schema.py` (unchanged save, change with and without reset, explicit reset), `test_execution_store.py::test_finished_execution_is_done_for_its_key` (next occurrence) |
| T08 | `test_delivery_guarantees.py::test_t08_recreated_task_is_a_new_identity` |
| T09 | `test_pipeline_execution.py::test_manual_send_with_the_same_key_is_sent_once` |
| T10 | `test_pipeline_execution.py::test_dry_run_*`, `test_dispatch_outcome.py::test_dry_run_skips_*` |
| T11 | `test_pipeline_execution.py::test_no_attempt_starts_after_the_window_end` |
| T12 | `test_pipeline_execution.py::test_window_occurrence_in_utc` and neighbours (midnight, DST forward and back, overlap, inclusive ends) |
| T13 | `test_delivery_guarantees.py::test_t13_failed_report_is_retried_without_resending_tasks`, `test_reports.py` |
| T14 | `test_dispatch_outcome.py`: `test_report_subject_prefix_rejects_newlines` (model and XML), `test_web_rejects_report_subject_prefix_with_newline`, `test_migration_replaces_newlines_in_stored_report_prefix` |
| T15 | `test_execution_store.py::test_claim_pins_version_and_deduplicates_recipients`, `test_mailer_attempts.py::test_snapshot_is_7bit_with_its_message_id_and_without_bcc` |
| T16 | `test_execution_store.py::test_message_is_pinned_once_and_survives_a_new_owner`, `test_mailer_attempts.py::test_retries_send_the_same_bytes`, `test_pipeline_execution.py::test_taken_over_execution_sends_the_pinned_version` |
| T17 | `test_execution_schema.py` constraint tests, `test_execution_store.py::test_failed_operation_leaves_no_partial_write` |
| T18 | `test_delivery_guarantees.py::test_t18_configuration_can_be_saved_while_smtp_is_in_progress` |

All ten audit reproductions were added as `xfail(strict=True)` before any fix (commit `927e5de`) and lost the marker in the stage that made them pass; none keeps it.

## 5. Failure matrix

All rows of the plan's matrix (7.3) pass; the mapping to tests is in the ADR, section 5, "Stage 7 (failure matrix)". Process crashes are real: a separate process exits with code 86 at the crash point (the test asserts the code, proving the point was reached) or is killed while the sink holds its connection. The passage of a lease is simulated by moving `lease_expires_at` into the past. In every row the sink accepted each message at most once, and every delivery the database could not prove ended `UNKNOWN`.

## 6. Migration

Automated: `test_execution_schema.py` migrates databases left by every released version 1-4 (frozen schema `tests/fixtures/schema_v4.sql`) and compares the result with a fresh database (columns, foreign keys, indexes, partial-index predicates); checks idempotent `init_db`, resumption after a crash inside migration 5, and the legacy rows of migration 6 (`test_legacy_state_blocks_only_its_own_window`).

Manual run for this report, on a version-4 database with five legacy tasks:

| Task | Legacy `last_status` / `last_run` | After migration |
|---|---|---|
| OK | SUCCESS at 09:00 inside 06:00-18:00 UTC | `SUCCEEDED`, legacy, key `S1@2026-10-17T06:00:00+00:00` |
| PART | PARTIAL at 09:00 | `NEEDS_REVIEW`, legacy, same key |
| OUT | SUCCESS at 03:00, outside every window | no execution; event `legacy_state_dropped` |
| BAD | FAILED with an unparsable time | no execution; event `legacy_state_dropped` |
| NEW | never sent | nothing |

`user_version` went from 4 to 6; a second `init_db` changed nothing; five identities and five versions were created, one schedule version.

## 7. Generative tests (Hypothesis)

`tests/test_execution_model.py`: `RuleBasedStateMachine` against an independent plain-Python model; `derandomize=True`, `database=None`, `max_examples=500`, `stateful_step_count=60`, `print_blob=True`. Last run: 500 passing, 0 failing, 22 invalid examples (sequences rejected before running, for example a rule whose bundle was still empty); typical runtime 4-24 ms per example.

Seeds: derandomised runs derive their examples from the test's source, so every run of the same source is identical; nothing random needs to be stored. A failure prints the shrunk sequence of steps and a `@reproduce_failure` blob, which replays it exactly. Because a change to the test file changes the examples, rare states are made common on purpose: a second attempt ending in an exhausted budget occurs in about 6% of the examples (measured with temporary `event()` calls, not committed).

## 8. Mutation checks

Local changes to the code, one at a time, each followed by the relevant tests; the code was restored after each one and none was committed.

| Area | Mutation | Caught by |
|---|---|---|
| repository | no fencing-token check | `test_fencing_token_rejects_an_old_claim_of_the_same_owner`, model |
| repository | accepted recipients attempted again | 5 repository tests, model |
| repository | no busy check for a new execution | `test_only_one_owner_per_task`, generations test |
| repository | late result accepted after recovery | `test_an_attempt_result_is_recorded_once` (added after this mutation first survived) |
| repository | no window-end check | `test_no_attempt_starts_after_the_window_end` |
| repository | retry budget ignored / backoff ignored | model, `test_transient_recipient_waits_for_backoff_then_is_retried_alone` |
| repository | pending work outweighs UNKNOWN | model |
| repository | takeover ignores another live owner | model (manual keys), 3 repository tests |
| repository | attempt on an expired lease | model, `test_no_attempt_starts_on_an_expired_lease` |
| repository | recovery makes in-flight recipients sendable | 9 failure-matrix and repository tests |
| mailer | lost reply after DATA treated as transient | 3 mailer tests |
| mailer | login refusal classified by its code | `test_login_refusal_says_nothing_about_recipients` |
| mailer | 8-bit snapshot | `test_snapshot_is_7bit_*` |
| mailer | no RSET after all recipients refused | `test_all_refused_sends_no_data_and_keeps_the_connection` |
| mailer | pool keeps a client after any error | `test_pool_drops_a_client_after_any_error` (added after this mutation first survived) |
| pipeline | claim result ignored | T01-T05 and others (5 tests), T02 |
| pipeline | earlier outcome in the window not carried | T04, lost reply, migration 6 |
| pipeline | dry run writes execution state | 7 tests |
| pipeline | message rendered from today's task, not the pinned one | `test_taken_over_execution_sends_the_pinned_version` (added after this mutation first survived) |
| pipeline | database error after SMTP reported as success | 3 failure-matrix tests |
| pipeline | transaction held during SMTP | T18 (`database is locked`) |
| projection | result of an old version updates `last_status` | `test_old_version_result_does_not_update_the_projection` |
| reports | earlier reports not retried / no budget / unknown retried | `test_reports.py`, T13 |
| panel | attempt id not checked / actor not recorded | `test_web_executions.py` |
| retention | review executions purged / period ignored / not applied by a run | `test_retention.py` |

One mutation survives on purpose: removing the owner check from `record_attempt_result`. The check is redundant, because a result is stored only for an attempt still `IN_FLIGHT` under the caller's fencing token, and every takeover first turns in-flight attempts into `UNKNOWN`. It is kept as a second barrier (ADR, stage 7).

## 9. Known limits and open items

- Exactly-once delivery is impossible between SQLite and SMTP; the system guarantees at-most-once whenever the outcome is ambiguous and makes such cases visible (`UNKNOWN`, exit code 3, the Executions pages).
- Rate limits are per window occurrence and per process, not for the SMTP account (ADR D18).
- "Send again" on an `UNKNOWN` recipient works for scheduled executions while their window occurrence is open; a manual send is repeated with the Run button.
- The CI matrix was reproduced locally (section 2a); the GitHub runs of the fixed commit and the first `race-stress` run on GitHub runners are still to be checked.
- The 22 invalid Hypothesis examples are rejected sequences, not failures; they are listed so that a change in that number is noticed.
