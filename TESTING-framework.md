# Test framework after test reset A

Decisions 125 and 126 retire homemade fingerprint and approval governance. Preflight
runs guarded discovery, validates stage/layer declarations, declared requirement tags,
source/catalogue links, golden-helper usage in fully tagged modules, and entrypoints.
It does not hash test implementations, fixtures, helpers or values, replay approvals or
release bookkeeping, inspect historical budget debt, or require a trusted base.
`--base` remains accepted because existing CI jobs pass it.

Use the prepared test interpreter and the ordinary runner:

```
python -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module root:test_test_framework
python -B scripts/parallel_tests.py --save-timings /tmp/cocolo-tests.json
```

Use at most ten selected modules per focused run. The primary runs one quiet full run
at the default job count, with no competing test or review load. Record the printed
`preflight_seconds` (acceptance: below 60 seconds); C13's known load flake is the only
accepted full-run failure. Record socket tests blocked by the local sandbox separately.

## Which tests to run

While working, run `python -B scripts/parallel_tests.py --affected BASE`, with BASE the
commit before your changes. It selects changed test modules and modules that executed
changed files, including dependencies reached through fixtures and child Python processes.
It prints each selection reason. Missing or invalid coverage data, a map commit outside
HEAD's ancestry, an unseen changed Python file, or changed non-Python files under
`scripts/` or `packages/` trigger a full run. Staged, unstaged and untracked changes count,
as do changes since the map's commit so an older map cannot hide dependencies.

Run the full suite before every merge/install: `python -B scripts/parallel_tests.py`.
Refresh the ignored `.cocolo-test-cache/coverage-map.json` with a full
`python -B scripts/parallel_tests.py --record-coverage` run. This uses the pinned
coverage.py subprocess patch and reports its wall time, including preflight. The map
contains HEAD and each test module's executed repository-relative files. Ordinary and
affected runs do not enable coverage. Recording preserves each worker's short TMPDIR.

The runner retains unittest, per-worker temporary directories and stripped environments,
module/stage selection, five resource layers, process/socket/sleep guards, hard timeouts,
Hypothesis profiles, golden provenance, exclusive crash-campaign scheduling, complete
module budgets, and disjoint complete shard/combine checks. Current non-ceiling budget
debt files retain existing measured limits; changes receive ordinary review. No tests
or helpers need a legacy-baseline migration to be edited.

Requirements without a check have coverage state `unchecked`; run records contain
`unchecked_requirement_count` and `unchecked_requirement_ids`, and the last output line
lists both. They do not fail the run. Tagged tests still validate IDs, source statements
and reciprocal links. Executed failed coverage and worker, metadata and budget failures
remain failures. Coverage is a report of what ran; an unselected check is not passing.
`test_e2e_requirements` validates the lifecycle catalogue schema and any declared links;
an empty check list is allowed. Stage-combine validation still detects missing or
incompatible execution records and linked checks that did not pass.

`.github/CODEOWNERS` assigns owner review for root/package tests, runner/framework/worker
sources, assertion-diff and `.github/`. The new `assertion-diff` CI job reports added,
removed and changed assert expressions and deleted test methods between commits using
only the Python standard library. It never fails the build. Existing CI job and step
names remain intact. Locally: `python -B scripts/assertion_diff.py BASE HEAD`.
The report covers Python files under tests directories, including shared test helpers;
changes are paired within the same lexical scope, and syntax errors appear in the report.

Run records go to a temporary directory by default, an explicit `COCOLO_TEST_RUN_RECORD`,
or beside `--save-timings`. CI retains them as artifacts. `measurements/` is ignored
and removed from the tracked tree. The primary must tag the last commit containing it
as `measurements-0.8.52` (the starting HEAD is `36b7ef5f`) before committing this removal.
See REMOVED-CHECKS-test-reset.md for the complete removal/replacement ledger.
