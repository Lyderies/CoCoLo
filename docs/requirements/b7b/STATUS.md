# B7b correction-round status — 2026-11-25

Base: `37ebd229df05321db70b022b67bf200e0d08e511`, worktree `cocolo-b7b`.
Exactly the six READY-WITH-CORRECTIONS items were addressed. Changes remain
uncommitted as requested for this sandbox. No other worktree or running service
was touched. No schema/record/wire-shape change; `project_start.py`, VERSION and
the runtime digest catalogue are unchanged.

## Requirement → test → evidence

Test names below are in `tests.test_b7b_recovery.RecoveryTests` unless qualified.
Numbers and complete measurement scope are in [measurements.json](measurements.json)
and the [requirement map](../b7b.md).

| Review item / requirement | Test | Red → green evidence |
|---|---|---|
| 1 / RS-B-B7-5 B4 | `tests.test_b7b_budgets.BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures`; `test_attempt_identity_requires_colon_free_run_handle` | Before runtime edits the strengthened test failed on B4: holds 1,722/9,001,722, recovery 354/3,900,352, has_holds 252/1,000,250 VM steps at 0/100,000 tombstones. After PK equality/live-hold query changes it passes with equal work (numbers below). New identity test was red because `attempt_identity` accepted a colon; now green. `_RUN` is `hrn_[0-9a-f]{48}`, and the real lease validator also rejects a colon. |
| 2 / RS-B-B7-5 B3/B4/B5; RS-D S3 | Both `BudgetMeasurements` tests | Red on real B4 numbers rather than passing a failed status; green with `met` and independent numeric limits. B3 <50 ms, B4 difference <=16 VM instructions per operation, B5 1,000 releases <=18 batches, retirement of ten <50 ms and successful authenticated lookup <5 ms. B1/B2/B6 stay unmeasured with explicit reasons; S3 startup is separately unmeasured. Missing memory tracking and unavailable socket binding are executable assertions, requiring new full measurements when those capabilities appear. |
| 3 / RS-B-B7-4/-6 | `test_learning_status_notice_only_when_recovery_is_stale`; `tests.test_b7b_terminal.PlainTerminalOutputTests.test_plain_terminal_output_without_socket_privilege`; `test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report` | CLI tests red with missing `recovery_pending_notice`, now green with the plain sentence “Recovery was not checked recently.” Fresh and unavailable states omit the stale notice. Real HistoryService with no RunDriver returns all-zero reasons plus stale=true. `ProjectServiceHost(bundle_root=None)` constructs ProjectRuntime but skips RunDriver (`service_host.py`), so history-only serving is possible. |
| 4 / RS-B-B7-6 | `test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report` | Existing gate already green: differing request/service runtime digests give history_not_authorized with result=null. Authorizer and report computation are bombed if reached. No artificial failing gate or production gate change was introduced. The new test’s mistaken first-draft absent-result assertion was corrected to the protocol-required null field; recorded in assertions.md. |
| 5 / RS-B-B7-4 | `test_sweep_resets_account_capture_on_cleanup_and_publication_exceptions` | Red twice: capture remained set after `journal.instances` and `notes.publish` exceptions. Green with cleanup in finally. Sweep body unchanged except indentation. |
| 6 / RS-B-B7-4 | `test_account_refusal_beside_unreachable_counts_only_transport_failure` | Already green before production edits: each simultaneous pair (not_authorized/unavailable, wrong_owner/unavailable) reports exactly one account_unreachable and one needs_settlement. Capture also resets on normal return. No classification change needed. |

## Current measurements

- B3 maximum **16.611 ms <50 ms**; B5 **1,000 releases / 16 batches <=18**.
- B4 at 0/100,000: holds **994/994**, recovery **455/455**, has_holds
  **335/335**, context candidates **223/223**, reconciliation **157/157**,
  absent lookup **15/15**, retirement **10,512/10,512 VM instructions**.
  All measured deltas are zero; allowance <=16.
- S3 retirement of ten **45.986/7.973 ms <50 ms**;
  authenticated lookup **1.071/0.722 ms <5 ms**, **491/491 VM steps**.
- B1 partial register calls **0.175 ms** (five calls); B2 store-open
  delta **+1.720 ms**; B6 partial lease pass **0.384 ms** (one batch);
  S3 store-open delta **+1.656 ms**. These do not establish the
  missing full lifecycle/scheduler/service-startup budgets.

The source-tombstone fixture reuses B6's valid sealed lease/reservation rows:
100,000 of each, with released hold rows, belonging to an open owner with no live
execution hold. A second open owner has an armed closing-denied lease. Thus the
empty owner's two EXISTS branches finish, and the pending lease join is exercised.
The harness traces the actual has_holds SQL and counts its VM work separately.
The custody/context fixture retains 100,000 released/expired rows. No production
store or service is benchmarked. Timing limits are unchanged; 16 VM instructions
is the explicit small-constant allowance, with observed deltas of zero.

## Verification

Only the supplied venv, `-B`, and named modules/classes were used. No full run,
`--affected`, schema migration, copier sweep or container scenario was run.
No schema/record/wire shape changed, so the copier rule is not triggered.

- Direct red: recovery module **13 tests, 0.091 s**, three failures (identity and
  both finally subcases) and one missing-notice error. Plain subprocess red also
  raised `KeyError: recovery_pending_notice`. Budget red: **2 tests, 27.666 s**,
  one failure on B4 status and the raw work above.
- Final focused direct confirmation: **94 tests, 2.283 s, PASS** —
  `tests.test_b7b_recovery`, `tests.test_b7b_terminal.PlainTerminalOutputTests`,
  `tests.test_holds_report`,
  `tests.test_account_settlement_driver.ConfirmFixSweepTests`,
  `tests.test_project_holds_schema`, `tests.test_reply_shape_compatibility`,
  `tests.test_lifetime_cap_guard`.
- Named metadata/regression runner: **122 tests, 6.3 s**, `metadata_errors: []`.
  Same modules above, with full `test_b7b_terminal` and full
  `test_account_settlement_driver`: **3 terminal bind errors (EPERM)** and
  **25 driver service-start failures** (`collector endpoint could not be created`).
  The other 94 tests pass. These are execution limitations, not green results.
- Final budget runner: **2 tests, 28.4 s, PASS**, `metadata_errors: []`.
- `tests.test_runtime_release`: **3 tests, 0.005 s**, two expected failures:
  runtime digest mismatch and the pre-round `recovery_report.py` omission from
  RUNTIME_FILES. Per instructions, VERSION/catalogue remain for the primary.
- `git diff --check`: PASS. No test run remains in flight.

All changed assertions and their replacements are listed in
[assertions.md](assertions.md). Existing negative/type/privacy assertions remain.
Existing B7b test modules were reused; new checks are registered under the same
three requirement IDs. No new test module or requirement ID was introduced.

## Remaining execution limits / primary handoff

The plain isolated Python subprocess test passes; no PTY is needed. Real Unix
socket exchange and complete project/account service startup remain blocked.
With the supplied venv assigned to `PY`, the primary can rerun:

```sh
mkdir -p /private/tmp/b7b
TMPDIR=/private/tmp/b7b "$PY" -B scripts/parallel_tests.py --module codex:tests.test_b7b_terminal --module codex:tests.test_holds_report_socket --module codex:tests.test_account_settlement_driver
```

B1 full normal-run writer delta, B2 full service startup, B6 generic re-due/arrival
scheduler and S3 full service startup remain unmeasured. Socket availability now
fails the budget test loudly, requiring actual B2/S3 service measurements instead
of leaving these entries silently unmeasured. Memory tracking becoming available
similarly forces revisiting B1/B6. No primary decision is needed for these fixes.
Prior-round result-persistence/migration failures were not rerun: this correction
round does not edit those paths; their earlier evidence remains in base 37ebd229's
STATUS.md. The runtime-release update remains a primary merge task.

## Files changed in this round

Runtime (under `packages/codex-agents/native-package/.codex/orchestration/`):
`cli.py` (item 3), `codex_agents/project_holds_report.py` (item 1),
`codex_agents/recovery_report.py` (item 1),
`codex_agents/project_holds_schema.py` (item 1 invariant),
`codex_agents/project_driver.py` (item 5 finally).

Tests/tooling (under `packages/codex-agents/`):
`scripts/measure_b7b.py` (items 1–2), `tests/test_b7b_budgets.py` (items 1–2),
`tests/test_b7b_recovery.py` (items 1, 3–6),
`tests/test_b7b_terminal.py` (item 3).

Requirement/evidence files: `docs/requirements/b7b.md`,
`docs/requirements/b7b/{STATUS.md,assertions.md,measurements.json}`,
`tests/requirements/b7b.json`, `scripts/test-requirements.json`.
