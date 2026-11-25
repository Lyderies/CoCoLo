# B7b status — 2026-11-25

Worktree: `cocolo-b7b`, branch `feat/rsb-b7b`. Changes are uncommitted because the
sandbox forbids commits. No other worktree or live service was modified.
`project_start.py`, VERSION and the runtime digest catalogue are untouched.

## Requirement → test → evidence

| Requirement | Tests | Evidence / remaining work |
|---|---|---|
| RS-B-B7-4 | `tests.test_b7b_recovery`: `RecoveryTests`, `RecoveryScopeTests`, `AccountObservationTests`, `OwnRecoveryTests` | Red: missing recovery module/key; later negative cases caught transport refusal misclassification, fenced intents, own failed stops/runs, overlapping reasons and missing-journal reservations. Green: eight tests on real store/driver control flow with controlled OS/account boundaries. Strict stale boundary is 20 seconds, never refreshed by own-run updates or failed sweeps. Two projects, read-only/privacy, deduplication, release clearing, restart, account refusal vs transport failure covered. |
| RS-B-B7-6 | `RecoveryTests.test_response_requires_closed_reasons_types_and_stale`, `tests.test_b7b_terminal.PlainTerminalOutputTests.test_plain_terminal_output_without_socket_privilege`, B7a store/client/protocol tests, `tests.test_reply_shape_compatibility` | Red missing output key → green. Plain terminal is isolated `python -I -B -S`: real store, report, service authorization, client response validation and CLI. Only enrollment, framed exchange boundary and unrelated account status are substituted. All three real Unix-socket tests in the new terminal module fail at bind with EPERM; primary must rerun them. |
| RS-B-B7-5 | Both `tests.test_b7b_budgets.BudgetMeasurements` tests | Green measurement harness; **this does not mean all budgets pass**. B4 fails at **1,722 → 9,001,722 VM steps**. B3 max **18.942 ms**, B5 drain **16/18 batches**. B1, B2, B6 and S3 service startup have explicit coverage gaps. Complete numeric map is in `../b7b.md`; machine-readable numbers are in `measurements.json`. No limit was loosened. |

The initial B7b acceptance allowed overlapping reasons. RS-B draft 3 §10 actually
requires one latest reason per row. The acceptance, red tests and implementation
were corrected in that order. Every replaced assertion is listed in
[assertions.md](assertions.md), including B7a's exact top-level field set and CLI
fixture expansion. No prior hold/custody/repair assertion was removed.

## Verification

All invocations used the supplied test venv, `-B`, and named modules/classes only.
No full-suite or `--affected` run. No test remains running.

- Initial direct red run: 8 tests, 0.051 s: missing module/harness/response field,
  plus Unix bind EPERM. Independent plain-terminal red: 1 test, 0.418 s,
  `KeyError: recovery_pending` before implementation.
- Final measurement/terminal/B7a/compatibility named runner: **41 tests, 21.4 s**,
  `metadata_errors: []`; only the three socket-binding cases failed. Budget
  harness itself took 21.2 s, within the scenario-layer budget. The original
  slower fixture setup was replaced by ordinary below-cap lease writes with only
  repeated capacity checks bypassed during setup; the unmodified capacity check
  and store reopen/validators run before any measurement.
- Final direct store/terminal/driver confirmation: **42 tests, 1.037 s, PASS**
  (`test_b7b_recovery`, `test_holds_report`, plain-terminal class, and
  `test_account_settlement_driver.ConfirmFixSweepTests`). This includes the
  source-contract correction to exclusive reasons and missing-journal fallback.
- Lifetime guard + reply compatibility direct confirmation: **7 tests, 0.617 s,
  PASS**. The new SQL COUNT is registered as a snapshot aggregate, not an admission
  limit; its count disappears when execution holds release.
- Copier/backup/schema/enumeration regression runner: **234 tests, 22.7 s**,
  `metadata_errors: []`. Named modules: `test_learning_migration`,
  `test_learning_source_image`, `test_learning_source_image_legacy`,
  `test_learning_source_transfer`, `test_backup_reply_private`,
  `test_backup_installed_project`, `test_large_project_backups`,
  `test_owner_backup_streamed`, `test_account_observation_backup`,
  `test_project_holds_schema`, `test_account_settlement_wire`,
  `test_batch2_b4s_integration`, `test_refusal_steps`, plus B7a/B7b/compatibility
  and `test_runtime_release`. All passed except the failures below.
- Final writer-lane/metadata runner: **66 tests, 10.6 s**, `metadata_errors: []`.
  B7b recovery, B7a, reply compatibility and lifetime guard all pass; six of 25
  `test_result_persistence` tests fail (listed below).
- Final focused confirmation after the retry/failure-observation fix:
  **49 tests, 1.142 s, PASS**: B7b recovery, plain-terminal output, existing driver
  sweep confirmation, B7a, reply compatibility, lifetime cap guard.
- `git diff --check`: PASS.

Regression failures left visible:

1. `tests.test_runtime_release`: two expected failures, changed runtime digest and
   new `codex_agents/recovery_report.py` absent from RUNTIME_FILES. The primary must
   add the file and update VERSION/digest catalogue at merge, as instructed.
2. `tests.test_learning_migration.MigrationCliTests.test_first_start_of_the_project_service_moves_the_history_by_itself`:
   `orchestration_client_unavailable`. The test needs a serving project service;
   this sandbox refuses Unix socket binding.
3. `MigrationCliTests.test_command_moves_reports_refuses_and_rolls_back`:
   migrated `learning-run-cycle` returns `project learning has moved to its project
   store` instead of success. It also needs the project-service path. No baseline
   comparison was performed; do not label this proved pre-existing or green.

4. `tests.test_result_persistence`: six failures in
   `test_actual_client_usage_reaches_writer_without_model_usage_or_content`,
   `test_explicit_lost_commit_reply_retry_keeps_first_facts_body_and_retention`,
   `test_full_sized_bytes_are_handed_off_without_owner_json_or_byte_copy`,
   `test_original_prelaunch_assignment_and_actual_return_reach_one_writer`,
   `test_slow_database_does_not_block_control_cancellation_or_second_result_intake`,
   `test_validator_reason_survives_commit_without_rejected_answer_or_reasoning`.
   The provider fixture reports observation_unavailable/unverified output or
   fails to launch, rather than producing the expected fixture result. **Baseline
   lane comparison:** loaded HEAD's original `result_persistence.py` into that
   module's namespace in memory (no worktree files changed), then ran the same
   named module: **25 tests, 11.133 s, exactly the same six failures**. This isolates
   the three lane wiring additions; it is not a full baseline checkout comparison.
   No assertions were changed to accommodate these failures.

## Primary checks still needed

Set `PY` to the supplied venv. From the repository root:

```sh
mkdir -p /private/tmp/b7b
TMPDIR=/private/tmp/b7b "$PY" -B scripts/parallel_tests.py --module codex:tests.test_b7b_terminal --module codex:tests.test_holds_report_socket --module codex:tests.test_learning_migration
TMPDIR=/private/tmp/b7b "$PY" -B scripts/parallel_tests.py --module codex:tests.test_account_settlement_driver --module codex:tests.test_result_persistence
```

The all-operation `test_reply_shape_live` aggregator was not run: it embeds many
other service modules, requires sockets, and violates this unit's least-test / no
long broad run constraint. The frozen reply-field compatibility test did run.
No PTY is required by B7b; a plain subprocess terminal test passes here. No real
Podman/container-stop scenario was run (containers remain disabled).

B1 needs the full normal-run writer delta, not just the measured register helper
calls. B2/S3 need complete service startup, not just store open. B6 requires the
missing generic register scheduler with re-due rows and continuing arrivals;
actual lease-pass numbers cannot prove that absent mechanism. Account-dependent
round-trip drain bounds are also unmeasured. B4's failure is reproducible in the
checked-in harness and must not be called history independent.

## Files changed

Runtime: `cli.py`, `codex_agents/{recovery_report,project_driver,result_persistence,
project_holds_report,holds_report_contracts,reply_shapes}.py` under
`packages/codex-agents/native-package/.codex/orchestration/`.

Tests/tooling: `tests/test_b7b_{recovery,terminal,budgets}.py`,
`tests/test_holds_report.py`, `tests/lifetime_caps.json`, `scripts/measure_b7b.py`
under `packages/codex-agents/`; root `scripts/test-{layers,stages,timings,requirements}.json`,
`tests/requirements/b7b.json`; `docs/requirements/b7b.md` and this directory's
`STATUS.md`, `assertions.md`, `measurements.json`.
