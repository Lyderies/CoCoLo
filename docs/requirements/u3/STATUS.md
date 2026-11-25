# Unit 3.9 / U3 status

Base `8b959668`, branch `feat/u3-sql-growth`, VERSION 0.8.63 plus accepted RS-B memory holds. Implementation and synthetic SQL-growth acceptance pass. Changes remain uncommitted as required in this sandbox. VERSION and the runtime digest catalogue are unchanged.

## Requirement → test → evidence

Requirement **U3** is registered through `tests/requirements/u3.json#U3`; the acceptance mapping is [u3.md](../u3.md). Exact test names, outcomes, timings and named module selections are in [test-evidence.json](test-evidence.json). All completed parallel-runner invocations represented there report `metadata_errors: []`.

The new modules pass **16/16 tests**: `tests.test_read_growth` **16.40 s**, `tests.test_read_plans` **7.33 s**, 16.4 s runner wall plus 12.0 s preflight. Both are scenario/integration modules because their fixtures can start processes. No database or file access was added at module import.

The following labels identify methods in `tests.test_read_growth.ReadGrowthTests`; full names and actual explained SQL are in [measurement.json](measurement.json). Fast suite population is 100/2,000 rows. The standalone script retains the required **1,000/100,000** populations, with fixed relevant evidence/live rows and unchanged query answers.

| U3 clause / method prefix | Red before its fix, 100 → 2,000 history rows | Final 1,000 → 100,000 work |
| --- | --- | --- |
| `test_U3_1_` open preflight | 4 → 44 callbacks | 1 → 0 callbacks |
| `test_U3_2_` run admission | 121 → 1,899 VM steps | 38 → 38 steps |
| `test_U3_3_` digest/run lineage | 131 → 1,649 steps | 89 → 89 steps |
| `test_U3_4_` recent account admissions | 311 → 5,137 steps | 83 → 83 steps |
| `test_U3_5_` terminal identity conflicts | 1,844 → 36,044 steps | 60 → 60 steps |
| `test_U3_6_` effective admission capture | 32 → 465 callbacks | 19 → 19 callbacks |
| `test_U3_7_` attestations/signature capture | Already bounded by existing P9/AP work | 67 → 67 steps; 0 → 0 callbacks |
| `test_U3_8_` registration cleanup receipts | 539 → 10,039 steps | 102 → 102 steps |
| `test_U3_9_` candidate lineage | Large gate exposed 12,034 → 1,200,034 steps with the first candidate-index shape | 80 → 80 steps, hit and miss |
| `test_U3_10_` original provenance with retained metadata | Regression guard for existing keyed/held-only reads; also exercises corrected run lookups | 1 → 0 callbacks |
| `test_U3_11_` project registrations without leases, in prior orchestrations | Existing composite index retained | 29 → 29 steps |
| `test_U3_12_` completed probe origins/requests | Existing keyed/partial indexes retained | 208 → 208 steps |
| `test_U3_13_` Claude first pass and latest older readings for a fixed account | Additional guard for recursive account skips; anchor evidence remains present | 25 → 23 callbacks |

Additional checks:

- `test_U3_additive_indexes_rebuild_on_reopen_without_changing_rows_or_version`: initially failed on missing indexes; now passes populated drop/reopen, exact records and unchanged `user_version`.
- `ReadPlanTests.test_U3_6_every_budgeted_read_that_runs_uses_a_live_or_selective_index`: exposed the remaining broad original-open provenance predicates; passes with **44 distinct budgeted read plans**. Its lifecycle fixtures cover preflight, original open/run provenance, denial, probe/observation reservation, admission and signature capture. Terminal, registration, candidate and receipt plans are captured by the growth cases as well.
- `ReadPlanTests.test_U3_guard_rejects_residual_scans_and_accepts_expression_seeks`: proves a coarse-index residual JSON scan fails even with `LIMIT 1`, while the expression seek passes. A plain indexed prefix count with an explicit cap is allowed because it cannot walk beyond that cap.
- Standalone measurement: **13/13 read families pass**, 54.05 s total case time plus the small plan inventory. Largest observed production-budget read/action **3.63 ms**, largest actual scope **25 callbacks**, versus production limits of 1,000 ms and 100,000 callbacks. Callback granularity is 256 instructions; the work table uses exact VM steps for non-callback measurements. These are disposable synthetic, warm-store observations, not an installed-owner-ledger or cold-disk performance claim.

Reproduce the large measurement from the repository root with the prepared test interpreter as `$PY`:

```sh
$PY -B scripts/measure_u3_sql_growth.py --output docs/requirements/u3/measurement.json
$PY -B scripts/parallel_tests.py --module codex:tests.test_read_growth --module codex:tests.test_read_plans
```

## Implementation and compatibility

Five additive indexes are created through the normal fenced ledger-open path after migration: protected JSON run identity; a partial candidate identity index; record type/time for account-wide windows; foreign admission decisions; unreleased cleanup receipts. They do not change the schema version. `records_project_digest`, `records_type_project`, `records_observed_at` and P9 projected attestation membership are reused. The new type/time key serves account-wide time-window reads, a different key from AP's project/observed-time lookup.

Admission capture first seeks the candidates for each existing scope rule, then applies the unchanged scope and row ordering. Recursive MIN seeks skip each Claude account's history when selecting its first pass/latest older readings, retaining ties. A dedicated 100k Claude-history case keeps the three explicit anchor records and measures flat work. Terminal conflict checks seek the three existing unique identities separately. Run and candidate lineage queries select by identity before reading content. Guarded JSON expressions allow malformed retained rows to remain subject to existing validation rather than preventing additive index construction.

The genuine schema-11 fixture drops all five new indexes before its unchanged exact-layout assertion. Schema, backup, restore, transfer, historical-layout, resumable migration and fence tests were run; no table/column/record/reason/op-set change was made. The previous `u3-plans` worktree was read only. No existing acceptance assertion was removed or weakened; see [assertions.md](assertions.md).

## Named regression runs and remaining environment checks

The machine-readable evidence lists every selected module and every completed test outcome, without raw logs or local paths:

- Copier/schema/migration/backup/enumeration batch: **470/487 pass**, 51.47 s wall, 7.24 s preflight. Failures: socket/service startup (including five LearningStore socket cases, migration CLI and state-move checks), the expected runtime-release digest pin, and two publication-lock scenarios that time out waiting for signature workers. Existing `test_owner_backup_streamed` passes its assertions but its combined fragments take **60.10 s**, slightly exceeding its scenario budget under load; this module was not changed.
- Read/answer regressions: **285/288 completed tests pass** across 16 completed modules. Failures are the socket-dependent collector retention/receipt scenario and the existing deferred-signature case. The separate `test_admission_history_growth` worker was interrupted after repeated signature-worker waits; it is incomplete, not counted as green. Four additional synchronous capture checks pass (first Claude pass, latest older Claude readings, recapture clock, coverage corruption); a fifth reaches its signature-dependent public-open tail and times out.
- Additional copier/CLI/installer/account/op-enumeration batch: **279/322 pass**, 32.42 s wall. Socket/service-dependent account/CLI/withdrawal/installer scenarios cannot start their collector here. Three installer `verify(...)["ready"]` checks also remain failing with the intentionally unrecorded candidate runtime. Existing combined module budgets: `test_learning_cli` **75.91 s**, `test_run_holds` **62.73 s**; these modules were not changed.
- Untouched-base comparison from `git archive 8b959668`: the same deferred-signature case and both publication-lock cases reproduce their timeouts, **3 tests / 4 failure reports in 62.51 s**. No test assertion was changed to mask those failures.

Unix socket binding is denied (`Operation not permitted`) in this sandbox. No PTY checks were needed. The primary should rerun the nonpassing/incomplete modules from `test-evidence.json` where sockets and signature workers function, with a short TMPDIR. For the interrupted module the exact named rerun is:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_admission_history_growth
```

The primary owns the version/digest refresh and full merge validation. No full suite was run and no test run is left in flight.

## Files changed

- Runtime: `account_host_open.py`, `account_original_open.py`, `account_run_provenance.py`, `account_service.py`, `learning_store.py`, `project_history.py`, all under the native Codex orchestration package.
- Tests: new `test_read_growth.py`, new `test_read_plans.py`; schema-11 setup in `test_account_observation_schema.py`.
- Runner metadata: `scripts/test-layers.json`, `test-stages.json`, `test-timings.json`, `test-requirements.json`; `tests/requirements/u3.json`.
- Measurement: `scripts/measure_u3_sql_growth.py`.
- Requirements/evidence: `docs/requirements/u3.md`, this status, `assertions.md`, `measurement.json`, `test-evidence.json`.
