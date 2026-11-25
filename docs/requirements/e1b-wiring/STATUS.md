# E1b wiring status

Base `afb6f610` (0.8.66), branch `feat/e1b-wiring`. Implementation is complete;
real socket/worker validation remains blocked in this sandbox. No commit was
attempted. VERSION and the runtime digest catalogue are unchanged. No schema,
persisted record shape, wire operation or refusal code changed.

## Requirement → test → evidence

Names below belong to `tests.test_ledger_owner_wiring.LedgerOwnerWiringTests`.
The initial nine checks ran red before runtime implementation (eight missing
adapter errors and one measured-default failure). Two additional regression
checks independently exposed signature preparation and sweep-containment bugs
before their fixes. Details and sanitized results: `test-evidence.json`.

| ID | Tests | Evidence |
| --- | --- | --- |
| E1b-1 | `test_sql_trace_without_transport_covers_wakes_sweeps_restart_and_close`; `test_all_sql_from_open_through_close_has_one_owner` | In-process SQL trace passes: construction, views, wakes, sweep, starter, restart and close all use one thread. Real socket variant blocked by AF_UNIX bind permission. |
| E1b-2 | `test_transaction_rechecks_are_one_request_and_revocation_rolls_back`; `test_real_original_open_rechecks_and_rolls_back_within_one_request` | Pass. Original open's final authorization runs inside its transaction, rollback removes pending identity, next request cannot interleave. |
| E1b-5 | `test_handler_refusal_preserves_answer_and_next_request_runs`; `test_real_account_handler_keeps_failure_answer_and_processes_next_request`; `test_sweep_failure_keeps_existing_containment_and_owner_serves_next`; `test_fatal_sql_error_reaches_owner` | Pass. Existing collector/account refusal answers preserved, sweep failures contained, next request runs, fatal SQL reaches C10 with `OperationalError`. Sweep check independently red → green. |
| E1b-6 | `test_paused_owner_leaves_project_health_responsive`; `test_exit_is_recorded_after_final_handled_request`; `test_published_health_evicts_past_project_limit` | Pass. Published project health answers during paused work, real stop pump acknowledges final producer work before C10 writes exit, ninth project evicts an old snapshot. |
| E1b-8 | `test_preparation_does_not_occupy_owner_and_commit_rechecks_authority`; `test_signature_preparation_yields_owner_and_changed_generation_refuses_commit`; original-open check above | Pass. Paused protected-copy inspection/signature verification permits unrelated SQL; revoked project/replaced signer refuses publication. Signature check independently red → green. |
| E1b-3/4/7 | `test_measured_queue_defaults`; `test_full_queue_returns_existing_account_busy_and_retry_succeeds`; existing queue modules | Pass. Defaults 12/8/6/800 ms; full queue gives existing counted account busy, retry succeeds; event/handled-driven custody and fairness checks retained. |

## Validation

- Final registered wiring + lifetime guard run: 19 tests, 18 pass, one socket
  block; 1.5 s execution. New wiring module: 15 tests, 14 pass, one socket block.
  **`metadata_errors: []`**, no budget failures.
- Registered eight-module run (wiring, queue, queue lifecycle, waits, SQL read
  budget, startup, restart, lifetime guard): 98 tests, 97 pass, same socket
  block; 1.7 s execution, **`metadata_errors: []`**.
- Registered `tests.test_learning_store`: 100 tests, 95 pass, five socket cases
  blocked at bind; 6.8 s execution, **`metadata_errors: []`**.
- Final targeted named unittest selection: 28 tests pass in 6.733 s. Includes
  unchanged C10 main-loop tests, original-open pending tests, ingress limits,
  busy answers, and the real account/open wiring checks.
- `tests.test_runtime_release`: three pass; the current-version digest pin
  fails as explicitly expected. Runtime file inventory passes with the new
  owner module included. VERSION and release digests were not edited.
- Earlier broader named run stopped and reaped at 126 s because socket and
  reviewed worker-sandbox restrictions caused stalls. Completed waits, busy
  answers, startup, restart, SQL budget and lifetime modules passed. Account
  API's three socket cases and one lock-limit socket case were blocked.
  Full account ingress/original-open worker modules remain incomplete.
- `git diff --check` passes. No full suite was run. No tests remain in flight.

Run the following on an owner machine with a reviewed worker sandbox and local
sockets. `$PY` denotes the agreed test venv; use a short disposable TMPDIR.

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_ledger_owner_wiring --module codex:tests.test_account_host --module codex:tests.test_account_ingress --module codex:tests.test_account_original_open --module codex:tests.test_collector_exit
$PY -B scripts/parallel_tests.py --module codex:tests.test_learning_store --module codex:tests.test_ledger_lock_limits --module codex:tests.test_health_contention --module codex:tests.test_account_api
```

No PTY tests were attempted. Full-suite, installed lifecycle/soak and Linux
validation belong to the primary. No schema/record change triggered the copier,
backup, restore or enumeration expansion rule.

## Files and assertion changes

Runtime prefix: `packages/codex-agents/native-package/.codex/orchestration/`.
Changed `collector_host.py`; under `codex_agents/`: new `ledger_owner.py`, plus
`ledger_owner_queue.py`, `ledger_waits.py`, `learning_store.py`,
`sql_read_budget.py`, `runtime_files.py`, `account_host.py`, `account_service.py`
and `account_ingress.py`.

Tests: new `test_ledger_owner_wiring.py`; existing queue default assertions and
counter fixture; `lifetime_caps.json`. Metadata: layers, stages, timings and
requirement catalogue; `tests/requirements/e1b-wiring.json` and measured-default
statement in `e1b-queue.json`. Docs: wiring map, this status, evidence, assertion
record, and standalone queue's now-superseded provisional sizing paragraph.

Exact assertion replacements are in `assertions.md`: production defaults
16/8/2 → 12/8/6 and maximum 16 → 12, plus an explicitly configured two-slot
counter fixture whose assertions stay unchanged. No other existing assertion
was changed or removed. The health snapshot cap is registered with its ninth
project eviction test.

## Fix round (status regression)

Found by the primary's affected run: `test_learning_cli` `test_status_reports_unregistered_project_without_identifiers`
failed (`'unregistered' != 'unavailable'`) on this branch and passed on 0.8.66.

Cause: `CollectorServer._published_project` answered a project with no published snapshot by offering a refresh and
returning nothing, which the health handler turned into `ledger_busy`. An unregistered project can never be published
(the refresh raises `ProjectNotRegisteredError`), so it stayed busy forever and the CLI reported `unavailable` instead of
the `project_unregistered` answer. Any registered project not yet published (cold) also changed from a full answer to busy.

Fix: for a cold project only, `_published_project` waits on its own refresh for at most `HEALTH_LEDGER_WAIT_MS` (the old
ledger-lock wait), so a registered project answers in full and the refusal reaches the handler's existing
`project_unregistered` mapping. Published projects are still read without any owner wait (E1b-6, paused-owner test
unchanged). Late or refused refresh stays `ledger_busy`. No new wire code or field.

New checks in `tests.test_ledger_owner_wiring` (E1b-2, E1b-5, E1b-6):
`test_health_for_unregistered_project_keeps_its_unregistered_answer` (red before: busy shape; green after),
`test_health_for_registered_project_not_yet_published_answers_in_full` (red before: `ledger_busy`; green after),
`test_health_for_registered_idle_project_keeps_its_answer_shape` (pinned; already passed, red only on the wrong
first expectation while writing). Stale-collector and missing-ledger answers come from the client side and file reads, not
this path; they are covered by the unchanged `test_learning_cli` modules, which pass. No existing assertion changed.

Run: `parallel_tests.py --jobs 8` over test_learning_cli, test_ledger_owner_wiring, test_ledger_owner_queue,
test_ledger_owner_queue_lifecycle, test_collector_exit, test_account_ingress, test_ledger_busy_answers, test_e1a_evidence:
failed_units [], metadata_errors [], 155 codex tests. VERSION and digests untouched.
