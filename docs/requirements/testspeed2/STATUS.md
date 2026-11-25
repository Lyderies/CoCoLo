# TESTSPEED-2 status

Branch `fix/testspeed2`, base 0.8.64 batch. Part B (lost-run helpers read under the ledger lock) at `e24346cf`.
Part A (setup_straight speed) done and committed; no product code changed, no version bump, nothing pushed.

## Results: sum of the module's fragment seconds (the budget check's number)

| Run | before | after |
| --- | --- | --- |
| alone (`--module root:test_setup_straight`) | 181.6 | 101.4 |
| `--jobs 16` beside `codex:tests.test_project_driver` and `root:test_install` | 233.6 | 156.0 |
| one process, serial, 19 tests | 165 | 91 |

Full-run load (the last five full runs, 285-299 s) is heavier than the three-module run; the saving is CPU
(compile), so it grows with load: 156/234 = 0.67 applied to 290 s gives about 195 s, under the 230 s target.
`metadata_errors: []`, no failed units, in all runs. The measurements ran on a shared machine at load 6-10, so
single numbers have a few seconds of noise.

Slowest tests, serial: `update_skips_billing_when_attested_and_runs_it_otherwise` 29.9 -> 16.4,
answers rerun with sentinels 25.0 -> 13.8, answers file 16.6 -> 9.1, each `installed()`-based test 9 -> 5.7.

## Not done / next

- `test_setup_update` and `test_setup_wizard_direct` can set `shared_pycache = True` the same way; not measured here.
- The budget-debt ceiling (293.12 s) was left for the primary to tighten after the next full run.
- Rerun: `$PY -B scripts/parallel_tests.py --jobs 16 --module root:test_setup_straight --module codex:tests.test_project_driver --module root:test_install --save-timings <file>` from the repository root, short `TMPDIR`.
