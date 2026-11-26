# Batch 0.8.69 pre-merge (install point IP-C)

Branch `merge/batch-0869`, worktree `cocolo-batch12`, base e46759a5 (accepted 0.8.68). `version/0.4.0` not moved. No VERSION
bump, no digest change, no push. `test_reply_shape_live` NOT run (primary, quiet machine).

## Merges (`--no-ff`, in order)

| # | Branch | Tip | Decisions | Merge commit | Conflicts |
|---|--------|-----|-----------|--------------|-----------|
| 1 | feat/s13b-due-drain (with S13 a09e71f0, S13a d1af5e86) | 94a3eecb | 322, 354, 356, 359 | 8d1ebba6 | test-requirements |
| 2 | feat/s10-resources | d5052285 | 355, 360, 361 | e22b0274 | learning_store.py, test_health_containers.py, test-layers, test-requirements, test-stages, test-timings |

## Conflicts and resolutions

- Registry JSON (test-layers, test-stages, test-timings, test-requirements): both sides appended entries at the end of the same
  map. Resolved as unions (ours then theirs, comma-joined); nothing dropped (checked that every key of both parents is
  present and unchanged; all registries parse with `json.tool`).
- `learning_store.py` (S10 vs S8/S11 wiring): imports unified to `from . import health_resources, health_workspaces, memory_holds`;
  the collector registers holds (when a ledger owner exists), workspaces, then `health_resources.register(...)`. All checks kept.
- `test_health_containers.py`: expected check names are now `['self_check','containers','workspaces','resources']` and the
  state map gains `'resources': 'ok'` (no ledger owner here, so no holds).
- Non-textual consequence of the union: `test_health_monitor.py` line 413 expected
  `['self_check','containers','workspaces']` -> now `[..., 'workspaces','resources']` (merge union, not a product change).
  `test_health_holds` uses a subset check on `holds`, unchanged.
- `lifetime_caps.json`: auto-merged, but S13a's new `min(limit,64)` in `project_history.py::release_earlier_unarmed_leases`
  was never listed, so `test_lifetime_cap_guard` failed (it was not in S13a/S13b's run lists). Added an entry (BOUNDED: one
  selection per call, rows leave the due set by release or backoff; test
  `test_full_blocked_pass_reaches_eligible_rows_then_waits_for_backoff`). Registry data, no product change.
- Op sets, reason vocabularies (`resource_low`), `run.check` in the driver ops, runtime_files: auto-merged, nothing lost.

## Modules run (named, `--jobs 2`, TMPDIR=/tmp/claude-501/batch12; all `metadata_errors []`, `failed_units []`)

- After S13b: test_s13b_due_drain, test_s13_budgets, test_s13a_lease_pass, test_result_leases, test_project_history,
  test_result_lease_lane, test_project_driver (269-287 s, over its 60 s budget under 2 jobs; the budget debt is not new),
  test_b7b_budgets: green.
- After S10: test_health_resources, test_health_resources_open, test_health_containers, test_health_monitor, test_health_holds,
  test_health_workspaces, test_lifetime_cap_guard, test_account_original_open, test_dispatch_client_runtime, test_refusal_steps,
  test_contracts, test_project_driver, test_run_driver_retries, test_history_api, test_reply_shape_compatibility,
  test_dispatch_delivery_cli, test_project_host_open, test_host_slots, test_learning_cli (65 s): green after the two fixes above.

## Carried items

a. S13b LOW (`result_persistence.py`, expire path). Test first: `test_wrapper_failure_before_the_body_leaves_no_stale_continuation`
   (S13B-IDLE, registered in test-requirements). Red: calls `[64, 64, 64]` instead of `[64]` (a stale True requeued a failed
   request). Fix: `history._unarmed_lease_pass_pending = False` before the call, flag read after it; a body that fails late keeps
   what it set. Comments added on the live-lease deferral case (it backs the row off like any blocked row and sets the flag by
   design) in both files. Stray third blank line before `S13RegressionControls` in `test_s13_budgets.py` removed. Green.
b. S10 `test_account_original_open.test_M11_refused_registration_retries_...`: old -> new, assertions unchanged.
   Old: `mock.patch.object(client_runtime, '_check_new_run')` (guard bypassed) and `_run_driver` side_effect list.
   New: the real `_check_new_run` runs (its `run.sock` guard is satisfied by a touched file in a temp dir that replaces the
   hard-coded `/private/tmp/m11` endpoints); `run.check` goes through the real `_run_driver` and its reply validation, with
   `client_runtime.exchange_frame` replaced by a protocol-valid ready reply `{'protocol':1,'state':'ready','reason_code':None}`
   (it also asserts the request is `run.check`); registration calls still go to the `register` mock, so
   `register.call_count == 2` is unchanged. Green.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## IP-C: live reply produces history.run_usage partial_reason (S44A-2)

Finding: `codex:tests.test_reply_shape_live` (soak) failed on 0.8.68 and on the batch: optional field
`history.run_usage.partial_reason` was never produced by a real reply (red: both A/B runs in
docs/batch-0869-reply-shape-live-ab.md, 673 s / 633 s).
Fix (test only, no product hook, no exemption): new driver test
`HoldsProtocolTests.test_S44A_2_live_service_reply_carries_partial_reason_when_its_deadline_passes` in
tests/test_holds_report.py (already a reply_shape_live driver). It drives a real HistoryService.handle through the
real HistoryClient.run_usage with the existing `RUN_USAGE_SECONDS=-1` / `RUN_USAGE_PROGRESS=1` seams, so the live
suite records a real reply carrying `partial_reason`. No existing assertion changed.
Green: soak `test_reply_shape_live` 5 tests, failed_units [], metadata_errors [], 638.9 s. Normal profile
test_s44a_usage_install, test_usage_report, test_reply_shape_compatibility, test_holds_report: 67 tests, pass.

## Full run (0.8.69, commit 1ef995e2 on 98baf436; base e46759a5)

Command: `parallel_tests.py --base e46759a5 --save-timings docs/batch-0869-full-run-timings.json` (output: docs/batch-0869-full-run.txt).
- Exit 1. 554 units, tests claude 134 / codex 6910 / root 1194. Elapsed 625 s (preflight 7.5 s, discovery 2.5 s; the rest is test time). `metadata_errors`: []. Not partial.
- Budget failures (2): `test_project_start` 207.3 s (budget 60, debt ceiling 183.4) and `test_run_retention_scale` 68.9 s (budget 60). Both flake (load): rerun alone through the runner 23.5 s and 32.1 s, no budget failure; project_start also passes alone and on base.
- `requirement_failures`: AP3, B2-OUTCOME, E2c-10, E2c-2, TESTSPEED-1, TESTSPEED-3, TESTSPEED-4 (derived from the failed units and the budget failures above).
- Failed units (4):
  1. `test_project_start#0` (E2c-2, start_timeout slot `failed` != `starting`): FLAKE (load). Passes alone (3 tests) and on base.
  2. `test_cli_open_client_runtime#0/#1` (`history_not_authorized` != `open_service_unreachable`, 2 tests each): REGRESSION, deterministic. Passes on base e46759a5 and on 8d1ebba6 (S13b merge); fails from e22b0274 (IP-C merge of S10 HM-6, run.check fails closed) onward, so it is S10's change, not the bump. Not fixed.
  3. `test_candidate_evidence_lookup` AP3 (`LedgerError: store owner replaced or lost its writer lock` in `check_floor(PACKAGE_VERSION)`): REGRESSION from the 0.8.69 version bump (passes on base and on 98baf436, fails at 1ef995e2). Likely the test's real-previous-release scenario assumes a floor that the new PACKAGE_VERSION changes (same family as the 0.8.68 b4t floor fix 42875305). Not fixed.
- Debt retirement required: test_container_stop, test_result_lifetime, test_session_guard (runner note, not failures).

## Full-run fixes (decisions 362, 363)
- Requirement failures traced: AP3 -> `test_candidate_evidence_lookup` (fixed below); B2-OUTCOME, TESTSPEED-1, TESTSPEED-3, TESTSPEED-4 -> only through the two `test_cli_open_client_runtime` checks (fixed below; no other check of those requirements failed); E2c-10 and E2c-2 -> `test_project_start` (load flake, passes alone). Budget failures (project_start, run_retention_scale) are load flakes. Nothing is unexplained.
- `test_cli_open_client_runtime` (S10, decision 360): root cause is the fixture, not an authority masking bug. The fixture has no run driver, and the open now asks `run.check` first and fails closed (`open_service_unreachable`, a real reason: no driver). With a real driver the account's `history_not_authorized` still wins, because `run.check` answers ready before admission. Moving the authority check before `run.check` was tried and rejected: enrollment is single-use, and the S10 tests (`test_health_resources_open`) went red. Fix: `dispatch_cli_fixture` gains the opt-in `COCOLO_TEST_RUN_CHECK_READY` (protocol-valid `ready` for `run.check` only through the real `_run_driver`; every other driver call still fails); the test sets it and has a `run.sock` file only while an open runs (a lingering file changes dispatch). No expected code changed. S10 guarantees (no account writes before the check, plain refusals, exit non-zero) and their tests stay green.
- `test_candidate_evidence_lookup` AP3 (decision 362, supersedes 348's same-schema branch): B4t H1d floor = PACKAGE_VERSION, so every older release refuses. Assertion changes, old -> new:
  - "if a same-schema recorded release exists: it opens, reads, validates and appends the copy, then current code reopens it" -> removed; replaced by always: the newest recorded previous release (`releases[-1]`, now 0.8.68) refuses the copy (`LedgerError`) with zero ledger/artifact writes (collector.lock excluded).
  - `assertGreater(current_schema, max(schemas))` and `assertEqual(17, schemas[baseline])` -> `assertLessEqual(schema_of(previous), current_schema)` (the old pair only held while no release shared the schema).
  - refusal message `'released build must refuse a newer ledger schema'` -> `'a released build older than the current one must refuse (B4t H1d floor; decision 362)'`.
  - `test_AP3_missing_previous_source_has_actionable_failure` unchanged and green; no skip. AP3 statement amended in tests/requirements/ap5-lookup.json, docs/requirements/ap5-lookup.md, scripts/test-requirements.json.
- Rerun, all green, `metadata_errors: []`: test_cli_open_client_runtime 9.8 s, test_candidate_evidence_lookup 2.3 s, test_health_resources 0.4 s, test_health_resources_open 5.7 s, test_account_original_open 8.1 s, test_dispatch_client_runtime 7.3 s, test_b4t_takeover 13.6 s, test_runtime_release 0.2 s.

## Full run 2 (0.8.69, after merging fix/0869-workspaces-live @00451171 and re-recording the digest; HEAD 7baaa123; base e46759a5)

Command: `parallel_tests.py --base e46759a5 --save-timings docs/batch-0869-full-run2-timings.json` (output docs/batch-0869-full-run2.txt).
- Exit 1. 554 units; elapsed 696.5 s; `metadata_errors` []; not partial. The two run-1 regressions (cli_open_client_runtime, candidate_evidence_lookup) are green.
- Failed units (2), both LOAD FLAKES (rerun alone through the runner: `failed_units` []):
  `test_project_start#2` (E2c-1 "due projects not serving within one interval"), `test_s13_budgets` (S13C-1/2/3, RS-10-DUE: batch 61.6 ms vs 50 ms limit under load).
- Budget failures (11): admission_history_growth 65.9, b4t_takeover 92.8, cli_observation_runtime 85.1, host_commands 127.5, host_slots 64.5, large_project_backups 70.3, lost_run_settlement 140, project_start 215.5, routing_judgment 5.2 (budget 5), run_retention_scale 78.5, root:test_setup_update 384.9 (sum of fragments). Rerun together with 12 modules (still loaded) only project_start (190), routing_judgment (5.0) and run_retention_scale (68) stayed over: all known load/sum-of-fragments flakes (project_start 23 s and run_retention_scale 32 s alone in the 0.8.69 run 1). No test failure needed a base comparison (all pass on rerun). Nothing fixed.
