# Merge-library first-full-run handoff

The single full run on `f69da048` used default 16 jobs, the prepared pinned environment, explicit base `b531844030fa619c1f62ddd6c092ebc2ea4fca38`, and `--save-timings`: **496.778018 s elapsed, 4535.410045 job-seconds, 395 units / 6193 tests**. Zero budget failures; all merge modules below 60 s. The original run had seven unexpected bookkeeping-related failed units plus the deferred e2e module. Every unit/cause and current debt ceiling is in REVIEW-test-framework-fullrun-fixes.md; original diagnostics remain saved. Exactly one full run, followed only by affected-module runs.

Requirements were recorded before copying the primary-reviewed files (`4561b23b`, `b312c401`); unchanged tests supplied the reds. Registrations `db76fc52`, paired version/digest `f49092d9`, immutable binding version `11d36a06`. No merge algorithm or test assertion changed. All four runtime metadata files exactly match primary bookkeeping `0ca9c36f`.

| Fix | Requirement / enforcement | Unchanged witness | Red → green and control |
|---|---|---|---|
| runtime-registration | TF-24 / `packages/codex-agents/native-package/.codex/orchestration/codex_agents/runtime_files.py:128` | `tests/test_attest.py:1956` | `runtime-registration-red.json` (f69da048) → `runtime-registration-green.json`; control `root:test_attest.ProjectRuntimeTests.test_an_exact_copy_passes_with_the_projects_own_files` |
| runtime-caps | TF-24 / `packages/codex-agents/tests/lifetime_caps.json:1069` | `packages/codex-agents/tests/test_lifetime_cap_guard.py:72` | `runtime-caps-red.json` (f69da048) → `runtime-caps-green.json`; control `codex:tests.test_lifetime_cap_guard.LifetimeCapGuardTests.test_a_new_limit_is_caught` |
| runtime-version | TF-24 / `packages/codex-agents/native-package/VERSION:1` | `packages/codex-agents/tests/test_runtime_release.py:28` | `runtime-version-red.json` (db76fc52) → `runtime-version-green.json`; control `codex:tests.test_runtime_release.RuntimeReleaseTests.test_the_runtime_file_set_is_exactly_the_package_orchestration_files` |

Evidence paths are under `measurements/test-framework/release-0850`; TF-23 declarations are in `scripts/test-review-witnesses-runtime-integration.json`. The intermediate version-guard failure is preserved separately. Final affected run: **36.655826 s elapsed, 159.420827 job-seconds**, default 16 jobs, 11 units / 254 tests, every worker passes, zero budget failures/retirements, exactly 80 deferred lifecycle metadata errors.

The active snapshot adds 1,080 value bindings affected by the primary's runtime tuple/version constants; its four source hashes match 0ca9c36f, test method/fixture fingerprints are unchanged, every earlier snapshot is untouched, and committed introduction validation applies. The 32 incoming test files and six original discovery/resource files remain byte-identical to their required bases. The framework branch is merged into this branch.

No original assertions removed, replaced or weakened. New framework-witness corrections are enumerated in ASSERTIONS-test-framework.md. The aggregate requirement gate remains non-green for the deferred lifecycle checks and separately reported review/soak/image/future-step evidence, preserved at the owner's instruction. Steps 4–5 and prior LOW gaps are unchanged.
