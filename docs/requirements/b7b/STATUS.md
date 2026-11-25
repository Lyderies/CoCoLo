# B7b final targeted fix — 2026-11-25

Implemented all four requested fixes in `~/Code/temp/cocolo-b7b`.
Changes are uncommitted because this builder is sandboxed. VERSION, runtime
catalogue and runtime file list are unchanged; the primary owns release updates.
The accepted final requirements are in [../b7b.md](../b7b.md), assertion changes in
[assertions.md](assertions.md), and raw samples/test summaries in
[measurements.json](measurements.json). This section supersedes prior-round
startup guards and cold timing evidence.

## Requirement → test → evidence

| Item / requirement | Tests | Evidence |
|---|---|---|
| 1 / RS-B-B7-5 B2; RS-D S3 | `BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures`; `test_service_startup_owns_real_writer_and_driver_and_cleans_up` | Replaced the PermissionError requirement with an OSError bind probe. Available sockets run real `ProjectServiceHost` startup: writer bootstrap, RunDriver, three listeners and published hint; median populated-minus-empty <=100 ms at 1,064 held leases and <=200 ms at 64 live contexts. Each service_startup_status is met only when its measured limit passes; failures stay failed. Here both are unmeasured with `PermissionError (errno=1)`, and only the real_service_startup subtest skips after all store assertions. The added lifecycle test was red with driver=None, now green: two real startups with binding alone stubbed, preserved lease, stopped threads, withdrawn hints, released writer/journal locks. Its timings are excluded from evidence. |
| 2 / RS-B-B7-5 B3/B4; RS-D S3 | Both original `BudgetMeasurements` tests | Red on missing warmup/sample evidence. Green with one warm-up plus three measured runs and independently asserted medians. B3 maximum per-batch median **16.765 ms <50**; ten-result retirement **6.869/7.217 ms <50**; authenticated lookup **0.619/0.654 ms <5**. All seven VM comparisons have zero delta; every corresponding retirement trial also has zero delta. No cold warm-up outlier is used as evidence. |
| 3 / RS-B-B7-5 | `RecoveryTests.test_attempt_identity_requires_colon_free_run_handle` | Red: bare assert raised AssertionError. Green: explicit ValueError with colon-free message. Same named test passes under `python -O -B` (1 test, 0.007 s). Valid identity and lease-grammar rejection assertions retained. |
| 4 / RS-B-B7-4/-6 | `test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report`; `test_learning_status_notice_only_when_recovery_is_stale`; `test_empty_restart_and_strict_stale_boundary`; `test_held_reservation_reports_settlement_even_without_a_journal_observation`; `test_response_requires_closed_reasons_types_and_stale`; `RecoveryScopeTests`; `PlainTerminalOutputTests.test_plain_terminal_output_without_socket_privilege` | Red: stale numeric counts, stale null response rejected, CLI subprocess numeric output. Green: all five outward counts are null when stale, exact notice retained, fresh counts remain numeric; fresh nulls/bool/negative/string counts, missing/extra fields and non-bool stale rejected. Source settlement count remains one after a fresh observation. Runtime refusal/privacy/scope behavior retained. |

## Measured numbers

- B3 **16.765 ms** maximum corresponding-batch median; B5 **1,000 released / 16 batches <=18**.
- At 0/100,000 tombstones: holds **994/994**, context sweep **223/223**,
  reconciliation **157/157**, absent lookup **15/15**, recovery_pending **455/455**,
  has_holds **335/335** SQLite VM steps. Released lease/reservation neighbours are
  valid sealed source rows. Live context partial-index plan remains asserted.
- Retirement measured trials: **[10716, 11027, 11347]** VM steps
  at both history sizes; medians **6.869/7.217 ms**.
  Ten newly committed results/contexts per run; trusted time advances between runs.
- Successful authenticated lookup: **491/491** VM steps,
  **0.619/0.654 ms** median of three after warm-up.
- B2 store-only proxy delta **+1.932 ms**;
  S3 store-only proxy delta **+0.673 ms**.
  Real B2/S3 startup is **unmeasured** here because binding returns EPERM.
  B1/B6 remain explicitly unmeasured for the existing memory/scheduler capability gaps.

## Named verification and limitations

- Final `scripts/parallel_tests.py --module codex:tests.test_b7b_budgets --keep`:
  **3 tests, 30.4 s, PASS**, `metadata_errors: []`;
  exactly one real service-startup subtest skipped. Store budgets, successful lookup
  and the functional startup lifecycle check all executed.
- Named regression modules: `tests.test_b7b_recovery` (13 pass),
  `tests.test_holds_report` (26 pass), `tests.test_reply_shape_compatibility` (3 pass),
  `tests.test_project_holds_schema` (40 pass), `tests.test_lifetime_cap_guard` (4 pass),
  `tests.test_b7b_terminal` (isolated subprocess passes, three bind setup errors).
  **87 pass / 3 socket errors**, `metadata_errors: []`; module execution times are
  stored in measurements.json. The combined 92-test runner took 17.3 s and also
  exposed an intermediate budget fixture clock-rollback error, corrected before
  the passing final budget run. These socket errors are not claimed green.
- Red named run: 19 tests, 25.5 s, `metadata_errors: []`; recovery 6 failures/1 error,
  terminal 1 failure plus 3 socket errors, both budget tests error on missing
  warmed-sample evidence. Functional startup test separately red→green as above.
- Startup lifecycle follow-up: **1 test, 0.208 s, PASS**; stopped threads,
  withdrawn hint and released journal asserted before temporary-directory cleanup.
- `tests.test_runtime_release`: **3 tests, 0.005 s**; two expected failures
  (runtime digest mismatch, pre-existing `recovery_report.py` omission from
  RUNTIME_FILES), one pass. No release metadata was changed.
- No persistent schema, record columns, reason codes or operations changed;
  report keys are unchanged, with stale values now null. The affected response
  enumeration/compatibility and project schema modules above pass. No broader
  copier/backup/move/migration set is triggered by this value-only report fix.
- Existing B7b modules are registered scenario/unit layers as before. The new
  method uses existing RS-B-B7-5 and is linked in scripts/test-requirements.json;
  no new module or requirement ID. No import-time I/O was introduced.
- `git diff --check`: PASS. No full-suite run, install, live-service access,
  commit or test run left in flight. No PTY required.

The primary must execute real bound-socket startup/terminal checks unsandboxed:

```sh
mkdir -p /private/tmp/b7b
TMPDIR=/private/tmp/b7b "$PY" -B scripts/parallel_tests.py --module codex:tests.test_b7b_budgets --module codex:tests.test_b7b_terminal
```

`PY` is the supplied test venv. When binding works, the budget test measures the
real service path automatically and requires both startup limits/statuses; it
never expects a binding failure or accepts a store proxy for those limits.

## Files changed in this final round

- Runtime: `codex_agents/project_holds_schema.py`, `recovery_report.py`,
  `holds_report_contracts.py` under the native orchestration bundle.
- Harness/tests: `packages/codex-agents/scripts/measure_b7b.py`,
  `tests/test_b7b_budgets.py`, `test_b7b_recovery.py`, `test_b7b_terminal.py`.
- Requirements/evidence: `docs/requirements/b7b.md`,
  `docs/requirements/b7b/{assertions.md,measurements.json,STATUS.md}`,
  `tests/requirements/b7b.json`, `scripts/test-requirements.json`.
