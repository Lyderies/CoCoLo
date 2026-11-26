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

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### S14b-6 follow-up: learning-migrate (primary decision)
`cli._migrate_learning` can perform the 17 -> 18 step. Test first (red: schema 18 with 0 indexes after `learning-migrate-project` on a schema-17 ledger), then `_ledger_takes_additive_step` + `create_retention_indexes()` in the same operation. The fixture was split so only the two upgrade tests downgrade to schema 6. Running-collector registration accepted as is (no new collector op). Modules: the part-2 list plus test_learning_migration: 39 units, 585 tests, 128.9 s, 0 failed, metadata_errors empty. No VERSION bump.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
