# TESTSPEED-3 status

Branch `fix/testspeed3`, base 0.8.65 (`c30e2129`). Done and committed; no product code changed, no version bump,
nothing pushed. `metadata_errors: []`, no failed units, in every run.

## Results: the budget check's number (sum of the module's fragment seconds)

| Run | driver (before: one module) | driver | recovery | lifecycle |
| --- | --- | --- | --- | --- |
| alone (one module) | 49.0 | 25.2 (two-way split, before the sweep change) | | |
| three modules together, default jobs | | 14.3 | 19.3 | 14.3 |
| `--jobs 16` beside `codex:tests.test_project_driver` and `root:test_install` | 61-64 in the last four full runs | 20.9 | 33.9 | 24.1 |

The three-module run beside `test_project_driver` and `test_install` is heavier relative to its length than a full
run (the same module was 1.78 times its quiet seconds there, 1.25 times in a full run), so the 33.9 s of the
slowest module is a pessimistic number against the 45 s target. 32 tests before and after (16 + 12 + 4).

## Not done / next

- The budget-debt file has no entry for these modules; the old single module needed none.
- `SWEEP_SECONDS` 0.1 is set in the settlement harness only; other driver-based modules (`test_project_driver`,
  `test_lost_run_settlement`) wait for the same one-second sweep after a restart and could do the same.
- Rerun: `$PY -B scripts/parallel_tests.py --jobs 16 --module codex:tests.test_account_settlement_driver --module codex:tests.test_account_settlement_recovery --module codex:tests.test_account_settlement_lifecycle --module codex:tests.test_project_driver --module root:test_install --save-timings <file>` from the repository root, short `TMPDIR`.
