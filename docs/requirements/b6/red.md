# B6 red -> green evidence

Red = the final test text run against the base runtime (6a1ea9da, exported with `git archive` to a scratch folder,
the tests and scripts of this branch beside it); green = the same tests on this branch.

| Requirement | Tests (tests/test_b6_unattended_close.py unless named) | Red on base | Green |
|---|---|---|---|
| RS-B-B6-1 | RegisterRowTests x4 | 4/4 fail (no orchestration row, no state column) | pass |
| RS-B-B6-2 | EligibilityTests x3, EligibilityWithRunsTests x2, LeaseTests x2 | 7/7 fail | pass |
| RS-B-B6-3 | ReplayAndFairnessTests.test_B6_3_* | fail (no derived request) | pass |
| RS-B-B6-4 | ReplayAndFairnessTests x2, LeaseTests x2, EligibilityWithRunsTests x1 | 5/5 fail | pass |
| RS-B-B6-5 | LeaseTests K1, EligibilityWithRunsTests K2 | K1 fails (no register row); K2 passes on base (regression pin, as the analysis expected) | pass |
| RS-B-B6-6 | tests.test_rsd_runtime_close (RS-D-2-N1/N2) | built by S2, pass on base | pass (fixture line, assertions.md B) |
| RS-B-B6-7 | SchemaStepTests x5 (real format-20 store, failure, process death, unknown objects, indexes) | 5/5 fail | pass |
| RS-B-B6-8 | OrchestrationStateTests x1 | fail (no state column) | pass |
| RS-D-9-S2/S3 | tests/test_rsd_budget.py (root) x6 | 2/6 fail: retirement VM steps [296, 275] vs [700296, 500275]; plan `result_expiry` + temp sort | 6/6 pass |

Base run of the B6 module: 25 tests, failures=8, errors=19 (24 tests red, K2 green).
