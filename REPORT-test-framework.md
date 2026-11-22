# Test framework handoff

## Step 0 — measure (complete)
- Commit: `2846356e` — baseline, top-15 profiles, reproducible capture and discovery inventory.
- Decision 75 / step 0 evidence: `MEASURE-test-framework.md:16`, `measurements/test-framework/baseline-timings.json:1`.
- Full default run: 16 jobs, 4,849.08 job-seconds, 329.6 s runner wall; 5,683 tests (95 existing skips), 288 modules.
- Existing red: `tests/test_e2e_requirements.py:53` — 80 requirements have no check; unchanged, for step 4.
- Top-15 evidence: `MEASURE-test-framework.md:85`; 2,552.35 baseline job-seconds (52.6%); profile exit 0, 2,339.63 job-seconds, 844 test IDs selected exactly once.
- Cheapest candidates and measurement limits: `MEASURE-test-framework.md:150` and `:68`; no optimization savings claimed.
- Removed/changed assertions: none; deleted tests: none. Production, runner and test files unchanged.
- Open: Linux/Python 3.11 evidence, descendant/native-flush visibility, existing skips; scope and limits documented.

## Step 1 — requirements (complete; ready for review)
- Commit: `c5b1d0bc` — TF-1..TF-22 and step-0 handoff; measurement Markdown formatting only otherwise.
- TF-1..7 -> `REQUIREMENTS-test-framework.md:47` (runner, layers, coverage, shrink-only legacy list); TF-8..9 -> `:106` (budgets/timing).
- TF-10..13 -> `REQUIREMENTS-test-framework.md:125` (real-I/O guards); TF-14..15 -> `:153` (one gated golden helper).
- TF-16..18 -> `REQUIREMENTS-test-framework.md:170` (test-only Hypothesis/profiles/soak); TF-19..22 -> `:196` (merge/scenario/pruning/review gates).
- Validation: 22 unique sequential IDs; failing witnesses specified; identifier commit hook and whitespace checks pass.
- Red/green framework tests: not started; requirements review precedes step 2. Step-0 baseline failure remains visible.
- Removed/changed assertions: none; deleted tests: none. Existing source/tests/runner/stage files unchanged.
- Open for review: numeric budgets and stage/layer mapping (`:25`), catalogue and legacy fingerprint/base contract (`:69`, `:94`).
- Grandfather manifest generation and verified Hypothesis version/hash are step 2; committed baseline inventory supplies the starting IDs.
- Stopped after step 1 for primary/owner CoCoLo review; merge-library worktree untouched, no rebase or implementation begun.

## Step 2 — runner rules and test tools (implementation complete)
- Commits: `05225579` immutable 5,683-test baseline; framework implementation follows this report in the same commit.
- TF-1..18 -> `tests/test_test_framework.py`, `tests/test_test_framework_guards.py`; red/green witnesses in `measurements/test-framework/step2-evidence.json`.
- Latest affected run: 45 tests, four modules, no failed workers or over-budget modules; 8.534 job-seconds (`measurements/test-framework/step2-run.json`).
- Global exit remains 1: 80 missing lifecycle checks and TF-19..22 later-step checks; none waived or marked passing.
- Existing runner/CI-stage tests pass unchanged. Structured worker outcomes reject skipped-only proofs, zero exits without results and caught guard violations.
- CI prepares hashed test dependencies on Python 3.11 and combines six shard records before enforcing complete module budgets.
- Hypothesis 6.170.17 and sortedcontainers 2.4.0 installed only in the owner-authorized temporary test environment; hashes and primary-source provenance committed.
- Removed/changed assertions: none; deleted tests: none. New negative tests were run red before corresponding code changes.
- Review checkpoints moved to completion by owner; continuing to step 3. Linux execution remains an outstanding CI gate; local evidence is macOS.

## Step 1 review r1 — applied before implementation reconciliation
- Review source: `REVIEW-testfw-step1-r1.md`; fixes 1–14 and optional 15 applied; TF-8 option A selected by owner.
- Fix -> `REQUIREMENTS-test-framework.md` line: 1 -> 266, 2 -> 257, 3 -> 191, 4 -> 77, 5 -> 225.
- Fix -> `REQUIREMENTS-test-framework.md` line: 6 -> 23, 7 -> 63, 8 -> 87, 9 -> 103, 10 -> 165.
- Fix -> `REQUIREMENTS-test-framework.md` line: 11 -> 219, 12 -> 260, 13 -> 110, 14 -> 58, 15 -> 44.
- Additional fix-14 witnesses: TF-5 -> 97, TF-13 -> 183, TF-15 -> 202, TF-19 -> 250.
- Budget option A -> 128; discovery/preflight timings (fix 6) -> 147.
- Commit: this requirements/report commit; no executable or test assertions changed. Existing step-2 implementation now needs reconciliation with these stricter rules.
- Next review stop: after step 3, per latest owner instruction; steps 4–5 wait for that review.

## Step 2 — step-1 r1 implementation reconciliation
- Commits: `f9902898` fixes 1–14 plus optional 15; `29ddc549` immutable dependency hashes and budget census; implementation in this report's commit.
- TF-8 option A: 16 baseline scenario modules exceed 60s; ceilings generated from saved timings, warned every run, shrink-only/history checked (`MEASURE-test-framework.md`, `scripts/test-budget-debt.json`).
- TF-4/7/14: exact source comparison, registered pending review/future checks, transitive fixture hashes, explicit trusted bases, golden-bypass rejection (`tests/test_test_framework_review.py`).
- TF-1/9/10/11/18: bounded worker-group teardown, secrets/temp isolation, saved preflight failure timing, discovery-time guards, inherited sockets, explicit/conflicting soak/profile refusal (`tests/test_test_framework_guards.py`).
- Red/green ledger: `measurements/test-framework/review-evidence.json`; six affected modules passed 62 tests in 10.913 job-seconds before the last two added guard witnesses.
- Latest affected guard/review/stage run: 32 tests, no failed workers or budget failures, 16.771 job-seconds; `measurements/test-framework/review-framework-run.json`.
- TF-13/16 entry inventory names real checks for 13 entry points. Owner-approved replacement host modules passed 46 tests in 37.096 job-seconds (`review-replacement-run.json`).
- Assertion replacements: `ASSERTIONS-test-framework.md:10` lists the two removed host help-probe cases and their unchanged start-to-finish replacements; no test function deleted and no existing assertion weakened.
- Global exit still 1 for the 80 missing lifecycle checks; future ML/scenario/review records remain not_run, not passing. Linux/Python 3.11 execution still awaits CI; source grammar checked for 3.11.
- Next: carry reconciliation to merge branch and finish step 3; stop for review after step 3, as owner requested. No push or product install performed.
