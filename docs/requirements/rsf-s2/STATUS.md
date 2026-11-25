# RS-F S2 status — 2026-11-25

## Fixed-scope full-run correction — current handoff

The primary's unsandboxed full run (876 s, 459 units, metadata `[]`) superseded
the earlier targeted evidence below. Its nine supplied failure logs are retained
in [fix-round-primary-failures](fix-round-primary-failures/). This round fixes
only the requested items; changes remain uncommitted. VERSION and the runtime
digest catalogue are untouched. No socket/native-process rerun is claimed here.

Runtime paths in the cause column abbreviate
`native-package/.codex/orchestration/codex_agents` as `codex_agents`; other paths
are relative to `packages/codex-agents`. Line numbers refer to the corrected
worktree (the preserved logs retain pre-fix line numbers).

| Item / requirement | Cause file:line and fix | Tests and evidence | Primary rerun |
| --- | --- | --- | --- |
| 1 — RSF-S2-1/2/4/6 | `native-package/.codex/orchestration/cli.py:872` rejected `ledger_schema_behind()` before maintenance could stop the old collector. `_schema_behind`, `codex_agents/learning_store.py:1588`, and `_connect:2412` treated the epoch-only step as a record-changing upgrade. Exempt schema 16 from extra record-upgrade consent, while retaining the lifetime lock and all older gates. The log shows `ledger_upgrade_required`, not a stranded writer-lock refusal. | `test_rsf_s2_regressions.FenceRegressionTests.test_schema16_registration_gate_waits_for_writer_then_upgrades_without_record_consent`: red → green. A held prior lock leaves version 16 and no epoch; after release, default open registers, preserves records, and claims exactly epochs 1 then 2. | Both `test_upgrade_handover.Earlier0855Tests` and `Earlier0858Tests.test_registering_stops_a_running_earlier_collector_and_completes`; requires old collector sockets/processes. |
| 2 — RSF-S2-2/6 | `codex_agents/learning_store.py:3520` unnecessarily called `_require_project_migration_backups(16,17)`. Remove that call: this step adds only `runtime_takeover`, changes no project records, and follows the existing additive-step precedent. `tests/test_project_start.py:1463` incorrectly expected the untouched 0.8.59 binary to have written the current schema. Pin its actual version 16; the later current-open assertion still requires 17. | `test_schema16_explicit_upgrade_does_not_request_project_backups`: red → green, backup gate patched to fail if called, exact record rows retained. `test_record_changing_schema15_still_requires_explicit_upgrade`: green, older consent unchanged. All 236 copier/backup/restore/source tests in the batch pass. | `test_project_start.ReleasedLedgerUpgradeTests` E2a-1 backup/restore/move and E2c-10 released-runtime cases require sockets/processes. |
| 3 — RSF-S2-3 | `codex_agents/store_fence.py:175`: SAVEPOINT previously entered the automatic BEGIN/commit branch, immediately destroying the named savepoint and committing fixture corruptions. Handle SAVEPOINT as transaction control and RELEASE as a commit boundary; permit ROLLBACK TO after revocation. `codex_agents/project_cleanup.py:202`: historical material was checked before live custody, so corruption could mask `history_not_authorized`. Check custody before entering the transaction, then retain every existing binding check and custody recheck. | Nested standalone/explicit named savepoints and rollback-to, plus epoch-refused outer RELEASE: both new tests red → green for both owners. `test_cleanup_checks_live_custody_before_historical_material`: red → green. Unchanged `test_release_batch_queries` (3), `test_learning_cycle_core` (15), and `test_project_cleanup` (8) pass. The process assertion was not changed. | `test_project_execution_processes.ManagedProjectExecutionProcessTests.test_cleanup_recovery_needs_original_journals_ack_and_consumption_after_supervisor_exit` needs native supervision/sockets. |
| 4 — RSF-S2-6 | `tests/test_launch_receipts_layout.py:31` pinned current schema 16. Pin 17 and add its layout hash; retain the original schema-15 hash assertion. Both hashes are `e2d505aac99614b436bb135af66d7e62c9bedf10309cb4688e6cce552c27fa9f`. The paired upgrade is the epoch-only 16→17 branch, with no receipt DDL/row change. | Both unchanged-layout/reason tests pass after the supplied red. | None for this item. |
| 5 — RSF-S2-3/6 | `codex_agents/project_history.py:495` checked the filesystem fence for all operations, including reads. Use a read-only process/thread/closed-owner check in `project_holds_report.py:120` and its nested `result_retirement_repairs` read at `project_history.py:3210`; retain the fence on mutations. | All 26 `test_holds_report` tests pass, including the unchanged `test_B7_1_clock_floor_and_read_only_no_private_file_reads`: supplied red → green. | None for this item. |
| 6 — RSF-S2-6 | `codex_agents/learning_store.py:2322`'s legacy schema comparison matches the count guard. Add `learning_store.py::__init__` to `tests/lifetime_caps.json` with its end (one connection; singleton epoch replacement, no accumulating quota) and the named schema-16 upgrade/reopen test. | All four `test_lifetime_cap_guard` tests pass: supplied red → green. | None for this item. |
| 7 | Expected `test_runtime_release` current-digest failure remains the primary's merge bookkeeping. No VERSION/catalogue edits or extra runtime-release rerun in this round. | Previous expected failure retained below. | Primary handles at merge. |

All six new unit tests are registered as **unit** stage/layer, with timing and
requirement links. Their red run has `metadata_errors: []` (6 tests, 5 failures,
1 error, including nested subtest failures); their final run has all six green.
The full red log is [fix-round-unit-red.log](fix-round-unit-red.log).

Final named batches: 11 successful modules / **110 tests** in the 4.200 s
unit batch, plus **236 tests** in 18 copier modules / 20 units in **22.500 s**.
Both have `metadata_errors: []`, no budget failures, and no partial work. Nine
existing in-process S2 fence tests also pass in **0.381 s**, covering RSF-S2-1
through RSF-S2-6, including exact refusal text, epoch corruption, failed
migration rollback, hard links, lost/replaced ownership and same-build reopen.
Raw summaries and full module/test outcomes are in
[fix-round-evidence.json](fix-round-evidence.json),
[fix-round-final-unit.txt](fix-round-final-unit.txt),
[fix-round-copiers.txt](fix-round-copiers.txt), and
[fix-round-fence-unit.txt](fix-round-fence-unit.txt).
After explicitly closing the new unit fixtures' raw SQLite connections, the six
new regressions pass again in **0.500 s**, `metadata_errors: []`:
[final regression rerun](fix-round-final-regressions.txt). RSF-S2-5's exact
refusal evidence in the nine-test pass is
`FenceTests.test_lost_or_replaced_lifetime_lock_refuses_admissions` and
`FenceTests.test_unsafe_hard_link_lock_is_refused_before_writing_an_owner_stamp`.

The unit batch also attempted the separate native-worker module
`test_cleanup_custody`: **7 pass / 1 fail**, at
`test_reaped_process_before_actual_worker_return_is_still_unavailable`'s
`cleanup_confirmed` fixture assertion, before any changed cleanup issuance code.
Its worker/supervisor modules are untouched. The exact cause in this restricted
environment is unverified; preserve it for the primary's native-process rerun:
[log](fix-round-cleanup-custody.log). Including that module, the batch ran 118
tests; it is deliberately not reported as an entirely green run.

The prior exhaustive copier/enumeration audit and primary full run remain the
baseline for mixed modules requiring sockets or native process inspection:
`test_learning_store`, `test_learning_migration`, `test_project_start`,
`test_upgrade_handover`, `test_project_execution_processes`, and the other
previously identified service modules below were not rerun against denied
facilities. The current copy batch covers both backup formats, isolated restore,
offline move/source transfer, legacy images, schema upgraders and code inventories.
Exact commands and remaining primary reruns are in
[fix-round-commands.txt](fix-round-commands.txt).

Only two pre-existing assertions changed this round (released fixture
current-version → 16, launch-layout current-version 16 → 17); both replacements
and the additional schema-17 hash assertion are paired in
[assertions.md](assertions.md#fixed-scope-full-run-correction-assertions-2026-11-25).
This round's files: [fix-round-files.txt](fix-round-files.txt).
`git diff --check` passes. No run is left in flight.

## Prior S2 implementation evidence (before the primary's full run)

Implemented in `feat/rsf-s2-fence`, based on `cbc757d5` (0.8.60). Changes remain
uncommitted in this worktree as requested. No install, merge, push, VERSION or
runtime digest catalogue change was made. No live account/project service was
used. Tests used disposable roots and genuine archived runtime files.

## Requirement → test → evidence

All method names below are in `tests.test_rsf_s2_fence`. `F` means `FenceTests`;
`H` means `HistoricalExecutableTests`. All **12 tests pass** in the final run,
with **`metadata_errors: []`** and no budget failures (2.313 seconds including
five separate `test_ledger_startup` tests). Exact outcomes are in
[evidence.json](evidence.json), and commands in [commands.txt](commands.txt).

| Requirement | Tests | Evidence |
| --- | --- | --- |
| RSF-S2-1 | `H.test_real_0855_owners_block_migration_then_cannot_reopen`; `F.test_lost_or_replaced_lifetime_lock_refuses_admissions`; `F.test_another_lock_holder_is_not_mistaken_for_our_lifetime_ownership`; `F.test_paused_ledger_writer_cannot_resume_after_successor`; `F.test_paused_project_process_cannot_resume_after_takeover`; `F.test_unsafe_hard_link_lock_is_refused_before_writing_an_owner_stamp` | Red → green. Real 0.8.55 holds each lifetime flock: migration leaves version/epoch untouched. Wait for its process exit, then migrate. Replaced/unlinked/unlocked ownership, another descriptor's flock and an unsafe hard link refuse. Paused current writers cannot resume writes after replacement. |
| RSF-S2-2 | `F.test_epoch_advances_and_signed_records_are_unchanged`; `F.test_epoch_is_inside_the_schema_step_before_each_legacy_adoption_and_rolls_back_with_it`; `F.test_current_format_missing_epoch_refuses_without_recreating_it` | Initial missing epoch and subsequent missing-row reset tests red → green. Both adoption callbacks observe epoch 1 inside their schema transaction; injected failure leaves the old version and no epoch table. Successful reopen increments exactly once and retains old/new digests. A missing current-format epoch is corruption, not permission to reset it. |
| RSF-S2-3 | `F.test_epoch_gate_covers_connection_cursor_batch_script_and_maintenance`; `F.test_commit_rechecks_epoch_and_rolls_back`; both paused-writer tests | Red → green for explicit/implicit writes, created and returned cursors, cursor batch writes, scripts, BEGIN, commit, VACUUM and journal maintenance. Epoch-only corruption refuses even while the lifetime lock remains held. A native fault between write and commit rolls back. A lock-only mutation of `StoreFence.check` fails both epoch/commit tests as expected: [mutation evidence](epoch-mutation.txt). |
| RSF-S2-4 | `H.test_real_0855_owners_block_migration_then_cannot_reopen`; `H.test_old_offline_cli_cannot_reopen_migrated_ledger` | Red → green. Unmodified `git archive 8d4a7ce3` code cannot reopen ledger 17 / history 22. Historical record bytes/digests survive. The actual old offline backup CLI receives its own valid runtime authorization first, reaches the newer-schema refusal, and changes no database bytes. |
| RSF-S2-5 | lost/replaced-lock, unsafe-hard-link and paused-project tests above | Green. Current fencing refusals include the literal trusted command `./setup.sh --update`; tests escape the punctuation or check the literal substring. Existing busy/wrong-owner reason codes remain. Old executable messages are frozen; their schema refusal is the negative proof. |
| RSF-S2-6 | atomic-adoption / historical-executable tests above; named copier/backup/source/upgrade modules below | Green for the completed store/copy checks. Genuine old records upgrade; current source metadata accepts 17 and retained metadata still accepts 13–16. Machine-local ledger epochs are excluded from per-project learning archives. Historical rows, signed bytes, receipts and backup-column assertions remain checked. |

## Implementation boundary

`store_fence.py` owns the singleton epoch and connection boundary. Both owners
stamp and retain the original protected lock inode/descriptor before SQLite is
opened. Every write checks that owner and epoch after BEGIN and before commit;
returned cursors are fenced too. The ledger's file-writing/removal helpers also
check ownership before touching their artifacts. SQLite forbids VACUUM and
journal transitions inside a transaction, so these check the epoch in a short
serialized transaction, retain the lifetime flock across the operation and
check again afterwards. Session-only PRAGMAs used by bounded readers do not
become write transactions.

The historical-writer proof uses the stores' lifetime-flock contract: release
retires that connection. The real-executable fixture additionally waits for the
old process to exit. This slice refuses a held predecessor; signalling services,
non-idle child disposition and record-custody takeover remain B4/S6 work. It
provides no compatibility route for an old client/runtime. Native code holding
raw SQLite base-class/file-descriptor capabilities is outside the cooperative
owner API; the tests use those capabilities only for explicit fault injection.

## Validation completed

- Requirements were written before tests, then tests before implementation.
  Initial red: 7 tests, 2 failures and 6 errors, `metadata_errors: []`.
  Further red-first proofs cover epoch reset, descriptor ownership, unsafe
  lock hard links and returned-cursor writes. Preserved excerpts:
  [initial](red.txt), [epoch reset](missing-epoch-red.txt),
  [descriptor](lock-owner-red.txt), [hard link](hardlink-red.txt),
  [returned cursor](returned-cursor-red.txt).
- Final acceptance: **380 tests pass**, 20 explicitly named modules, **28.143 s**,
  `metadata_errors: []`, no budget failures.
- Final copier/store regressions: **414 tests pass**, 18 explicitly named
  modules, **56.593 s**, `metadata_errors: []`, no budget failures.
  These two batches total **794 passing tests** and include ledger/history
  migrations, both backup formats, streamed owner backups, legacy source
  images/transfers, project hold validation, result/lease/release stores,
  admission/signature inputs, and read-budget checks. No full run was invoked.
- Final S2 plus isolated startup: **17 tests pass**, **2.313 s**,
  `metadata_errors: []`. An earlier parallel startup measurement was 5.212 s
  against its 5 s unit budget; isolated and final bounded runs pass the budget.
- **96 named modules** were attempted across the targeted copy/backup/restore/
  move/record-enumeration audit. All completed run durations were below
  300 seconds (maximum 167.279 s). Per-module attempts, failures and durations
  are retained in [evidence.json](evidence.json). Earlier intermediate failures
  are superseded by later green runs where recorded; they are not hidden.
- `git diff --check` passes. New tests are registered in layers, stages,
  timings and the requirement catalogue/source. There is no new persistence
  access in module-level test setup.

## Blocked / not completed in this sandbox

Unix socket bind returns `PermissionError: [Errno 1] Operation not permitted`.
This blocks service/collector portions of the mixed ledger, project, migration,
CLI, wake and upgrade-handover modules. Kernel process inspection and `/bin/ps`
are also denied; native execution reports `process_tracking_unavailable`, and
some original-signature/deferred-claim workers time out. The diagnostic-delivery
fixture was inspected separately: its bridge reports `process_tracking_unavailable`
with no result frame, before the asserted result diagnostic can exist. Installer package
checks succeed, but its project check reports that the operational-check sandbox
preflight must run from an owner shell. These service/process checks are not
claimed green. A bounded run with the two signature-worker modules was
interrupted before five minutes; its partial record is preserved. No test run
or test worker is left running.

The live reply-producer matrix `tests.test_reply_shape_live` was **not run**:
it requires the soak profile, Unix sockets and native child supervision.
No PTY-specific check was required or run. Exact named retry commands for the
remaining service/process checks and that matrix are in
[owner-retry-commands.txt](owner-retry-commands.txt).

`tests.test_runtime_release` has its **one expected failure**:
`test_current_runtime_is_recorded_for_the_current_version`. Runtime inventory
and byte-completeness tests pass; `store_fence.py` is registered in
`runtime_files.py`. The primary must bump VERSION and the digest catalogue at
merge, as instructed. Neither was edited here.

## Assertion audit and files

All changed pre-existing assertions and fixture boundaries are recorded old →
new, with reasons and exact remaining assertion hunks, in
[assertions.md](assertions.md). Changes are version/copy-name pins (ledger
16→17, project 21→22), boundary-version coverage, explicit epoch handling in
reopen comparisons, the exact epoch lookup in indexed-read checks, and genuine
historical-layout/fault fixtures. No business refusal, hold, receipt, signature
or historical-record assertion was removed to allow a different business rule.

Full changed-file inventory: [files.txt](files.txt). Production changes are the
new fence, the two owners, the SQL connection base, schema metadata, backup/
source readers and the standalone owner backup tool. Remaining changes are
requirements/evidence/registry files and the audited tests/fixtures.
