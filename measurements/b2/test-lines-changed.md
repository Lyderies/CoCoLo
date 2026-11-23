# Existing tests that fail because project-history schema is now 20 (owner list; no existing test line was edited)
Root cause 1 (most failures): the legacy-layout helper `legacy_project_holds_layout` in
packages/codex-agents/tests/project_schema_fixtures.py does not drop the new table `project_result_recorders`, so every
fixture that builds an older store from a current one ends with an extra table and is refused (history_incompatible).
One added line there (`DROP TABLE IF EXISTS project_result_recorders`, as decision 108 did for the 19 index) clears all
of those; verified by a throwaway edit (reverted), after which only root cause 2 remains.
Root cause 2: literal pins of format 19 (`assertEqual(19, ...user_version`) and of the copy name `before-19`.

Failing (module:line of the failing statement, by test):
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_1_an_ordinary_open_still_refuses_a_format_16_store test line 231 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_1_the_service_upgrades_a_format_16_store_whether_or_not_learning_moved test line 54 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_10_an_existing_copy_is_never_reused test line 123 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_16_a_copy_never_replaces_an_existing_one test line 170 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_18_a_copy_made_for_an_upgrade_that_fails_is_removed test line 218 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_2_the_store_is_kept_as_it_was_before_the_upgrade test line 62 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_5_a_busy_store_is_refused_before_any_copy test line 98 (raised at 34)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_5_the_offline_upgrade_keeps_the_copy_too test line 79 (raised at 79)
- test_history_service_upgrade.HistoryServiceUpgradeTests.test_HS_6_a_wrong_result_key_leaves_the_old_store_untouched test line 110 (raised at 34)
- test_project_holds_schema.SchemaStepTests.test_D111_a_failing_validator_in_the_18_to_19_step_leaves_18_and_removes_its_copy test line 238 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_M2_a_failed_backup_refuses_the_step test line 293 (raised at 293)
- test_project_holds_schema.SchemaStepTests.test_M2_a_repeated_step_is_a_no_op test line 266 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_M2_an_interrupted_step_rolls_back_to_17_and_a_later_attempt_upgrades test line 278 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_M2_an_old_writer_still_running_excludes_the_step test line 312 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_M2_the_copy_keeps_the_17_store_as_it_was test line 317 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_X5_M2_a_17_binary_refuses_an_18_store_and_changes_nothing test line 258 (raised at 258)
- test_project_holds_schema.SchemaStepTests.test_X5_M3_a_settlement_row_is_written_and_read_back test line 171 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_X5_M3_one_step_creates_the_four_tables_adopts_nothing_and_both_validators_pass test line 138 (raised at 121)
- test_project_holds_schema.SchemaStepTests.test_X5_M3_the_validators_run_inside_the_step_before_it_commits test line 194 (raised at 121)
- test_project_orchestration.ProjectOrchestrationTests.test_R6_a_populated_schema_16_store_gains_the_table_only_by_explicit_upgrade test line 211 (raised at 211)
- test_project_results.ResultSchemaMigrationTests.test_actual_process_death_during_schema_upgrade_rolls_back_and_can_restart test line 806 (raised at 806)
- test_project_results.ResultSchemaMigrationTests.test_exact_schema1_migration_preserves_every_existing_byte_and_sequence test line 733 (raised at 733)
- test_record_context.LegacyAdoptionTests.test_M1_a_legacy_result_is_refused_plainly_and_keeps_its_body_to_its_own_deadline test line 567 (raised at 121)
- test_record_context.LegacyAdoptionTests.test_M1_a_legacy_row_retiring_at_the_update_is_released_when_its_retirement_finishes test line 567 (raised at 121)
- test_record_context.LegacyAdoptionTests.test_M1_each_adoption_class_is_adopted_as_tabled_and_nothing_is_invented test line 567 (raised at 121)
- test_result_leases.ResultLeaseTests.test_process_death_during_schema2_upgrade_rolls_back_all_lease_ddl test line 485 (raised at 485)
- test_result_leases.ResultLeaseTests.test_schema2_normal_open_refuses_without_writes_and_explicit_upgrade_preserves_existing_bytes test line 413 (raised at 48)
- test_result_leases.ResultLeaseTests.test_schema4_upgrade_preserves_all_rows_body_windows_and_does_not_invent_orchestrations test line 456 (raised at 48)

Pins found by grep in modules NOT run (the run was limited to the eight named modules); expected to fail the same way:
- test_project_hold_register.py:357, test_project_host_open.py:308, test_project_qa_commit.py:294,
  test_learning_chunk_upgrade.py:94 (each `assertEqual(19, ...user_version`), test_open_withdrawal.py:478 and :495
  (`_SCHEMA_VERSION` and `user_version` == 19), and any module using the legacy layouts or `before-19`.
Not a schema pin, none found: the admission changes (final predicate, K1/K2/K3, recorder insert) broke no existing test in
the eight modules once the schema pins are set aside (test_result_persistence, test_record_context's other tests,
test_project_orchestration's other tests, test_result_leases and test_project_results's other tests all pass).
