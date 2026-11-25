# Unit 3.9 / U3 status

Base `8b959668`, branch `feat/u3-sql-growth`, VERSION 0.8.63 plus accepted RS-B memory holds. Implementation and synthetic SQL-growth acceptance pass. First round committed as `ee27c775`; fix round below (merged 0.8.64 `30e0a8b7`). VERSION and the runtime digest catalogue are unchanged.

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
| `test_U3_6_` effective admission capture | 32 → 465 callbacks | 21 → 21 callbacks (fix round; was 19) |
| `test_U3_7_` attestations/signature capture | Already bounded by existing P9/AP work | 67 → 67 steps; 0 → 0 callbacks |
| `test_U3_8_` registration cleanup receipts | 539 → 10,039 steps | 102 → 102 steps |
| `test_U3_9_` candidate lineage | Large gate exposed 12,034 → 1,200,034 steps with the first candidate-index shape | 80 → 80 steps, hit and miss |
| `test_U3_10_` original provenance with retained metadata | Regression guard for existing keyed/held-only reads; also exercises corrected run lookups | 1 → 0 callbacks |
| `test_U3_11_` project registrations without leases, in prior orchestrations | Existing composite index retained | 29 → 29 steps |
| `test_U3_12_` completed probe origins/requests | Existing keyed/partial indexes retained | 208 → 208 steps |
| `test_U3_13_` Claude first pass and latest older readings for a fixed account | Additional guard for recursive account skips; anchor evidence remains present | 27 → 27 callbacks (fix round; was 25 → 23) |

Additional checks:

- `test_U3_additive_indexes_rebuild_on_reopen_without_changing_rows_or_version`: initially failed on missing indexes; now passes populated drop/reopen, exact records and unchanged `user_version`.
- `ReadPlanTests.test_U3_6_every_budgeted_read_that_runs_uses_a_live_or_selective_index`: exposed the remaining broad original-open provenance predicates; passes with **44 distinct budgeted read plans**. Its lifecycle fixtures cover preflight, original open/run provenance, denial, probe/observation reservation, admission and signature capture. Terminal, registration, candidate and receipt plans are captured by the growth cases as well.
- `ReadPlanTests.test_U3_guard_rejects_residual_scans_and_accepts_expression_seeks`: proves a coarse-index residual JSON scan fails even with `LIMIT 1`, while the expression seek passes. A plain indexed prefix count with an explicit cap is allowed because it cannot walk beyond that cap.
- Standalone measurement: **13/13 read families pass**, 54.05 s total case time plus the small plan inventory. Largest observed production-budget read/action **1.60 ms** in the fix-round rerun (33.8 s wall; first round 3.63 ms), largest actual scope **27 callbacks**, versus production limits of 1,000 ms and 100,000 callbacks. Callback granularity is 256 instructions; the work table uses exact VM steps for non-callback measurements. These are disposable synthetic, warm-store observations, not an installed-owner-ledger or cold-disk performance claim.

Reproduce the large measurement from the repository root with the prepared test interpreter as `$PY`:

```sh
$PY -B scripts/measure_u3_sql_growth.py --output docs/requirements/u3/measurement.json
$PY -B scripts/parallel_tests.py --module codex:tests.test_read_growth --module codex:tests.test_read_plans
```

## Fix round (after merging 0.8.64)

- Merged `30e0a8b7` (0.8.64, version/0.4.0) with `--no-ff`: no conflicts; the four runner registries are valid JSON.
- The primary's `--affected 8b959668` run found U3 breaking existing tests. Four were U3's (the two named, plus the two 0.3.3 C2 tests in the same module, which pass on untouched `30e0a8b7`). Decisions, each paired old -> new in [assertions.md](assertions.md):
  - Verified denial settlement (`docs/service-api-development.md`, no registry ID): the legacy read now seeks the run identity (about 90 VM steps, under one 256-step callback), so history cannot exhaust any legal budget. The test still provokes a real exhaustion of the actual read by counting one callback per VM step for that read only (limits unchanged), checks the refusal is that read's `sql_read_work_limit`, rollback and `query_only`, then retries under the same smallest budget at normal granularity.
  - 0.3.3 C2 (both tests): the claim now closes under the old 0.3.3 budget that used to refuse it (stronger C2 property).
  - P9b-2: U3's first admission-union shape dropped P9b-2's per-branch `LIMIT` (every window row reached the final sort). SQL fixed: attestations keep their bounded `capture_candidates` branch; U3's indexed candidate seeks feed the second branch, scoped and bounded to the capture limit before the final sort and joins. The plan pin's index name changes from `records_type_project` to `records_type_observed`; no-records-scan and rows-before-sort assertions are unchanged. The `reading` CTE was renamed `older` so no plan line reads `SCAN r...`.
- Named modules: `test_claim_denial_recovery` 18/18 (16.2 s), `test_p9b_attestation_followups` 15/15 (2.3 s).
- `--affected 30e0a8b7` (jobs 8): no coverage map in this worktree, so it ran the full set: 482 units, 779 s, `metadata_errors: []`. Failed units: `test_runtime_release` (digest not bumped, expected) and eight units of `test_bridge`, `test_project_start`, `test_result_lifetime`, `test_run_holds`, `test_trial_run_driver_bugs`, every error being installed collector identity/integrity or `collector_code_stale` (the same unbumped digest). With the new digest recorded temporarily (then restored, not committed), those five modules plus `test_runtime_release` pass: 0 failed units, 71.8 s.
- Standalone measurement rerun: 13/13 families and the 44-read plan inventory pass; `measurement.json` refreshed.
- The primary still owns VERSION and the runtime digest refresh.

## Review fix round (reviews/040-u3-r1, READY-WITH-CORRECTIONS at 92abd22e)

- **MED-1 first-open index build, measured.** `scripts/measure_u3_index_build.py` (opt-in script, not a suite test; never reads an owner ledger) builds a disposable ledger: 20,000 event records through the real `LearningLedger.append` path, plus `dispatch_intent`, `provider_usage`, `admission_decision`, `outcome`, `provider_billing`/`provider_headroom` rows copied from rows the real open path wrote, through the ledger's own INSERT and triggers (provider evidence is not appendable from outside). 220,018 rows, 296 MiB after dropping the five U3 indexes and vacuuming (310.6 MB file). The real open path then rebuilds them. Three runs agree:

  | Measure | Result |
  | --- | --- |
  | First open after install (whole `LearningLedger(...)` open, all five indexes built) | 0.75 to 0.79 s |
  | Index build time (sum of the five CREATE INDEX) | 0.38 to 0.42 s |
  | of which `records_run` | 0.25 to 0.28 s |
  | other four | 0.03 (candidate), 0.03 (foreign admissions), 0.08 (type/time), under 0.01 (cleanup receipts) |
  | Reopen with the indexes present | 0.37 to 0.38 s (their DDL under 1 ms) |
  | File growth, all five indexes | +22.7 MB, +7.3 percent (310.6 MB -> 333.3 MB) |

  **Decision: `records_run` stays non-partial.** It costs about 0.25 s at this scale (limit about 1 s) and the whole set grows the file 7.3 percent (limit 10 percent), so the partial form is not warranted. Scaling note: the build is linear in rows; a ledger with the same 296 MiB in much smaller rows would build slower (several times the row count) but is still expected to stay a few seconds. The first service start after install is not at risk of a start timeout at this scale; the primary should still watch the first open of the real local-routing ledger at install (owner consent needed to read it).
- **LOW-1 fail closed (primary decision).** A malformed `dispatch_intent`, `provider_usage` or `dispatch_result` row no longer lets the "already dispatched / already recorded" checks pass. `_refuse_malformed_rows` (learning_store.py) seeks `records_run` for a NULL run identity of that record type (a handful of rows) and raises `LedgerError("a malformed <type> record blocks this check")` when one is not valid JSON. It runs before the three duplicate guards (`record_dispatch_intent` preflight and claim, `record_provider_usage`, `record_dispatch_result`). Absence checks (the same lookups used to require a row to exist) already fail closed. Not changed: read-only lineage and usage readers, where a malformed row simply has no answer. Red on 92abd22e: `test_U3_a`, `test_U3_b`, `test_U3_c` in `tests/test_u3_review_fixes.py` each fail with `LedgerError not raised`; green after. `test_U3_d` pins that the check seeks `records_run` with no scan.
- **LOW-2 equivalence.** `AdmissionQueryEquivalenceTests` keeps the pre-U3 full-scan admission query (the unchanged scope applied to the whole records table) as a reference and compares the real capture with it and with the candidate query, same mixed fixture, asserting identical rows and order. Fixture: own and foreign project, identity inside and past 30 minutes and in the future, lifecycle/foreign decisions inside and past five hours, first and later Claude passes per account, Claude billing/headroom ties, superseded and invalid-JSON readings, no-account readings, live-window headroom, future rows. Green on both (it is a lock-in, not a bug repro).
- **LOW-3.** `codex:tests.test_admission_history_growth` unsandboxed: passes (26.3 s), see below.
- Named run (unsandboxed, 8 jobs, short TMPDIR): `test_read_growth`, `test_read_plans`, `test_p9b_attestation_followups`, `test_claim_denial_recovery`, `test_admission_history_growth`, `test_account_observation_schema`, `test_learning_store`, `test_candidate_evidence_lookup`, `test_type_lookups`, `test_u3_review_fixes`: 189 tests, `failed_units: []`, `metadata_errors: []`, 26.3 s wall (111.5 job seconds, 6.8 s preflight). Version and digest unchanged; the primary still owns them (the digest changes with this runtime edit when refreshed).
- Extra check because the guards sit on the bridge dispatch path: `codex:tests.test_bridge` 168 tests, `failed_units: []`, `metadata_errors: []`, 48.3 s wall.
- New module registered in `test-layers.json` (scenario), `test-stages.json` (integration), `test-timings.json` and the U3 checks in `test-requirements.json`.

## Implementation and compatibility

Five additive indexes are created through the normal fenced ledger-open path after migration: protected JSON run identity; a partial candidate identity index; record type/time for account-wide windows; foreign admission decisions; unreleased cleanup receipts. They do not change the schema version. `records_project_digest`, `records_type_project`, `records_observed_at` and P9 projected attestation membership are reused. The new type/time key serves account-wide time-window reads, a different key from AP's project/observed-time lookup.

Admission capture first seeks the candidates for each existing scope rule, then applies the unchanged scope and row ordering. Recursive MIN seeks skip each Claude account's history when selecting its first pass/latest older readings, retaining ties. A dedicated 100k Claude-history case keeps the three explicit anchor records and measures flat work. Terminal conflict checks seek the three existing unique identities separately. Run and candidate lineage queries select by identity before reading content. Guarded JSON expressions allow malformed retained rows to remain subject to existing validation rather than preventing additive index construction.

The genuine schema-11 fixture drops all five new indexes before its unchanged exact-layout assertion. Schema, backup, restore, transfer, historical-layout, resumable migration and fence tests were run; no table/column/record/reason/op-set change was made. The previous `u3-plans` worktree was read only. No existing acceptance assertion was removed or weakened; see [assertions.md](assertions.md).

## Named regression runs and remaining environment checks

The machine-readable evidence lists every selected module and every completed test outcome, without raw logs or local paths:

- Copier/schema/migration/backup/enumeration batch: **470/487 pass**, 51.47 s wall, 7.24 s preflight. Failures: socket/service startup (including five LearningStore socket cases, migration CLI and state-move checks), the expected runtime-release digest pin, and two publication-lock scenarios that time out waiting for signature workers. Existing `test_owner_backup_streamed` passes its assertions but its combined fragments take **60.10 s**, slightly exceeding its scenario budget under load; this module was not changed.
- Read/answer regressions: **285/288 completed tests pass** across 16 completed modules. Failures are the socket-dependent collector retention/receipt scenario and the existing deferred-signature case. The separate `test_admission_history_growth` worker was interrupted after repeated signature-worker waits in the sandbox; it passes unsandboxed in the review fix round (below). Four additional synchronous capture checks pass (first Claude pass, latest older Claude readings, recapture clock, coverage corruption); a fifth reaches its signature-dependent public-open tail and times out.
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
- Measurement: `scripts/measure_u3_sql_growth.py`, `scripts/measure_u3_index_build.py` (review fix round); new test module `test_u3_review_fixes.py`.
- Requirements/evidence: `docs/requirements/u3.md`, this status, `assertions.md`, `measurement.json`, `test-evidence.json`.
