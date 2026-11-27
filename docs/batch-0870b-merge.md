# Batch 0.8.70 rebuild (install point IP-D, branch merge/batch-0870b)

Base b63b6f5c (final 0.8.69, decision 375). `version/0.4.0` not moved, no push.

## Merge
- `feat/s14d-measure-budgets` @3e90decb (on top of the old 0.8.70 batch dbad7664: 0.8.69@77bca2e5 + 366/367 merge 25be003e + doc eb99f6a2 + bump 808b0f46; decisions 370, 374), merge commit 5f70f276.
- Only conflict: `runtime-releases.json` (kept 0.8.69 from b63b6f5c, added 0.8.70). VERSION is 0.8.70; registries auto-merged.

## Re-record (84380c7e)
- 0.8.70 runtime digest re-recorded: 56b8b87ba0ee752fc8f2f1d39db7c400d28a0ade30b463eeda7aeceb4fc3df4b (never installed).
- Previous-release record for 0.8.69 now `source_commit` b63b6f5c. Claude package recompiled from b63b6f5c: bundle digest unchanged (eb34209d...), so only the source commit changed; catalogue count stays 30.
- Green (each through the runner, metadata_errors [], failed_units []): test_runtime_release, test_previous_release_catalogue, test_b4t_takeover (incl. FloorTests 0.8.68/0.8.69 refused), test_candidate_evidence_lookup.

## Budget items
- test_result_lifetime debt entry removed from scripts/test-budget-debt-0850.json (registry only).
- test_run_retention_scale alone through the runner: 32.9 s and 32.8 s (under 60 s; the S14d change cut it from 68.9 s). Ceiling not raised.

## Full run (`docs/batch-0870b-full-run.txt`, timings json beside it)
556 units, 623.4 s elapsed, 8.0 s preflight, `failed_units []`, `metadata_errors []`, `requirement_failures []`. No test failure, so no base re-run. Over budget:
- test_project_start 192.8 s vs ceiling 183.4 s in the full run; alone 161.8 s (within ceiling): load flake.
- test_run_retention_scale 75.9 s in the full run; alone 32.8 s: load flake.
- Runner retired debt for test_container_stop and test_session_guard (registry entries not changed here).
Nothing fixed. Unchecked requirements (88) as before.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
