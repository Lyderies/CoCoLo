# B6 status (resume here)

Worktree ~/Code/temp/cocolo-b6, branch feat/rsb-b6-close, base 6a1ea9da (0.8.57). Requirements: docs/requirements/b6.md.
Evidence: docs/requirements/b6/red.md. Assertion ledger: docs/requirements/b6/assertions.md.

## State: final fix (decision 210) COMPLETE, committed on feat/rsb-b6-close (on 020e530d). No further review round.

### Final fix (decision 210; red evidence red.md "Final fix", ledger assertions.md F, acceptance b6.md "Final fix")
- Item 1 HIGH: `_unfinished` reads every held lease by verified identity (`_held_leases`: live leases by `lease_expiry`
  and held lease register rows, which must match), verifies each seal, then compares its orchestration; a lease that
  does not verify refuses the close. The target-index loop stays (scope conflicts of finished leases).
- Item 2: step 21 index `project_execution_consumptions_orchestration` (grant's orchestration, `json_valid` guarded);
  `_ORPHAN_CONSUMPTION` refuses a consumption of the target with neither reservation nor lease; a target lease's
  claim whose consumption has no reservation refuses too.
- Item 3: marker `.history.sqlite3.upgrade-21.<random>.attempt` with content {copy, partial, size, sha256}, fsynced
  with the folder before the link; recovery deletes only exact matches whose link count is only those names.
- Item 4: `_check_orchestration_states` refuses an unverifiable pointer (`history_incompatible`).
- Item 5: FinalCapIsolationTests (4,097 valid-seal leases + reservations of another orchestration; red on 700d41f8).
- Runs (named modules, all green): batch A 28 store modules 728 tests 235 s; B1 14 service modules 226 tests 226 s;
  B2 8 modules (backups, learning source, cap guard) 112 tests 54 s; test_project_driver alone 99 tests 247 s; root
  tests.test_rsd_budget 2 s. Not run: full suite / --affected (primary). Digests not refreshed.

### Decision 212 (same round, on 3db04d46): COMPLETE
- `_verify_held_reservations`: every held/releasing reservation register row's reservation is read and verified
  (seal covers its orchestration column) before the target's are selected by column; missing/unverifiable refuses.
- `_remove_stale_partials`: before an attempt, this store's own stale `.history.sqlite3.before-21.<n>.<16hex>.partial`
  (regular, one link, own uid) go; nothing else.
- Runs (all green): store batch 731 tests 241 s; service batch 226 tests 229 s; backup/learning/guard 112 tests 55 s;
  test_project_driver alone 99 tests 251 s. Red/green in red.md "Decision 212"; ledger assertions.md G.

## Earlier state: review round 1 fix COMPLETE (020e530d)

### Review round 1 (one fix round; red evidence in red.md, ledger section E in assertions.md, acceptance in b6.md)
- HIGH adoption window: `_LAST_ATTEMPT_ACTIVITY` (project_history) = max of the orchestration's reservations'
  created/taken and leases' created/taken; `adopt_orchestrations` dates an existing held row with a null due time.
  Fixture `tests/fixtures/history_step20/store_leases.sql` (`make_store.py --leases`, 6a1ea9da runtime; store.sql
  untouched).
- HIGH retained history: step 21 also adds `result_reservations_orchestration` and `result_leases_orchestration`
  (expression index on the lease binding's orchestration handle); `_positions(table, handle)` / `_unfinished` /
  `_unattended_eligible` read only the target's rows; the project-wide consumption scan is gone (the target's
  consumptions are verified per reservation, as before).
- MED clock: `end_idle(now=None, limit)` ignores `now`, reads `_hold_now()`; apply(_unattended) reads it inside the
  transaction; service passes `end_idle(None, UNATTENDED_CLOSE_BATCH)`.
- MED crash copy: marker `.history.sqlite3.upgrade-21.<dev>-<ino>.attempt` (no extra fsync); `_recover_interrupted_upgrade`.
- MED layout: `_normalized_sql` keeps quoted literals exact. MED state: `_check_orchestration_states` in
  `_check_project_holds`.
- LOW: end_idle clamps limit to 4; lifetime_caps.json `<module>` + `_open_count`; tombstone-purge claim corrected.

### Round 1 test runs (named modules, all green)
- Batch A, 27 store modules: 643 tests, 180 s (2 errors fixed: `_keep_copy_before_upgrade` keeps returning a path;
  rerun of the 3 modules 6 s).
- Batch B1, 12 service modules: 376 tests, 286 s. Batch B2, 15 modules (settlement, lifetime, backups, learning,
  cap guard): 324 tests, 279 s. tests.test_project_driver alone: 99 tests, 299 s. Root tests.test_rsd_budget 4 s.
- Not run: full suite / --affected (primary's). Digests not refreshed.
- For the primary: assertions.md section E (one B6-own assertion text changed, outside decision 195).

## Earlier state: COMPLETE, committed on feat/rsb-b6-close (700d41f8)

## Done
1. Requirements RS-B-B6-1..8 written (B6-6 = RS-D N1/N2, already built by S2; B6-7 = schema step; B6-8 = state column).
2. RS-D S3 red tests brought in from 9a3c6860 (cherry-pick, catalogue merged by hand): tests/test_rsd_budget.py.
3. Real format-20 store fixture: packages/codex-agents/tests/fixtures/history_step20/store.sql, generated by
   make_store.py with the base runtime (git archive 6a1ea9da). Never regenerate it with a later runtime.
4. Red tests: packages/codex-agents/tests/test_b6_unattended_close.py (24/25 red on base, K2 a regression pin).
5. Code (schema 21):
   - orchestration_schema.py: `state` column text, `with_orchestration_state`, state index.
   - project_history.py: `_SCHEMA_VERSION = 21`, `_SCHEMA_V20`, STEP_21_SCHEMA (state index, `result_reservations_live`,
     `project_holds_orchestration_check`), step in `_upgrade` (ALTER + indexes + `adopt_orchestrations`, one
     transaction), `_orchestrations_to_adopt`, retirement selector `INDEXED BY result_reservations_live`.
   - project_holds_schema.py: validator rules (open <-> held row), ORCHESTRATION_IDLE_MS, back-off constants, activity on
     execution-hold take, next-check reset on execution-hold release, `adopt_orchestrations`.
   - orchestration_store.py: row taken at open, due moved by checkpoint/pause/resume, released `closed` (primary,
     update) / `interrupted_unattended` (unattended) in the close transaction; `_open_count` by column; `end_idle`
     rewritten (fair select, derived request, in-transaction eligibility, back-off); `apply(..., _unattended=True)`.
   - result_persistence.py: the service pass uses UNATTENDED_CLOSE_BATCH (4).
6. Existing tests: 33 version/copy-name pins 20 -> 21 and fixture lines (all listed in assertions.md).
7. scripts/test-layers.json, test-stages.json, test-requirements.json: new module + RS-B-B6-*, RS-D-9-S2/S3, P4-REPORT.

## Test runs (named modules only, all green at commit)
- Batch A, 25 project-store modules (schema pins, orchestration, holds, retention, B6): 557+ tests, ~170 s; reruns of the
  fixed modules 78 s.
- Batch B, 24 modules (history, persistence, purge, replay, backups owner_backup_streamed/large_project_backups/
  backup_installed_project, B2 paths, lost-run settlement, run holds, holds report socket): 581 tests, 389 s (over the
  5-min guide: split it next time); 3 fixed modules rerun 94 s.
- tests.test_project_driver alone: 278 s, OK.
- Final: test_b6_unattended_close + rsd_runtime_close + project_orchestration + deadlines_retention 13 s; root
  tests.test_rsd_budget 1 s.
- Not run: full suite / --affected (the primary's, before merge).

## Open for the primary
- Batch 3's OrchestrationCountsCostTests is not on this base: un-skip it against `project_orchestrations.state`
  (index `project_orchestrations_state`) when batches merge (decisions 185/187).
- B7a's holds report still prints `orchestration` as `not_tracked`; rows now exist (B7b's switch).
- RS-B-B6-6 has no check tagged with its own ID (its evidence is RS-D-2-N1/N2's checks).
- Runtime digests not refreshed (rule); release bookkeeping untouched.
