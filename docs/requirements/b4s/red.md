# B4s red run (base 8d4a7ce3, tests only)

Command: unittest of the three new modules (driver: A1, A3, two B4-4 checks, the authority test) in the worker's
environment allowlist. 37 tests: 4 failures, 27 errors, 6 passed (the three section 9 key-identity proofs, the B4-7
kind-mismatch and releasing-accepted rows, which existing rules already give).

```
ERROR: test_9_a_closure_the_account_keys_verifies_at_a_project_store_of_the_same_state
ERROR: test_B4_2_a_lease_never_bound_carries_the_outbox_on_its_lease_row
ERROR: test_B4_2_a_publishable_reservation_is_never_fenced_without_arbitration
ERROR: test_B4_2_open_marks_the_reservation_releasing_with_the_request_id_and_digest
ERROR: test_Q1_after_releasing_a_successor_resends_the_identical_request
ERROR: test_Q1_after_the_call_the_kept_closure_records_after_a_restart
ERROR: test_Q1_after_the_receipt_a_replay_is_known_and_writes_nothing
ERROR: test_Q2_a_closure_naming_another_request_digest_is_refused_and_writes_nothing
ERROR: test_B4_1_the_closure_past_the_size_limit_is_refused
ERROR: test_B4_3_its_register_writes_equal_the_planted_helpers
ERROR: test_V1_a_lost_unpublished_attempt_is_settled_in_one_transaction
ERROR: test_V2_a_released_committed_attempt_keeps_its_body_for_retention
ERROR: test_V4_a_lease_never_bound_expires_and_releases_lost_unbound
ERROR: test_V5_a_normally_released_attempt_is_never_settled
ERROR: test_V6a_a_closure_keyed_with_another_key_is_refused
ERROR: test_V6b_a_closure_for_another_claim_lease_or_attempt_is_refused
ERROR: test_B4_5_a_verified_settlement_releases_the_attempt_for_the_purge_only
ERROR: test_B4_6_after_the_purge_an_identical_retry_gets_the_tombstones_receipt
ERROR: test_B4_6_another_project_cannot_present_the_closure
ERROR: test_B4_1_a_closure_for_another_project_handle_claim_or_request_is_refused
ERROR: test_B4_1_a_matching_closure_decodes
ERROR: test_B4_1_settle_lost_runs_reply_shape_is_unchanged
ERROR: test_B4_1_the_closure_is_bounded
ERROR: test_B4_1_the_operation_is_new_and_offered_to_the_project_role_only
ERROR: test_B4_1_the_request_encodes_and_a_malformed_payload_is_refused
ERROR: test_A1_a_run_lost_after_its_claim_is_settled_in_the_project_store
ERROR: test_B4_1_only_the_runs_own_project_role_gets_its_closure_once_settled
FAIL: test_B4_7_a_held_reservation_with_a_settlement_row_is_refused
FAIL: test_B4_7_a_lease_only_settlement_needs_its_lease_released_lost_unbound
FAIL: test_A3_the_orchestration_of_a_settled_run_closes_and_the_run_reads_lost
FAIL: test_B4_4_every_caller_reaches_the_account_only_through_the_one_settlement_sequence
Ran 37 tests in 33.619s
FAILED (failures=4, errors=27)
   1 AssertionError: 533418.079926583 not less than 533418.073312083 : service did not become ready
   1 AssertionError: Lists differ: ['_settle_entry'] != []
   2 AssertionError: True is not false
  17 AttributeError: 'ProjectHistory' object has no attribute 'open_account_settlement'
   4 ModuleNotFoundError: No module named 'codex_agents.lost_run_closure'
```

## Review r1 (Codex security review NOT-READY, e57f84a5): red before each fix (2026-11-24)
Store (tests.test_account_settlement.ReviewRoundOneStoreTests, 5 tests, all FAILED on e57f84a5 code):
- test_r1_H2_an_admitted_unbound_settlement_releases_the_open_binding_as_claimed: ('released','claimed') != ('held', None)
- test_r1_M3_a_settlement_row_that_does_not_verify_is_never_answered_settled: HistoryError not raised (answered settled)
- test_r1_M3_a_verified_closure_whose_hold_is_not_released_does_not_end_the_attempt: True is not false
- test_r1_M3_the_validator_refuses_a_settlement_naming_an_absent_hold: True is not false
- test_r1_M4_a_stored_closure_that_is_not_bytes_is_refused_before_any_coercion: 'history_incompatible' != 'history_conflict'
Driver/account (tests.test_account_settlement_driver, 9 tests, 130 s, all FAILED):
- ReviewRoundOneDriverTests.test_r1_H1_a_settlement_the_account_purged_before_the_project_recorded_it_still_finishes:
  timed out waiting for the settlement row (account answered claim_storage_invalid / unavailable after the purge)
- test_r1_H1_a_recorded_settlement_finishes_its_journal_from_the_local_receipt_without_the_account: journal never settled
- test_r1_M1_a_slot_released_as_crashed_has_no_released_unacknowledged_closure: AccountError not raised
- test_r1_M2_a_workspace_that_could_not_be_removed_keeps_the_journal_unsettled: workspace never removed (timed out)
- test_r1_M2_the_container_recovery_writes_no_journal_while_the_workspace_remains: 'settled' is not None
- test_r1_M2_the_live_container_path_settles_its_journal_only_after_the_workspace_is_gone: no _remove_workspace call
- test_r1_H3_an_old_settled_journal_with_an_outstanding_project_hold_is_recovered: timed out (no settlement row)
- test_r1_H3_a_claim_admitted_before_its_first_journal_write_is_settled_from_the_store: timed out; STAYS RED -> skipped
  with its cause (see STATUS.md)
- SettlementThroughTheDriverTests.test_A2 (new assertion): [('released','claimed')] != [('held', None)]
RC-3 (tests.test_result_lifetime, skip removed): timed out waiting for the abandoned binding (31 s).
Already red at e57f84a5, found in this round: tests.test_project_holds_schema test_SB_a_copy_of_an_18_store... and
test_X5_M3_a_settlement_row_is_written_and_read_back (history_incompatible: their planted settlement rows break B4-7).

## HIGH 3 process mode (decision 146), red on ee4ee867 code with the new tests
- test_r1_H3_a_claim_admitted_before_its_first_journal_write_is_settled_from_the_store (skip removed): timed out
  waiting for the settlement row (30 s).
- test_r1_H3_an_intent_whose_claim_never_reached_the_account_is_released_and_nothing_is_counted (new):
  [['intent']] != [[]] (no intent on disk when the claim is sent).
Both green after the intent (2 tests, 32 s red / ~33 s green).

## Confirming check fix (decision 154, one targeted round on cef20694): red before each fix (2026-11-25)
Store (tests.test_account_settlement.ConfirmFixStoreTests, 2 tests, both FAILED before the fix):
- test_c_M3_a_settlement_row_with_its_hold_absent_is_a_conflict_not_nothing_to_settle:
  'history_conflict' != 'history_result_not_reserved'
- test_c_M4_the_second_stored_size_guard_is_not_hidden_by_a_broad_handler: AssertionError not raised (swallowed)
Sweep (tests.test_account_settlement_driver.ConfirmFixSweepTests, 6 tests, all red):
- test_c_prune_removes_only_entries_recovery_inspected_in_the_same_pass: the uninspected aged entry was pruned
- test_c_settled_checked_drops_pruned_keys_and_is_capped: pruned key kept, set not capped
- test_c_a_long_failing_recovery_backs_off_at_the_cap_without_overflow: OverflowError: int too large to convert to float
- test_c_one_entry_failing_never_aborts_the_sweep_for_later_entries: OverflowError escaped _settle_lost
- test_c_M2_a_symlink_at_the_workspace_path_is_not_reported_removed: True is not false
- test_c_M2_a_wrong_owner_or_non_folder_entry_is_not_reported_removed: True is not false
HIGH 3 second stop (ReviewRoundOneDriverTests, with only the intent-fence request disabled; 66 s):
- test_c_H3_a_second_stop_after_the_lease_was_fenced_still_settles_the_intent_and_its_workspace: timed out (intent
  never settled by the third service)
- test_c_H3_a_second_stop_after_an_admitted_intent_was_settled_at_the_account_still_settles_it: timed out (same)
- test_c_H3_an_intent_whose_claimed_entry_exists_is_taken_over_by_that_entry: green before and after (coverage gap
  the check named; no code change needed)
All green after the fix.
