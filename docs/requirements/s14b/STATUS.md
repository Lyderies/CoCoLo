# S14b handoff

Implementation and sandbox checks complete on `feat/s14b-retention`, base
`ce81bd96` (0.8.67). Changes are uncommitted because this builder is sandboxed.
VERSION and `runtime-releases.json` are unchanged. This is not a completed
master-plan gate: real signature execution, service/socket flows, Linux,
other-provider review and the primary's batch/full-suite gates remain pending.

## Requirement evidence

Test abbreviations: `R = tests.test_run_retention.RunRetentionTests`,
`I = tests.test_run_retention_integration.RunRetentionIntegrationTests`.

| Requirement | Test / evidence | Result |
| --- | --- | --- |
| S14b-1 | R.test_five_year_projection_at_double_peak_includes_every_family | Red on base: 22.12 GiB even after crediting OP-2; red again before outcome retention: 22.42 GiB. Green after implementation. |
| S14b-1 | I.test_real_open_claim_footprint_retires_to_measured_metadata_and_summary; scripts/measure_s14b_retention.py | Real persistence with a stand-in signature executor: **15,090 → 5,209 retained column bytes; 14.219 GiB at 774 runs/day for five years**, below 16 GiB. Includes released hold receipts, partial summaries, all backed-up tables in the fixture, 92 days of recent detail, weighted events and event summaries. Real signature executor timed out before open completion in this sandbox; primary must rerun it. |
| S14b-2 | R.test_cutoff_summary_counts_and_old_detail_answer; R.test_unreferenced_outcome_leaves_status_counts_and_digest_lookup_answer | Missing sweep / outcome removal red first; green. Exact 90-day cutoff, counts, observation range and permanent summaries verified. |
| S14b-2 | R.test_full_outcomes_and_attested_and_file_backed_rows_stay; R.test_live_windows_Claude_bootstrap_and_latest_reading_preserve_admission_answers | Green: linked outcomes, files, attestations, first Claude pass, latest older reading and unreset windows survive. Admission's selected identities agree before/after. |
| S14b-3 | R.test_past_batch_limit_and_protected_prefix_drain_without_starvation; R.test_sql_work_is_bounded_even_when_every_row_is_protected; R.test_released_capacity_history_does_not_exhaust_retention_work_budget | Green: bounded pages, progress beyond protected prefixes, cooperative work refusal and 5,000 released capacity rows under a reduced read budget. |
| S14b-3 | R.test_summary_failure_rolls_back_and_readers_and_transactions_skip; R.test_failed_fast_continuation_stops_and_batch_ceiling_never_limits_a_run; R.test_collector_maintenance_contains_failure_and_is_the_only_automatic_entry | Green: failed append/delete rolls back, readers/transactions skip, no hot retry after failure, and ordinary writes still work. |
| S14b-3 | I.test_collector_sweep_sql_runs_on_the_E1b_owner_thread | Green, no socket: real ledger owner thread executes the sweep SQL; subsequent ordinary owner work succeeds. |
| S14b-4 | R.test_old_detail_resolves_its_own_summary_after_other_batches; R.test_historical_lookup_past_SQL_work_limit_returns_plain_age_answer | Red first (wrong summary / unbounded lookup); green. Exact ID-digest lookup, typed outcome digest lookup, and plain age answer past the read budget. Unknown IDs still refuse normally when the bounded lookup completes. |
| S14b-4 | R.test_summary_stays_readable_when_live_writer_vocabulary_shrinks; R.test_summary_contract_is_closed_and_old_vocabulary_remains_readable | Red first on shrinking writer vocabulary and malformed count map; green. Independent additive stored vocabularies; 33 represented rows refused; existing record/code validators retained. |
| S14b-5 | I.test_summary_survives_original_source_image_and_learning_transfer; R.test_summary_contract_is_closed_and_old_vocabulary_remains_readable | Green: summary copied through original source imaging, inert learning transfer and streamed backup verification. |
| S14b-5 | Named backup, restore, corpus, upgrade and enumeration modules below | Green except the intentional release digest pin; runtime file-set/sort check fixed and green on the targeted followup. |

The original keep-every-outcome decision failed measured sizing. Its replacement
is documented before outcome implementation in [assertions.md](assertions.md):
unreferenced outcomes expire with the existing learning window; linked outcomes
remain full. No assertions in tests present on the base were changed.

## Implementation bounds and readers

The collector maintenance request calls one page of at most 32 candidates, with
two cooperative read scopes (each 2,048 callbacks, 25 ms, 4 MiB). Protection is
one bulk SELECT, staying inside the shared 16-cursor limit. Selection is indexed
by expiry/project/sequence; the memory cursor advances over protected/malformed
rows and resets at the end. Restart re-reads durable rows safely. Summary append
and removal are one transaction. A failed pass clears immediate continuation.
The existing maintenance loop yields between batches. Open/claim/dispatch never
invoke the new sweep. Small writes remain bounded by the page size; this is not
a hard real-time guarantee against operating-system I/O stalls.

Permanent replay/provenance families and their sizes/readers are in
[readers.md](readers.md) and [record-readers.md](record-readers.md). OP-2's existing
finished-open purge is retained. Controlled evidence, signed evidence, file
lineage, live bindings and unreleased slots pin detail. Summary vocabulary is
independent of live writer vocabularies. Existing row layouts/schema remain 17;
the record type and copier vocabulary are additive. `runtime_files.py` includes
the new module; the digest catalogue is untouched.

Projection scope follows accepted S14: installed retention, typical 106-event
runs, the sourced 387/day peak doubled, ordinary unreferenced outcomes. The
SQLite/backup overhead extrapolation is still S14's 1.745 × 0.972, not a new
five-year physical-database measurement. Explicit owner-controlled evidence,
unusual long-lived runs, owner-selected maximum event retention and different
payload sizes can require more space; the sweep preserves their authority.

## Checks run

- Named runner: 19 modules, 213 tests, 37.103 seconds, **metadata_errors []**,
  no budget failures. Seventeen modules passed completely. The integration
  module's owner-thread and source-transfer cases passed; its real signature
  fixture timed out. Release checks had the expected digest pin plus a file-list
  ordering issue; ordering was then fixed. Sanitised results:
  [test-evidence.json](test-evidence.json).
- Targeted followup: 39 tests in 4.401 seconds; 38 passed, only the intentionally
  unchanged release digest pin failed. Includes all 15 new direct tests, weighted
  event retention excluding its socket-starting class, and runtime file-set/order.
- Additional named learning copier/read modules: 36 tests in 4.284 seconds,
  metadata_errors []; all passed (`test_learning_cycle_core`,
  `test_learning_chunk_membership`, `test_learning_outcome_pages`,
  `test_learning_analysis`).
- Final point-lookup followup: all 15 direct retention tests passed in 1.666
  seconds, including lookup of the retained summary itself by its identifier.
- Core reader/expiry checks plus non-CLI migration classes: 19 tests in 5.936
  seconds; all passed. Includes immutable reviewed-case lookup, frozen case
  lineage, expiry lineage and partition-backup removal.
- Earlier copier run: 12 modules in 35.6 seconds. Two migration CLI cases failed
  at unavailable project-service boundaries; core migration tests passed. Those
  CLI cases are primary reruns, not passing evidence.
- `git diff --check` passed. Ruff was unavailable in the supplied interpreter
  (`No module named ruff`); no dependency was installed. No full suite was run.

Passing complete named modules in the final 19-module run: `test_run_retention`,
`test_lifetime_cap_guard`, `test_learning_contracts`, `test_owner_backup_streamed`,
`test_large_project_backups`, `test_learning_source_image`,
`test_learning_source_image_legacy`, `test_learning_source_transfer`,
`test_account_observation_backup`, `test_backup_installed_project`,
`test_backup_reply_private`, `test_project_learning_corpus`, `test_refusal_steps`,
`test_reply_shape_compatibility`, `test_learning_chunk_upgrade`,
`test_learning_qa_upgrade`, `test_legacy_capacity_upgrade`.

Reproduce the measured sandbox check from repository root, using the supplied
test interpreter as `$PY`:

```sh
$PY -B scripts/measure_s14b_retention.py --stand-in-signatures --output docs/requirements/s14b/measurement.json
$PY -B scripts/parallel_tests.py --module codex:tests.test_run_retention --module codex:tests.test_run_retention_integration --module codex:tests.test_lifetime_cap_guard
```

The second command intentionally retains the real-worker integration gate;
do not interpret the stand-in check as cryptographic or worker-confinement proof.

## Primary reruns and remaining gates

1. Unsandboxed real-worker check: `test_run_retention_integration`, then run the
   measurement script **without** `--stand-in-signatures`.
2. Socket/service suites: `test_account_original_open`, `test_account_claim_jobs`,
   `test_open_request_lifetime`, `test_active_run_capacity`, `test_run_holds`,
   `test_ledger_owner_wiring`, `test_event_retention` (CollectorSweepTests),
   `test_learning_retention`, `test_learning_migration` (MigrationCliTests),
   `test_learning_engine` (includes socket/confinement checks),
   `test_learning_cli`, `test_reply_shape_live`, `test_upgrade_handover`.
   Use a short temporary socket directory and a clean environment as builder
   rules require. No PTY check was run here.
3. Merge the accepted S14 tests/evidence first when forming the batch, then rerun
   its `test_owner_backup_streamed` U7 backup/upgrade boundary cases and
   `test_learning_source_image` source-image boundary case. These assertions are
   absent from this unit's required base and were not copied or weakened here.
4. Run the conservative copier/enumeration inventory in
   [copy-module-inventory.md](copy-module-inventory.md), the primary's
   `--affected ce81bd96`, and affected lifecycle flows (especially L10.3,
   L11.1/L11.2 and account open/claim/settlement) on macOS and Linux. Then the
   quiet batch full-suite gate, review and confirming review.
5. Primary owns VERSION/digest catalogue bookkeeping and the release-pin rerun.
   Run Ruff where available. No commit, merge, install or push was attempted.

## Changed files

- Runtime: `learning_store.py`, `learning_contracts.py`, `run_retention.py`,
  `project_learning_corpus.py`, `runtime_files.py`.
- Tests: `test_run_retention.py`, `test_run_retention_integration.py`,
  `lifetime_caps.json`; `tests/requirements/s14b.json`.
- Tool/registration: `scripts/measure_s14b_retention.py`, `test-layers.json`,
  `test-stages.json`, `test-timings.json`, `test-requirements.json`.
- Documentation: `docs/requirements/s14b.md` and this directory's requirements
  evidence, inventories, assertion map and measurements. No absolute home paths
  or raw test logs are stored in these documents.
