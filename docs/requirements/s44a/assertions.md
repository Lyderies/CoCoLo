# S44a: changed or removed assertions

No existing assertion was changed or removed.

One draft of a new test was corrected before it first went green (it is new in this step): the window-with-no-data test
had expected `no_runs_in_window` for a window that ends before the first recorded run, which the requirement calls
`before_usage_recorded`; it now uses a window between two recorded runs and a separate test covers the earlier window.

## Correction after the primary's --affected run (test_health_monitor, test_cli_status_client_boundary failed)

Root cause: `learning-status` called `status_run_usage(state, root)`, which opened the learning databases in the status
client process. The client boundary (`test_cli_status_client_boundary`: "status client never opens a database") forbids that;
the guard raised, the CLI printed a traceback instead of JSON, so `test_health_monitor` and the boundary tests got
`JSONDecodeError`. Fix: `status_run_usage()` opens nothing and returns plain data naming `learning-usage-report`; the per-provider
counts stay in the report (`usage_coverage`). No existing assertion changed. Tests of THIS step (new, never merged), old -> new:
- `test_s44a_2_status_shows_recent_run_usage_per_provider` (status read counts) -> `test_s44a_2_status_says_where_run_usage_is_without_opening_any_database` (requirement narrowed: status names the report).
- `test_s44a_2_status_run_usage_is_unknown_not_invented_when_the_project_is_not_known` removed: status no longer reads, so there is nothing to be unknown; replaced by `test_s44a_2_every_json_output_here_is_strict_json_with_the_new_fields`.
