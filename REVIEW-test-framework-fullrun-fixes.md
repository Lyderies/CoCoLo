# First full-suite integration fixes

Incoming release `b531844030fa619c1f62ddd6c092ebc2ea4fca38` merged without conflicts into both own branches (merge-library first at `5cdbec83`). All incoming tests remain intact. No existing test body or assertion changed. The only new-witness corrections are listed in ASSERTIONS-test-framework.md.

Requirements: TF-24 supplements TF-1/7/8/10/12; TF-23 evidence is in `scripts/test-review-witnesses-integration.json`. Requirements commit `97bc854c`, red witnesses `9bbf1582`, implementation `56941ec4`, reviewed metadata version `51257b92`, defining-source binding version `9f76022e`. Exact old implementation replay uses `9bbf1582`; its runner/framework implementation is the pre-fix merged code. The data-only debt witness uses the initial pre-data red.

| Fix | Enforcement | Witness | Red → green; positive control |
|---|---|---|---|
| discovery (TF-24) | `scripts/parallel_tests.py:102` | `tests/test_test_framework_integration.py:20` | `discovery-red.json` → `discovery-green.json`; A plain package test is discovered; broken import keeps tests.test_broken. |
| layers (TF-24) | `scripts/test-layer-reasons.json:2` | `tests/test_test_framework_integration.py:34` | `layers-red.json` → `layers-green.json`; Plain root:test_check_identifiers remains unit; all five original modules pass unchanged. |
| campaigns (TF-24) | `scripts/parallel_tests.py:136` | `tests/test_test_framework_integration.py:51` | `campaigns-red.json` → `campaigns-green.json`; Ordinary workers can overlap up to default capacity; exclusive campaign runs with an empty pool. |
| base (TF-24) | `scripts/parallel_tests.py:366` | `tests/test_test_framework_integration.py:103` | `base-red.json` → `base-green.json`; Explicit b5318440 affected runs execute; every CI selector names the release base and existing event-path tests pass. |
| legacy (TF-24) | `scripts/test_framework.py:125` | `tests/test_test_framework_integration.py:68` | `legacy-red.json` → `legacy-green.json`; Reviewed fingerprints pass; changed implementation and untagged additions fail. |
| debt (TF-24) | `scripts/parallel_tests.py:316` | `tests/test_test_framework_integration.py:85` | `debt-red.json` → `debt-green.json`; Measured current debt validates; added keys and higher ceilings fail. |
| history (TF-24) | `scripts/parallel_tests.py:277` | `tests/test_test_framework_integration.py:113` | `history-red.json` → `history-green.json`; Real Git introduction and retirement pass; immutable tamper, raised ceiling and historical restoration fail. |

All paths in the evidence column are under `measurements/test-framework/release-0850`. Initial red: 0.156114 job-seconds; final old-code replay: 0.171270. Affected framework run: 31.580453 job-seconds, all workers passed. The binding correction plus final seven witnesses: 0.794242 job-seconds, only the 80 deferred lifecycle metadata entries remain.

The release baseline contains 5,739 untagged reviewed tests (5,683 original + 56 incoming) with 315 source hashes verified against integration revision `5cdbec83`. It retains existing helper/value checks and separately freezes the new immutable snapshots; current lists may only shrink. The single slice-constant binding correction has a new immutable file and binds the defining test source instead of every loaded test module. Old baselines are untouched.

The four new **existing-file** debt ceilings come directly from the primary's report: billing_gate 142.255175 s, billing_rejudge 110.520920 s, ledger_startup 11.210668 s, session_guard 6.301415 s. Bridge retains its original 299.5 s ceiling; 283.334960 s was a warning, not a budget failure. cli_billing_runtime and run_network retire using the primary's complete under-budget evidence. No merge-library file receives debt.

Ten affected merge modules passed at default 16 jobs: elapsed 161.515750 s, 161.490655 job-seconds. The six campaign times are 44.453241, 23.542513, 24.127025, 30.384197, 21.320694 and 13.107547 s (adversarial, contract, forward faults, kills, owners, recovery faults). Same assertions, same normal persistence samples, unchanged 60 s limits. Queue waits are excluded; all worker time counts.

Full runs use `--base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --save-timings ...` and omit `--jobs`. CI schedule/tag selectors use that release base; PR/push selectors retain their trusted event bases and fail-closed validation. The nightly schedule is retained. The existing hash-pinned test-only environment was reused without installation; both installed distribution versions and all RECORD hashes verified. The framework changes do not alter shipped algorithms; the merge branch subsequently carries the primary-reviewed runtime registration/version metadata documented below.

Outstanding review-evidence, soak and future-step obligations remain visible and are reported separately by owner instruction. Previously listed LOW gaps remain unchanged. Python 3.11/Linux execution remains a CI gate; local measurements use macOS/Python 3.14.7.


## Single full-run result

Revision `f69da048`, explicit base `b531844030fa619c1f62ddd6c092ebc2ea4fca38`, pinned prepared environment, default **16 jobs**: **496.778018 s elapsed, 4535.410045 job-seconds, 395 units, 6193 tests**. Discovery/preflight remain separately recorded. Exactly one full suite was run. Saved timings and report: `measurements/test-framework/release-0850/integrated-full-timings.json` and `integrated-full.run.json`; raw-report digest and command are in `integrated-full-provenance.json`. No module exceeded its budget.

| Failed unit in the full run | Cause / disposition |
|---|---|
| `codex:tests.test_host_commands#0` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `codex:tests.test_host_commands#2` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `root:test_install#1` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `codex:tests.test_host_commands#1` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `root:test_attest` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `codex:tests.test_lifetime_cap_guard` | Missing two bounded merge-library cap registrations supplied by primary bookkeeping 0ca9c36f; affected rerun after copying the registrations. |
| `codex:tests.test_runtime_release` | Missing merge_library.py runtime registration from primary bookkeeping 0ca9c36f; exact file-list guard/refusal. Affected rerun after copying its runtime registration and paired version/digest. |
| `root:test_e2e_requirements` | Expected: 80 step-4 lifecycle checks remain unimplemented; unchanged and deferred. |

The full-run bookkeeping failures were real failures and remain in the saved record. They were fixed afterward on feat/merge-library using the primary-reviewed metadata, with affected-only reruns; this report does not represent the original full run as green. All six original failure-source test files and all 32 incoming test files remain byte-identical. No original assertion was changed.

The four newly admitted existing-module debts retired from this full run: billing_gate 56.596 s, billing_rejudge 37.463 s, ledger_startup 1.359 s, session_guard 3.824 s. Their immutable admission ceilings remain recorded. Current debt list (14 entries; all ceilings unchanged):

| Module | Ceiling seconds | Full-run seconds |
|---|---:|---:|
| `codex:tests.test_bridge` | 299.500 | 277.278 |
| `codex:tests.test_container_stop` | 67.620 | 62.123 |
| `codex:tests.test_host_commands` | 119.560 | 61.072 |
| `codex:tests.test_learning_cli` | 95.440 | 89.422 |
| `codex:tests.test_lost_run_settlement` | 98.640 | 88.740 |
| `codex:tests.test_project_driver` | 390.320 | 316.821 |
| `codex:tests.test_trial_run_driver_bugs` | 70.220 | 63.795 |
| `codex:tests.test_write_merge` | 241.240 | 196.939 |
| `root:test_install` | 110.140 | 94.972 |
| `root:test_install_claude_upgrade` | 120.330 | 111.117 |
| `root:test_setup_sh` | 177.880 | 159.281 |
| `root:test_setup_straight` | 264.440 | 236.005 |
| `root:test_setup_update` | 167.880 | 152.170 |
| `root:test_setup_wizard_direct` | 265.860 | 236.071 |

The debt witness's admission assertions now read the immutable ceiling and separately validate current shrink-only debt; its old rejection of valid retirement is saved (0.576609 job-seconds), and its corrected module plus TF-23 checks pass (5.661973 job-seconds). All assertion replacements are in ASSERTIONS-test-framework.md.

## Preserved obligations (owner-authorized separate reporting)

The only expected failed worker is `root:test_e2e_requirements`, and the only metadata errors are the 80 deferred lifecycle requirements. The aggregate requirement gate is intentionally still non-green: TF-5/6/19/21/22 review records, the step-3 placeholder, TF-18/20 and soak execution are not run. ML-4/12 also retain skipped real-filesystem/image probes, reported as failed requirement coverage by the strict AND gate. No skipped or missing evidence was converted into a pass. `outstanding-obligations.json` records every exact non-passing check/status. Steps 4–5 remain untouched.

Final affected bookkeeping verification on feat/merge-library: **36.655826 s elapsed / 159.420827 job-seconds**, default 16 jobs, 11 units / 254 tests; every worker passes, zero budget failures/retirements, exactly 80 deferred lifecycle metadata errors. The unchanged host-command, install, attest, lifetime-cap and runtime-release modules all pass. Evidence: `measurements/test-framework/release-0850/runtime-final-green.run.json`. The merge branch carries exactly the primary's runtime-file registration, bounded-cap entries, VERSION and runtime digest file from `0ca9c36f`; tests and Claude release catalogues are unchanged. Its added immutable runtime binding snapshot records 1,080 reviewed constant changes.

Final evidence-manifest check on the merge branch: **5.653291 job-seconds**, 17 tests pass at default 16 jobs, no budget failures, exactly 80 deferred lifecycle metadata errors. All final TF-23 declarations, including unchanged runtime witnesses and the retirement correction, are validated.
