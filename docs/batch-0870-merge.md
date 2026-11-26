# Batch 0.8.70 pre-merge (install point IP-D)

Branch `merge/batch-0870`, worktree `cocolo-batch13`, base 77bca2e5 (accepted 0.8.69). `version/0.4.0` not moved, no push.

## Merge
- `fix/0870-unverified-hold` @98a9f5eb (decisions 366/367, UH-1..UH-7), `--no-ff`, merge commit 25be003e. No conflicts
  (the fix branch base eb1eae9a is an ancestor of 77bca2e5). Registries auto-merged cleanly.
- 18 affected modules, each alone through the runner (`--module codex:tests.<m>`), all `metadata_errors []`, `failed_units []`:
  test_health_holds, test_b4t_takeover, test_lost_run_settlement, test_account_settlement_driver, test_project_driver,
  test_container_recovery, test_health_monitor, test_lifetime_cap_guard, test_reply_shape_compatibility,
  test_b2_service_outcome, test_account_observation_backup, test_backup_installed_project, test_backup_reply_private,
  test_large_project_backups, test_learning_migration, test_owner_backup_streamed, test_project_history,
  test_result_persistence. (The "22 modules" list named in the brief was not in the fix branch STATUS; this set covers every
  module the brief named plus the backup/migration/history modules.)

## Correction (decision 370)
`docs/requirements/unverified-hold.md` (UH-3) and `docs/troubleshooting.md` "Update hand-over": more than 64 recurring unverified
run ids can be retried locally on every sweep (no account calls, no change). Commit eb99f6a2.

## Bump 0.8.69 -> 0.8.70 (808b0f46)
VERSION, `runtime-releases.json` (0.8.70 digest a2a112a4...), CHANGELOG, previous Claude bundle recorded
(`previous-releases.json` +0.8.69 source 77bca2e5; Claude package unchanged since 77bca2e5, same bundle digest eb34209d...),
`packaging.py` PINNED_RELEASE_DIGESTS comment, `test_previous_release_catalogue` count 29 -> 30.
Takeover floor: derived from PACKAGE_VERSION (`store_fence` -> `takeover_schema.raise_floor`). New test
`test_b4t_takeover.FloorTests.test_a_store_written_by_this_build_refuses_0868_and_0869` (registered in
scripts/test-requirements.json under RS-B-B4t-3 and RS-B-B4t-9): floor row is `0.8.70`; builds reporting 0.8.68 and 0.8.69
are refused with zero bytes changed. Real older-bundle proof: `test_candidate_evidence_lookup` runs the newest recorded
previous release (now 0.8.69, source 77bca2e5) against a copied store and requires refusal.
Green: test_runtime_release, test_previous_release_catalogue, test_b4t_takeover, test_candidate_evidence_lookup.

## Full run (`docs/batch-0870-full-run.txt`, timings json beside it)
554 units, 650.6 s, 8.2 s preflight, `failed_units []`, `metadata_errors []`, requirement_failures []. Exit 1 only from
budget checks (no test failure, so no base re-run needed). Over layer budget (60 s):
- test_b2_service_outcome_paths 70.4 s (alone 44.9 s), test_billing_gate 79.1 s (alone 54.5 s): load flakes; alone they are
  within their ceilings and the runner retires the debt.
- test_project_start 203.9 s vs ceiling 183.4 s (alone 170.8 s, within ceiling): load flake; was 207.3 s over budget in 0.8.69.
- test_run_retention_scale 81.1 s (alone 68.9 s): same 68.9 s as the 0.8.69 run, pre-existing, unchanged by this batch.
- test_result_lifetime 57.2 s: now within its layer budget, debt retirement required (registry entry; not changed here).
Nothing fixed. Unchecked requirements (88) and 3 skipped-only requirements are as in the 0.8.69 run.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
