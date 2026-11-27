# Exact assertion audit against ce81bd96

Companion to [assertions.md](assertions.md). Every removed or changed assertion is paired below with the added replacement assertions in its function. Unchanged assertions remain in place. Repeated occurrences are counted separately; a combined assertion may replace several originals. See assertions.md for stimulus and fixture changes.

## packages/codex-agents/tests/test_account_billing_admission.py :: test_standalone_backup_preserves_genuine_sealed_admission_and_rejects_missing_request

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, body['ledger_schema_version'])` | `self.assertEqual(18, body['ledger_schema_version'])` |

## packages/codex-agents/tests/test_account_cleanup_release.py :: test_registered_backup_preserves_original_release_journal_and_standalone_restore

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, body['ledger_schema_version'])` | `self.assertEqual(18, body['ledger_schema_version'])` |

## packages/codex-agents/tests/test_account_cleanup_schema.py :: test_verified_upgrade_preserves_old_manifest_and_invents_no_cleanup

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.base.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.base.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_observation_backup.py :: test_both_verifiers_preserve_committed_and_unknown_original_requests

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.body['ledger_schema_version'])` | `self.assertEqual(18, self.body['ledger_schema_version'])` |

## packages/codex-agents/tests/test_account_observation_schema.py :: test_default_refuses_without_writes_and_explicit_upgrade_keeps_all_original_cells

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, verified['ledger_schema_version'])` |
| `self.assertEqual(17, verified['ledger_schema_version'])` | `self.assertEqual(18, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, verified['ledger_schema_version'])` |

## packages/codex-agents/tests/test_account_observation_schema.py :: test_or3_upgrade_backup_verify_keep_a_legacy_inconsistent_axes_outcome_byte_identical

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, verified['ledger_schema_version'])` |
| `self.assertEqual(17, verified['ledger_schema_version'])` | `self.assertEqual(18, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, verified['ledger_schema_version'])` |

## packages/codex-agents/tests/test_account_observation_schema.py :: test_interrupted_upgrade_rolls_back_both_tables_and_releases_original_writer

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_observation_schema.py :: test_schema_twelve_upgrade_preserves_original_cells_and_installs_empty_probe_state

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_observation_schema.py :: test_schema_twelve_interruption_rolls_back_probe_objects_and_version

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_open_schema.py :: test_fresh_layout_has_explicit_origin_and_independent_original_journals

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, db.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_open_schema.py :: test_explicit_old_upgrade_preserves_original_partition_and_never_invents_opens

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_open_schema.py :: test_all_additive_ddl_and_version_roll_back_after_injected_interruption

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_account_open_schema.py :: test_latest_backup_preserves_new_manifests_and_original_origin

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, checked['ledger_schema_version'])` | `self.assertEqual(18, checked['ledger_schema_version'])` |

## packages/codex-agents/tests/test_active_run_capacity.py :: test_schema_eight_backfills_unknown_capacity_and_preserves_signed_bytes

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b2_service_outcome.py :: test_schema_is_20_and_the_table_is_exactly_as_designed

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, project_history._SCHEMA_VERSION)` | `self.assertEqual(23, project_history._SCHEMA_VERSION)` |

## packages/codex-agents/tests/test_b2_service_outcome.py :: test_a_format_19_project_store_upgrades_to_20_and_keeps_its_rows

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-23.*'))))` |
| `self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-22.*'))))` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-23.*'))))` |

## packages/codex-agents/tests/test_b2_service_outcome.py :: test_a_failed_validator_rolls_the_step_back_and_removes_its_copy

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-22.*')))` | `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-23.*')))`<br>`self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-23.*')))`<br>`self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_1_end_earlier_releases_the_row_taken_at_open_closed_and_the_unattended_close_interrupted_unattended

Replacement function: `test_B4t_keeps_open_rows_and_unattended_close_still_uses_its_own_rule`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(2, self.owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))` | `self.assertEqual(0, self.owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual(('held', None, 1000, None), (state, kind, taken, evidence))` |
| `self.assertEqual(('released', 'closed', 1000, {'ended_by_update_unrecorded': False}), (state, kind, taken, json.loads(evidence)))` | `self.assertEqual(0, self.owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual(('held', None, 1000, None), (state, kind, taken, evidence))` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-23.'))`<br>`self.assertEqual([(1, PACKAGE_VERSION)], after.pop('runtime_floor'))`<br>`self.assertEqual([], after.pop(name))` |
| `self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-22.'))` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-23.'))`<br>`self.assertEqual([(1, PACKAGE_VERSION)], after.pop('runtime_floor'))`<br>`self.assertEqual([], after.pop(name))` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_a_failure_inside_the_step_leaves_20_and_removes_its_copy_then_a_later_attempt_upgrades

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_process_death_inside_the_step_leaves_20_and_a_later_attempt_upgrades

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_an_unknown_object_in_a_step_20_store_is_refused_before_any_write

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_a_stale_marker_after_inode_reuse_leaves_a_copy_it_did_not_make_alone

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_a_dead_attempts_copy_with_another_hard_link_is_left_alone

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_b6_unattended_close.py :: test_B6_7_a_crash_before_the_marker_leaves_no_partial_after_the_next_attempt

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_e2a_launch_record.py :: test_E2a_1_additive_columns_and_wake_table_keep_the_schema_version

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, reopened._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `self.assertEqual(17, reopened._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, connection.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(18, reopened._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_history_service_upgrade.py :: test_HS_1_the_service_upgrades_a_format_16_store_whether_or_not_learning_moved

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_holds_report.py :: test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release

Replacement function: `test_B4t_runtime_change_creates_no_legacy_update_close_count`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, owner.end_earlier(replace(fixture.scope, runtime_digest='b' * 64), 10))` | `self.assertEqual(0, owner.end_earlier(replace(fixture.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual(0, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])` |
| `self.assertEqual(1, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])` | `self.assertEqual(0, owner.end_earlier(replace(fixture.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual(0, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])` |

## packages/codex-agents/tests/test_launch_receipts_layout.py :: test_receipts_ddl_changes_only_with_a_schema_bump

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, LEDGER_SCHEMA_VERSION, 'the ledger schema changed: add the new layout hash and its upgrade, then update this test')` | `self.assertEqual(18, LEDGER_SCHEMA_VERSION, 'the ledger schema changed: add the new layout hash and its upgrade, then update this test')`<br>`self.assertEqual(FROZEN_FOR_SCHEMA_18, stored_layout())` |

## packages/codex-agents/tests/test_learning_chunk_upgrade.py :: test_interrupted_upgrade_rolls_back_then_exact_replay_backfills_only_membership

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_migration.py :: test_schema15_store_gains_the_cutover_table_only_by_explicit_upgrade

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_outcome_pages.py :: test_exact_v11_upgrade_requires_drained_owner_and_rolls_back_failed_index_creation

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_qa_upgrade.py :: test_explicit_upgrade_rolls_back_failure_then_preserves_every_original_row

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_PC4_schema_15_upgrade_preserves_legacy_bytes_and_null_probe_id

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_or3_installer_path_schema15_ledger_with_legacy_stored_records_upgrades_unchanged

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_goal_statistics_and_original_open_use_schema_ten

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, version)` | `self.assertEqual(18, version)` |

## packages/codex-agents/tests/test_learning_store.py :: test_additive_hook_ingestion_migration_backfills_existing_events

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_billing_probe_backup_expiry_and_restart_preserve_all_sealed_cells

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, report['ledger_schema_version'])` | `self.assertEqual(18, report['ledger_schema_version'])` |

## packages/codex-agents/tests/test_learning_store.py :: test_schema_five_migration_uses_exact_backup_and_seeds_history

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_schema_five_launch_migration_interruption_rolls_back_the_whole_upgrade

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_learning_store.py :: test_collector_rejects_forged_binding_and_unavailability_is_bounded

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, health['schema_version'])` | `self.assertEqual(18, health['schema_version'])` |

## packages/codex-agents/tests/test_open_withdrawal.py :: test_D108_a_format_18_project_store_upgrades_to_19_and_keeps_its_rows_and_its_refusals

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, project_history._SCHEMA_VERSION)` | `self.assertEqual(23, project_history._SCHEMA_VERSION)`<br>`self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])` |
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, project_history._SCHEMA_VERSION)`<br>`self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_execution_grant.py :: test_ordinary_schema3_open_refuses_and_explicit_upgrade_preserves_original_bindings

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.fixture.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.fixture.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_hold_register.py :: test_the_step_adopts_every_lease_and_reservation_by_class

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_X5_M3_one_step_creates_the_four_tables_adopts_nothing_and_both_validators_pass

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(before, {name: rows for name, rows in after.items() if name not in (*SCHEMA_18_TABLES, 'runtime_takeover', 'runtime_floor', 'takeover_record', 'takeover_runs', 'takeover_obligations')})` |
| `self.assertEqual(before, {name: rows for name, rows in after.items() if name not in (*SCHEMA_18_TABLES, 'runtime_takeover')})` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])`<br>`self.assertEqual(before, {name: rows for name, rows in after.items() if name not in (*SCHEMA_18_TABLES, 'runtime_takeover', 'runtime_floor', 'takeover_record', 'takeover_runs', 'takeover_obligations')})` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_D111_a_failing_validator_in_the_18_to_19_step_leaves_18_and_removes_its_copy

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.version())` | `self.assertEqual(23, self.version())` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_X5_M2_a_17_binary_refuses_an_18_store_and_changes_nothing

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.version())` | `self.assertEqual(23, self.version())` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_M2_an_interrupted_step_rolls_back_to_17_and_a_later_attempt_upgrades

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.version())` | `self.assertEqual(23, self.version())` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_M2_an_old_writer_still_running_excludes_the_step

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.version())` | `self.assertEqual(23, self.version())` |

## packages/codex-agents/tests/test_project_holds_schema.py :: test_M2_the_copy_keeps_the_17_store_as_it_was

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertTrue(copy.name.startswith('history.sqlite3.before-22.'))` | `self.assertTrue(copy.name.startswith('history.sqlite3.before-23.'))` |

## packages/codex-agents/tests/test_project_host_open.py :: test_explicit_schema5_upgrade_preserves_old_rows_and_creates_no_open_facts

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_learning_corpus.py :: test_v10_upgrade_is_explicit_preserves_existing_bytes_and_rolls_back_interruption

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_orchestration.py :: test_R6_a_populated_schema_16_store_gains_the_table_only_by_explicit_upgrade

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_orchestration.py :: test_034_R10_an_update_ends_the_earlier_runtimes_open_orchestrations

Replacement function: `test_B4t_an_update_keeps_earlier_open_and_paused_orchestrations`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(2, owner.end_earlier(updated, 10))` | `self.assertEqual(2, owner._open_count())`<br>`self.assertEqual(0, owner.end_earlier(updated, 10))`<br>`self.assertEqual(expected, ended['status'])`<br>`self.assertIsNone(ended['ended_at_ms'])` |
| `self.assertEqual(0, owner._open_count())` | `self.assertEqual(2, owner._open_count())`<br>`self.assertEqual(0, owner.end_earlier(updated, 10))`<br>`self.assertEqual(expected, ended['status'])`<br>`self.assertIsNone(ended['ended_at_ms'])` |
| `self.assertEqual('interrupted', ended['status'])` | `self.assertEqual(2, owner._open_count())`<br>`self.assertEqual(0, owner.end_earlier(updated, 10))`<br>`self.assertEqual(expected, ended['status'])`<br>`self.assertIsNone(ended['ended_at_ms'])` |
| `self.assertIsNotNone(ended['ended_at_ms'])` | `self.assertEqual(2, owner._open_count())`<br>`self.assertEqual(0, owner.end_earlier(updated, 10))`<br>`self.assertEqual(expected, ended['status'])`<br>`self.assertIsNone(ended['ended_at_ms'])` |

## packages/codex-agents/tests/test_project_orchestration.py :: test_034_R10_a_used_request_id_never_stops_the_end

Replacement function: `test_B4t_a_used_legacy_close_id_does_not_trigger_an_update_close`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))` | `self.assertEqual(0, owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual('active', owner._current(self.scope, handle)['snapshot']['status'])` |
| `self.assertEqual('interrupted', owner._current(self.scope, handle)['snapshot']['status'])` | `self.assertEqual(0, owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))`<br>`self.assertEqual('active', owner._current(self.scope, handle)['snapshot']['status'])` |

## packages/codex-agents/tests/test_project_orchestration.py :: test_034_R10_each_pass_tries_at_most_its_limit

Replacement function: `test_B4t_runtime_change_does_not_attempt_the_retired_close`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(3, len(tries))` | `self.assertEqual(0, len(tries))`<br>`self.assertEqual(0, len(set(tries)))` |
| `self.assertEqual(3, len(set(tries)))` | `self.assertEqual(0, len(tries))`<br>`self.assertEqual(0, len(set(tries)))` |

## packages/codex-agents/tests/test_project_qa_check_receipts.py :: test_explicit_schema6_upgrade_keeps_all_original_rows_and_invents_no_receipts

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_qa_commit.py :: test_explicit_schema7_upgrade_preserves_original_checks_and_invents_no_qa

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_release_ack.py :: test_old_schema_refuses_without_writes_and_explicit_upgrade_keeps_original_rows

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_results.py :: test_exact_schema1_migration_preserves_every_existing_byte_and_sequence

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_project_results.py :: test_actual_process_death_during_schema_upgrade_rolls_back_and_can_restart

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_result_leases.py :: test_schema2_normal_open_refuses_without_writes_and_explicit_upgrade_preserves_existing_bytes

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_result_leases.py :: test_schema4_upgrade_preserves_all_rows_body_windows_and_does_not_invent_orchestrations

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.store._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_rsd_runtime_close.py :: test_N1_recorded_closes_and_releases_custody_without_counting_unrecorded

Replacement function: `test_N1_recorded_custody_stays_held_after_runtime_change`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, owner.end_earlier(self.changed(), 10))` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |
| `self.assertEqual('interrupted', owner.metadata_for(self.binding)['snapshot']['status'])` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |
| `self.assertEqual(1, self.store.expire_results())` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |
| `self.assertEqual(('released', 'orchestration_closed'), rc.custody(self.store, *self.attempt)[:2])` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |
| `self.assertEqual([], rc.context_files(self.directory))` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |
| `self.assertEqual((0, 0, 0), self.store._result_counts())` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(0, self.store.expire_results())`<br>`self.assertEqual(('held', None), rc.custody(self.store, *self.attempt)[:2])`<br>`self.assertEqual(before, (self.store._result_counts(), rc.context_files(self.directory)))` |

## packages/codex-agents/tests/test_rsd_runtime_close.py :: test_N1_N2_real_release_then_update_counts_unrecorded_once_across_restart

Replacement function: `test_N1_N2_real_release_then_update_keeps_unrecorded_custody_until_retention`. Reason: D2.5 / D2.11: custody retained; ordinary close and retention remain.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, owner.end_earlier(self.changed(), 10))` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |
| `self.assertEqual('interrupted', owner.metadata_for(self.binding)['snapshot']['status'])` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |
| `self.assertEqual(('released', 'orchestration_closed'), rc.custody(store, *self.attempt)[:2])` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |
| `self.assertEqual({'ended_by_update_unrecorded': 1}, store.runtime_close_status())` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |
| `self.assertEqual({'ended_by_update_unrecorded': 1}, self.status())` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |
| `self.assertEqual({'ended_by_update_unrecorded': 1}, self.status())` | `self.assertEqual(0, owner.end_earlier(self.changed(), 10))`<br>`self.assertEqual('active', owner.metadata_for(self.binding)['snapshot']['status'])`<br>`self.assertEqual(('held', None), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, store.runtime_close_status())`<br>`self.assertEqual(0, store.expire_results())`<br>`self.assertEqual(('released', 'retention'), rc.custody(store, *self.attempt)[:2])`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual({'ended_by_update_unrecorded': 0}, self.status())`<br>`self.assertEqual(0, self.store._purge_finished_results(now, 10))` |

## packages/codex-agents/tests/test_rsf_s2_fence.py :: test_epoch_is_inside_the_schema_step_before_each_legacy_adoption_and_rolls_back_with_it

Reason: D2.8: hand-over wording and the shared B4m command.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, len(seen))` | `self.assertEqual(1 if cut else 2, len(seen))`<br>`self.assertTrue(all((row == seen[0] for row in seen)))`<br>`self.assertEqual(prior if cut else 18 if kind == 'ledger' else 23, connection.execute('PRAGMA user_version').fetchone()[0])` |
| `self.assertEqual(prior if cut else 17 if kind == 'ledger' else 22, connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(1 if cut else 2, len(seen))`<br>`self.assertTrue(all((row == seen[0] for row in seen)))`<br>`self.assertEqual(prior if cut else 18 if kind == 'ledger' else 23, connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_rsf_s2_regressions.py :: test_schema16_registration_gate_waits_for_writer_then_upgrades_without_record_consent

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_rsf_s2_regressions.py :: test_schema16_explicit_upgrade_does_not_request_project_backups

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

## packages/codex-agents/tests/test_rsf_s3_resumable.py :: interrupted_equivalence

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-23.*'))))` |

## packages/codex-agents/tests/test_rsf_s3_resumable.py :: final_boundary_equivalence

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-23.*'))))` |

## packages/codex-agents/tests/test_rsf_s3_resumable.py :: test_migration_context_is_reset_after_adoption_and_validation_exceptions

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-22.*.attempt'))))` | `self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-23.*.attempt'))))` |

## scripts/e2e_checks_update_safety.py :: l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on

Replacement function: `l12_3_update_with_a_running_child_goes_ahead_and_the_child_ends_lost`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, "the project's service reports the child really running before the update")` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(update.steps == ['setup_update'], 'the update was one command')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(not update.run.timed_out and update.run.code != 0, 'the update is refused (it exits non-zero)')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(child_refusal_is_plain(update.run.text), 'the refusal says a child run is active, that nothing was changed and to try again when none is running')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(not any((state == 'OK' for number, state in states.items() if 3 <= number <= 8)), 'the refusal comes before the install step: steps 3 to 8 (providers to commands) ran none of their work')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect('The update stopped (child_run_active)' in update.run.text and states.get(2) != 'OK', 'the refusal is reported as the update stopping at step 2 (Project) with the reason child_run_active')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(update.before.pids == update.after.pids and all(update.alive_after.values()) and update.alive_after and (update.before.service_pids == update.after.service_pids), "the earlier collector and the project's service are the same live processes right after the refusal")` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(runs_from(update.after.collector, scene.clone) and runs_from(update.after.service, scene.clone), 'they still run from the earlier build')` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(len(scene.child_before) == 1 and scene.child_after == [(scene.child_before[0], True)], "the child's own process is the same one and still running right after the refusal")` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(scene.dispatch_code == 0, "the child's dispatch finishes normally once the child ends")` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |
| `phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), "the child's slot is released in the ledger once it ends")` | `phase.expect((scene.running.get('liveness') or {}).get('last_activity_category') == 'startup' and scene.running.get('stop_requested') is False, 'the child really runs before the update')`<br>`phase.expect(update.steps == ['setup_update'], 'the busy update is one command')`<br>`phase.expect(not update.run.timed_out and update.run.code == 0 and ('FAIL' not in states_of(update).values()), 'the busy update goes ahead without an idle wait or manual stop')`<br>`phase.expect(replaced(update.before.pids, update.after.pids, recovery.alive) and update.after.ready is True and (update.after.version == current_version()), 'the old collector is gone and the successor is healthy')`<br>`phase.expect(scene.child_before and all((not alive for _, alive in scene.child_after)), 'the updated project service ends its old child')`<br>`phase.expect(scene.lost_status.get('reason_code') == 'run_lost' and scene.terminal == 'run_lost', 'the new service and the stored binding independently say the old run is lost')`<br>`phase.expect(scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'B4s confirms the lost slot release')`<br>`phase.expect('Update hand-over' in scene.status_text and 'ended by the update' in scene.status_text, 'status names the hand-over and the ended run')`<br>`phase.expect('try again when none is running' not in update.run.text, 'no retired child-refusal advice is shown')` |

## scripts/e2e_checks_update_safety.py :: l12_3_refused_update_with_a_running_child_changes_nothing

Replacement function: `l12_3_no_half_installed_file_at_any_step`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect(update.run.code != 0, 'the update was refused')` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect('files' not in changed, 'the refusal left every file of the project and the home as it was')` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect('head' not in changed and 'commits' not in changed, 'the refusal made no commit in the project')` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect('dirty' not in changed, "the refusal left the project's uncommitted listing as it was")` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect('tables' not in changed, "the refusal changed no ledger table's row count")` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect(not changed, 'the refusal changed nothing at all')` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect(scene.recorded[0] == 0, "the primary can still record the child's result after the refusal")` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |
| `phase.expect(scene.closed[0] == 0, 'the primary can still close its orchestration after the refusal')` | `phase.expect(scene.updated.run.code == 0, 'the monitored busy update finishes')`<br>`phase.expect(scene.managed_samples and scene.monitor_finished, 'the file observer ran through the update and ended')`<br>`phase.expect(not bad, 'every observed managed file has complete predecessor or successor bytes')`<br>`phase.expect(scene.final.run.code == 0 and scene.final.after.ready is True, 'rerunning the completed update remains healthy')` |

## scripts/e2e_checks_update_safety.py :: l12_3_the_update_after_the_refused_one_goes_through_when_the_child_has_ended

Replacement function: `l12_3_finished_result_stays_recordable_after_the_update`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect(scene.dispatch_code == 0 and scene.slots and all((row['released_at_ms'] is not None for row in scene.slots)), 'the child has ended and its slot is released')` | `phase.expect(scene.finished.run.code == 0 and scene.finished.outcome == 'completed', 'a genuine finished result was committed on the predecessor')`<br>`phase.expect(scene.recorded[0] == 0 and scene.rechecked[0] == 0, 'the primary can record and recheck that result on the successor')`<br>`phase.expect(bool(kept) and all((after.get(name) == value for name, value in kept.items())), 'all original outcome, result and admission record bytes are retained')`<br>`phase.expect(scene.finished_history_after['integrity'] == 'ok', 'the final store is sound')`<br>`phase.expect(scene.finished_admissions_before == scene.finished_admissions_after, 'record/recheck creates no second admission for the committed result')` |
| `phase.expect(final.steps == ['setup_update'], 'the second update was one command')` | `phase.expect(scene.finished.run.code == 0 and scene.finished.outcome == 'completed', 'a genuine finished result was committed on the predecessor')`<br>`phase.expect(scene.recorded[0] == 0 and scene.rechecked[0] == 0, 'the primary can record and recheck that result on the successor')`<br>`phase.expect(bool(kept) and all((after.get(name) == value for name, value in kept.items())), 'all original outcome, result and admission record bytes are retained')`<br>`phase.expect(scene.finished_history_after['integrity'] == 'ok', 'the final store is sound')`<br>`phase.expect(scene.finished_admissions_before == scene.finished_admissions_after, 'record/recheck creates no second admission for the committed result')` |
| `phase.expect(not final.run.timed_out and final.run.code == 0 and ('FAIL' not in states.values()), 'the same update, with no child running, goes through')` | `phase.expect(scene.finished.run.code == 0 and scene.finished.outcome == 'completed', 'a genuine finished result was committed on the predecessor')`<br>`phase.expect(scene.recorded[0] == 0 and scene.rechecked[0] == 0, 'the primary can record and recheck that result on the successor')`<br>`phase.expect(bool(kept) and all((after.get(name) == value for name, value in kept.items())), 'all original outcome, result and admission record bytes are retained')`<br>`phase.expect(scene.finished_history_after['integrity'] == 'ok', 'the final store is sound')`<br>`phase.expect(scene.finished_admissions_before == scene.finished_admissions_after, 'record/recheck creates no second admission for the committed result')` |
| `phase.expect(replaced(final.before.pids, final.after.pids, recovery.alive) and final.after.version == current_version() and (final.after.ready is True) and runs_from(final.after.service, ROOT), "and replaces the earlier collector and the project's service, ready on this build")` | `phase.expect(scene.finished.run.code == 0 and scene.finished.outcome == 'completed', 'a genuine finished result was committed on the predecessor')`<br>`phase.expect(scene.recorded[0] == 0 and scene.rechecked[0] == 0, 'the primary can record and recheck that result on the successor')`<br>`phase.expect(bool(kept) and all((after.get(name) == value for name, value in kept.items())), 'all original outcome, result and admission record bytes are retained')`<br>`phase.expect(scene.finished_history_after['integrity'] == 'ok', 'the final store is sound')`<br>`phase.expect(scene.finished_admissions_before == scene.finished_admissions_after, 'record/recheck creates no second admission for the committed result')` |

## scripts/e2e_checks_update_safety.py :: l12_5_second_project_updated_after_the_first_works_and_the_first_still_works

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect(scene.start['second'].version == previous_version(current_version()) and runs_from(scene.start['second'].service, scene.clone), 'the second project was installed by the earlier build and served by its service')` | `phase.expect(scene.start['second'].version == PREDECESSOR_VERSION and runs_from(scene.start['second'].service, scene.clone), 'the second project was installed by the earlier build and served by its service')` |

## scripts/e2e_checks_update_safety.py :: l11_2_update_failed_part_way_is_rolled_back_records_unchanged_next_attempt_succeeds

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect('records' not in changed and 'tables' not in changed, "the collector's records by kind and the ledger's table row counts are unchanged")` | `phase.expect('records' not in changed and all((failed.snapshot_after['ledger'].get(table) == count for table, count in failed.snapshot_before['ledger'].items() if table not in {'runtime_floor', 'runtime_lineage'})), "the collector's records and original table row counts are unchanged; additive authority is journaled")`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_failed), 'every original project row is unchanged except the hand-over epoch; SQLite integrity ok')`<br>`phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT) and replaced(failed.before.pids, failed.after.pids, recovery.alive) and (not any((recovery.alive(pid) for pid in failed.before.service_pids))), 'account_done names the completed hand-over; the new collector owns the records and the old service exited')`<br>`phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text and ('nothing was changed' not in failed.run.text), 'the failure reports the completed hand-over and names its resumable next step')`<br>`phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and (nxt.before.pids == nxt.after.pids), 'and keeps the handed-over collector, ready on this build')`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_next), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` |
| `phase.expect(history_kept(scene.history_before, scene.history_failed, exact=True), 'the project history store is exactly as before the failed update: every record, byte for byte, integrity ok')` | `phase.expect('records' not in changed and all((failed.snapshot_after['ledger'].get(table) == count for table, count in failed.snapshot_before['ledger'].items() if table not in {'runtime_floor', 'runtime_lineage'})), "the collector's records and original table row counts are unchanged; additive authority is journaled")`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_failed), 'every original project row is unchanged except the hand-over epoch; SQLite integrity ok')`<br>`phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT) and replaced(failed.before.pids, failed.after.pids, recovery.alive) and (not any((recovery.alive(pid) for pid in failed.before.service_pids))), 'account_done names the completed hand-over; the new collector owns the records and the old service exited')`<br>`phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text and ('nothing was changed' not in failed.run.text), 'the failure reports the completed hand-over and names its resumable next step')`<br>`phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and (nxt.before.pids == nxt.after.pids), 'and keeps the handed-over collector, ready on this build')`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_next), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` |
| `phase.expect(failed.before.pids == failed.after.pids and failed.before.service_pids == failed.after.service_pids and runs_from(failed.after.collector, scene.clone), "the collector and the project's service are the same live processes, still on the earlier build")` | `phase.expect('records' not in changed and all((failed.snapshot_after['ledger'].get(table) == count for table, count in failed.snapshot_before['ledger'].items() if table not in {'runtime_floor', 'runtime_lineage'})), "the collector's records and original table row counts are unchanged; additive authority is journaled")`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_failed), 'every original project row is unchanged except the hand-over epoch; SQLite integrity ok')`<br>`phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT) and replaced(failed.before.pids, failed.after.pids, recovery.alive) and (not any((recovery.alive(pid) for pid in failed.before.service_pids))), 'account_done names the completed hand-over; the new collector owns the records and the old service exited')`<br>`phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text and ('nothing was changed' not in failed.run.text), 'the failure reports the completed hand-over and names its resumable next step')`<br>`phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and (nxt.before.pids == nxt.after.pids), 'and keeps the handed-over collector, ready on this build')`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_next), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` |
| `phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and replaced(nxt.before.pids, nxt.after.pids, recovery.alive), 'and replaces the earlier collector by itself, ready on this build')` | `phase.expect('records' not in changed and all((failed.snapshot_after['ledger'].get(table) == count for table, count in failed.snapshot_before['ledger'].items() if table not in {'runtime_floor', 'runtime_lineage'})), "the collector's records and original table row counts are unchanged; additive authority is journaled")`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_failed), 'every original project row is unchanged except the hand-over epoch; SQLite integrity ok')`<br>`phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT) and replaced(failed.before.pids, failed.after.pids, recovery.alive) and (not any((recovery.alive(pid) for pid in failed.before.service_pids))), 'account_done names the completed hand-over; the new collector owns the records and the old service exited')`<br>`phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text and ('nothing was changed' not in failed.run.text), 'the failure reports the completed hand-over and names its resumable next step')`<br>`phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and (nxt.before.pids == nxt.after.pids), 'and keeps the handed-over collector, ready on this build')`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_next), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` |
| `phase.expect(history_kept(scene.history_before, scene.history_next, exact=False), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` | `phase.expect('records' not in changed and all((failed.snapshot_after['ledger'].get(table) == count for table, count in failed.snapshot_before['ledger'].items() if table not in {'runtime_floor', 'runtime_lineage'})), "the collector's records and original table row counts are unchanged; additive authority is journaled")`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_failed), 'every original project row is unchanged except the hand-over epoch; SQLite integrity ok')`<br>`phase.expect(scene.pending.get('phase') == 'account_done' and runs_from(failed.after.collector, ROOT) and replaced(failed.before.pids, failed.after.pids, recovery.alive) and (not any((recovery.alive(pid) for pid in failed.before.service_pids))), 'account_done names the completed hand-over; the new collector owns the records and the old service exited')`<br>`phase.expect('The hand-over was done' in failed.run.text and './setup.sh --update' in failed.run.text and ('nothing was changed' not in failed.run.text), 'the failure reports the completed hand-over and names its resumable next step')`<br>`phase.expect(nxt.after.ready is True and nxt.after.version == current_version() and (nxt.before.pids == nxt.after.pids), 'and keeps the handed-over collector, ready on this build')`<br>`phase.expect(handover_history_kept(scene.history_before, scene.history_next), 'after the next successful update every earlier history record is still in the store, unchanged, integrity ok')` |

## scripts/e2e_checks_update_safety.py :: g6_update_never_needs_the_collector_stopped_by_hand_and_ends_healthy_on_the_new_build

Reason: H1d: additive 18/23 targets; retained historical layouts.

| Original assertion | Replacement assertion(s) |
|---|---|
| `phase.expect(len(start.pids) == 1 and runs_from(start.collector, main.clone) and runs_from(start.service, main.clone) and (start.version == previous_version(current_version())) and (start.ready is True), "before the first update the collector and the project's service run from the earlier build, ready")` | `phase.expect(len(start.pids) == 1 and runs_from(start.collector, main.clone) and runs_from(start.service, main.clone) and (start.version == PREDECESSOR_VERSION) and (start.ready is True), "before the first update the collector and the project's service run from the earlier build, ready")` |

## tests/test_e2e_update_safety.py :: test_g6_red_by_the_product_bug_proves_the_fault_only_by_an_update_that_turned_from_ok_to_failed

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertTrue(us.fault_isolated(normal, fault, 'update_handover_broken', expected_ids=ids)['isolated'])` | `self.assertFalse(us.fault_isolated(normal, fault, 'update_handover_broken', expected_ids=ids)['isolated'])` |

## tests/test_e2e_update_safety.py :: test_g6_exception_is_restricted_to_g6_the_named_updates_and_the_known_baseline_bug

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertTrue(isolated(['g6_first_update_ok', *base], ['g6_first_update_failed', *base]))` | `self.assertFalse(isolated(['g6_first_update_ok', *base], ['g6_first_update_failed', *base]))`<br>`self.assertFalse(isolated([f'g6_{name}_ok', *base], [f'g6_{name}_failed', *base]), name)` |
| `self.assertTrue(isolated([f'g6_{name}_ok', *base], [f'g6_{name}_failed', *base]), name)` | `self.assertFalse(isolated(['g6_first_update_ok', *base], ['g6_first_update_failed', *base]))`<br>`self.assertFalse(isolated([f'g6_{name}_ok', *base], [f'g6_{name}_failed', *base]), name)` |

## tests/test_e2e_update_safety.py :: test_only_an_update_marked_as_expected_to_refuse_passes_refuses

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertEqual(3, sum(refusing))` | `self.assertEqual(2, sum(refusing))` |

## tests/test_e2e_update_safety.py :: test_l11_2_seeds_real_child_history_and_compares_the_stores_contents

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('history_kept(scene.history_before, scene.history_failed, exact=True)', body)` | `self.assertIn('handover_history_kept(scene.history_before, scene.history_failed)', body)`<br>`self.assertIn('handover_history_kept(scene.history_before, scene.history_next)', body)`<br>`self.assertIn('account_done', body)`<br>`self.assertIn('update-takeover.json', text)` |
| `self.assertIn('history_kept(scene.history_before, scene.history_next, exact=False)', body)` | `self.assertIn('handover_history_kept(scene.history_before, scene.history_failed)', body)`<br>`self.assertIn('handover_history_kept(scene.history_before, scene.history_next)', body)`<br>`self.assertIn('account_done', body)`<br>`self.assertIn('update-takeover.json', text)` |

## tests/test_e2e_update_safety.py :: test_the_previous_build_is_a_copy_with_only_its_version_changed

Replacement function: `test_the_predecessor_is_an_unmodified_catalogue_listed_archive`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('VERSION', SOURCE)` | `self.assertIn("'archive', PREDECESSOR_REVISION", SOURCE)`<br>`self.assertIn("PREDECESSOR_REVISION = 'afb6f610'", SOURCE)`<br>`self.assertNotIn('write_text(previous_version', SOURCE)`<br>`self.assertNotIn('handle.write(PREVIOUS_BUILD_MARK)', SOURCE)` |
| `self.assertRegex(SOURCE, 'ls-files')` | `self.assertIn("'archive', PREDECESSOR_REVISION", SOURCE)`<br>`self.assertIn("PREDECESSOR_REVISION = 'afb6f610'", SOURCE)`<br>`self.assertNotIn('write_text(previous_version', SOURCE)`<br>`self.assertNotIn('handle.write(PREVIOUS_BUILD_MARK)', SOURCE)` |

## tests/test_setup_update.py :: test_UPDFIX_3_an_update_with_a_child_running_is_refused_before_anything_is_installed

Replacement function: `test_B4t_unverified_predecessor_is_refused_before_anything_is_installed`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('child_run_active', flat)` | `self.assertIn('predecessor_unverified', flat)` |

## tests/test_setup_update.py :: test_UPDFIX_3_a_child_that_appears_before_the_install_is_refused_before_any_write

Replacement function: `test_B4t_a_predecessor_that_becomes_unverified_is_refused_before_any_write`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('child_run_active', flat)` | `self.assertIn('predecessor_unverified', flat)`<br>`self.assertIn('the running version could not be confirmed', flat)` |
| `self.assertIn('a child run is active', flat)` | `self.assertIn('predecessor_unverified', flat)`<br>`self.assertIn('the running version could not be confirmed', flat)` |

## tests/test_setup_update.py :: test_UPDFIX_3_an_interactive_refused_update_leaves_the_remembered_projects_alone

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('a child run is active', ' '.join(self.plain(self.transcript).split()))` | `self.assertIn('the running version could not be confirmed', ' '.join(self.plain(self.transcript).split()))` |

## tests/test_setup_update.py :: test_UPDFIX_3_a_refusal_at_the_check_before_the_install_leaves_the_remembered_projects_alone

Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn('a child run is active', ' '.join(screen.split()))` | `self.assertIn('the running version could not be confirmed', ' '.join(screen.split()))`<br>`self.assertIn('the running version could not be confirmed', ' '.join(self.plain(self.transcript).split()))` |
| `self.assertIn('a child run is active', ' '.join(self.plain(self.transcript).split()))` | `self.assertIn('the running version could not be confirmed', ' '.join(screen.split()))`<br>`self.assertIn('the running version could not be confirmed', ' '.join(self.plain(self.transcript).split()))` |

## tests/test_setup_update.py :: test_UPDFIX_4_a_child_that_started_later_is_told_the_files_were_already_updated

Replacement function: `test_B4t_a_failed_service_stop_is_resumable_before_the_installer_writes`. Reason: SW-3 / D2.7: busy update proceeds; T0 refusals stay write-free; post-intent failures resume.

| Original assertion | Replacement assertion(s) |
|---|---|
| `self.assertIn("had already updated this project's files in step 4", flat)` | `self.assertIn('service_stop_failed', flat)`<br>`self.assertIn('old service was told to stop, and 1 child run ended as lost', flat)`<br>`self.assertIn('./setup.sh --update', flat)`<br>`self.assertEqual(before, self.snapshot(project))` |
| `self.assertIn('run the update again when no child is running', flat)` | `self.assertIn('service_stop_failed', flat)`<br>`self.assertIn('old service was told to stop, and 1 child run ended as lost', flat)`<br>`self.assertIn('./setup.sh --update', flat)`<br>`self.assertEqual(before, self.snapshot(project))` |



## Fix round — exact pairs against 44f16529 / 146955fc

The earlier base pairs remain historical. These pairs supersede the earlier sole-holder, J6a, journal-refusal and harness descriptions. Assertions not listed in these changed methods are retained. New witnesses are additional obligations, not replacements for base safeguards.

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store

Replacement: `StoreTests.test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store`. MED-1/T-4: fake flag replaced by archived consumed run, real publish at stop, actual successor classification and recording authority. Mutation removing classification is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(self.ops.kept_result)` | `self.assertEqual(0, made.returncode, made.stderr)`<br>`self.assertEqual(0, done.returncode, done.stderr)`<br>`self.assertIsNotNone(self.store.result_reservation(self.running).committed_at_ms)`<br>`self.assertEqual(0, stopped_children(self.store, self.intent['runs']))`<br>`self.assertIsNotNone(self.store.read_result(self.running))`<br>`self.assertIsNone(db.execute('SELECT committed_at_ms FROM result_reservations WHERE claim_id=?', (self.running.claim_id,)).fetchone()[0])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_4_another_projects_child_keeps_running

Replacement: `AccountLineageTests.test_B4t_4_another_projects_child_keeps_running`. MED-1/T-17: constant replaced by a live subprocess, authentic Q claim, real P account owner adoption, exact Q row equality, diagnostic and Q settlement. Mutating Q custody during P adoption is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(self.ops.other_child)`<br>`self.assertNotIn('other_project_stop', self.ops.events)` | `self.assertEqual('ready', child.stdout.readline().strip())`<br>`self.assertTrue(before)`<br>`self.assertEqual(before, after)`<br>`self.assertIsNone(child.poll())`<br>`self.assertEqual((1, 1), (report['other_projects'], report['other_runs']))`<br>`self.assertEqual('0.8.66', report['updates'][0]['version'])`<br>`self.assertEqual(1, report['updates'][0]['children'])`<br>`self.assertIsNotNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity WHERE project_id=?', (self.project,)).fetchone()[0])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_security_takeover_has_no_writer_of_its_own

Replacement: `JournalTests.test_B4t_security_takeover_has_no_writer_of_its_own`. LOW-3: the same writer prohibition covers all five coordinator/OS modules. Preflight is the sole SQL exception and each of its two SQLite connections must explicitly be mode=ro and query_only=ON.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertFalse(any(('sqlite' in name for name in imports)))` | `self.assertEqual(2, len(sql))`<br>`self.assertEqual(2, source.count('PRAGMA query_only=ON'))`<br>`self.assertFalse(any(('sqlite' in name for name in imports)), module.__name__)`<br>`self.assertIn('?mode=ro', ast.get_source_segment(source, node))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_4_runs_admitted_after_the_intent_are_ended_by_update

Replacement: `JournalTests.test_B4t_4_runs_admitted_after_the_intent_are_ended_by_update`. A1.12 T-35: all three reads select unsettled entries; the same four run identities and restart equality remain. Settled old/late tombstones are separately excluded by the real OwnerOperations reconciliation test.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual(2, self.ops.events.count(('snapshot', False)))`<br>`self.assertEqual(1, self.ops.events.count(('snapshot', True)))` | `self.assertEqual(3, self.ops.events.count(('snapshot', False)))`<br>`self.assertEqual(0, self.ops.events.count(('snapshot', True)))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_journal_past_the_run_and_byte_limits_changes_nothing

Replacement: `JournalTests.test_journal_past_the_run_and_byte_limits_changes_nothing`. A1.11 supersedes the old refusal at 1,000 runs: the update must finish, list 64, count 936, and stay <=16,384 bytes. The independent oversized-file and 1,025-byte authorization refusals remain unchanged.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual([], list(self.root.iterdir()))`<br>`self.assertEqual(self.ops.original, self.ops.authorization)`<br>`self.assertRaises(JournalError)` | `self.assertEqual(64, len(result['runs']))`<br>`self.assertEqual(936, result['runs_unlisted'])`<br>`self.assertLessEqual(self.journal().path.stat().st_size, 16384)`<br>`self.assertEqual('account_done', result['phase'])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: StoreTests.test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata

Replacement: `StoreTests.test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata`. A1.11: new operation row shape adds two content-free durable integer counts. Closed key-set comparison remains exact and the actual counts (1,0) are pinned.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual({'predecessor_version', 'successor_version', 'state'}, set(report['records'][0]))` | `self.assertEqual({'predecessor_version', 'successor_version', 'state', 'runs_listed', 'runs_unlisted'}, set(report['records'][0]))`<br>`self.assertEqual((1, 0), (report['records'][0]['runs_listed'], report['records'][0]['runs_unlisted']))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: StoreTests.test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds

Replacement: `MemoryTakeoverTests.test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds`. MED-1/T-30: the former double-open fixture had no memory hold. A real SIGKILLed predecessor now leaves two memory holds, its inode is removed, and recovery must retain both. Every pre-existing cell except the diagnostic reason is compared, and two held rows are required. Premature release mutation is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual(before, self.rows('project_holds'))` | `self.assertEqual(2, len(before))`<br>`self.assertEqual([row[:-1] for row in before], [row[:-1] for row in after])`<br>`self.assertIn('instance_unproven', after[0][-1])`<br>`self.assertEqual(2, history._connection.execute("SELECT COUNT(*) FROM project_holds WHERE kind IN ('driver_entry','preparation') AND state='held'").fetchone()[0])`<br>`self.assertTrue(all((observed.get(name) == value for name, value in facts.items() if name != 'reason')))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: ProcessProofTests.test_process_scan_past_limit_is_unproven_and_never_signals

Replacement: `ProcessProofTests.test_linux_foreign_flock_holder_is_busy_without_descriptor_reads`. A1.4 removes the census and its 4096-process/5-second limits. T-36 statically forbids it, candidate-only native pinning passes, and Linux foreign-flock ownership is checked without reading descriptors. This supersedes, rather than weakens, the retired scan requirement.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertRaises(TakeoverError)`<br>`self.assertRaises(TakeoverError)` | `self.assertTrue(process.foreign_lock_holder(42, [path]))`<br>`self.assertFalse(process.foreign_lock_holder(99, [path]))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: ProcessProofTests.test_process_arguments_and_fd_scan_past_byte_limits_are_unproven

Replacement: `ProcessProofTests.test_process_arguments_past_byte_limit_are_unproven`. A1.4 removes every descriptor read. The original oversized-argument OSError remains in the renamed test; T-36 forbids fd APIs and the real native candidate test passes.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertFalse(process.Kernel().sole_holder(os.getpid(), path))` | `self.assertRaises(OSError)` |

### packages/codex-agents/tests/test_b4t_takeover.py :: OwnerIntegrationTests.test_J6a_adds_a_late_settled_entry_but_not_an_older_tombstone_after_restart

Replacement: `OwnerIntegrationTests.test_J6a_excludes_both_old_and_late_settled_entries_after_restart`. A1.12 T-35 explicitly excludes settled entries, including first seen at J6a. Both restart and repeated reconciliation now require exactly the one unsettled run.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual([run_entry(2), run_entry(3)], document['runs'])`<br>`self.assertEqual([run_entry(2), run_entry(3)], restarted.reconcile_runs(document, late=True)['runs'])` | `self.assertEqual([run_entry(2)], document['runs'])`<br>`self.assertEqual([run_entry(2)], restarted.reconcile_runs(document, late=True)['runs'])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: MemoryTakeoverTests.setUp

Replacement: `JournalTests.setUp`. Equivalent closed comparison under the accepted additive target; existing original-cell checks are retained.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(select.select([child.stdout], [], [], 10)[0])`<br>`self.assertLess(child.returncode, 0)` |  |

### tests/test_e2e_update_safety.py :: RegistryTests.test_the_predecessor_is_an_unmodified_catalogue_listed_archive

Replacement: `RegistryTests.test_the_predecessor_is_an_unmodified_catalogue_listed_archive`. MED-5: vendored unmodified P66 archive, pinned SHA-256, runtime version assertion, no Git invocation in a shallow/offline fixture; corrupted artifact must fail with a digest reason.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertIn("'archive', PREDECESSOR_REVISION", SOURCE)` | `self.assertIn('tests/fixtures/b4t/0.8.66.tar.gz', SOURCE)`<br>`self.assertIn('hashlib.sha256(raw).hexdigest() != PREDECESSOR_ARCHIVE_SHA256', SOURCE)` |

### T-12b product-path replacement (no old assertion expression)

The old fake port ran under `os.kill`/`os.killpg` guards but never called product stop code. Its replacement is `CollectorStopTests.test_B4t_3_collector_and_new_services_are_never_signalled`: real `OwnerOperations.stop_collector` and `stop_service`, guards for kill/killpg/pidfd_send_signal, exact shutdown shape and exact `service.stop_for_update` call. Injecting a collector signal turns it red. No existing assertion was removed.

### MED-6: expected-value assignments, base → reviewed tree → final

| Existing test | Old oracle → replacement | Strength |
|---|---|---|
| `test_project_start.StartTests.test_E2c_7_another_version_s_live_host_is_not_replaced` | Base: `'recovery: needs update handover: ' + self.command('alpha')`; reviewed: `'recovery: needs update hand-over: ' + compatibility_message(...)`; final: literal `recovery: needs update hand-over: Another CoCoLo version (number unavailable) holds the project service. The local install record is unavailable. In a plain terminal, open your CoCoLo clone, run ./setup.sh with this project's path, then choose Configure, then "Update the install and walk through every step".` | Entire exact string, including punctuation and owner action, independent of the product builder. Live host, process identity, no-spawn and collector identity assertions all remain. |
| `test_project_start.OtherVersionTests.test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced` | Same three oracle stages and final literal as above. Fixture now creates its empty ledger using the earlier runtime's own owner open, before it starts; no published floor is lowered. | Entire exact literal; every live host and collector assertion is retained. This socket test still needs the primary rerun. |

### Previously omitted fixture/target changes against ce81bd96

| File / stimulus | Old → new | Preserved check / justification |
|---|---|---|
| `test_account_launch_claims.drop_admission_evidence_for_legacy_fixture` | Inline S2-epoch drop → `drop_runtime_authority_for_legacy_fixture`: remove floor/lineage/epoch before labelling a fixture pre-17; refuse a nonempty lineage. | No assertion removed. Historical DDL and source bytes remain historical; adopted custody cannot be relabelled. |
| `test_run_holds.schema_fourteen` | Drop register objects → first drop empty machine-local runtime authority, then the same register objects. | No assertion expression changed; all original-cell, hold and receipt comparisons remain. |
| `test_learning_source_image.test_schema_version_columns_and_primary_key_are_exact` | Newer unsupported stimulus `user_version=18` → `19`; supported 18 is exercised by existing supported-layout iteration and current source image cases. | The same `history_incompatible` refusal and unexpected-column/PK checks remain. |
| `test_learning_source_transfer.target_rows` | All project tables except epoch → all original tables except epoch and the four new machine-local authority tables. | Exactly the same tables/cells that existed at base are compared. New authority is not partition data; backup/restore validators separately exclude it. |
| `test_learning_source_image_legacy` | No direct diff against ce81bd96; shared downgrade helper now retires empty machine-local authority. | Every historical-layout and original-source assertion unchanged. |
| `test_backup_installed_project.test_*` unsupported-version loop | `(0,4,18)` → `(0,4,19)`. | Same `RuntimeError` at each unsupported boundary; exact supported schema 18 columns and table ordering are checked by the supported iteration. |
| `test_reply_shape_live.DRIVERS` | Existing producer list → existing list plus `test_b4t_takeover`. | No assertion removed; extra real producer coverage. New native socket tests are a separate module, so this addition does not introduce sockets into the B4t component module. |
| `test_e2e_update_safety.test_handover_keeps_every_original_row_except_the_explicit_writer_epoch` | Snapshot without schema → literal accepted schema-23 hash in both snapshots. | Same positive and tamper negatives, now additionally checked against a separately pinned schema. |

### HIGH-2 / L11.2 strictness

Base `history_kept(..., exact=True)` → `handover_history_kept(...)` now calls `history_kept(..., exact=True)` after allowing only the existing epoch row's value to differ and new keys in `takeover_record`, `takeover_runs`, `takeover_obligations`, `runtime_floor`. Existing authority rows remain exact; epoch key additions/deletions fail. The before schema must be pinned 22 or 23 and the after schema must be pinned 23. Added `outcome:2`, hold/result/unknown/epoch rows, changed original rows and unexpected DDL all fail the negative unit witness. Thus no business-row or schema comparison is weaker than ce81bd96.

### Renames retaining every assertion

The real kernel candidate-pin test retains its readiness, exit and lock-free comparisons under its A1 name. The formerly standalone SIGKILL/missing-inode memory test becomes T-30 with all its cell, reason and held-count assertions retained. The oversized argument test retains its OSError under its new name. Retired two-opener/census cases map to A1 E's real cross-process flock refusal and T-36's ban on all descriptor/process censuses.

## S5 final fix — pairs against 094f73d1 (fix round ff35ecdd)

No assertion was removed or loosened and no failure became a skip. Each change below is an oracle correction
from the requirement, a fixture correction, or an added check.

### test_project_start E2c-7 (both cases): oracle corrected from the requirement

| Test | Old → new | Reason |
|---|---|---|
| `StartTests.test_E2c_7_another_version_s_live_host_is_not_replaced` | literal `... holds the project service. The local install record is unavailable. In a plain terminal, open your CoCoLo clone, run ./setup.sh with this project's path, then choose Configure, then "Update the install and walk through every step".` → literal `recovery: needs update hand-over: Another CoCoLo version (number unavailable) holds the project service. In a plain terminal, run:\ncd <root> && ./setup.sh <root>\nThen choose Configure, then "Update the install and walk through every step".` (`<root>` = the fixture's own project path, no product call) | Verdict: the fixture oracle was wrong, the product is right. D2.8's RS-5 line names the B4m command; B4m gives the local command whenever the install binding exists and the "record unavailable" guidance only when it is missing. The collector status is read through the project's recorded launch copy, and `install_copy` (via `install_runtime_copy`, no `clone_root`) binds that copy with the project itself as its clone. So the binding exists and the command form is the requirement's answer. Still an exact literal, independent of `compatibility_message`. The failure message is now a lambda (it printed the status of the first poll, `None`, instead of the last). |
| `OtherVersionTests.test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced` | same literal change as above | Same reason (its launch is recorded with the same bound copy). |
| `OtherVersionTests.test_E2c_7_…` fixture | `self.add('alpha', record=False)` (first owner open by this build, which writes its runtime floor) then delete `learning.sqlite3`/-wal/-shm and reopen with the earlier runtime (`create=False`) → install the package, have the earlier runtime make the first owner open (`LearningState(create=True)` of a bootstrap-pending root) and register the project, then `install_copy`; added `assertEqual(0, database size)` before it | Deleting the ledger left the state's `ledger-initialized` marker, which every build rightly refuses ("initialized ledger database is missing or empty"). A real earlier runtime originates its own ledger before an update, and no build may lower B4t's floor (E4), so the earlier runtime now opens first. Every host, no-spawn, collector and status assertion is unchanged. |

### test_b4t_native (fixture defects; product defects found)

| Test / fixture | Old → new | Reason |
|---|---|---|
| `NativeArchiveTests.setUp` serving wait | `_hinted_host(self.state, …)` → `_hinted_host(self.paths, …)` | `runtime_hint_path` accepts only `CollectorPaths`; with a `LearningState` every read is `dispatch_unavailable`, so the predicate was never true although the archived host served (its hint, sockets and locks were all on disk). Same wait, same 30 s, same assertion. |
| `ResidualStub.run_stub` hint path | `runtime_hint_path(self.state, …)` → `runtime_hint_path(self.paths, …)` | Same type rule. |
| `ArchivedServiceAudit` hint reads | `_hinted_host(state, …)` → `_hinted_host(CollectorPaths(state.root), …)` | Same type rule. |
| `ArchivedServiceAudit` P66 fixture stop | `pin_predecessor(pid, entry=<project>/…/project_host.py).stop()` → check the peer pid's arguments (stand-in `boot.py`, an archived `project_host.py` inside the World and outside this tree, exact `--state-root/--project-root`), then `os.kill(pid, SIGTERM)` and wait for `process_gone` | The World runs every product process through its stand-in interpreter, so a World service never presents A1 I3's launch line and `pin_predecessor` rightly refuses it (`predecessor_unproven`). This is only the fixture's own teardown of its idle service by exact pid; the audited assertions are unchanged. |
| `ArchivedServiceAudit` audited service interpreter | `sys.executable` → `world.shim` (the World's stand-in) | Without the stand-in the audited service resolves the account home from the password database, i.e. the real home; the stand-in confines it to the World. The audit hook and every event assertion are unchanged; added: the hint's peer pid is the audited process. |
| `ArchivedServiceAudit` V34 setup | `set_up_previous` (0.8.6x scripted wizard answers) → `e2e_checks_update_history.archive_tag` + `set_up_release` (the L12.1 path: 0.3.4's own installer, registration and attestation); no fixture service exists to stop (0.3.4 has no collector starter), asserted; the fake Claude init frame lists V34's `CLAUDE_TOOLS` (Read/Glob/Grep) | The 0.8.6x wizard answers never complete 0.3.4's setup; 0.3.4 rejects an init frame with the later `StructuredOutput` tool (`init_tool_not_allowed`). The offline plain skip stays exactly as before (archive unavailable). Every audit assertion is unchanged. |
| `ResidualStub.run_stub` | added `assertEqual(30.0, takeover_owner.SERVICE_SELF_STOP_SECONDS)`; the run patches that wait to 1 s; added `assertGreaterEqual(KILL time − TERM time, 29.9)` for the TERM-ignoring case | The stub never stops by itself, so A1.5 row 3's wait for a withdrawn idle service always ran out (30 s per case) before the proven signal. The product value is pinned, the TERM/KILL bounds these cases witness are untouched and now measured. |
| `ResidualStub` C stub accept loop | accept then close at once → keep each accepted connection until its client closes it (up to 64) | Found as a flake in the affected run (`'signal' != 'hint'`): macOS `LOCAL_PEERPID` reports no peer once the listener has closed its side (reproduced in isolation), so `_peer` read `None` whenever the stub won the race, and the plan correctly fell back to `hint`. A real service keeps the connection open for its request; the stub now does too. No assertion change. |
| `relaunch` | account start after the hand-over keeps the stopped predecessor collector for reaping | Removes the unreaped-child `ResourceWarning`s; no assertion change. |

### Module split (budget), every assertion retained

| Old name | New name |
|---|---|
| `test_b4t_native.AcceptedResidualStubTests.test_same_user_impostor_ignoring_TERM_is_KILLed_after_bound` | `test_b4t_native_kill.AcceptedResidualKillTests.test_same_user_impostor_ignoring_TERM_is_KILLed_after_bound` |
| `test_b4t_native.ArchivedServiceAuditTests.test_B4t_A1_real_P66_service_does_not_exec_during_a_claim` | `test_b4t_native_audit_p66.ArchivedServiceAuditTests.test_B4t_A1_real_P66_service_does_not_exec_during_a_claim` |
| `test_b4t_native.ArchivedServiceAuditTests.test_B4t_A1_real_V34_service_does_not_exec_during_a_claim` | `test_b4t_native_audit_v34.ArchivedServiceAuditTests.test_B4t_A1_real_V34_service_does_not_exec_during_a_claim` |

All three are registered (scenario layer, integration stage, timings, RS-B-B4t-3/-9 checks). The 60 s scenario ceiling is unchanged.
