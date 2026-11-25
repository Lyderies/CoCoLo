# S13 status: DONE, all budgets met, no product finding

Branch `feat/s13-budgets` on 0.8.66 (afb6f610). Test-only: new `packages/codex-agents/tests/test_s13_budgets.py`,
`packages/codex-agents/scripts/measure_s13.py`, registry entries, docs. Requirement map: [../s13.md](../s13.md).
Raw numbers (macOS, CPython 3.14, disposable stores, 100,000 run): [measurements.json](measurements.json).

## Red then green

Red: the four tests run before `measure_s13.py` existed all errored (FileNotFoundError), 0.001 s. Green: 4 tests, about 2.3 s.
No existing assertion was changed (no assertions.md). `tests.test_b7b_budgets` keeps its `unmeasured` label for B1/B6.

## Measured against budget

| Row | Measured | Budget |
|---|---|---|
| B1 register work of a normal run | 0.127 ms register (3 takes, 1 arm, 2 releases, 1 wake_before, 6 wake_settled) + 1.667 ms memory writer = 1.794 ms | under 5 ms |
| B6 one eligible behind 128 blocked, 50 arrivals before each batch | released in 1 batch, 0.85 ms (0 tombstones), 0.94 ms (100,000) | within 4 batches, each under 50 ms |
| B6 / RS-10 flat work at 100,000 released lease+reservation rows | 3,067 VM steps at 0 and at 100,000 | equal (tolerance 16) |
| RS-10 1,000 due holds | 1,000 released in 16 batches, max batch 21.1 ms, total 0.29 s | at most 18 batches, under 50 ms, 60 s |
| RS-D S3 six budget tests (`root:test_rsd_budget`) | rerun: 6 tests pass | per `docs/requirements/rsd-s3/` |

## Verification (unsandboxed, TMPDIR short dir, --jobs 4)

`scripts/parallel_tests.py --jobs 4 --module codex:tests.test_b7b_budgets --module codex:tests.test_holds_report
--module root:test_rsd_budget --module codex:tests.test_s13_budgets`: `metadata_errors []`, `failed_units []`,
`requirement_failures []`; 33 codex + 6 root tests; test_s13_budgets 3.6 s, test_b7b_budgets 45.4 s (machine loaded; 44.7 s before this change).
Rerun the 100,000-tombstone numbers: from `packages/codex-agents`, run `scripts/measure_s13.py` with `tests` importable
(PYTHONPATH=. and `import tests.support` first), same python.

## Caveat for the primary

B6's design text (RS-B draft 4 section 5) describes back-off for blocked rows. The product's lease start pass has none: it skips
live rows each pass at negligible cost, so the budget holds. If back-off is later added, this test still measures the real pass.
