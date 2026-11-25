# Assertion changes for the required schema fence

No behavioral refusal, custody or signature assertion is weakened. Schema-version pins advance because RSF-S2-4 must exclude real old executable reopens.

- `test_learning_store.py:648`: `self.assertEqual(16, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:694`: `self.assertEqual(16, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:713`: `self.assertEqual(16, version)  # PC-4: current schema includes probe provenance` → `self.assertEqual(17, version)  # PC-4: current schema includes probe provenance`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:756`: `self.assertEqual(16, self.ledger._connection.execute(` → `self.assertEqual(17, self.ledger._connection.execute(`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:2679`: `self.assertEqual(16, report['ledger_schema_version'])  # PC-4: current schema includes probe provenance` → `self.assertEqual(17, report['ledger_schema_version'])  # PC-4: current schema includes probe provenance`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:3006`: `self.assertEqual(16, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]` → `self.assertEqual(17, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:3073`: `self.assertEqual(16, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])`. Required ledger 17 / project 22 version fence.
- `test_learning_store.py:3655`: `self.assertEqual(16, health["schema_version"])  # PC-4: current schema includes probe provenance` → `self.assertEqual(17, health["schema_version"])  # PC-4: current schema includes probe provenance`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:139`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:251`: `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)` → `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:270`: `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)` → `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:293`: `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)` → `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:325`: `self.assertEqual(21, self.version())  # B6: schema 21 (decision 131)` → `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_project_holds_schema.py:331`: `self.assertTrue(copy.name.startswith('history.sqlite3.before-21.'))  # B6: schema 21 (decision 131)` → `self.assertTrue(copy.name.startswith('history.sqlite3.before-22.'))  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 version fence.
- `test_learning_source_image.py:213`: `# PC-4: schema 16 is supported; 17 is newer and refused.` → `# RSF-S2: schema 17 is supported; 18 is newer and refused.`. Required ledger 17 / project 22 version fence.
- `test_learning_source_image.py:214`: `self.connection.execute('PRAGMA user_version=17').close()` → `self.connection.execute('PRAGMA user_version=18').close()`. Required ledger 17 / project 22 version fence.
- `test_learning_source_image.py:216`: `self.connection.execute('PRAGMA user_version=16').close()` → `self.connection.execute('PRAGMA user_version=17').close()`. Required ledger 17 / project 22 version fence.

Fixture-only change: `project_schema_fixtures.legacy_orchestration_state_layout` explicitly retires the new connection gate and removes `runtime_takeover` before constructing a pre-22 layout; all historical-layout assertions remain.

- `test_project_holds_schema.test_M2_a_repeated_step_is_a_no_op`: whole-store equality → equality of every historical table plus an explicit epoch +1 assertion. A successful successor must advance the machine-local epoch.
- `test_project_holds_schema.test_M2_an_old_writer_still_running_excludes_the_step`: `^history_writer_busy$` → exact plain busy message, exact update command and retained `history_writer_busy` reason suffix.
- The manual schema-18 fixture now also removes `runtime_takeover`; its rollback assertions remain unchanged.

- `test_project_holds_schema.test_X5_M3_one_step_creates_the_four_tables_adopts_nothing_and_both_validators_pass`: equality excluding only schema-18 tables → equality also excluding the new epoch table, plus epoch == 1. Every historical record is still compared.
- `test_project_holds_schema.test_SB_a_copy_of_an_18_store_carries_the_new_tables_and_a_restore_opens_validated`: whole-store equality → every historical table equal plus the restored owner epoch incremented exactly once.

Fixture-only change: `drop_admission_evidence_for_legacy_fixture` now explicitly retires the epoch gate and removes the machine-local epoch before producing an exact prior-source ledger. This preserves the existing golden layout equality assertion, rather than relaxing it.

- `test_account_open_schema.py:64`: `self.assertEqual(16, db.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, db.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_open_schema.py:92`: `self.assertEqual(16, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_open_schema.py:111`: `self.assertEqual(16, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_open_schema.py:123`: `self.assertEqual(16, checked['ledger_schema_version'])  # O0b RS-A (requirement)` → `self.assertEqual(17, checked['ledger_schema_version'])  # O0b RS-A (requirement)`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_cleanup_schema.py:49`: `self.assertEqual(16,self.base.ledger._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17,self.base.ledger._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:126`: `self.assertEqual(16, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:133`: `self.assertEqual(16, verified['ledger_schema_version'])  # O0b RS-A (requirement)` → `self.assertEqual(17, verified['ledger_schema_version'])  # O0b RS-A (requirement)`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:145`: `self.assertEqual(16, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:152`: `self.assertEqual(16, verified['ledger_schema_version'])` → `self.assertEqual(17, verified['ledger_schema_version'])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:175`: `self.assertEqual(16, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:206`: `self.assertEqual(16, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, upgraded._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_account_observation_schema.py:237`: `self.assertEqual(16, recovered._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, recovered._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger version fence; historical cells and rollback assertions retained.

- `test_learning_source_transfer.target_rows` (used by transfer/restart/no-write assertions): all project tables → every historical project table excluding `runtime_takeover`, which changes on a successful owner acquisition and is not imported learning data. S2's epoch tests separately assert the exact increment and record bytes; all signature/QA comparisons remain. This also keeps the manual schema-14 fixture from copying a table that did not exist.

- `test_history_service_upgrade.py:42`: `return sorted(path for path in self.directory.glob('history.sqlite3.before-21.*')  # B6: schema 21 (decision 131)` → `return sorted(path for path in self.directory.glob('history.sqlite3.before-22.*')  # B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_history_service_upgrade.py:55`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_history_service_upgrade.py:115`: `impostor = self.directory / 'history.sqlite3.before-21.1'  # (requirement) B6: format 21 (decision 131)` → `impostor = self.directory / 'history.sqlite3.before-22.1'  # (requirement) B6: format 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_history_service_upgrade.py:167`: `existing = self.directory / f'history.sqlite3.before-21.{fixed}'  # (requirement) B6: format 21 (decision 131)` → `existing = self.directory / f'history.sqlite3.before-22.{fixed}'  # (requirement) B6: format 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_project_results.py:733`: `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_project_results.py:806`: `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_project_release_ack.py:39`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_project_hold_register.py:357`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_e2a_launch_record.py:62`: `self.assertEqual(16, connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, connection.execute('PRAGMA user_version').fetchone()[0])`. Required version fence / kept-copy enumeration for ledger 17 or project 22.

- `test_e2a_launch_record.py:74`: `self.assertEqual(16, reopened._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(17, reopened._connection.execute('PRAGMA user_version').fetchone()[0])`. Required version fence / kept-copy enumeration for ledger 17 or project 22.


- `test_b6_unattended_close.py:561`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:563`: `self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-21.'))` → `self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-22.'))`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:631`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:655`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:683`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1024`: `kept = self.directory / 'history.sqlite3.before-21.1'` → `kept = self.directory / 'history.sqlite3.before-22.1'`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1029`: `marker = self.directory / f'.history.sqlite3.upgrade-21.{status.st_dev}-{status.st_ino}.attempt'` → `marker = self.directory / f'.history.sqlite3.upgrade-22.{status.st_dev}-{status.st_ino}.attempt'`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1032`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1040`: `(marker,) = self.directory.glob('.history.sqlite3.upgrade-21.*.attempt')` → `(marker,) = self.directory.glob('.history.sqlite3.upgrade-22.*.attempt')`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1047`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1207`: `linked = self.directory / '.history.sqlite3.before-21.2.0123456789abcdef.partial'` → `linked = self.directory / '.history.sqlite3.before-22.2.0123456789abcdef.partial'`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1208`: `lookalike = self.directory / '.history.sqlite3.before-21.3.partial'` → `lookalike = self.directory / '.history.sqlite3.before-22.3.partial'`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.py:1212`: `self.assertEqual(21, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` → `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_account_cleanup_release.py:294`: `self.assertEqual(16, body['ledger_schema_version'])  # O0b RS-A (requirement)` → `self.assertEqual(17, body['ledger_schema_version'])  # O0b RS-A (requirement)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_active_run_capacity.py:329`: `self.assertEqual(16, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])` → `self.assertEqual(17, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_orchestration.py:214`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_result_leases.py:414`: `self.assertEqual(21, self.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_result_leases.py:457`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_host_open.py:308`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_qa_commit.py:294`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b6_unattended_close.test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations`: compare every other table → assert epoch 1 separately, then compare every historical table byte for byte.
- `test_backup_installed_project.test_exact_version_manifests_match_owner_reader_and_keep_old_tables_absent`: supported 5–16 / reject 17 → supported 5–17 (all original column assertions also apply to 17) / reject 18.
- `test_b2_service_outcome_round2.durable` (rollback/reopen/ownership-transfer assertions): every historical row and artifact is still compared; exclude only `runtime_takeover`, which must advance for the successor. The S2 test independently pins that exact increment and rejects the stale writer.

- `test_b6_unattended_close.test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations` (reopen assertion): full-store equality → equality of every historical table and exact owner epoch +1.

- `test_account_observation_backup.py:31`: `self.assertEqual(16, self.body['ledger_schema_version'])  # PC-4: current schema includes probe provenance` → `self.assertEqual(17, self.body['ledger_schema_version'])  # PC-4: current schema includes probe provenance`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_learning_qa_upgrade.py:80`: `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_learning_chunk_upgrade.py:94`: `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b2_service_outcome.py:787`: `self.assertEqual(21, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)` → `self.assertEqual(22, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b2_service_outcome.py:807`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b2_service_outcome.py:811`: `self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-21.*'))))  # the copy made for it` → `self.assertEqual(1, len(list(self.directory.glob('history.sqlite3.before-22.*'))))  # the copy made for it`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b2_service_outcome.py:826`: `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-21.*')))  # B6: schema 21 (decision 131)` → `self.assertEqual([], list(self.directory.glob('history.sqlite3.before-22.*')))  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_b2_service_outcome.py:828`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_execution_grant.py:285`: `self.assertEqual(21, self.fixture.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.fixture.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_open_withdrawal.py:499`: `self.assertEqual(21, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)` → `self.assertEqual(22, project_history._SCHEMA_VERSION)  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_open_withdrawal.py:518`: `self.assertEqual(21, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)` → `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])  # B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_learning_migration.py:415`: `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_learning_outcome_pages.py:167`: `self.assertEqual(21, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_learning_corpus.py:289`: `self.assertEqual(21, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_project_qa_check_receipts.py:243`: `self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)` → `self.assertEqual(22, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_account_billing_admission.py:783`: `self.assertEqual(16, body['ledger_schema_version'])  # O0b RS-A (requirement)` → `self.assertEqual(17, body['ledger_schema_version'])  # O0b RS-A (requirement)`. Required ledger 17 / project 22 schema or upgrade-copy identity.

- `test_learning_outcome_pages.test_exact_bytes_and_sequence_pagination_without_owner_json_or_hash_work`: every statement uses the outcomes index → every statement uses that index or is the exact singleton epoch lookup; additionally require the epoch lookup. No unindexed data query is accepted.

- `test_rsf_s4_slots.test_unreadable_instance_lock_cannot_establish_positive_unsent` fixture: refusing every `*.lock` open → refusing journal-instance lock opens while the ledger writer lock remains readable. The original hold-count assertion is unchanged; add an assertion that the journal fault was exercised. The old global injection also denied the new owner-lock check, testing a different failure.

## Exact remaining assertion replacements

These are the concrete old/new hunks for the epoch comparisons and query/fixture changes described above.

`packages/codex-agents/tests/test_b6_unattended_close.py`

```diff
@@ -570,3 +570,4 @@ class SchemaStepTests(OrchestrationStore):
         old_holds = before.pop('project_holds')
-        self.assertEqual(before, after)  # every other table byte for byte
+        self.assertEqual(1, after.pop("runtime_takeover")[0][1])
+        self.assertEqual(before, after)  # every historical table byte for byte
         self.assertLessEqual(set(old_holds), set(holds))  # the update's tombstone kept as it was
```

`packages/codex-agents/tests/test_b6_unattended_close.py`

```diff
@@ -602,3 +603,6 @@ class SchemaStepTests(OrchestrationStore):
         self.store = self.open_history()
-        self.assertEqual(upgraded, self.rows())
+        reopened = self.rows()
+        self.assertEqual({k: v for k, v in upgraded.items() if k != "runtime_takeover"},
+                         {k: v for k, v in reopened.items() if k != "runtime_takeover"})
+        self.assertEqual(upgraded["runtime_takeover"][0][1] + 1, reopened["runtime_takeover"][0][1])
         self.assertEqual(1, len(self.copies()))
```

`packages/codex-agents/tests/test_learning_outcome_pages.py`

```diff
@@ -69,3 +69,5 @@ class LearningOutcomePageTests(unittest.TestCase):
         self.assertFalse(any(s.startswith(('BEGIN', 'COMMIT')) for s in statements))
-        self.assertTrue(all('INDEXED BY records_learning_outcomes' in s for s in statements))
+        fence_sql = 'SELECT epoch,new_digest FROM runtime_takeover WHERE singleton=1'
+        self.assertIn(fence_sql, statements)
+        self.assertTrue(all('INDEXED BY records_learning_outcomes' in s or s == fence_sql for s in statements))
 
```

`packages/codex-agents/tests/test_project_holds_schema.py`

```diff
@@ -165,3 +165,5 @@ class SchemaStepTests(unittest.TestCase):
         self.assertEqual([], after.pop('project_result_recorders'))  # schema20 (B2, decision 114.1 A): created empty, adopts nothing
-        self.assertEqual(before, {name: rows for name, rows in after.items() if name not in SCHEMA_18_TABLES})
+        self.assertEqual(before, {name: rows for name, rows in after.items()
+                                  if name not in (*SCHEMA_18_TABLES, "runtime_takeover")})
+        self.assertEqual(1, after["runtime_takeover"][0][1])
         self.assertTrue(project_holds_schema.register_consistent(db))
```

`packages/codex-agents/tests/test_project_holds_schema.py`

```diff
@@ -284,3 +287,5 @@ class SchemaStepTests(unittest.TestCase):
         self.store.close()
-        self.assertEqual(upgraded, self.rows())
+        self.assertEqual({k: v for k, v in upgraded.items() if k != "runtime_takeover"},
+                         {k: v for k, v in self.rows().items() if k != "runtime_takeover"})
+        self.assertEqual(upgraded["runtime_takeover"][0][1] + 1, self.rows()["runtime_takeover"][0][1])
 
```

`packages/codex-agents/tests/test_project_holds_schema.py`

```diff
@@ -319,3 +324,3 @@ class SchemaStepTests(unittest.TestCase):
         fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the old service still holds the writer lock
-        with self.assertRaisesRegex(HistoryError, '^history_writer_busy$'):
+        with self.assertRaisesRegex(HistoryError, r'^Another store owner is still running\. Run: \./setup\.sh --update \(history_writer_busy\)$'):
             self.upgrade()
```

`packages/codex-agents/tests/test_project_holds_schema.py`

```diff
@@ -412,3 +417,6 @@ class SchemaStepTests(unittest.TestCase):
         self.store.close()
-        self.assertEqual(kept, self.rows())
+        restored = self.rows()
+        self.assertEqual({k: v for k, v in kept.items() if k != "runtime_takeover"},
+                         {k: v for k, v in restored.items() if k != "runtime_takeover"})
+        self.assertEqual(kept["runtime_takeover"][0][1] + 1, restored["runtime_takeover"][0][1])
 
```

## New acceptance-test strengthening

The initial offline-CLI negative case allowed an authorization refusal. It now
installs the genuine old runtime's valid authorization and requires the literal
`newer` schema refusal, proving the CLI reaches the store fence. Command regexes
now escape `.` literally. The epoch matrix also exercises write/batch reuse of
cursors returned by `connection.execute`, in addition to `connection.cursor`.
These strengthen newly added tests; no existing assertion was relaxed.

## Fixed-scope full-run correction assertions (2026-11-25)

- `test_project_start.ReleasedLedgerUpgradeTests.test_E2c_10_a_released_0_8_59_ledger_opens_and_works_under_this_build`:
  **old** `assertEqual(learning_store.LEDGER_SCHEMA_VERSION, connection.execute('PRAGMA user_version').fetchone()[0])`
  on the untouched 0.8.59 fixture → **new** `assertEqual(16, connection.execute('PRAGMA user_version').fetchone()[0])`.
  The released binary always writes 16; S2's current schema is 17. The existing
  later assertion still requires `LEDGER_SCHEMA_VERSION` after each current open,
  and all column, starter, backup/restore/move checks remain unchanged.
- `test_launch_receipts_layout.LaunchReceiptsLayoutTests.test_receipts_ddl_changes_only_with_a_schema_bump`:
  **old** `assertEqual(16, LEDGER_SCHEMA_VERSION, ...)` → **new** `assertEqual(17, LEDGER_SCHEMA_VERSION, ...)`.
  Add `assertEqual(FROZEN_FOR_SCHEMA_17, stored_layout())` with hash
  `e2d505aac99614b436bb135af66d7e62c9bedf10309cb4688e6cce552c27fa9f`.
  Keep the original schema-15 hash assertion. The paired production upgrade is
  `LearningLedger._migrate`'s 16→17 branch: it adds/claims only `runtime_takeover`
  in `_account_transaction`, changing no launch-receipt DDL or records.

No other pre-existing assertion changes in this fix round. In particular the
process cleanup `history_not_authorized` assertion, all named-savepoint callers,
read-only no-private-file check, and lifetime-cap guard remain intact.
