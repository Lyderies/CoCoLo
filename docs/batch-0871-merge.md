# Batch 0.8.71 (install point IP-E, branch merge/batch-0871)

Base e5174f14 (accepted 0.8.70, decision 378, not installed). `version/0.4.0` not moved, no push.

## Merges
- `feat/s14e-custody-query` @c24d3491 (decisions 380, 381) -> 57b90fa4, no conflicts.
- `feat/s15-fault-matrix` @5558cb48 (decisions 383, 384; base 88822538) -> 938d7fbf. Conflicts only in the four registries
  (test-layers/stages/timings/requirements: both sides appended at the end). Resolved as unions: all entries of both sides kept
  (requirements 437 = 427 + S15 additions; nothing from either side lost). Product files auto-merged, no judgement needed.
- S15 guard re-run (`test_lifetime_cap_guard`, through the runner): green. No lifetime_caps.json or test-checks.json change landed
  on the 0.8.70 side after 88822538, so no entry needed a new kind/checks/binding.

## Bump 0.8.71 (47b44470)
VERSION, runtime digest 80b34174... in runtime-releases.json, CHANGELOG, previous-release record for 0.8.70 (source e5174f14;
Claude package recompiled from that source, bundle digest unchanged eb34209d...), packaging.py PINNED comment, catalogue count
30 -> 31 (two assertions in test_previous_release_catalogue). Takeover floor is PACKAGE_VERSION = 0.8.71.
Assertion changed (old -> new): `test_b4t_takeover.FloorTests.test_a_store_written_by_this_build_refuses_0868_and_0869` renamed
`..._refuses_0868_0869_and_0870` (registered in scripts/test-requirements.json under both RS-B-B4t entries); floor literal
'0.8.70' -> '0.8.71'; refused builds ('0.8.68','0.8.69') -> ('0.8.68','0.8.69','0.8.70'). Reason: floor moves with the version (owner brief).
Green through the runner (metadata_errors [], failed_units []): test_runtime_release, test_previous_release_catalogue,
test_previous_release_recorded, test_b4t_takeover, test_candidate_evidence_lookup, test_lifetime_cap_guard.

## Full run (`docs/batch-0871-full-run.txt`, timings json beside it)
568 units, 612.7 s elapsed, 8.0 s preflight, metadata_errors []. NOT green: 3 failed units + requirement failure S8-HM3-5
(its checks are the test_health_holds tests). Nothing fixed.

### Test failures (each alone on HEAD: fails; on base e5174f14 in a temporary worktree: passes; on feat/s15 @5558cb48 alone: also fails)
All three come from S15 (decision 383 re-shaped lifetime_caps.json) against tests that S15 did not update; they were already red on the S15 branch.
- `test_rsf_s7_project.ProjectReplayTests.test_retained_hold_closure_has_an_inclusive_byte_ceiling_and_a_bounded_window`: expects registry entry `project_history.py::record_account_settlement` `test` == its own name; S15 bound `test_reaudited_retained_settlement_evidence_crosses_the_history_writers_own_cap`.
- `test_holds_report.HoldsLimitRegistryTests.test_B7_3_presentation_bounds_registered_with_past_limit_tests`: KeyError `account_api.py::__init__` in `guard.problems` (S15's guard indexes `sites[key]` for a registered key whose site the test's two-file scan does not contain).
- `test_health_holds.HoldsTests.test_unrelated_lifetime_caps_bytes_are_preserved` (also fails S8-HM3-5): `project_driver.py::_release_failed` registry entry differs from the pinned original and is not in `s15-cap-shape-updates.json`. (The 0.8.70 side also added 65 lines to test_health_holds; S15 changed 30 lines of it.)
Needs a primary decision: update these assertions to the S15 registry (decided requirement) or extend s15-cap-shape-updates.json.

### Budget
- budget_failures: test_project_start 188.1 s (alone 158.7 s: load flake, within its 183.4 s ceiling); test_run_retention_scale 69.6 s (alone 32.1 s: load flake).
- Runner asks to retire debt (registry not changed here): test_b2_service_outcome_paths, test_container_stop, test_session_guard.
Unchecked requirements (88) as before.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

## Full-run fixes
Product code is unchanged since the full run; only tests and a test fixture changed.
- `test_rsf_s7_project`: expected `record_account_settlement` binding old `test_retained_hold_closure_has_an_inclusive_byte_ceiling_and_a_bounded_window` -> new `test_reaudited_retained_settlement_evidence_crosses_the_history_writers_own_cap` (decision 384; cap-binding-audit.json `bound-audited-383`).
- `test_holds_report` B7_3: `account_api.py::__init__` was never removed (entry present, kind validation, checks 1). Decision 382's guard now checks every registry entry against its site count, and B7_3 scanned two files into a plain dict, so the unscanned entry raised KeyError. Test now filters the registry to the two scanned files and uses a Counter (old: whole registry + dict).
- `s15-cap-shape-updates.json`: added `project_driver.py::_release_failed` (5558cb48: time bindings). OPEN: `user_settings.py::load` (same commit, test -> `test_shared_settings_byte_cap_rejects_valid_json_one_byte_over`) is also unlisted, so `test_health_holds` still fails until it is added; not added without approval.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
