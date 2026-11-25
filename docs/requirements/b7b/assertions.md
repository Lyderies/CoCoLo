# Assertion replacements

## READY-WITH-CORRECTIONS round (base 37ebd229)

- `BudgetMeasurements.test_measured_budgets_report_numbers_without_relabelling_failures`:
  accepting any status in `{met,failed,unmeasured}` -> requiring `met` for B3,
  B4, B5 and S3 retirement/lookup, with independent numeric assertions (<50 ms
  batch/retirement, <5 ms lookup, <=18 batches, all seven paired VM counts within
  16 instructions). B1/B2/B6 must stay `unmeasured` with explicit reasons; S3
  service startup is separately `unmeasured`. The exact missing memory tracking
  set is asserted so its implementation forces revisiting the B1/B6 gaps.
  Socket binding is also required to remain unavailable (PermissionError);
  when it becomes available the test fails with instructions to replace B2/S3
  startup proxies with full service measurements.
  Previously the test passed the known B4 regression; now it fails on it.
- `BudgetMeasurements.test_successful_outcome_lookup_at_zero_and_100000_tombstones`:
  nonnegative timings/positive VM work -> those assertions **plus** <5 ms per
  successful authenticated lookup and paired VM counts within 16 instructions.
- `PlainTerminalOutputTests.test_plain_terminal_output_without_socket_privilege`:
  exact zero/stale shape and privacy -> those assertions **plus** the plain
  `recovery_pending_notice` sentence. Wire/report shape assertions are unchanged.
- New `test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report`:
  its first draft asserted no `result` key in a refusal -> `result is None`.
  The existing closed refusal envelope always requires that null field
  (`HistoryService._reply` / `decode_history_response`); only a successful
  report body is forbidden. The test still bombs if authorization or report
  computation is reached with a mismatched runtime, and requires
  `history_not_authorized` from the real decoder. This corrects a new test's
  mistaken envelope assertion before any implementation change to the gate.

No other existing assertion was removed or changed in this round. New tests add
the colon-free identity assertion, cleanup/publication finally checks, stale vs
fresh/unavailable CLI behavior, history-only serving, and simultaneous refusal
vs transport-failure classification.

## Original B7b assertions

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
