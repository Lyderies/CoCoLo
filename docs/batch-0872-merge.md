# Batch 0.8.72 (install point IP-F, branch merge/batch-0872)

Base 8004695d (accepted 0.8.71, decision 386, not installed). `version/0.4.0` not moved, no push.

## Merges
- `feat/s14f-owner-bounds` @0e3dd5f6 (decision 391; base 8004695d) -> 2bf8af1f, no conflicts.
- `feat/s13g-budgets` @0893f7f6 (decision 392; base 195e779f = 8004695d + S16 run docs) -> 746d121a, no conflicts
  (the registry JSONs merged automatically; S13g touches only two test modules and docs). Nothing from either side lost.
- `test_lifetime_cap_guard` (exact counts, kinds) green after both merges and after the bump; no reconciliation of S14f caps
  (`_scan`, `advance`, `_drain`) with S13g was needed (S13g changes no lifetime_caps entry).

## Bump 0.8.72 (1b8667a2)
VERSION 0.8.72; runtime digest 1ccaf49f... in runtime-releases.json; CHANGELOG; previous-release record for 0.8.71
(source 8004695d; Claude package compiled with `packaging.compile_bundle` from `git archive 8004695d`, bundle digest eb34209d...
unchanged, already pinned; PINNED comment extended); catalogue count 31 -> 32 (two assertions in test_previous_release_catalogue).
Floor: PACKAGE_VERSION 0.8.72, builds 0.8.71 and older refused.
Assertions changed (old -> new), reason: floor and version move with the release:
- `test_b4t_takeover.FloorTests.test_a_store_written_by_this_build_refuses_0868_0869_and_0870` renamed `..._refuses_0868_0869_0870_and_0871` (also renamed in both RS-B-B4t entries of scripts/test-requirements.json); stored floor `'0.8.71'` -> `'0.8.72'`; refused builds (`'0.8.68','0.8.69','0.8.70'`) -> adds `'0.8.71'`.
- `test_b4t_native_p69`: every `0.8.71` (floor and update target) -> `0.8.72`; proves 0.8.69 -> 0.8.72 natively. Also tests/fixtures/b4t/README.md text.
- `test_previous_release_catalogue`: 31 -> 32 (twice).
Green through the runner (metadata_errors [], failed_units [], 15.6 s): test_runtime_release, test_previous_release_catalogue,
test_previous_release_recorded, test_b4t_takeover, test_b4t_native_p69, test_candidate_evidence_lookup, test_lifetime_cap_guard.

## Full runs (both on 1b8667a2, `--base 8004695d`, real sockets; home path redacted to /HOME/, user to <user>)
| | run 1 | run 2 |
|---|---|---|
| output / timings | docs/batch-0872-full-run1.txt, -timings.json | docs/batch-0872-full-run2.txt, -timings.json |
| exit | 0 | 0 |
| units | 572 | 572 |
| elapsed | 607.4 s | 600.8 s |
| preflight | 8.07 s | 8.29 s |
| metadata_errors | [] | [] |
| failed_units | [] | [] |
| budget_failures | [] | [] |
| test_project_start | 146.5 s (ceiling 183.4 s, debt) | 141.9 s |
| test_run_retention_scale | 14.9 s | 14.7 s |

S13g's condition holds: neither run has a budget failure for test_project_start or test_run_retention_scale.
No failures, so nothing was classified or fixed.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
