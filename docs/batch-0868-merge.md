# Batch 0.8.68 pre-merge (install point IP-B)

Branch `merge/batch-0868`, worktree `cocolo-batch11`, base ce81bd96 (0.8.67 bookkeeping). `version/0.4.0` was not moved.
No VERSION bump, no digest change, no push. Final merge commit before this note: 99c9f928.

## Merge list (in order, all `--no-ff`)

| # | Branch | Tip | Decision | Merge commit | Conflicts |
|---|--------|-----|----------|--------------|-----------|
| 1 | feat/s7-e2e3 | 4b193ead | 329 | d11ec9e0 | none |
| 2 | feat/s8-hm3 | a26f016a | 330 | f73ac8ee | test-layers, test-requirements, test-stages |
| 3 | fix/s13c-headroom | 3416a263 | 331 | 51b1ffe0 | none |
| 4 | feat/s13e-tests | 3d21dd16 | 332 | 73beb185 | test-layers, test-requirements, test-stages |
| 5 | feat/s11-hm2a | e53b55f3 | 333 | 7ff2b5dc | lifetime_caps, test-layers, test-requirements, test-stages |
| 6 | feat/s12-lanes | 3d9e712e | 334 | a2870454 | lifetime_caps, test-layers, test-requirements, test-stages, test-timings |
| 7 | feat/s14a-capture | a62e9685 | 335 | e5ba1bb5 | none |
| 8 | feat/s13d-idle | feaf0aa5 | 338 | 99c9f928 | test-layers, test-requirements, test-stages |

## Conflicts and resolution

Every conflict was in a JSON registry (every branch appends its own entries at the end of the same maps). There was no
code conflict. `runtime_files.py` merged without conflict (the S8 `health_holds.py` and S11 `health_workspaces.py`
entries are both present). The tests/requirements/*.json files are new per step and did not collide.

Resolution was a key-wise three-way union against the merge base (ours, theirs, base), written by a small script:
- key present on one side only: kept;
- both sides changed the same key: nested objects merged recursively, lists merged as union (ours first, then new items from theirs), a scalar clash would be recorded and the first side kept;
- output keeps each file's original formatting (verified by a round-trip check before writing).

Registry-union decisions: no key had a different value on both sides. There were no scalar clashes and no list unions
needed, so no "stricter value" choice arose in any file. Every key from every branch is present in the final files
(checked per branch against the final tree). In `scripts/test-requirements.json` the S13d merge also normalised one
non-canonical `}` newline `,` fragment left by an earlier clean auto-merge (whitespace only). All registries and
tests/requirements/*.json validate with `python3 -m json.tool`.

## Items to wire after S5

- S8 (330): the `health_monitor` registration of the HM-3 holds check. It is not in the S8 branch at all (the branch edits neither `health_monitor.py` nor `learning_store.py`), so nothing was merged or left out. Wire after S5.
- S11 (333): `health_workspaces.register(...)` in `learning_store.py`. Likewise not in the branch. Wire after S5.

## Test results

Command (unsandboxed, short TMPDIR): `parallel_tests.py --jobs 6 --affected ce81bd96`. The coverage map
(`.cocolo-test-cache/coverage-map.json`) does not exist in a fresh worktree, so the run fell back to a full run of all
535 units (1020 s elapsed, 4972 job seconds). metadata_errors: none (`requirement_failures: []`). Three units failed:

1. `codex:tests.test_runtime_release` - expected: stale runtime digest (7b25594c... recorded vs c9365973... computed); the version bump comes later.
2. `codex:tests.test_health_holds` (2 of 33) - `test_registration_does_not_edit_primary_owned_files_or_builtin_api` and
   `test_unrelated_lifetime_caps_bytes_are_preserved`. Both are S8's own guards and compare against base ce81bd96:
   learning_store.py must be byte-identical to base, and lifetime_caps.json must keep the base bytes as a prefix.
   Other merged steps legitimately change both (S14a edits learning_store.py; S11, S12, S14a add lifetime_caps entries in
   sorted positions). Fails alone, rerun confirmed; the test does not exist on base. Merge interaction, not a
   product defect. Not edited: these guards need an owner decision (for example compare against the batch parent of
   the S8 merge, or retire them once S8 is merged).
3. `codex:tests.test_crashed_settlement_socket` - `TypeError ... str + NoneType` in `test_lost_run_settlement.py`
   `start_host_process`. Passes alone on cocolo-batch10 (ce81bd96); fails alone on the batch. Cause: S13c changed that
   helper to `'pycache_prefix=' + self.dispatch_pycache`, but this "PRIMARY ONLY" module builds the harness by hand
   (`LostRunAfterCrashTests(methodName='runTest')`, no `setUpClass`), so `dispatch_pycache` stays `None`. S13c itself
   carries this defect (its affected run could not exercise a socket module). Proposed minimal fix, not applied: use
   `['-B']` when `dispatch_pycache is None`, as `project_driver.py` line 151 already does.

All other units passed. Budget debt lines were the usual ones; four units are now within budget (test_b2_service_outcome_paths,
test_container_stop, test_result_lifetime, test_session_guard).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

## Follow-up: S8 guards made precise, S13c harness fallback

Both S8 guards compared against base ce81bd96 and only made sense inside S8's own branch. Replaced in `packages/codex-agents/tests/test_health_holds.py`:

| Test | Old | New | Reason |
|------|-----|-----|--------|
| test_registration_does_not_edit_primary_owned_files_or_builtin_api | learning_store.py and health_monitor.py byte-identical to ce81bd96 | learning_store.py source must not mention `health_holds` or `HoldsCheck` (no registration or import of the S8 holds check yet; the primary wires it after S5) | S14a and others legitimately edit learning_store.py |
| test_unrelated_lifetime_caps_bytes_are_preserved | lifetime_caps.json keeps base bytes as a prefix | every `sites` entry present at ce81bd96 is still present with an unchanged value (parsed JSON, superset check, new entries allowed) | S11, S12, S14a add entries in sorted positions. Exception: S14a (335) deliberately re-audited `account_service.py::_capture_admission_rows_locked` and `admission_input_decode.py::_preflight` (checks 5 -> 6, new ends text), so those two are checked for presence only |

Mutations (each reverted): a fake `from .health_holds import HoldsCheck` line in learning_store.py turns the first red; altering a base cap entry's `checks`, or deleting a base entry, turns the second red.

S13c: `packages/codex-agents/tests/test_lost_run_settlement.py` (test harness code, not product) `start_host_process` now uses `['-B']` when `dispatch_pycache` is None, else `-X pycache_prefix=...` (same fallback as `tests/test_project_driver.py` line 151).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

## IP-B part 1: S9, S44a, S14b

Merged with `git merge --no-ff` from the local worktree branches (shared object store). VERSION and the runtime digest are not bumped (after S5). The 349 index wiring for S14b is not done (needs S5's schema step). Modules ran with TMPDIR=/tmp/claude-501/batch11, `parallel_tests.py --jobs 4 --module ...`; metadata_errors empty each time.

### S9 (decision 347) - commit 88c7b50c (merged 94a3c1a1)
- Conflicts: none (automatic).
- Modules: test_health_ledger, test_health_ledger_fix, test_health_holds, test_lifetime_cap_guard, test_learning_store, test_health_monitor (212 tests, 12.4 s, 0 failed).

### S44a (decision 350) - commit 19b1613d (merged 50bd0ff2)
- Conflicts: scripts/test-layers.json, test-requirements.json, test-stages.json, test-timings.json (both sides appended at the end of the same object). Resolved as a parsed-JSON union (base/ours/theirs three-way: new keys from both sides kept, no entry dropped, no value conflicts; same indent and escaping as before). Added S44A-1..3 requirements and the test_s44a_usage_install registration beside S9's entries.
- Because S44a adds reply shapes/refusal steps/service_host ops, ran the op-set, reply-shape and refusal modules too: test_s44a_usage_install, test_b2_service_outcome, test_b2_service_outcome_round2, test_holds_report_socket, test_lifetime_cap_guard, test_reply_shape_compatibility, test_refusal_steps, test_contracts, test_usage_recheck, test_usage_report, test_project_result_usage, test_cli_status_client_boundary, test_health_holds (285 tests, 30.8 s, 0 failed). test_reply_shape_live is a soak module (needs --profile soak) and was not run.

### S14b (decision 349) - commit 850309ef (merged e5a70144)
- Conflicts: packages/codex-agents/tests/lifetime_caps.json plus the same four scripts/test-*.json files. Resolved by the same three-way parsed-JSON union. lifetime_caps.json: 514 (ours) + new S14b sites = 521 sites, every ours entry byte-for-value unchanged, all theirs entries present, note unchanged. Registry entries added: test_run_retention, test_run_retention_integration, test_run_retention_scale plus S14b requirements.
- Modules: test_run_retention, _integration, _scale, test_learning_store, test_lifetime_cap_guard, test_health_holds, test_health_workspaces, test_health_ledger, test_health_ledger_fix, test_admission_capture_bytes, test_admission_capture_claim, test_admission_history_growth, test_admission_inputs, test_s44a_usage_install, test_cli_status_client_boundary, test_git_plumbing, test_codex_preparation, test_health_monitor, test_s13d_regressions (443 tests, 42.3 s, 0 failed).
- Not run (heavy, left to the batch run): test_project_driver, test_project_start, test_project_service_startup, test_lost_run_settlement, test_account_lane_restart and other socket modules S14b touched; test_runtime_release (digest, expected).

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## IP-B part 2: S5 (351), STATUS fix, S14b-6 wiring (349)

### S5 (decision 351) - commit 691dd511 (merged d1d3671f)
- Conflicts: the four scripts/test-*.json files and packages/codex-agents/tests/lifetime_caps.json (parsed-JSON three-way union, nothing dropped, no value conflicts); runtime_files.py (both new files kept: update_takeover.py and user_settings.py, sorted); project_history.py (purge list keeps S44a's history-index rebuild step AND S5's `_purge_takeover`); service_host.py (OWNER_HISTORY_OPERATIONS and the owner-read dispatch set now carry both `history.run_usage` and `history.takeover_report`; `service.stop_for_update` came in from S5 unconflicted); tests/test_b2_service_outcome.py (the pinned op set is the union of S44a's `history.run_usage` and S5's `history.takeover_report`, `service.stop_for_update`); tests/test_holds_report_socket.py (learning-status request order is now holds_report, run_usage, takeover_report, the order cli.py asks them).
- Modules (before commit): test_b2_service_outcome, test_holds_report_socket, test_b7b_terminal, test_account_host, test_admission_inputs, test_signature_inputs, test_signature_job_intake, test_lifetime_cap_guard, test_reply_shape_compatibility, test_refusal_steps, test_contracts, test_s44a_usage_install, test_b4t_takeover, test_upgrade_handover, test_history_service_upgrade (428 tests, 32.8 s, 0 failed).

### S5 STATUS.md fix
docs/requirements/b4t/STATUS.md: the open AP3 note replaced by the decision 348 resolution (eddd2d00) and a pointer to evidence/regression-fix/affected.txt.

### S14b-6 wiring (decision 349) - commit 60a4857d
See docs/requirements/s14b/STATUS.md "S14b-6". Requirement id S14b-6 (S14b-5 was taken). Red -> green: only the takeover adoption test was red (5 != 0); the cli upgrade test, the running-collector test and the plain-open check were already green on the merged tree because S14b's regression fix already created the indexes in cli --upgrade-ledger. Code: `takeover_owner.adopt_ledger` calls `create_retention_indexes()`. Registration with a running collector cannot create indexes (a running collector refuses it); it relies on the consented step that made the ledger current.
- Modules (named list from the brief plus test_learning_cli and the op-set pin modules): 37 units, 567 tests, 123.9 s, 0 failed, metadata_errors empty, requirement_failures empty. No --affected/full run, no VERSION bump.

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### S14b-6 follow-up: learning-migrate (primary decision)
`cli._migrate_learning` can perform the 17 -> 18 step. Test first (red: schema 18 with 0 indexes after `learning-migrate-project` on a schema-17 ledger), then `_ledger_takes_additive_step` + `create_retention_indexes()` in the same operation. The fixture was split so only the two upgrade tests downgrade to schema 6. Running-collector registration accepted as is (no new collector op). Modules: the part-2 list plus test_learning_migration: 39 units, 585 tests, 128.9 s, 0 failed, metadata_errors empty. No VERSION bump.

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### S14b-6 rewrite (decision 352, Codex merge review r1)
Per-path index calls replaced by one rule inside `LearningLedger._migrate`'s 17 -> 18 transaction (details in docs/requirements/s14b/STATUS.md). Also: registration `--upgrade-ledger` now allows the step on a schema-17 ledger (MED 1); `learning-migrate-project --status` never performs it (MED 4). Four tests red on 17a70460, green now. Two existing tests changed (old -> new in docs/requirements/s14b/assertions.md): the takeover adoption test and test_project_start E2a-1 layout comparison. Modules: part-2 list + test_learning_migration + test_learning_cli: 39 units, 589 tests, ~130 s; one failure (E2a-1 layout, fixed as above, rerun with test_state_location and the new module: 13 units, 67 tests, 46.7 s, 0 failed). No VERSION bump.

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### S14b-6 refinement (decision 352 refined)
The five indexes are part of the schema-18 layout: `_migrate` now adds them on every 17 -> 18 step including a fresh create (same transaction as the version write); a plain open of an existing 18 ledger creates none. New test (red on ed6aae6b: 5 != 0): fresh ledger -> 5 indexes, retention ready and due set, sweep removes the old row. The E2a-1 edit is reverted to the strict original (it passes again). `test_plain_open_makes_no_index...` (S14b-3) and the new module's setUp changed old -> new, recorded in docs/requirements/s14b/assertions.md; the takeover "5 before adoption" still reads right. Full named set: 39 units, 590 tests, 130.4 s, 0 failed, metadata_errors empty. No VERSION bump.

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### S14b-6 part 2e (decision 352, Codex merge review r2c NOT-READY: 2 MED, 1 LOW)
1. Above 64 hold-source rows the hold step commits version 18 in chunks before the indexes: `_initialize_run_holds_locked(target_version=18)` now creates the five (IF NOT EXISTS) just before `migration.finish()`, i.e. in the transaction that drops the progress table, so "18 and not pending" implies five; a resumed pending-18 pass creates them too. New test red on 79341664 (retry ended 18/not pending/0 indexes), green now.
2. Schema 16 no longer takes 17 -> 18 unconsented: `_migrate` requires consent for 17 -> 18 unless the ledger is fresh (was: also exempt when it started at 16). The 16 -> 17 epoch-only exemption in `_connect`/`_schema_behind` stays (basis: RSF-S2 fix rounds 1-2, docs/requirements/rsf-s2/STATUS.md). New test red (reached 18), green now. Consequence for the primary/owner: a default open or plain `learning-register` of a schema-16 ledger now stops at 17 and refuses; `--upgrade-ledger` (or allow_schema_upgrade) completes it. Five existing tests adapted (docs/requirements/s14b/assertions.md).
3. S14b-3 test reopens the ledger before asserting zero indexes.
Modules (15, 30 units, 465 tests, ~112 s wall, 0 failed, metadata_errors []): test_s14b_upgrade_indexes, test_run_retention, test_run_retention_scale, test_learning_store, test_rsf_s3_resumable, test_rsf_s6_classify, test_rsf_s2_regressions, test_b4t_takeover, test_project_start, test_state_location, test_learning_migration, test_learning_cli, test_upgrade_handover, test_history_service_upgrade, test_lifetime_cap_guard. No VERSION bump.

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## Full run (bump 325d8427, 0.8.68)

Command: `parallel_tests.py --base ce81bd96 --save-timings docs/batch-0868-full-run-timings.json` (output docs/batch-0868-full-run.txt). Exit 1. 549 units, 8179 tests (claude 134, codex 6851, root 1194). Elapsed 613 s (16 jobs, 5974 job seconds); preflight 7.5 s vs tests ~605 s. metadata_errors: none. Budget failures: 2 over-budget lines (test_project_start integration/scenario 189 s, test_run_retention_scale 70 s, budget 60 s) plus the usual debt list. requirement_failures: S8-HM3-5, RS-B-B4t-0, RS-B-B4t-9.

Wiring (step 1) not done: the S8 holds registration (health_monitor) and S11 `health_workspaces.register` (learning_store.py) are product code, not registry JSON.

Bump (step 2) note: the 0.8.67 bump also recorded the previous Claude bundle in packages/claude-agents/previous-releases.json (+ packaging.py PINNED_RELEASE_DIGESTS, test_previous_release_catalogue count). Not done in 325d8427 (needs compiling 0.8.67's Claude bundle from its source); only VERSION, runtime-releases.json, CHANGELOG were bumped.

| Unit | Result | Classification |
|------|--------|----------------|
| claude:tests.test_previous_release_recorded | fails alone: 0.8.67 Claude bundle not in previous-releases.json | regression from the incomplete bump (bookkeeping to finish) |
| codex:tests.test_b4t_takeover#1 | test_released_0859_owner_upgrade_preserves_project_local_enumeration expects runtime_floor (1,'0.8.67'), now 0.8.68 (module needs the test_framework path, so it only runs via parallel_tests) | regression from the bump: hardcoded version literal in the test (needs owner/primary choice: derive from VERSION) |
| codex:tests.test_account_observation_schema | 4 of 7 fail alone: "fixture must match genuine prior-source layout" (layout differs); passes on base ce81bd96 | regression from the batch (probably the schema 18 retention indexes/S14b), not a flake |
| codex:tests.test_health_holds | test_unrelated_lifetime_caps_bytes_are_preserved: lifetime_caps site project_holds_report.py::_ended_by_update `test` name changed (B7_2... -> B4t_runtime_change...); test not on base | merge interaction (a batch step re-pointed the base entry's `test`); guard needs an owner/primary decision |
| root:test_test_framework_affected | ModuleNotFoundError: coverage | environmental (venv lacks coverage; also on base) |

## Full run fix round (IP-B 0.8.68)

| Unit | Root cause | Fix |
|------|-----------|-----|
| claude:tests.test_previous_release_recorded | 325d8427 did not finish the bump: 0.8.67's Claude bundle was not recorded | Recorded 0.8.67 (source ce81bd96) in previous-releases.json (bundle compiled with `packaging.compile_bundle`; digest equals the pinned one, as for 0.8.41 to 0.8.66), added it to the PINNED_RELEASE_DIGESTS comment, catalogue count 27 -> 28. Product code unchanged. |
| codex:tests.test_b4t_takeover test_released_0859_owner_upgrade... | hardcoded floor literal; the product sets the floor to PACKAGE_VERSION (`raise_floor(..., PACKAGE_VERSION)` in learning_store/project_history/store_fence) | assertion `[(1,'0.8.67')]` -> `[(1,PACKAGE_VERSION)]` (collector_client.PACKAGE_VERSION, what the product writes) |
| codex:tests.test_account_observation_schema | fixture recipe `eleven()` drops every additive index newer than schema 11, but schema 18's five S14b retention indexes (decision 352: now in the fresh layout) were not dropped: records_run_retention, record_links_retention_to, account_open_retention_run, consumed_grants_retention_promotion, stored_files_retention_source. The product layout is right (352). | Added those five to the drop list in the recipe. The strict assertion against schema11-layout.json is untouched. Test-only. |
| codex:tests.test_health_holds test_unrelated_lifetime_caps_bytes_are_preserved | S5 B4t (146955fc, accepted, assertion-audit.md) replaced test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release (removed) with test_B4t_runtime_change_creates_no_legacy_update_close_count (exists, test_holds_report.py) and re-pointed lifetime_caps.json | cap entry already points at a real test. Guard: for site `project_holds_report.py::_ended_by_update` expects the base entry with only `test` changed `test_B7_2_ended_by_update_unrecorded_...` -> `test_B4t_runtime_change_creates_no_legacy_update_close_count`; all other bytes of the entry stay pinned. |

Assertions changed (old -> new): see the Fix column of each row (b4t floor literal -> PACKAGE_VERSION; guard equality for the renamed site; test_previous_release_catalogue counts 27 -> 28 (two assertions)).

Budgets (each module alone via `parallel_tests.py --module`, 4 jobs, twice): test_run_retention_scale 30 s of test time per run (35.2 and 36.0 s elapsed with preflight), not over the 60 s limit, so the 70 s in the full run was 16-job load. test_project_start 151.1 s and 150.9 s of job seconds (44 s elapsed across fragments) against the 60 s limit; it is a listed debt module with ceiling 183.4 (test-budget-debt-0850.json), so within its debt ceiling; the 189 s in the full run exceeded that ceiling under load (it was 210.5 s in the saved timings). No ceiling raised. After fixes: test_b4t_takeover, test_account_observation_schema, test_health_holds, test_lifetime_cap_guard, test_runtime_release, test_previous_release_recorded, test_previous_release_catalogue all pass, metadata_errors [].

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## S8/S11 wiring

Both registrations are done (IP-B 0.8.68), tests first (red on 506f6d1a, green after).

- S8 (330, new ID S8-HM3-7): `HealthMonitor.register_state_checks(state, sweep_interval_seconds=...)` in `health_monitor.py` registers `HoldsCheck` (lazy import, no I/O); `CollectorServer.__init__` calls it after the container check, only when `_ledger_owner` is set (the check reads the ledger only through the owner; without one it could only report "cannot read"). `learning_store.py` mentions neither `health_holds` nor `HoldsCheck`, so the S8 guard test stays true. `with_builtin_checks` unchanged.
- S11 (333, new ID HM2A-11): `health_workspaces.register(self.health_monitor, self.state.root, pending=...)` in `CollectorServer.__init__`. The pending reader is new `memory_holds.pending_workspaces(state_root)`: read-only snapshots of project histories (as the holds check), at most 256 projects and 64 rows per project, None when incomplete (so nothing is removed that pass). Entry added to `tests/lifetime_caps.json`. Registering does no I/O (test: state folder listing unchanged); status reads run no pass.
- S8-HM3-5 (full run): not this wiring. It was `test_unrelated_lifetime_caps_bytes_are_preserved` (old byte-prefix rule), already replaced by the superset rule above; it passes.
- Assertions changed: see docs/requirements/s11-hm2a/assertions.md (collector check-name lists gain `workspaces`; fixtures keep the real scratch folder out of healthy-collector tests).
- Runs (TMPDIR=/tmp/claude-501/ipb-wire, `parallel_tests.py --jobs 3`, metadata_errors []): test_health_holds, test_health_workspaces, test_health_monitor, test_cli_status_client_boundary, test_learning_store, test_lifetime_cap_guard, test_health_containers, test_codex_preparation (28 s, 282 tests); test_memory_holds, test_memory_holds_lane, test_ledger_owner_wiring (17 s). test_runtime_release still fails on the expected stale digest (version bump comes later; the runtime source changed again here).

## Full run 2 (HEAD d048e8b9, base ce81bd96)
- Exit 1; 549 units; elapsed 669.8 s (wall 11:21); preflight 9.1 s vs job seconds 6724.8; metadata_errors: [].
- requirement_failures: RS-B-B7-5, S8-HM3-3.
- Failure 1: tests.test_project_service_startup (2 errors: test_HM3_faulted_service_reaches_health_monitor_status,
  test_HM3_stop_signal_first_reaches_health_monitor_status): `ValueError: a check is registered once, with a run method`
  at tests/test_project_service_startup.py:395 (`server.health_monitor.register(HoldsCheck(...))`); health_monitor.py:173
  now registers HoldsCheck itself (S8/S11 wiring). Rerun alone: same 2 errors. Base ce81bd96: 13 tests OK.
  Classification: REGRESSION from the S8 wiring (the test registers a check the monitor now registers). Drives S8-HM3-3. Not fixed.
- Failure 2: tests.test_b7b_budgets#0 (B3 'failed' vs 'met', timing measurement under load; drives RS-B-B7-5).
  Rerun alone: 3 tests OK in 29 s. Classification: FLAKE (load).
- Budget failures (over 60 s scenario / 5 s unit): test_b7b_budgets 65.6, test_claim_denial_recovery 70.8,
  test_host_slots 60.6, test_project_start 195.7, test_routing_judgment 6.1 (unit), test_rsf_s3_resumable 63.0,
  test_run_retention_scale 80.8. Alone: claim_denial 17 s OK, b7b 29 s OK. test_project_start alone 136 s (debt ceiling 183.4 in
  scripts/test-budget-debt-0850.json; 195.7 in the full run exceeded it, 136 s alone is under it) -> load. The other four
  modules (host_slots, rsf_s3_resumable, run_retention_scale, routing_judgment) cannot be rerun with plain `unittest`
  (import test_framework fails outside the runner), so no alone-time was taken; they are barely over (except retention 80.8)
  and have no debt entry. Classified load-related, unconfirmed.
- Retire notices: test_container_stop, test_session_guard now within layer budget.
- Only action needed: the test_project_service_startup regression (primary/builder to fix; no fix made here).

## Final IP-B pass: memholds merge and test_project_service_startup (decision after 5cd6edd8)
- Merged fix/ipb-memholds-bound f282d69a (HM2A-11 bounded pending read) into merge/batch-0868 (a9fba888, no conflicts).
- tests/test_project_service_startup.py, assert_service_health_fault: OLD `from codex_agents.health_holds import HoldsCheck` and
  `server.health_monitor.register(HoldsCheck(state, sweep_interval_seconds=60))` -> NEW both removed (the product registers the
  check via HealthMonitor.register_state_checks in LearningStore). Every assertion kept.
- Result: the ValueError is gone and the holds check now runs from the product registration (first pass reports
  service_writer_fault/holds as expected). BUT both HM3 tests still fail on the second observation: `assertIn('no findings')`
  gets 17 `owner_unknown` findings from the S11 workspaces check. That check (registered by the same wiring) scans the real
  per-user scratch container (host_scratch_base()/codex-agents-scratch, shared by every agent run on the machine) for
  prepare-* folders, and finds other runs' leftover folders. Classification: ENVIRONMENTAL / test isolation gap (the test
  does not point bridge.WORKSPACE_PARENT at a private directory). Not fixed here; decision needed (isolate the container in
  the test, or filter the view to the holds check).
