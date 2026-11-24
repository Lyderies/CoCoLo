# S3 stopped: retirement selection visits tombstones

Product source remains the supplied base `8d4a7ce3`; no git command was invoked.
Requirements preceded the tests, and the six new tests ran against unchanged
product code. Four pass and two fail. There is no red-to-green implementation:
the owner's schema-migration stop condition applies.

The retirement selector in `ProjectHistory.expire_results` uses
`result_expiry(project_id,expires_at_ms)`, a non-partial index. EXPLAIN describes a
`SEARCH result_reservations USING INDEX result_expiry (project_id=?)`, followed
by a temporary sort. This is not a literal table `SCAN` node: it scans the whole
project's history through an index, including every expired tombstone. The work
counter establishes the history dependence independently of plan terminology.

| Production selection statement | VM steps: 0 tombstones | VM steps: 100,000 tombstones |
|---|---:|---:|
| Retirement, cursor page | 296 | 700,296 |
| Retirement, first page | 275 | 500,275 |

Both statements return the same ten live candidates. Each store has identical
live identities, with the historical rows inserted before them. The context
lookup and both context-sweep cursor shapes pass equal-work and partial-plan
checks. Full captured SQL, plans, counts and test outcomes are in `red.json`.

## Schema boundary

`project_holds_schema.py` defines the shared tables and their indexes for the
17 -> 18 step. It defines live-context and outcome partial indexes but no partial
index for result-reservation retirement. `project_history.py` fixes historical
schemas through version 20 and compares the complete schema at open. Its
authorized `_upgrade` path has fixed steps through 19 -> 20, with no existing
step for the required retirement index. Editing `_RESULT_SCHEMA` or the shared
historical schema in place would change the expected layout of old stores;
adding an index at ordinary startup would also fail schema identity checks.

A correction therefore needs a new, separately scoped schema migration, with
backup/rollback and old-layout compatibility coverage. No schema, runtime or
release bookkeeping was changed. Per the owner's instruction to stop when the
needed schema upgrade is absent, P4's measurement script is deferred, not claimed
complete. P4 remains a report without a numeric gate (catalogued with no checks).

## Validation

Registered `root:test_rsd_budget` as integration/component in test-stages.json and
test-layers.json; updated test-requirements.json through
`python -B scripts/generate_test_metadata.py --catalogue` using the prepared test
interpreter. The final focused runner reports no metadata errors or budget
failures, six tests: four pass, two fail, 1.674 seconds in unittest / 1.887 seconds
for the worker. Discovery took 2.355 seconds; preflight took 6.735 seconds.

Command: `/private/tmp/cocolo-testfw-venv/bin/python -B scripts/parallel_tests.py
--module root:test_rsd_budget --save-timings /private/tmp/cocolo-rsd-s3-red-final.json`.
The worker log is `/private/tmp/pt7rxu48ng/1.log`. No full suite ran. A first local
test-harness attempt captured an unrelated purge statement; tightening the trace
filter corrected that fixture before the recorded product-failure run. The
initial runner also exposed a requirement-source formatting mismatch, corrected
before the final run. No existing test assertions changed; see assertions.md.
