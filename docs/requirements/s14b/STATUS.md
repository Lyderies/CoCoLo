# Fix round — S14b / review 040-s14b-r1

**NOT READY: S14b-1 still fails.** Functional retention, idle and lookup fixes
are in the worktree; the corrected five-year projection is **16.464 GiB**, or
**0.464 GiB over** the unchanged 16 GiB ceiling. This supersedes the earlier
14.219 GiB claim, whose outcome fixture bypassed product provenance.

Branch `feat/s14b-retention`, starting at `ef0c6eb4`, base `ce81bd96`.
No commit was attempted under the sandbox rule. VERSION and the runtime digest
catalogue are unchanged. No sockets, installed services, PTYs, full suite,
merge, installation or push were used.

## Requirement → test → evidence

`R` = `tests.test_run_retention.RunRetentionTests`;
`I` = `tests.test_run_retention_integration.RunRetentionIntegrationTests`.

| Requirement / finding | Test | Evidence |
| --- | --- | --- |
| S14b-1 / HIGH-1 sizing | I.test_real_open_claim_footprint_retires_to_measured_metadata_and_summary | **FAIL**, 16.464 GiB at 774 runs/day for 1,825 days; no threshold relaxation. Product validation and auto-linking, matching terminal host event, and actual claimed run replace the unlinked fixture. Before/retained columns: 16,280 → 5,589. |
| S14b-2 / HIGH-1 behavior | R.test_unreferenced_outcome_leaves_status_counts_and_digest_lookup_answer | Red first: expected 2 swept rows, got 0 with real auto-links. Green: expired terminal host event + outcome commit with one summary, exact counts/status and digest answer. |
| S14b-2 / LOW-1 | R.test_protection_is_checked_inside_write_transaction_and_propagates | Red first: protection check saw no write transaction. Green: recheck is inside BEGIN IMMEDIATE, sharing summary append and deletion. |
| S14b-2 live dependencies | R.test_completion_replay_fence_pins_expired_outcome_and_its_event; R.test_full_outcomes_and_attested_and_file_backed_rows_stay; R.test_live_windows_Claude_bootstrap_and_latest_reading_preserve_admission_answers | Completion-fence regression red first (2 rows incorrectly removed), then green. Full linked group survives any live/controlled member, file, attestation, outside link or completion fence. Existing admission selection remains unchanged. |
| S14b-2 atomicity / progress | R.test_linked_group_rolls_back_together_and_reconsiders_released_protection; R.test_oversize_linked_group_stays_atomic_without_starving_later_work | Green: summary + both detail rows roll back together; releasing protection revisits them. Oversized-group progress test red first (later pair not reached), then green: 34-row group stays full while a later pair sweeps. |
| S14b-3 / MED-1 | R.test_protected_tail_reaches_idle_and_does_not_wrap_on_short_pages | Red first: continuation remained true; 40 read scopes instead of at most 20 for idle checks. Green: 70 pinned rows reach idle; 20 subsequent full-interval checks read only the empty indexed tail. |
| S14b-3 owner budget | I.test_pinned_ledger_reaches_idle_without_delaying_owner_requests; I.test_collector_sweep_sql_runs_on_the_E1b_owner_thread | Green without sockets: 256 pinned rows drain the walk to idle, later maintenance reports no more work, ordinary owner requests remain below the existing 800 ms budget; SQL stays on the owner thread. |
| S14b-3 bounds / failure | R.test_sql_work_is_bounded_even_when_every_row_is_protected; R.test_released_capacity_history_does_not_exhaust_retention_work_budget; R.test_summary_failure_rolls_back_and_readers_and_transactions_skip; R.test_failed_fast_continuation_stops_and_batch_ceiling_never_limits_a_run | Green: bounded SQL, released-history seek, active-reader/transaction skip, rollback, and no 10 ms continuation on refusal/failure. |
| S14b-4 / MED-2 / LOW-4 | R.test_typed_history_and_years_of_summaries_use_indexed_membership | Wrong-type lookup red first. Green at 365,001 summaries: exact old summary, wrong type not found, typo not found. The reviewed JSON scan demonstrably exhausts the same read budget; indexed lookups finish within it. |
| S14b-4 membership / refusal | R.test_historical_lookup_budget_failure_does_not_invent_membership; R.test_mistyped_content_digest_is_not_found; R.test_old_detail_resolves_its_own_summary_after_other_batches | False age-answer regression red first, then green: read refusal propagates, never invents membership. Invalid content digest returns not found. Lookup is also correct after rebuilding the derived index solely from retained summaries. |
| S14b-4 stored vocabulary | R.test_summary_stays_readable_when_live_writer_vocabulary_shrinks; R.test_summary_contract_is_closed_and_old_vocabulary_remains_readable | Green: stored vocabularies stay independent; malformed member-type arrays refuse cleanly. Legacy summary shape remains accepted. |
| S14b-5 copying / restore | I.test_summary_survives_original_source_image_and_learning_transfer; R.test_summary_contract_is_closed_and_old_vocabulary_remains_readable; named copier modules below | Green: optional typed membership survives summary validation, original-source imaging, inert learning transfer and streamed backup verification. Corrected footprint case verifies backup before its failing size assertion. |

## Measured footprint and build cost

[fix-measurement.json](fix-measurement.json) is the corrected product-path
measurement. The signature executor was substituted only in this measurement;
persistence, validation, terminal provenance, auto-links, release, purge,
summaries and backup verification run normally. It does **not** prove real
signature-worker execution. Account attestations measure zero added bytes/run;
no synthetic per-run signature allowance is credited as a saving.

The retained/recent/event projection is 15.201 GiB before the live membership
index. There are 15 indexed members/run. Reserving 64 bytes/member over five
years adds 1.263 GiB, bringing the total to **16.464 GiB**. The accepted S14
1.745 database/column × 0.972 planned-backup/database factor remains the source
for persistent overhead; the rebuildable index is counted once, without backup
duplication. Even using the measured ~50.54 bytes/member instead of the 64-byte
reserve leaves the projection above 16 GiB. There is no positive stated margin.
Payload variance, controlled evidence and long-lived runs cannot improve this
result. Further storage reduction or an owner-approved plan revision is needed.

[fix-index-measurement.json](fix-index-measurement.json): 365,001 summaries,
50,000 product-path detail records, **545,497,088 bytes (520.2 MiB)** ledger.
Rebuilding the five persistent indexes took **0.197 s**; rebuilding the derived
membership index took **4.243 s**, with **18,448,384 bytes** of temp pages,
**50.543 bytes/member**. Three old/wrong-type/unknown lookups took **0.518 ms**
total under the existing 2,048-callback / 25 ms / 4 MiB SQL read budget. Persistent
index creation is an upgrade cost; the derived index rebuild recurs on each
ledger open, so its startup cost remains material at larger sizes.

The real worker was attempted and timed out before open completion (20.627 s
including cleanup); [fix-real-signatures.json](fix-real-signatures.json) records
that no real-worker footprint was collected. Primary must rerun it unsandboxed.

## Implementation bounds

A seed page contains at most 32 detail rows. Expired linked host events are
included through bounded same-run closure, never by sweeping arbitrary host
history. Distinct expired same-run members can retire together; external links,
self-links, stable control and live authority protect the whole component.
Groups larger than the page bound remain intact. At most 12 component queries
share one selection read scope, avoiding the shared 16-cursor limit. Page,
selection and transactional protection each have a cooperative 25 ms SQL scope;
these are not hard OS I/O latency guarantees.

The cursor survives short pages instead of wrapping. Dependency-release TEMP
triggers invalidate the completed walk; historical inserts also rewind it.
Stable pinned rows stay skipped while dependencies are unchanged. Time-sensitive
admission evidence gets a daily reconsideration, with full-interval backoff;
empty tails report no more work. Restart safely rewalks durable rows. No new
work is called from open/claim/dispatch.

Summaries remain durable authority-free history. An optional aligned
`record_id_types` array proves each new member's type. A compact TEMP B-tree
maps project, digest, lookup kind and type to the permanent summary sequence;
it is rebuilt at open and updated atomically with new summaries. No main table,
column, schema version or backup layout changes. Older mixed summaries cannot
prove an individual member's type: they remain readable and support untyped
membership, but ambiguous typed lookups refuse rather than guess. Reader and
copier inventories remain in [readers.md](readers.md) and
[copy-module-inventory.md](copy-module-inventory.md).

## Checks completed

- Named 23-module run: **255 tests, 53.6 s, metadata_errors []**. Retention direct
  tests and all selected copier modules passed. Failures: new component cap was
  not yet registered (fixed and rerun green below); real worker timed out;
  intentional runtime-release digest pin. Sanitised runner result:
  [fix-named-evidence.json](fix-named-evidence.json).
- Final named followup: **74 tests across 7 modules, 49.7 s, all green,
  metadata_errors [], budget_failures []**. Includes all 22 direct retention
  tests, cap guard, contracts, learning retention and append/type/candidate
  lookup readers. [fix-followup-evidence.json](fix-followup-evidence.json).
- Non-socket migration classes (`LearningMigrationTests`, `EmptyRetirementTests`,
  `ActivationSchemaTests`, `DrainedRuleTests`) and event-retention classes
  (`RetentionPolicyTests`, `EventRetentionTests`): **36 tests, 7.091 s, green**.
- Final non-worker retention integration cases + cap guard: **7 tests, 1.280 s,
  green**. Exact-once read-refusal followup: **1 test, 0.103 s, green**.
- Stand-in corrected footprint: **1 test, 0.771 s, FAIL only at unchanged size
  cap**, after product-path retirement and backup verification passed.
- Dedicated scale measurement: **1 test, 44.540 s, green** (includes fixture
  population, index builds and bounded lookup comparisons).
- `git diff --check` passed. No full-suite run. The runtime digest failure is
  intentionally left for primary release bookkeeping; VERSION/catalogue remain
  unchanged. Assertion replacements are recorded in [assertions.md](assertions.md).

The 23-module run covered `test_run_retention`,
`test_run_retention_integration`, `test_lifetime_cap_guard`,
`test_learning_contracts`, `test_owner_backup_streamed`,
`test_large_project_backups`, `test_learning_source_image`,
`test_learning_source_image_legacy`, `test_learning_source_transfer`,
`test_account_observation_backup`, `test_backup_installed_project`,
`test_backup_reply_private`, `test_project_learning_corpus`, `test_refusal_steps`,
`test_reply_shape_compatibility`, `test_learning_chunk_upgrade`,
`test_learning_qa_upgrade`, `test_legacy_capacity_upgrade`,
`test_learning_cycle_core`, `test_learning_chunk_membership`,
`test_learning_outcome_pages`, `test_learning_analysis`, `test_runtime_release`.

## Primary reruns / unmet gates

1. **S14b-1 storage gate remains failed**; do not merge as satisfying the 16 GiB
   requirement. Decide the next storage reduction/re-plan outside this one round.
2. Run `scripts/measure_s14b_retention.py` without `--stand-in-signatures`, and
   `test_run_retention_integration`, where real signature workers can execute.
   Reproduce scale cost with `scripts/measure_s14b_retention.py --index-scale`.
3. Socket/service suites not run here: `test_account_original_open`,
   `test_account_claim_jobs`, `test_open_request_lifetime`,
   `test_active_run_capacity`, `test_run_holds`, `test_ledger_owner_wiring`
   (socket cases), `test_event_retention.CollectorSweepTests`,
   `test_learning_migration.MigrationCliTests`, `test_learning_engine`
   (socket/confinement cases), `test_learning_cli`, `test_reply_shape_live`,
   `test_upgrade_handover`. Use the builder rules' short TMPDIR and clean env.
   No PTY checks were run.
4. Run the remaining conservative copier/enumeration inventory, primary affected
   and batch/full-suite gates, macOS/Linux lifecycle/settlement flows, accepted
   S14 backup boundaries after batch assembly, and review. The optional summary
   shape was exercised in all selected non-socket copier modules above; this is
   not a claim that every service-containing inventory module was run here.
5. Primary owns VERSION/digest-catalogue bookkeeping and the release-pin rerun.

## Files changed in this round

- Runtime: `learning_store.py`, `learning_contracts.py`, `run_retention.py`.
- Tests: `test_run_retention.py`, `test_run_retention_integration.py`,
  `lifetime_caps.json`; registration: `scripts/test-requirements.json`.
- Measurement tool: `scripts/measure_s14b_retention.py`.
- Requirements/evidence: `docs/requirements/s14b.md`, this STATUS,
  `assertions.md`, `fix-measurement.json`, `fix-index-measurement.json`,
  `fix-real-signatures.json`, `fix-named-evidence.json`,
  `fix-followup-evidence.json`.
