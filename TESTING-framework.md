# Framework workflow

Use the prepared, hash-checked test-only environment. Shipped programs need no test dependencies.
Full runs use the runner's default jobs. Pass a trusted review base explicitly (the candidate cannot choose its own grandfather baseline):

```
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --save-timings /tmp/cocolo-timings.json
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module root:test_test_framework --save-timings /tmp/cocolo-affected.json
```

The flat timing file remains scheduling input. Its `.run.json` companion records current worker job-seconds,
separate discovery/preflight time, module totals, selected checks, budgets/debt and actual outcomes.
A debt warning does not mean failure; exceeding its unchanged ceiling does. A complete under-budget result
requires removing that module's debt entry before the next gate. Entries cannot be restored or ceilings edited.
Combine all shard `.run.json` files using `--combine-timings`; partial records never certify module budgets.

Declare modules in `scripts/test-stages.json` and `scripts/test-layers.json`. Decorate new/migrated tests with
`test_framework.requirements`; non-unit tests need `why_not_scenario`. Keep all assertions when migrating.
Generate catalogue links with `python -B scripts/generate_test_metadata.py --catalogue`; remove migrated legacy
entries with `--shrink-legacy`. Original source and supplementary fixture/helper fingerprint baselines are immutable.

Use `test_golden.compare` for checked-in UTF-8 expected output. Updates require exactly UPDATE_GOLDEN=1,
a known declared requirement and its adjacent `.requirement` sidecar. Update runs fail intentionally; review
and rerun normally. Temporary helper contract fixtures may inspect their own writes; checked-in golden bypasses fail.

Runs default to ci. Select dev or soak explicitly; a soak module cannot be selected with ci/dev, and multiple
profile selectors are rejected. Complete merge campaigns belong to the explicit soak inventory.
Registered future execution and semantic review records live in `test-checks.json`; absent evidence remains not_run.
A full gate cannot pass missing or unexecuted required evidence. Entry-point links live in `test-entrypoints.json`.

Owner's next checkpoint is after merge-library step 3. Assertion migrations are in ASSERTIONS-test-framework.md;
requirements and per-step evidence are in REQUIREMENTS-test-framework.md and REPORT-test-framework.md.
