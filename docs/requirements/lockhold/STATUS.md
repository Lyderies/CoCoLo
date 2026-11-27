# LOCKHOLD status

## Fix round

Review findings MED-1, LOW-1 and LOW-2 are the fixed scope. Actual starting HEAD
is `0277f32c` (the reviewed implementation), directly after base `51623740`.
Requirements and assertion replacements are recorded before tests and runtime
edits. Changes remain uncommitted in the sandboxed worktree. VERSION and the
runtime catalogue are unchanged; no schema, index or record-shape change.

Tests below are in `tests.test_lockhold`:

| Review item / requirement | Test | Red evidence before runtime edits | Green evidence |
| --- | --- | --- | --- |
| MED-1 / LOCKHOLD-1 | `LockHoldRetentionTests.test_LOCKHOLD_1_persistent_batch_failure_waits_for_full_interval` | Persistent SQLITE_FULL after the first commit caused 20 attempts at simulated times 1.00 through 1.19 s, rather than four attempts over three intervals. | Attempts at 1.00/1.01/2.00/3.00 s: first commit, failed continuation, then one failed batch per full interval. Each failed batch is 500 rows, rolls back with no open transaction, and releases the lock before waiting. Only the initial 500 events are summarised/removed. |
| MED-1 / LOCKHOLD-1 | `test_LOCKHOLD_1_failed_batch_rolls_back_and_restarts` | Failed second batch returned `"more"` instead of `None`. | Returns `None`, clears pending events, performs no idle retry, and resumes on the same collector's next full cycle. A new collector completes all 2,501 events without duplicate summaries. |
| LOW-1 / LOCKHOLD-2 | `LockHoldReadTests.test_LOCKHOLD_2_pinned_lookup_classifies_only_index_errors` | Three subcases leaked OperationalError: real missing index, injected missing index, and injected no-query-solution. | All become plain LedgerError with reopen guidance; locked, I/O, interrupted and non-OperationalError cases propagate the identical original exception. |
| LOW-2 / LOCKHOLD-1 | `LockHoldRetentionTests.test_LOCKHOLD_1_appends_between_batches_match_unbounded_drain` | Baseline guard already passed: this finding requested missing coverage, not a runtime correction. | Fresh hook and app-server events appended between batches survive; all 2,501 expired events are summarised exactly once, each later hold removes at most 500, and the complete records table equals the former unbounded drain with the same append ordering. |

Red named run: 9 tests, two assertion failures and three error subcases, 4.90 s
worker elapsed, `metadata_errors: []`. Green named run: `test_lockhold` (9 tests,
4.36 s) plus `test_lifetime_cap_guard` (4 tests, 0.71 s): **13 pass**, 4.36 s
elapsed, `metadata_errors: []`, all three LOCKHOLD requirements passed.
The existing module stays scenario/integration; all three added checks are
registered under the existing requirement IDs. No module or requirement ID added.
Path-free structured evidence is in [fix-round-evidence.json](fix-round-evidence.json).

Assertion pairing: failed-batch continuation `"more"` → `None`, pending flag false,
no idle retry and recovery on the next full cycle, as documented in
[assertions.md](assertions.md). Other existing assertions are retained.
The lifetime-cap BOUNDED end now explicitly includes a failed batch and its
full-interval backoff.

Named regressions completed: **53 pass**, two sandbox socket errors and one
expected runtime-release assertion failure (56 tests, 16.02 s elapsed;
`metadata_errors: []`, no budget failures). No test run is left in flight.

| Named module | Fix-round result |
| --- | --- |
| `test_event_retention` | 21 pass; `CollectorSweepTests.test_the_collector_applies_retention_by_itself` errors because socket bind is denied; 2.30 s |
| `test_ledger_lock_limits` | 26 pass; `LedgerLockLimitTests.test_E1a_3_refusals_are_published_with_collector_health` errors because socket bind is denied; registered workers 9.65/16.01 s |
| `test_type_lookups` | 4 pass; 1.16 s |
| `test_runtime_release` | 2 pass; expected `test_current_runtime_is_recorded_for_the_current_version` failure because VERSION/catalogue stay unchanged; 0.22 s |

The two socket-backed tests need an unsandboxed owner rerun: `bind` is denied
with PermissionError (Operation not permitted), including with the short TMPDIR.
No full suite or standalone large-data measurement was run for this scoped
correction; no PTY tests are needed. No copier/backup/move/enumeration gate is
required because schema and record shapes are unchanged.
Reproduce from the repository root with the handed-off test Python in `$PY`:

```sh
"$PY" -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_lockhold --module codex:tests.test_lifetime_cap_guard
TMPDIR=/tmp/lockhold-r1 "$PY" -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_event_retention --module codex:tests.test_ledger_lock_limits --module codex:tests.test_type_lookups --module codex:tests.test_runtime_release
```

Fix-round files: `learning_store.py`, `test_lockhold.py`, `lifetime_caps.json`,
`scripts/test-requirements.json`, `docs/requirements/lockhold.md`, and this unit's
`assertions.md`, `STATUS.md`, `fix-round-evidence.json`.

## Original round evidence (before this fix round)

Implemented on `fix/lockhold`, base `51623740`. No commit (sandboxed worktree).
VERSION, the digest catalogue, schema version and record shapes are unchanged.
No new indexes: the provider lookup explicitly selects `records_type_project`.

Requirements and the existing assertion replacement were written before the
runtime changes. The final tests were also run against the original runtime in
a fresh process: five intended failures, one baseline parity guard passed.

| Requirement | Test (in `tests.test_lockhold`) | Red evidence | Green evidence |
| --- | --- | --- | --- |
| LOCKHOLD-1 | `LockHoldRetentionTests.test_LOCKHOLD_1_backlog_drains_across_bounded_holds_with_identical_final_state` | First cycle left 0 of 2,501 events instead of 2,001. | Six holds remove 500/500/500/500/500/1; final database records exactly equal the former full drain. |
| LOCKHOLD-1 | `test_LOCKHOLD_1_failed_batch_rolls_back_and_restarts` | First cycle returned no continuation. | Second-batch deletion fault rolls back all of that batch; earlier commit remains; a new collector drains the remaining rows without duplicate summaries. |
| LOCKHOLD-1 | `test_LOCKHOLD_1_loop_resumes_batches_and_stop_cancels_the_gap` | All 2,501 removals shared one hold. | Actual scheduled loop drains across six holds with five unlocked 10 ms gaps before the next full interval; a stop during the gap leaves the remaining 1,001 rows. |
| LOCKHOLD-2 | `LockHoldReadTests.test_LOCKHOLD_2_status_and_project_health_have_flat_vm_work` | 100→2,100 unrelated events: status 2,296→42,275 VM steps; project health 2,963→42,603. | Flat-work assertions, growing former-scan control and exact reply comparisons all pass. |
| LOCKHOLD-2 | `test_LOCKHOLD_2_status_answers_match_former_lookup` | Baseline guard passed. | Both providers: absent identity, signed evidence, invalid signatures, restored signatures and removed identity give the former answers. |
| LOCKHOLD-3 | `test_LOCKHOLD_3_open_admission_has_flat_vm_work_and_same_decision` | Both preflight and write-time checks: 833→10,838 steps. | Both real-open checks are flat and equal the former decision; the growing control still fails the bound. |

New tests plus `test_lifetime_cap_guard`: **10 tests pass**, 3.2 s worker elapsed,
`metadata_errors: []`, all three LOCKHOLD requirements passed. The existing
500-event count limit is registered with its new resumable end and a
2,501-event past-the-limit test. The new module is scenario/integration.
Path-free per-test results are recorded in [test-evidence.json](test-evidence.json).

The only existing assertion change is documented in [assertions.md](assertions.md):
one-cycle complete drain → bounded cycles with the complete-drain and summary-total
checks retained. All other existing assertions are unchanged.

The standalone [measurement](measurement.json), generated by
`scripts/measure_lockhold.py`, uses 1,000/100,000 unrelated synthetic events and a
20,000-event retention backlog; it opens no sockets and reads no live ledger:

| Read | Indexed VM steps (1k→100k) | Former-scan control (1k→100k) |
| --- | --- | --- |
| Provider attestation status | 139→133 | 20,196→2,000,191 |
| Health with project | 821→461 | 20,519→2,000,519 |
| Admission evaluation | 440→355 | 5,366→500,366 |

Every compared answer is identical. Retention: former one hold removes 20,000
events in 2,650.9 ms; replacement has forty 500-row holds, measured cycles
60.5–82.2 ms plus a final empty check. Final records are byte-for-byte identical
and no transaction remains open. These are synthetic local timings, not a repeat
of E0b-v2's concurrent 310 MB socket load; VM counts and row bounds are the gates.

The named regression run completed 12 workers / 236 tests: **216 passed**,
19 socket errors and the expected runtime-catalogue assertion failed. It was
interrupted at 165 s because the remaining `test_admission_history_growth` worker
was repeatedly spending its 10-second signature-worker polling deadline, rather
than allowing the named run to grow into a long suite run. The runner reaped its
worker group; nothing is left running. Its interrupted record is retained in the
path-free evidence, not reported as a clean metadata gate.

| Named module | Result |
| --- | --- |
| `test_providers` | 106 pass across its two registered workers |
| `test_account_observation` | 19 pass |
| `test_launch_signature_preparation` | 17 pass |
| `test_type_lookups` | 4 pass |
| `test_learning_retention` | 15 pass |
| `test_provider_restrictions` | 4 pass |
| `test_ledger_lock_limits` | 26 pass; one socket-bind error |
| `test_event_retention` | 21 pass, including the replacement bounded-cycle test and all atomicity/weighted-policy assertions; one socket-bind error |
| `test_health_contention` | 2 pass; 17 socket-bind errors |
| `test_runtime_release` | 2 pass; expected `test_current_runtime_is_recorded_for_the_current_version` failure |
| `test_admission_history_growth` | incomplete; fail-fast first case has the same signature-job polling timeout on unchanged base and changed runtime |

The diagnostic command `PYTHONPATH=../../scripts "$PY" -B -m unittest -f
tests.test_admission_history_growth` was run from `packages/codex-agents` against
both runtimes. In both runs,
`AdmissionCaptureLimitTests.test_C1_a_claim_at_the_byte_limit_is_claimed` timed out
in `OriginalAccountOpenJobTests.pump_wire` at its 10-second polling assertion
(20.93 s changed-runtime run, 20.63 s unchanged-base run including cleanup).
The remaining cases were not retried here; the full named module needs a working
signature-worker environment. The changed runtime was restored after the baseline
diagnostic and verified byte-for-byte against the implementation used for green
tests and measurement.

Socket-backed cases refuse `bind` with `PermissionError: Operation not permitted`
in this sandbox; they need an unsandboxed owner run. The runtime-release failure
is expected because runtime files changed while VERSION/catalogue remain
untouched. No full suite was run. No PTY test was requested or needed. There is no
schema/index addition or record-shape change requiring the copier/backup/move gate.

Reproduce the focused gates from the repository root with the handed-off test
Python in `$PY`:

```sh
"$PY" -B scripts/parallel_tests.py --jobs 2 --module codex:tests.test_lockhold --module codex:tests.test_lifetime_cap_guard
"$PY" -B scripts/run_measurement.py scripts/measure_lockhold.py -- --out docs/requirements/lockhold/measurement.json
```

The remaining environment gates for an unsandboxed primary:

```sh
"$PY" -B scripts/parallel_tests.py --jobs 4 --module codex:tests.test_event_retention --module codex:tests.test_ledger_lock_limits --module codex:tests.test_health_contention --module codex:tests.test_admission_history_growth
```

Files changed: `learning_store.py`; `test_lockhold.py`, `test_event_retention.py`,
`lifetime_caps.json`; `scripts/test-{layers,stages,timings,requirements}.json`;
`tests/requirements/lockhold.json`; `scripts/measure_lockhold.py`; this unit's
requirements, assertion note, status, test evidence and measurement.
