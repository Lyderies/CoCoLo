# S44a: changed or removed assertions

No existing assertion was changed or removed.

One draft of a new test was corrected before it first went green (it is new in this step): the window-with-no-data test
had expected `no_runs_in_window` for a window that ends before the first recorded run, which the requirement calls
`before_usage_recorded`; it now uses a window between two recorded runs and a separate test covers the earlier window.

## Correction after the primary's --affected run and Codex review r1

Root cause of the --affected failure: `learning-status` called a helper that opened the learning databases in the status
client process, which `test_cli_status_client_boundary` forbids; the guard raised and the CLI printed a traceback instead
of JSON (`JSONDecodeError` in `test_health_monitor` and the boundary test). The first fix narrowed S44A-2 to a pointer;
that was NOT accepted. Final shape: S44A-2 is as originally written (status says, per provider, whether child runs
reported usage). The counts come from the project's RUNNING SERVICE through a new read-only operation
`history.run_usage` (client `run_usage_client`, same pattern as `history.orchestration_counts`); when the service is not
running status shows `run_usage.state: "unavailable"`, `child_runs: null` and a plain reason, never zero. The status client
opens no database. `docs/requirements/s44a.md` and `tests/requirements/s44a.json` keep the original S44A-2 text.

Existing assertions changed (new service read operation; decision 132 / s2fix pattern), old -> new:
- `tests/test_b2_service_outcome.py` `AuthorityTests.OPERATIONS` (test_operation_set_unchanged): 27 history operations -> the same plus `'history.run_usage'`. Reason: learning-status reads run-usage coverage from the service; the reply-shape freeze requires a NEW operation for new reply data.
- `tests/test_holds_report_socket.py` `test_B7_1_2_3_6_learning_status_from_plain_terminal` (also run by `test_b7b_terminal`): requests seen `['history.holds_report']` -> `['history.holds_report', 'history.run_usage']`. Reason: learning-status now also asks the run-usage reading; nothing else in that test changed.

Tests of THIS step (new, never merged) reshaped during the fix, old -> new:
- `test_s44a_2_status_shows_recent_run_usage_per_provider` (helper read counts directly) -> `test_s44a_2_status_shows_per_provider_coverage_from_the_service` (real `cli.py learning-status` output, real history service) plus `..._says_unknown_with_a_reason_when_the_service_is_not_running`, `..._status_coverage_reads_only_the_last_days`.
- `..._status_run_usage_is_unknown_not_invented_when_the_project_is_not_known` -> the unavailable test above (unknown is null with a reason).
- `..._learning_status_carries_the_run_usage_and_stays_read_only`: kept, now asserts the status branch calls `run_usage_client` and never `usage_report(`.
- Added: partial measurement (MED 4), outcome-only run in the recorded span (MED 2), relative root symlink / `..` / `sub/..` tests (S44A-1; these passed on first run, they pin existing behaviour).

## Review r2c (two MED), no existing assertion changed

`history.run_usage` is bounded in time as well as rows: SQLite progress check, 1 s (below the client's 2 s), then a partial
reply (`truncated`, `partial_reason`, providers `unknown` with null counts, never zero); the service skips the per-run outcome
lookups (not needed for coverage). New tests: `..._the_service_reading_stops_at_its_time_bound_and_says_unknown_not_zero`
(red first: no time bound existed), `..._returns_at_most_its_row_cap_and_says_truncated`. The cap is registered in
`tests/lifetime_caps.json` (`project_usage_report.py::service_run_usage`) with that second test.
