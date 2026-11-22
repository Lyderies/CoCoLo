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
