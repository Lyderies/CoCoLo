# Final fix — S14b

The planned five-year backup/source image now projects to **15.201 GiB** at
774 runs/day (2× peak) for 1,825 days, leaving **0.799 GiB** below the unchanged
**16 GiB** `learning_source_image.MAX_BACKUP_BYTES` hard product ceiling.
The former **16.464 GiB** projection incorrectly applied that backup ceiling to
backup bytes plus a connection-local, rebuildable membership index.

Live storage is reported separately: **15.639 GiB** estimated main database
plus **0.789 GiB** reserved compact TEMP membership index = **16.428 GiB**.
The live database is **not** claimed to fit below 16 GiB. The live estimate uses
the accepted S14 `.972` planned-backup/database factor in reverse and reserves
40 bytes for each of 21,188,250 membership entries. Payload variance, controlled
evidence and long-lived runs retain the prior projection's limits.

[final-measurement.json](final-measurement.json) records the product-path
measurement: 16,280 before / 5,589 retained column bytes per run, including
actual terminal provenance, outcome auto-links, claimed run, released receipts,
permanent summaries, 92 days recent detail and weighted event retention. Only
signature execution was substituted; persistence, validation, provenance,
retention and actual backup verification were exercised. The real signature
worker still times out in this sandbox before open completes; primary must
rerun it unsandboxed. Neither the 16 GiB ceiling nor any existing lookup equality,
type or not-found assertion was relaxed.

Branch `feat/s14b-retention`, starting at `e32ade83`, base `ce81bd96`.
Changes are uncommitted, as required under the sandbox. VERSION, schema versions,
permanent record layouts, backup manifests and runtime digest catalogue are
unchanged. No installed services, sockets, PTYs, full suite, merge, install or
push were used.

## Requirement → test → evidence

`R` = `tests.test_run_retention.RunRetentionTests`;
`I` = `tests.test_run_retention_integration.RunRetentionIntegrationTests`.

| Requirement | Matching checks | Evidence |
| --- | --- | --- |
| S14b-1 | I.test_real_open_claim_footprint_retires_to_measured_metadata_and_summary; R.test_backup_bytes_exclude_index_and_restored_rows_rebuild_answers | Product projection red on old 32-byte keys (15 noncompact entries); green with stand-in signatures at 15.201 GiB, strict `< MAX_BACKUP_BYTES`, additional `> .79 GiB` margin. Populated/empty-index backup files are byte-identical, estimates equal. The exclusion property was already correct on e32ade83 because the old index was TEMP. |
| S14b-2 | All existing R cutoff, dependency, outcome/status, linked-group, rollback and admission-selection checks | All functional assertions passed in the named retention module; historical summary contents and product retention behavior remain unchanged. |
| S14b-3 | R.test_reopen_upgrade_rebuild_is_paged_resumable_and_lookup_is_safe; I.test_collector_sweep_sql_runs_on_the_E1b_owner_thread; I.test_pinned_ledger_reaches_idle_without_delaying_owner_requests; I.test_import_rebuild_pages_resume_and_preserve_digest_answers | Reopen regression red first: startup built all 70 entries instead of zero. Green: at most 32 summaries per owner turn, two projects, epoch-16 upgrade, mid-rebuild restart, read refusal retains checkpoint, ordinary live reads work; E1b rebuild SQL stays on its owner. Imported-owner interruption retains its checkpoint, resumes, and ordinary project cleanup completes rebuilding. |
| S14b-4 | R.test_equal_prefixes_never_return_another_members_summary; R.test_reopen_upgrade_rebuild_is_paged_resumable_and_lookup_is_safe; R.test_typed_history_and_years_of_summaries_use_indexed_membership | Forced prefix seam absent on e32ade83; green with 12-byte WITHOUT ROWID keys. Equal prefixes across distinct IDs and outcome digests return no foreign summary; a lost same-type collision returns not found. Full digest and member type are verified against the permanent summary before returning it. During rebuilding, misses and SQL read refusal return plain `history index rebuilding`; after completion, original exact history/type/not-found assertions remain. |
| S14b-5 | R.test_backup_bytes_exclude_index_and_restored_rows_rebuild_answers; R.test_reopen_upgrade_rebuild_is_paged_resumable_and_lookup_is_safe; I.test_summary_survives_original_source_image_and_learning_transfer; I.test_import_rebuild_pages_resume_and_preserve_digest_answers | Restore's scheduled-rebuild expectation and imported-history API failed before implementation. Green: actual backup summary rows rebuild the same answers; source archive bytes and sizes match the original backup exactly; imported-owner ID/digest answers equal original-owner summaries after paged rebuilding and restart. No membership table is copied. |

## Implementation and collision bound

`retention_members` stays TEMP, with a WITHOUT ROWID key containing a **12-byte
(96-bit) digest prefix**, numeric project/import reference, lookup kind and stored
member type. At 774 × 1,825 × 15 = **21,188,250 entries**, the birthday union bound
is `n(n−1)/2^97 < 2.84e−15`. This is a probabilistic efficiency/wording bound,
not the data-safety mechanism: full digests and types in each candidate summary
are checked before returning any data. `INSERT OR IGNORE` may lose a colliding
membership; it cannot make another item's summary a valid answer.

Opening/restoring/upgrading schedules rebuilding, with only an indexed existence
probe at open. Ledger maintenance seeks at most 32 summaries by project and
rowid through the existing `records_type_project` index; **no new persistent
index** is needed. Each page uses the existing 25 ms / 2,048-callback / 4 MiB
cooperative SQL read scope, commits its TEMP entries before advancing the cursor,
and yields the owner between pages. An interrupted/failed page retains its
checkpoint. Active readers/transactions skip work; refusal/failure ends fast
continuation until the next ordinary maintenance interval. New summaries still
update membership atomically with their permanent append. Existing exclusive
VACUUM maintenance invalidates numeric references and reschedules this walk.

The source-image import's project owner also builds only TEMP membership from
its permanent `project_learning_records`: bounded 32-summary pages use the
existing `(project, import, type, sequence)` index. Import chunks schedule the
walk; owner `expire_results` maintenance advances it. Restart reconstructs its
scheduling from durable imported summaries. This grants no learning/account
activation or evidence authority. The explicit imported-history lookup checks
full digests/types through the same verifier as the original ledger.

Lookups return a transient `{'record_type': 'history_index_rebuilding',
'detail': 'history index rebuilding'}` when membership is not yet known during
the walk. It is never persisted or presented as proof of membership. Live records
continue to resolve normally; already verified historical hits may return their
summary during rebuilding. Completed-index read refusals retain the existing
SQLReadError behavior rather than inventing membership.

## Checks and remaining gates

- Initial named core run: **154 tests, 91.1 s, metadata_errors []**; all functional
  assertions passed except the real signature-worker timeout. The retention
  module exceeded its 60-second runner budget at 91.125 s while other copier
  tests also ran. [final-core-evidence.json](final-core-evidence.json).
- Named copier/enumeration run: **152 tests, 32.5 s, metadata_errors [],
  budget_failures []**. Only failure: intentional `test_runtime_release` digest
  pin, left for primary release bookkeeping. [final-copiers-evidence.json](final-copiers-evidence.json).
- Product-path stand-in projection: **1 test, 0.868 s, green**. The real worker
  attempt in the core run timed out at the existing ten-second open pump bound
  (21.9 s including cleanup), before the footprint check.
- Direct final regressions: paged reopen, forced collisions, backup restore,
  imported lookup/restart and E1b owner rebuild: **5 tests, 1.068 s, green**.
  Additional two-project/imported-page/interruption check: **2 tests, 0.854 s,
  green**.
- Isolated named retention (before the VACUUM followup): **25 tests, all functional tests green,
  metadata_errors [], requirement_failures []**, but the runner remains **FAIL
  on its unchanged execution budget: 76.681 s > 60 s**. This is an unresolved
  merge gate, not a claimed all-green run.
  [final-retention-evidence.json](final-retention-evidence.json).
- Final non-socket migration/event classes plus all four non-worker integration
  checks: **40 tests, 7.516 s, green**. Includes imported pages/interruption,
  project cleanup advancing the rebuild, source-image restart and E1b owner SQL.
- Full-digest case normalization followup: added uppercase-digest assertion
  failed first with not found; byte-normalized full-digest verification now
  preserves that prior answer. **4 targeted tests, 1.636 s, green**.
- Final scale check: **1 test, 100.826 s, green** (separate measurement,
  below the builder's five-minute run limit). **365,001 product summaries +
  50,000 product details**, **545,497,088 bytes** main ledger, compact TEMP index
  **10,203,136 bytes / 27.954 bytes per entry**, below the strict 40-byte reserve.
  Rebuild: **11,407 bounded pages**, **24.175 s** cumulative build time; three
  typed/unknown lookups: **0.494 ms** total. This cumulative time is not one
  owner-thread operation. Five existing persistent indexes rebuilt in **0.280 s**.
  [final-index-measurement.json](final-index-measurement.json).
- Added exclusive-maintenance regression: existing project deletion invokes
  VACUUM, which may change rowids used by the walk/project references. Red first:
  no rebuild was scheduled. Green: maintenance discards derived references and
  schedules fresh pages, preserving safe lookup and the exact historical answer.
  This case + actual backup restore + 15 existing original-store backup,
  schema-five migration, expiry and deletion checks: **17 tests, 2.917 s, green**.
  Empty-history maintenance uses an indexed existence probe and keeps ordinary
  not-found behavior.
- Final named copier/reader followup after the VACUUM invalidation: **100 tests
  across six modules, 5.8 s, all green, metadata_errors [], budget_failures [],
  requirement_failures []**.
  [final-followup-evidence.json](final-followup-evidence.json).
- Final `git diff --check` passed. No test run remains in flight.

Named modules covered (all `codex:tests.`): `test_run_retention`,
`test_run_retention_integration`, `test_project_history`,
`test_project_learning_corpus`, `test_learning_source_transfer`,
`test_learning_source_image`, `test_learning_source_image_legacy`,
`test_learning_chunk_upgrade`, `test_learning_qa_upgrade`,
`test_legacy_capacity_upgrade`, `test_owner_backup_streamed`,
`test_large_project_backups`, `test_account_observation_backup`,
`test_backup_installed_project`, `test_backup_reply_private`,
`test_learning_contracts`, `test_learning_chunk_membership`,
`test_refusal_steps`, `test_reply_shape_compatibility`, `test_learning_cycle_core`,
`test_learning_outcome_pages`, `test_learning_analysis`, `test_learning_retention`,
`test_runtime_release`; selected original-store backup/migration/expiry checks
in `test_learning_store`, plus non-socket classes of `test_learning_migration`
and `test_event_retention`. No new test module or requirement ID was added; new
methods are registered under the existing S14b requirements. Existing modules
remain registered as integration/scenario. Imports perform no persistence I/O.

Primary must rerun these socket/service tests with the builder rules' short
TMPDIR and clean environment: `test_account_original_open`,
`test_account_claim_jobs`, `test_open_request_lifetime`,
`test_active_run_capacity`, `test_run_holds`, `test_ledger_owner_wiring`
(socket cases), `test_event_retention.CollectorSweepTests`,
`test_learning_migration.MigrationCliTests`, `test_learning_engine`
(socket/confinement cases), `test_learning_cli`, `test_reply_shape_live`,
`test_upgrade_handover`, `test_history_service_upgrade`,
`test_state_location.LegacyStateMoveTests`. No PTY checks were run.
Primary must also resolve/recheck the unchanged 60-second retention test-module
execution budget (76.681 s in isolation here); no timing ceiling was raised.
Primary also owns the real-signature measurement/integration rerun, digest
catalogue/release pin, remaining conservative inventory in
[copy-module-inventory.md](copy-module-inventory.md), affected/batch/full gates,
macOS/Linux lifecycle checks and accepted S14 backup boundaries after assembly.

## Files changed

Runtime: `learning_store.py`, `run_retention.py`, `project_history.py`,
`project_learning_corpus.py`. Tests: `test_run_retention.py`,
`test_run_retention_integration.py`. Measurement/registration:
`scripts/measure_s14b_retention.py`, `scripts/test-requirements.json`.
Requirements and evidence: `docs/requirements/s14b.md`, this STATUS,
[assertions.md](assertions.md), `final-*-evidence.json`,
`final-measurement.json`, `final-index-measurement.json`.

## Regression fix

Gate exception from the primary's unsandboxed `--affected ce81bd96` run
(`evidence/affected-after-final.txt`). Only these four regressions were changed.
VERSION, schema version (17), record layouts and the digest catalogue are untouched;
`test_runtime_release` still needs the primary's digest bookkeeping because three
runtime files changed (learning_store, run_retention, cli).

1. **RS-A-G6 idle read.** Maintenance now knows whether it is due without any
   statement. At open one indexed read stores
   `_run_retention_due_ms` (earliest retainable expiry) and `_retention_ready`.
   Appends lower the due time in `_append_checked_locked`; an empty walk from the
   start raises it to the next expiry. `sweep()` compares two Python numbers first
   and returns with no SQL (no `SELECT n FROM retention_revision`, no PRAGMAs) when
   nothing can be due. The idle path is the base cheap read again.
2. **No writes on open.** Two base tests pin the stored layout from both sides
   (a fresh default open must equal the schema-11 layout after the known additive
   indexes are dropped, and a consented upgrade open must equal a fresh open), so
   no open of any kind may add a persistent object. The five retention/protection
   indexes are now created only by `LearningLedger.create_retention_indexes()`,
   which `cli.py` calls in the `--upgrade-ledger` registration step (drained offline
   owner, after the whole-ledger copy when the ledger is behind). It is refused
   while a collector is active or for a sole-owner ledger. A default open makes only
   TEMP objects, so the schema digest and the refuse-without-writes, rollback and
   byte-identical checks are as on base. Without all five indexes maintenance does
   nothing (no SQL). `test_plain_open_makes_no_index_and_the_consented_step_enables_retention_with_a_silent_idle`
   covers both halves. **Open point for the primary:** a ledger never put through
   `--upgrade-ledger` runs no retention; if the owner wants a same-version ledger to
   gain the indexes without that flag, that is a decision beyond this exception.
3. **Unlisted cap.** `lifetime_caps.json` registers `advance_import_history` and the
   nested `rows` page reader with their `ends` and past-the-limit tests (see
   assertions.md).
4. **Module budget.** The 365,000-summary test (48.9 of the module's 50 s) is its own
   registered module `test_run_retention_scale` (36 s here); `test_run_retention` is
   now about 3 s. No ceiling was raised.

Evidence, unsandboxed, short TMPDIR, `env -i`: the four modules plus
`test_run_retention*`, `test_learning_store`, `test_learning_migration` and
`test_history_service_upgrade` were green (216 tests, metadata_errors [],
budget_failures []). Five-year backup projection re-measured with the real
signature worker: **15.198 GiB** at 774 runs/day (0.802 GiB below the 16 GiB
ceiling); live database estimate 16.425 GiB, reported separately as before
([regression-measurement.json](regression-measurement.json)). Index scale:
365,001 summaries, 27.95 bytes/entry, three lookups 0.4 ms
([regression-index-measurement.json](regression-index-measurement.json)).

## S14b-6 (decision 349, IP-B batch wiring)

Requirement S14b-6 (S14b-5 was already taken by the summaries requirement): the five retention indexes are created
inside the consented records-upgrade step on every path that performs it, never on a plain or unconsented open.

- `cli learning-register --upgrade-ledger` (offline owner): already called `create_retention_indexes()` (regression fix
  above). Test `test_s14b_upgrade_indexes...test_cli_upgrade_ledger_creates_the_five_indexes_and_a_sweep_removes_a_record`
  (a schema-6 ledger, so it passes through 17 -> 18): the unapproved run leaves 0 indexes (d), the approved run 5, then
  a sweep at the cutoff removes the old record. Green on the merged tree before any code change (guard, not red).
- Registration while a collector runs (the early return in `_register_project`): a running collector means an earlier
  consented step already made the ledger current and created the indexes; creation itself is refused while a collector
  is active, so nothing can be added there. Test `...test_registration_with_a_running_collector_keeps_the_indexes_of_the_consented_step`
  (second `--upgrade-ledger` returns `ledger_upgraded_from: None`, indexes still 5). Also green before the change.
- B4t takeover adoption (`takeover_owner.OwnerOperations.adopt_ledger`): RED before (0 indexes after adoption of the
  archived 0.8.66 store, `5 != 0`), now calls `ledger.create_retention_indexes()` inside the owner open. Test
  `test_b4t_takeover.AccountLineageTests.test_S14b_6_takeover_adoption_creates_the_retention_indexes_on_the_consented_open`.
- `learning-migrate-project` (`cli._migrate_learning`) opens with `allow_schema_upgrade=True` and CAN perform the 17 -> 18
  step: RED before (schema 18, 0 indexes), now it creates the five indexes in the same drained-owner operation when the
  open performed the step (`_ledger_takes_additive_step`). Test `test_learning_migrate_that_performs_the_schema_step_creates_the_indexes`.
- Primary decision (running-collector registration): accepted as is. In 0.8.68 a live ledger reaches 18 only through
  takeover adoption (creates the indexes), `--upgrade-ledger` or learning-migrate (both create them); no new collector
  operation. The running-collector test stays as a guard.
- Registered: requirement in `tests/requirements/s14b.json` and `scripts/test-requirements.json`, module
  `test_s14b_upgrade_indexes` in layers/stages/timings. Live proof after install (indexes present on the local ledger,
  `retention_due` set) is still to do (decision 349).
