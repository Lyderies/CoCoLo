# First full-suite integration fixes

Incoming release `b531844030fa619c1f62ddd6c092ebc2ea4fca38` merged without conflicts into both own branches (merge-library first at `5cdbec83`). All incoming tests remain intact. No existing test body or assertion changed. The only new-witness corrections are listed in ASSERTIONS-test-framework.md.

Requirements: TF-24 supplements TF-1/7/8/10/12; TF-23 evidence is in `scripts/test-review-witnesses-integration.json`. Requirements commit `97bc854c`, red witnesses `9bbf1582`, implementation `56941ec4`, reviewed metadata version `51257b92`, defining-source binding version `9f76022e`. Exact old implementation replay uses `9bbf1582`; its runner/framework implementation is the pre-fix merged code. The data-only debt witness uses the initial pre-data red.

| Fix | Enforcement | Witness | Red → green; positive control |
|---|---|---|---|
| discovery (TF-24) | `scripts/parallel_tests.py:102` | `tests/test_test_framework_integration.py:20` | `discovery-red.json` → `discovery-green.json`; A plain package test is discovered; broken import keeps tests.test_broken. |
| layers (TF-24) | `scripts/test-layer-reasons.json:2` | `tests/test_test_framework_integration.py:34` | `layers-red.json` → `layers-green.json`; Plain root:test_check_identifiers remains unit; all five original modules pass unchanged. |
| campaigns (TF-24) | `scripts/parallel_tests.py:136` | `tests/test_test_framework_integration.py:51` | `campaigns-red.json` → `campaigns-green.json`; Ordinary workers can overlap up to default capacity; exclusive campaign runs with an empty pool. |
| base (TF-24) | `scripts/parallel_tests.py:365` | `tests/test_test_framework_integration.py:101` | `base-red.json` → `base-green.json`; Explicit b5318440 affected runs execute; every CI selector names the release base and existing event-path tests pass. |
| legacy (TF-24) | `scripts/test_framework.py:124` | `tests/test_test_framework_integration.py:68` | `legacy-red.json` → `legacy-green.json`; Reviewed fingerprints pass; changed implementation and untagged additions fail. |
| debt (TF-24) | `scripts/parallel_tests.py:316` | `tests/test_test_framework_integration.py:85` | `debt-red.json` → `debt-green.json`; Measured current debt validates; added keys and higher ceilings fail. |
| history (TF-24) | `scripts/parallel_tests.py:277` | `tests/test_test_framework_integration.py:111` | `history-red.json` → `history-green.json`; Real Git introduction and retirement pass; immutable tamper, raised ceiling and historical restoration fail. |

All paths in the evidence column are under `measurements/test-framework/release-0850`. Initial red: 0.156114 job-seconds; final old-code replay: 0.171270. Affected framework run: 31.580453 job-seconds, all workers passed. The binding correction plus final seven witnesses: 0.794242 job-seconds, only the 80 deferred lifecycle metadata entries remain.

The release baseline contains 5,739 untagged reviewed tests (5,683 original + 56 incoming) with 315 source hashes verified against integration revision `5cdbec83`. It retains existing helper/value checks and separately freezes the new immutable snapshots; current lists may only shrink. The single slice-constant binding correction has a new immutable file and binds the defining test source instead of every loaded test module. Old baselines are untouched.

The four new **existing-file** debt ceilings come directly from the primary's report: billing_gate 142.255175 s, billing_rejudge 110.520920 s, ledger_startup 11.210668 s, session_guard 6.301415 s. Bridge retains its original 299.5 s ceiling; 283.334960 s was a warning, not a budget failure. cli_billing_runtime and run_network retire using the primary's complete under-budget evidence. No merge-library file receives debt.

Ten affected merge modules passed at default 16 jobs: elapsed 161.515750 s, 161.490655 job-seconds. The six campaign times are 44.453241, 23.542513, 24.127025, 30.384197, 21.320694 and 13.107547 s (adversarial, contract, forward faults, kills, owners, recovery faults). Same assertions, same normal persistence samples, unchanged 60 s limits. Queue waits are excluded; all worker time counts.

Full runs use `--base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --save-timings ...` and omit `--jobs`. CI schedule/tag selectors use that release base; PR/push selectors retain their trusted event bases and fail-closed validation. The nightly schedule is retained. The existing hash-pinned test-only environment was reused without installation; both installed distribution versions and all RECORD hashes verified. No shipped code changed.

Outstanding review-evidence, soak and future-step obligations remain visible and are reported separately by owner instruction. Previously listed LOW gaps remain unchanged. Python 3.11/Linux execution remains a CI gate; local measurements use macOS/Python 3.14.7.

## Additional integration requirements exposed by the full run

TF-24's reviewed-release integration preserves the package's exact runtime-file list (`tests/test_attest.py:1957`, `packages/codex-agents/tests/test_runtime_release.py:43`) and the existing O0c rule: every count-based limit names how its counted objects end (`packages/codex-agents/tests/test_lifetime_cap_guard.py:1`). The full run is the red evidence for these unchanged checks and for their host-command/install callers. `0ca9c36f` already supplies the required merge-library registration and two bounded-cap entries. Copy those two reviewed files exactly; do not change the tests, version or release catalogues. Existing unknown-file refusal and unknown-limit controls must still pass. This requirement note precedes applying the copied bookkeeping.
