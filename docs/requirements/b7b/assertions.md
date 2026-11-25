# Assertion replacements

- `tests.test_holds_report.HoldsReportTests.test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields`:
  exact top-level set `{project_holds, result_custody, result_retirement_repairs}` ->
  the same set plus `recovery_pending`. Required by RS-B-B7-4/-6; the new recovery
  module separately asserts the complete nested zero/stale shape. All previous
  hold/custody/repair assertions remain unchanged.
- `HoldsClientStatusTests.test_B7_6_learning_status_prints_sections_and_nulls_when_unavailable`:
  its synthetic report fixture gains `recovery_pending`; the existing loop now also
  asserts presentation/null behavior for that section. No previous assertion is removed.

- B7b's new `test_sweep_counts_real_blockers_excludes_active_and_clears_on_success`:
  mixed reasons `{needs_settlement:4, needs_process_evidence:2, stop_failed:1,
  account_unreachable:1}` -> `{needs_settlement:1, needs_process_evidence:1,
  stop_failed:1, account_unreachable:1}`; own stop `{needs_settlement:1,
  needs_process_evidence:1, stop_failed:1}` -> `{needs_settlement:0,
  needs_process_evidence:0, stop_failed:1}` (has_holds/account zero stay zero).
  Reason: RS-B draft 3 §10 states a row has exactly one latest reason. The initial
  B7b acceptance mistakenly permitted overlapping reasons; the source requires replacement.
- B7b's new `test_own_failed_run_account_backoff_is_counted_without_refreshing_sweep_age`:
  its two needs_settlement assertions `1` -> `0` while account_unreachable is `1`.
  Same source correction; no account-failure, privacy, staleness or dedup assertion removed.
