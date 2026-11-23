# Round-2 scoped framework review fixes

Owner decisions 99 A and 100 A; source review is REVIEW-testfw-step2-r2.md. This pass fixes all eleven MEDs and the explicitly mandated environment boundary. Other LOWs are recorded, not repaired. Paths below refer to this full worktree.

| Finding | Requirement | Fix | Witness | Intended failure / control |
|---|---|---|---|---|
| M1 | TF-7 | `scripts/test_framework.py:372` | `tests/test_test_framework_r2.py:34` | Old fingerprint function fails both independent unsupported-value and dynamic-helper subcases: unrelated source restored values/helpers eligibility. Exact defining-file positive controls pass. |
| M2 | TF-14 | `scripts/test_framework.py:511` | `tests/test_test_framework_r2.py:62` | Qualified/renamed, function-local and package imports reject direct golden reads. Initial and follow-up reds name the escaped import; old implementation and alias-omission mutants fail the intended assertion. |
| M3 | TF-8/9 | `scripts/test_framework.py:211` | `tests/test_test_framework_r2.py:72` | Absent/empty authoritative plans and a missing fragment cannot retire debt; complete plan can. Old implementation reports missing authoritative fragment authorized retirement. |
| N1 | TF-7 | `scripts/test_framework.py:490` | `tests/test_test_framework_r2.py:49` | Two a/fixture.py and b/fixture.py entries each change the digest independently; old implementation fails explicit dependency overwritten: a. |
| M4 | TF-5/9 | `scripts/parallel_tests.py:607` | `tests/test_test_framework_r2.py:92` | Real combiner rejects not_run/failed with requirements failed/not_run and incomplete shards with missing or repeated shards. Real CLI rejects missing outcome; old caller fails caller accepted not_run requirement report on a failure-bearing combiner result. |
| M5 | TF-15 | `tests/test_test_framework.py:444` | `tests/test_test_framework.py:467` | Recreate valid root, prove zero errors, mutate one pin/hash, require exact pin/hash error. Ignoring pin errors now fails M5 invalid pin/hash escaped. |
| N2 | TF-4 | `tests/test_test_framework_review.py:62` | `tests/test_test_framework_review.py:74` | Valid digest-backed review passes before changing only revision. Bypassed revision comparison fails N2 wrong revision was accepted; restored revision passes. |
| M6 | TF-11 | `tests/test_test_framework_guards.py:188` | `tests/test_test_framework_guards.py:208` | Build loop before guard, require coroutine-reached marker, actual async network denial and no socketpair confounder. Allowing async networking fails M6 prohibited asyncio API completed. Existing socketpair-denial and no fixture.sock side-effect checks remain. |
| M7 | TF-1/9/13 | `tests/test_test_framework_guards.py:542` | `tests/test_test_framework_guards.py:386` | Two real 2-shard invocations retain inherited/generated load_tests selections. Duplicate execution, wrong import diagnostic and delayed descendant teardown each fail their named outer assertion. Normal and interrupted teardown have external 5s bounds. |
| M8 | TF-5 | `tests/test_test_framework_guards.py:462` | `tests/test_test_framework_guards.py:469` | Require executed ID membership and exact expected_failure/unexpected_success before coverage rejection. Removed outcome and wrong status mutants fail their specific diagnostics. |
| N3 | TF-12 | `tests/test_test_framework_guards.py:512` | `tests/test_test_framework_guards.py:520` | Markers follow the existing <0.5s assertions inside workers and are required outside. Delayed denial fails N3 join elapsed assertion did not complete; join and poll both have outer marker checks. |
| D100 / L8 exception | TF-10 | `scripts/parallel_tests.py:71` | `tests/test_test_framework_guards.py:566` | Shared discovery/worker allowlist drops synthetic GITHUB_TOKEN, AWS_SESSION_TOKEN, DATABASE_PASSWORD and an unrelated plain variable in local and CI-parent cases; PATH/HOME/COCOLO_TEST_ALLOWED survive. Before fix the actual import/worker reports D100 leaked GITHUB_TOKEN. |

The test commits precede implementation: `4578546a`, `19ad4955`, `17a7d734`, then `7c53616c`; local/package-import reds `cc22fc21` precede `b0e4f432`. `d1bd5d63` narrows new witness requirement mappings; `e3b35ad5` retains the original async filesystem side-effect assertion. No test was deleted. See ASSERTIONS-test-framework.md for each changed assertion instance.

M1/N1 baseline migration: the old immutable baselines are untouched. The new immutable `scripts/test-legacy-bound-values.json` retains exactly 5,683 legacy IDs, binds specific helper sources for 1,356 checks and keys files by repository-relative path. All 304 contributing source files were compared with the pre-pass revision `8cfab050` before generation; `measurements/test-framework/bound-baseline-provenance.json` records that provenance. Zero unresolved fingerprints remain. No broader eligibility waiver was added.

The authoritative-plan fixture reconciliation is documented in CONTRADICTIONS-review-r2.md. Its expected assertions remain unchanged; a fabricated measurement must now declare its actual planned fragment.

Evidence: `measurements/test-framework/review-r2-framework/runs.json` contains digest-bound extracts, including initial intended reds and final affected-module results, not substitute CI evidence. The two sensitivity JSON files and saved red logs contain exact failure messages. The `.py.txt` files preserve the counterfactual procedures (substitute the indicated worktree roots to replay). Final four-module run: 95 checks, 20.520382499031257 job-seconds, default 16 jobs, no worker/budget failures. Earlier six-module run: 103 checks, 21.244645251019392 job-seconds. A final guard-only follow-up retains the filesystem side-effect assertion; its result is recorded in the main report and evidence.

Every complete affected run still exits 1 for exactly 80 missing step-4 lifecycle checks. No waiver was added. Executed on macOS/Python 3.14.7; macOS/Linux Python 3.11 runtime and Ruff remain open. Python 3.11 grammar checking is not a runtime certification. No full suite was rerun.

## Known LOW gaps (all 17)

- L1: installer/metadata dependency strings remain import-scanned only.
- L2: duplicate-ID parse may escape its handler.
- L3: whitespace-only rationale can block class fallback.
- L4 (conditional): evidence JSON non-object shape handling remains unverified.
- L5: omitted selector timeout can corrupt the xlist/signature.
- L6: per-group cleanup exceptions can skip later groups or obscure the original exception.
- L7: timeout PID reuse and elapsed-time inference remain.
- L8: resolved only because decision 100 explicitly requires the allowlist in this pass; this is the sole LOW exception.
- L9: existing rationales can repeat test names.
- L10: existing profile witness does not restore the Hypothesis profile.
- L11: golden witness reason content/diff assertions remain incomplete.
- L12: os.popen process probe remains omitted.
- L13: absent-platform API diagnostic gaps remain.
- L14: existing start()+join(0) probe is not a completion witness.
- L15: git fixture commands lack timeouts.
- L16: historical removed Hypothesis override remains absent from the earlier assertion ledger.
- L17: raw kqueue remains unguarded; r1 forbade a blanket patch, so this is not classified as a defect.
