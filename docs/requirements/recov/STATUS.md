# RECOV status — 2026-11-25

## Review correction round — base 18929e7e

Completed the four requested corrections in this worktree; no commit (sandbox instruction), VERSION bump, runtime
catalogue change, schema/record-shape change, or change to another worktree. Requirements were updated first, then
tests run red before the two behavioral fixes. No assertion present at the review base was changed or removed;
[assertions.md](assertions.md) also records two corrections to initial drafts of the newly added tests.

All test names below are in `codex:tests.test_recovery_line`.

| Item / requirement | Test / change | Red → green evidence |
| --- | --- | --- |
| 1 / RECOV-2, E2a-7 | `BackoffTests.test_release_then_crash_before_exact_leaves_collector_pending_instead_of_later_B6_retry` | Red on review base: ledger stays `exact`, due **86531000**, after committed release resets effective due to **86401000**. Green: ledger stays `pending` with the earlier due when transaction settlement is lost; draining cannot restore stale exact. Release now calls `wake_before` with the held orchestration's due before resetting B6; corrected release comment. |
| 2 / RECOV-1, RECOV-3 | `RecoveryTests.test_persistently_failed_substeps_do_not_renew_progress_and_warning_returns_at_threshold` | Red on review base for all seven sub-steps: each advanced the check through persistent failures. Green for `end_earlier`, `end_idle`, `settle_earlier_registrations`, `release_earlier_unarmed_leases`, `expire_result_leases`, `_adopt_committed`, and `record_pass`: three failed passes complete cleanup requests without renewing; null at threshold − 1 ms, exact E2c-9 warning at threshold. Pass renews only when every wrapped maintenance step succeeds. RECOV-1/3 text updated. |
| 3 / RECOV-2 query documentation | `next_hold_due` docstring; existing `BackoffTests.test_B6_backoff_is_published_and_other_holds_and_release_reset_still_win` | Documentation-only correction: ordinary minima use indexed deadlines, orchestration expression scans capped open orchestrations. The review base docstring already omitted “index seeks”; made the distinction explicit. Existing query regression stays green; no index change or behavioral red claimed. |
| 4 / RECOV-1, RECOV-3 | `RecoveryTests.test_real_hold_transaction_between_passes_preserves_without_renewing_the_floor` | Coverage-only addition, already green on review base. Real `reserve_result` on the history writer commits real `take_hold` between passes; floor survives, warning returns at original threshold, next successful pass renews it. Fault-sensitivity red: removing only the transaction publication floor in memory drops **1795572640229** to **1795572520223**. Restoring production behavior is green. |

Red evidence: corrected fixtures on unchanged review-base runtime, **10 tests, 8 assertion failures, 10.066 s**
(one crash test and seven sub-step subtests); `metadata_errors: []`. Initial drafts additionally exposed one
exclusive-writer fixture error (10 tests, 8 failures + 1 error, 4.423 s), corrected before implementation.
Floor fault campaign: named RECOV module, **10 tests, 2 failures, 4.951 s**, including the real-transaction floor
assertion and the pre-existing synthetic-write floor assertion. The mutation existed only in process memory.
Final unmodified runtime: RECOV module **10/10 pass, 4.9 s worker time**, `metadata_errors: []`.

Named validation (no full run):

- `tests.test_recovery_line`, `tests.test_b6_unattended_close`, `tests.test_project_holds_schema`: **91/91 pass**;
  worker times **5.1 / 7.6 / 1.4 s**, batch elapsed **7.6 s**, job-seconds **14.020**;
  `metadata_errors: []`, no budget failures.
- `tests.test_b2_service_outcome_sweep`: **19/19 pass, 10.369 s** (worker 11.1 s), including
  `test_the_cleanup_runs_even_when_the_record_pass_fails_unexpectedly`.
- `tests.test_result_persistence`: **25 tests, 6 failures, 11.229 s**. Loading the two edited runtime modules from
  **18929e7e** in memory and running the same named module gives the same six names and failures,
  **25 tests, 6 failures, 10.480 s**: actual client usage, lost commit reply, full-sized body, original assignment,
  slow database/second intake, validator reason. These native child/provider fixture failures predate this round.
- `tests.test_e2a_wake_wire`: **31 tests, 6 failures + 6 errors, 21.134 s**; socket binds fail with
  `PermissionError: [Errno 1] Operation not permitted`, and process inspection is denied. All non-socket cases
  pass. Real collector/wire/crash scenarios still require an unsandboxed rerun; the new release-crash test uses
  the real ledger directly and passes here. No pty-dependent check was needed or claimed.
- `tests.test_runtime_release`: expected **1 failure / 3 tests, 0.012 s**: recorded digest remains
  `152fc5bf7e3df433f177baf8ea2418c6835598dd840eb4546c296930b85c86d2`, runtime is now
  `5a6dfd177d09f750de0e557abfde9d897ccd3406d9db097c7655993a35c51b63`. Primary owns catalogue/version at merge.
- The latter four-module batch had `metadata_errors: []`, no budget failures, **21.6 s elapsed / 44.961 job-seconds**.
  Existing module registration is unchanged; new checks and exact requirement statements are synchronized in the
  registry/source JSON. No module was added or renamed. No schema/copier/backup/move checks required. All runs ended.

Round files: `project_holds_schema.py` (items 1, 3); `result_persistence.py` (item 2);
`packages/codex-agents/tests/test_recovery_line.py` (items 1, 2, 4); `docs/requirements/recov.md`,
`tests/requirements/recov.json`, `scripts/test-requirements.json` (requirements/check mappings);
`docs/requirements/recov/assertions.md` and this file (evidence).

Unsandboxed rerun with the supplied venv: `"$RECOV_PY" -B scripts/parallel_tests.py --module
codex:tests.test_e2a_wake_wire --module codex:tests.test_result_persistence`.
The original E2c-9 collector scenario remains unverified here as noted below; this round did not rerun
`tests.test_project_start`, the full suite, live probes, or installation.

## Original implementation evidence — 18929e7e

Implemented in `fix/recovery-line`, based on `cbc757d5` (0.8.60). Changes remain uncommitted as instructed for this
sandbox. Requirements preceded regression tests; failing tests preceded each behavior change. No existing test
assertion was edited or removed; see [assertions.md](assertions.md). VERSION and runtime-releases.json are unchanged.

## Cause and evidence

This investigation uses the supplied live observations and an isolated reproduction; it did not access or change
the live installation, live databases, or running services (builder rule).

1. At the base, `project_holds_schema.next_hold_due` takes the minimum raw `due_at_ms` over **all** held/releasing
   kinds, plus open-withdrawal retries. A reservation's deadline is its original `reserved_until_ms`;
   `project_history.arbitrate_publication` and `prepare_account_settlement` fence it as `releasing` without changing
   that deadline. A verified account closure, not elapsed project time, releases the obligation. Hence a past
   reservation deadline can remain the minimum indefinitely while the project service works normally.
2. This also applies to evidence waits: armed leases do not expire by time (`expire_result_leases` selects only
   reserved/unarmed leases), and unfinished custody cannot be discarded merely because its lease deadline passed.
   Ignoring these holds entirely would lose conservative wakeups after a stop, so this fix retains them.
3. The held account binding's future `next_eligible` comes from `host_run_bindings.expires_at_ms` in
   `LearningLedger.account_holds_status`. That separate account timer neither moves the project reservation's
   deadline nor participates in `start_record`. The final regression uses a real held account binding with future
   eligibility, zero held run slots, and a project reservation put into `releasing` by the actual arbitration method.
4. B6 is another contributor: `OrchestrationOwner.end_idle` requires both `due_at_ms` and `next_check_ms` to be due,
   but the old wake query ignores the second condition. Worse, `_back_off` did not dirty/invalidate the publisher,
   so merely correcting the query would still leave the old publication in the collector. The controlled-clock
   reproduction shows raw due **86401000**, current time **86521000**, retry **86531000**: the old code publishes
   **86401000**, the fix publishes **86531000**. An earlier independent hold still wins; actual execution-hold
   release resets B6's next check through the existing release helper.
5. `WakePublisher` publishes that minimum as `next_due_at_ms`; `LearningLedger.publish_wake` copies it directly to
   `project_wake.due_at_ms`; `start_record` returns it unchanged. `recovery_line` checks serving + due at least
   60 seconds past, without inspecting warm-up, account health or run slots. The sender also deduplicates identical
   exact values. Thus status reads cannot repair the past deadline, and neither can repeatedly computing the same
   minimum. B6 backoff alone cannot explain/fix a persistent overdue releasing reservation.

The fix publishes progress after validated writer startup and each completed periodic writer maintenance pass:
live exact due = max(actual outstanding check, last completed writer check), retaining null and future deadlines.
Ordinary writes retain that floor but never renew it. No sender heartbeat, status probe, or failed pass supplies
progress. The formatter and its 60-second E2c-9 predicate are unchanged. Clean stop bypasses the live floor and reads
the actual outstanding deadline. No database schema, persisted record shape, reason code, wire field or operation
was added; copier/backup/move/enumeration runs are consequently not required by the schema-change rule.

## Requirement → test → evidence

All names below are in `codex:tests.test_recovery_line`. Final module: **7/7 pass, 3.8 s**,
`metadata_errors: []`.
Final tests also run against the unchanged HEAD versions of the four edited runtime modules loaded in memory:
**6 failures / 7 tests, 3.078 s**; the negative failed-maintenance test passes at both base and fix. This confirms
the final fixtures (including the future account binding) still reproduce the installed behavior without modifying
the worktree or another checkout. Original red runs also preceded implementation: 3 assertion failures + 1 missing
helper / 5 tests (0.919 s), then the added startup test red (1/6, 1.247 s), then unrelated-write floor test red
(1/7, 1.255 s).

| Requirement | Test | Evidence |
| --- | --- | --- |
| RECOV-1 | `RecoveryTests.test_successful_writer_passes_keep_the_live_releasing_reservation_healthy` | Red: unresponsive after successful writer pass. Green: 130 seconds of passes and repeated reads return null; actual account binding held/future, run slots 0. |
| RECOV-1 | `RecoveryTests.test_initial_validated_writer_open_gives_the_service_its_first_check` | Red before startup hook; green after initial validated writer open. |
| RECOV-1, RECOV-3 | `RecoveryTests.test_unrelated_writes_and_sender_reads_neither_erase_nor_renew_progress` | Red: unrelated write restores old due. Green: retains check floor, warning still returns at original threshold. |
| RECOV-2 | `BackoffTests.test_B6_backoff_is_published_and_other_holds_and_release_reset_still_win` | Red: 86401000 instead of 86531000. Green: exact and clean_stop publish retry; earlier independent hold and release reset preserved. |
| RECOV-3 | `RecoveryTests.test_stopped_progress_returns_the_exact_E2c_9_warning_at_the_threshold` | Red: pass never advances old due. Green: null just before threshold, exact E2c-9 warning at and after threshold. |
| RECOV-3 | `RecoveryTests.test_failed_maintenance_does_not_supply_progress` | Green on base/fix: failed writer pass keeps old due and warning. |
| RECOV-1, RECOV-3 | `RecoveryTests.test_future_and_null_deadlines_and_clean_stop_are_preserved` | Base red; green: original releasing hold/deadline retained, clean_stop publishes raw past due, future/null unchanged. |

## Named validation and limits

- `tests.test_b6_unattended_close`: **41/41 pass, 7.482 s** on final runtime.
- `tests.test_project_holds_schema`: **40/40 pass, 0.873 s** on final runtime.
- `tests.test_e2a_wake_wire`: 31 tests, 6 failures + 6 errors, 20.165 s; all non-socket cases pass. Direct AF_UNIX
  bind independently fails with `PermissionError: [Errno 1] Operation not permitted`; collector launch/wire cases
  need an unsandboxed rerun. No existing E2a assertions changed.
- `tests.test_project_start`: 38 tests, collector scenarios fail to start in this sandbox (AF_UNIX and `/bin/ps`
  denied). **Existing real-collector E2c-9 is not verified green here.** Its assertions are unchanged; the new
  real-writer/ledger tests verify the preserved line and exact timing. Initial named batch: 178 tests, 21.8 s.
- `tests.test_result_persistence`: 25 tests, 6 failures, 12.803 s. The **same six test names/failures** occur with
  unchanged HEAD runtime loaded in memory (25 tests, 10.383 s): provider usage, lost commit reply, full sized body,
  original assignment return, slow database/second intake, validator reason. These exercise native child/provider
  execution unavailable here; the RECOV tests run the actual maintenance writer successfully.
- `tests.test_runtime_release`: expected **1 failure / 3 tests, 0.011 s**: catalogue still records
  `152fc5bf7e3df433f177baf8ea2418c6835598dd840eb4546c296930b85c86d2`, final runtime is
  `35b8775e3832b41ef40085cee03c71c76cd1e3fc795ff10699cd80abf0148c67`. Primary owns version/catalogue update at merge.
- New module registered as scenario/integration with timing and requirement mappings. Heavy fixture/schema/digest
  imports are deferred to `setUpModule`, so importing/discovering the new module does no persistence/file reads.
- No full suite, no live probes, no install, no sockets/pty workaround, no commit. All launched test runs finished.

Rerun outside the sandbox using the supplied test venv (set `RECOV_PY` to its Python path):

```sh
"$RECOV_PY" -B scripts/parallel_tests.py --module codex:tests.test_recovery_line --module codex:tests.test_b6_unattended_close --module codex:tests.test_project_holds_schema
"$RECOV_PY" -B scripts/parallel_tests.py --module codex:tests.test_project_start --module codex:tests.test_e2a_wake_wire --module codex:tests.test_result_persistence
```

Files changed: runtime `project_holds_schema.py`, `project_wake.py`, `orchestration_store.py`,
`result_persistence.py`; new `tests/test_recovery_line.py`; `scripts/test-{layers,stages,timings,requirements}.json`;
new `tests/requirements/recov.json`; `docs/requirements/recov.md`, `recov/assertions.md`, and this status file.
