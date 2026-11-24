# E2c: existing assertion changes

Build (d16a9448): none. New module: tests/test_project_start.py (20 tests).

## Review round 1 fix round (RESULT-codex.md r1, primary decision 241)

No assertion removed or weakened. Changed tests, old -> new:

1. `StartTests.test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client` (MED 2, tightened):
   old `wait_for(serving('never') and serving('stopped'), 30 s)`; new: each due project's `begin` is at or before
   ready + STARTER_CHECK_SECONDS + one tick + 1 s (the interval the collector runs with), then both serve within
   2 x START_TIMEOUT_SECONDS. The later assertions are unchanged.
2. `BudgetTests.test_E2c_3_a_collector_drains_1000_due_projects_within_60_seconds_and_starts_no_later` (HIGH 1 /
   MED 2, tightened): old patched `STARTER_CHECK_SECONDS` to 0.05 s; new runs the product's own intervals. All its
   assertions are unchanged (0 left within 60 s, start-up under settled + 200 ms, every hold under 750 ms).
3. `StrandedTests.test_E2c_10_a_killed_service_starts_and_a_restart_keeps_the_backoff` (HIGH 4, fixture only): the
   test kills every host after stopping the collector. Since probation is kept across a restart (HIGH 4), a host
   killed within its probation now rightly counts failed, which would back `killed` off for 300 s. The fixture now
   runs with `START_PROBATION_SECONDS: 1.0` and waits until `killed`'s start is settled and its probation served
   out before the stop. Every assertion is unchanged (the unknown project is started after the restart; `broken`'s
   failures, next start and streak are kept; its line). Its failure message also prints the start and wake rows.
4. `BudgetTests.bulk_projects`: now calls the module helper `bulk_projects` (same rows; the helper can also write
   launch records). No assertion involved.

Not an assertion change, for the primary: `tests.test_runtime_release.test_current_runtime_is_recorded_for_the_current_version`
fails on this branch by design. Runtime files changed (new `project_start.py`, learning_store, collector_client,
collector_contracts, client_runtime, cli, runtime_files), so the computed runtime digest is
`0eb7787ad120755ae5decf712f25a69c52878f3185989a261b85f9a1db34cdd0`. As briefed, `runtime-releases.json` keeps
`"0.8.59": "dcc18633d92e87eeabf2d52e3e8e0058a03e39621ccf8ff95b2d1e811846bfe9"` (E2a's in-branch refresh to 848d3e3a... was
put back) and no 0.8.60 key or VERSION bump was added: in this repo the version bump is the primary's release
bookkeeping commit (VERSION, runtime-releases.json, claude-agents previous-releases.json, catalogue test, CHANGELOG).
