"""Actual original owner -> complete inert image and record/signature/QA staging."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest import mock

from tests import test_learning_source_image_legacy as legacy
from tests.learning_support import backup_body, event_record
from codex_agents import project_history as history
from codex_agents import learning_source_transfer as transfer
from codex_agents import project_learning_source as archive
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents.project_learning_activation import ACTIVATION_COLUMNS
from codex_agents.result_contracts import ResultCapacity
from codex_agents.sql_read_budget import SQLReadLimits


class LearningSourceTransferTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def transfer(self, import_id='source'):
        return transfer.stage_source(self.source, self.target, self.project, getattr(self, 'file_id', 'source_image'),
            import_id=import_id, work_limits=SQLReadLimits(100_000, 1000, 2 * 1_048_576))

    def archive_body(self):
        info = archive.image_status(self.target, 'source')
        return b''.join(archive.read_image_chunk(self.target, 'source', n) for n in range(info['next_chunk']))

    def target_rows(self):
        return {name: [tuple(row) for row in self.target._connection.execute('SELECT * FROM ' + name)]
                for name in history._COLUMNS}

    def reopen(self):
        self.target.close()
        directory = Path(self.fixture.temporary.name) / 'image-target-history'
        self.target = ProjectHistory(directory, self.project, result_key=self.key,
            result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000, 120_000), allow_schema_upgrade=True)
        self.addCleanup(self.target.close)

    def test_complete_original_rows_signatures_and_qa_survive_restart_and_exact_replay(self):
        # The archived image is the registered backup file, byte for byte.
        original = self.backup.read_bytes()
        source_changes = self.source._connection.total_changes
        result = self.transfer()
        self.assertEqual(original, self.archive_body())
        self.assertEqual(hashlib.sha256(original).hexdigest(), result['source']['manifest_digest'])
        self.assertEqual(30, len(result['counts']))
        self.assertEqual((23, 2, 1), tuple(dict(result['counts'])[name] for name in
                                         ('records', 'owner_attestations', 'operational_qa')))
        self.assertTrue(all(result[part]['state'] == 'staged' for part in ('source', 'records', 'qa')))
        self.assertTrue(all(result[part]['learning_ready'] is False for part in ('source', 'records', 'qa')))
        before = self.target_rows()
        changes = self.target._connection.total_changes
        self.assertEqual(result, self.transfer())
        self.assertEqual(changes, self.target._connection.total_changes)
        self.assertEqual(before, self.target_rows())
        self.assertEqual(source_changes, self.source._connection.total_changes)
        self.reopen()
        self.assertEqual(before, self.target_rows())
        self.assertEqual(original, self.archive_body())
        self.assertEqual(result, self.transfer())
        self.assertEqual(0, self.target._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])

    def body_source(self):
        return backup_body(self.backup, self.project)

    def archived_rows(self):
        copy = Path(self.fixture.temporary.name) / 'archived-image.sqlite3'
        copy.write_bytes(self.archive_body())
        copy.chmod(0o600)
        try:
            return backup_body(copy, self.project)
        finally:
            copy.unlink()

    def test_full_original_qa_and_host_check_rows_are_not_replaced_by_subsets(self):
        self.transfer()
        body = self.archived_rows()
        self.assertEqual(self.body_source(), body)
        for name in ('run_provenance', 'host_run_bindings', 'operational_check_receipts',
                     'launch_inputs', 'account_open_receipts'):
            self.assertEqual(self.body_source()['tables'][name], body['tables'][name])
        self.assertEqual(self.body_source()['tables']['operational_qa'], body['tables']['operational_qa'])

    def test_changed_source_refuses_and_preserves_prior_inert_image(self):
        self.transfer()
        before = self.archive_body()
        self.source._connection.execute('UPDATE run_provenance SET created_at_ms=created_at_ms+1 '
                                        'WHERE project_id=?', (self.project,)).close()
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.transfer()
        self.assertEqual(before, self.archive_body())
        self.assertIs(False, archive.image_status(self.target, 'source')['learning_ready'])

    def test_source_reentrant_change_leaves_partial_staging_without_activation(self):
        original = archive.stage_image_chunk
        def changed(*args):
            result = original(*args)
            self.source._connection.execute('UPDATE run_provenance SET created_at_ms=created_at_ms+1 '
                                            'WHERE project_id=?', (self.project,)).close()
            return result
        with mock.patch.object(archive, 'stage_image_chunk', side_effect=changed), \
                self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.transfer()
        self.assertEqual('staging', archive.image_status(self.target, 'source')['state'])
        self.assertEqual(0, self.target._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertFalse(self.target._connection.in_transaction)
        self.assertFalse(self.source._connection.in_transaction)

    def test_interrupted_transfer_resumes_exact_original_chunks_after_restart(self):
        original = self.target.stage_learning_import_chunk
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt
        with mock.patch.object(self.target, 'stage_learning_import_chunk', side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            self.transfer()
        self.assertFalse(self.target._connection.in_transaction)
        self.assertFalse(self.source._connection.in_transaction)
        self.assertEqual('staging', archive.image_status(self.target, 'source')['state'])
        chunk = self.target.learning_import_chunk('source', 0)
        self.reopen()
        self.assertIs(False, self.transfer()['learning_ready'])
        self.assertEqual(chunk, self.target.learning_import_chunk('source', 0))
        self.assertEqual(self.backup.read_bytes(), self.archive_body())

    def test_different_key_default_owner_outer_transaction_and_foreign_thread_refuse(self):
        self.target._allow_schema_upgrade = False
        with self.assertRaisesRegex(HistoryError, '^history_not_authorized$'):
            self.transfer()
        self.target._allow_schema_upgrade = True
        with mock.patch.object(self.target, '_result_key', b'x' * 32), \
                self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.transfer()
        self.target._connection.execute('BEGIN').close()
        try:
            with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
                self.transfer()
            self.assertTrue(self.target._connection.in_transaction)
        finally:
            self.target._connection.rollback()
        errors = []
        def foreign():
            try:
                self.transfer()
            except HistoryError as error:
                errors.append(error.reason_code)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join()
        self.assertEqual(['history_wrong_owner'], errors)
        self.assertEqual(0, self.target._connection.execute('SELECT COUNT(*) FROM project_learning_imports').fetchone()[0])

    def test_image_corruption_is_not_accepted_even_on_finished_replay(self):
        self.transfer()
        self.target._connection.execute('UPDATE project_learning_source_chunks SET content=? WHERE chunk_index=0',
                                        (b'bad',)).close()
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            archive.read_image_chunk(self.target, 'source', 0)
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.transfer()

    def test_source_key_and_file_reads_never_occur_inside_target_transaction(self):
        original = self.fixture.state.digest_key
        def key():
            self.assertFalse(self.target._connection.in_transaction)
            self.assertFalse(self.source._connection.in_transaction)
            return original()
        with mock.patch.object(self.fixture.state, 'digest_key', side_effect=key):
            self.assertIs(False, self.transfer()['learning_ready'])

    def test_capacity_remains_bounded_by_original_import_slots(self):
        self.transfer('one')
        self.transfer('two')
        with self.assertRaisesRegex(HistoryError, '^history_learning_capacity$'):
            self.transfer('three')
        self.assertEqual(2, self.target._connection.execute('SELECT COUNT(*) FROM project_learning_source_images').fetchone()[0])

    def test_canonical_image_capacity_refuses_before_any_target_import(self):
        with mock.patch.object(archive, 'MAX_BYTES', 1), \
                self.assertRaisesRegex(HistoryError, '^history_learning_capacity$'):
            self.transfer()
        self.assertEqual(0, self.target._connection.execute('SELECT COUNT(*) FROM project_learning_imports').fetchone()[0])
        self.assertFalse(self.target._connection.in_transaction)
        self.assertFalse(self.source._connection.in_transaction)

    def old14(self):
        self.transfer()
        self.old_rows = {n: rows for n, rows in self.target_rows().items()
                         if n not in archive.SOURCE_COLUMNS and n not in ACTIVATION_COLUMNS
                         and n not in history.REGISTERED_RUN_COLUMNS}
        directory = Path(self.fixture.temporary.name) / 'old14'
        directory.mkdir(mode=0o700)
        database = directory / 'history.sqlite3'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(history._SCHEMA_V14)
            for name, rows in self.old_rows.items():
                for row in rows:
                    connection.execute('INSERT INTO ' + name + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
            connection.execute(f'PRAGMA application_id={history._APPLICATION_ID}')
            connection.execute('PRAGMA user_version=14')
        database.chmod(0o600)
        return directory, database

    def test_exact_schema14_upgrade_preserves_rows_and_does_not_infer_missing_image(self):
        directory, database = self.old14()
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            ProjectHistory(directory, self.project)
        with mock.patch.object(history, 'LEARNING_SOURCE_SCHEMA', 'CREATE TABLE partial(x); INVALID SQL;'), \
                self.assertRaises(HistoryError):
            ProjectHistory(directory, self.project, allow_schema_upgrade=True)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(14, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
        target = ProjectHistory(directory, self.project, result_key=self.key,
            result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000, 120_000), allow_schema_upgrade=True)
        self.addCleanup(target.close)
        for name, rows in self.old_rows.items():
            self.assertEqual(rows, [tuple(row) for row in target._connection.execute('SELECT * FROM ' + name)])
        with self.assertRaisesRegex(HistoryError, '^history_not_found$'):
            archive.image_status(target, 'source')
        self.target.close()
        self.target = target
        self.assertIs(False, self.transfer()['learning_ready'])
        self.assertEqual(self.backup.read_bytes(), self.archive_body())

    def test_many_original_records_use_bounded_archive_and_original_staging_pages(self):
        import time
        for number in range(150):
            self.source.append(self.project, event_record('source-page-' + str(number), observed_at_ms=number + 1),
                               producer='hook', now_ms=number + 1)
        self.file_id = 'source_image_pages'
        self.backup = self.source.create_sanitized_backup(self.project, file_id=self.file_id,
            expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        result = self.transfer()
        self.assertGreater(result['source']['next_chunk'], 1)
        self.assertEqual(self.backup.read_bytes(), self.archive_body())
        for index in range(result['source']['next_chunk']):
            self.assertLessEqual(len(archive.read_image_chunk(self.target, 'source', index)), archive.CHUNK_BYTES)
        self.assertEqual(173, dict(result['counts'])['records'])
        self.assertEqual(173, self.target._connection.execute(
            'SELECT COUNT(*) FROM project_learning_records').fetchone()[0])

    def test_unknown_schema14_trigger_refuses_without_changing_original_rows(self):
        directory, database = self.old14()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TRIGGER unexpected AFTER INSERT ON project_learning_imports '
                               'BEGIN UPDATE history_identity SET next_source_sequence=999; END;')
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            ProjectHistory(directory, self.project, allow_schema_upgrade=True)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(14, connection.execute('PRAGMA user_version').fetchone()[0])
            for name, rows in self.old_rows.items():
                self.assertEqual(rows, connection.execute('SELECT * FROM ' + name).fetchall())
