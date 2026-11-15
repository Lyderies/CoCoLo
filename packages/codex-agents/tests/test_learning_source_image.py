"""Offline row-image boundaries on disposable original owner databases."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock

from tests import test_learning_store as legacy
from codex_agents import learning_source_image as image
from codex_agents import learning_store as store
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.sql_read_budget import SQLReadLimits


class LearningSourceImageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.ledger.close()
        self.fixture.ledger = store.LearningLedger(self.fixture.state,
            client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.source = self.fixture.ledger
        self.connection = self.source._connection
        self.project = self.fixture.project_a_id
        self.file_id = 'source_image'
        self.limits = SQLReadLimits(100_000, 1000, 2 * 1_048_576)
        self.key = self.fixture.state.digest_key()
        self.domain = keyed_digest(self.key, b'project-learning-key-domain-v1\0' + self.project.encode())
        self.backup = self.source.create_sanitized_backup(self.project, file_id=self.file_id,
            expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)

    def capture(self, **kwargs):
        return image.capture_source_image(self.source, self.project, self.file_id,
            expected_domain_digest=self.domain, work_limits=kwargs.get('limits', self.limits))

    def refuse(self, reason, action=None):
        with self.assertRaises(HistoryError) as caught:
            (self.capture if action is None else action)()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def refresh(self):
        self.file_id += '_next'
        self.backup = self.source.create_sanitized_backup(self.project, file_id=self.file_id,
            expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)

    def rewrite(self, mutate):
        envelope = json.loads(self.backup.read_bytes())
        mutate(envelope['body'])
        envelope['backup_sha256'] = content_digest(envelope['body'])
        raw = json.dumps(envelope, ensure_ascii=False, indent=3).encode()
        self.backup.write_bytes(raw)
        self.connection.execute('UPDATE stored_files SET content_digest=? WHERE project_id=? AND file_id=?',
            (hashlib.sha256(raw).hexdigest(), self.project, self.file_id)).close()
        return envelope['body']

    def test_empty_project_still_queries_all_thirty_tables_and_preserves_settings(self):
        statements = []
        self.connection.set_trace_callback(statements.append)
        before = self.settings()
        changes = self.connection.total_changes
        result = self.capture()
        self.assertEqual(30, len(result['counts']))
        self.assertTrue(all(n == 0 for _, n in result['counts']))
        self.assertEqual(changes, self.connection.total_changes)
        self.assertEqual(before, self.settings())
        self.assertFalse(self.connection.in_transaction)
        self.assertIsNone(self.connection._sql_scope)
        for table in image._KEYS:
            self.assertTrue(any(' FROM ' + table + ' WHERE project_id=' in sql for sql in statements), table)
        self.assertFalse(any('OFFSET' in sql.upper() or 'JSON_' in sql.upper() for sql in statements))

    def settings(self):
        return (self.connection._pragma_integer('query_only'), self.connection._pragma_integer('busy_timeout'),
                self.connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))

    def test_default_owner_refuses_before_any_key_read(self):
        self.source._allow_schema_upgrade = False
        with mock.patch.object(self.source.state, 'digest_key', side_effect=AssertionError('key read')):
            self.refuse('history_not_authorized')

    def test_collector_reader_and_outer_transaction_refuse_without_taking_over(self):
        for attribute in ('_collector_active', '_active_readers'):
            with self.subTest(attribute=attribute), mock.patch.object(self.source, attribute, 1):
                self.refuse('history_unavailable')
        self.connection.execute('BEGIN').close()
        try:
            self.refuse('history_unavailable')
            self.assertTrue(self.connection.in_transaction)
        finally:
            self.connection.rollback()

    def test_wrong_pid_thread_connection_and_closed_owner_refuse(self):
        for origin in ((os.getpid() + 1, threading.current_thread()), (os.getpid(), threading.Thread())):
            with self.subTest(origin=origin), mock.patch.object(self.source, '_connection_origin', origin):
                self.refuse('history_wrong_owner')
        with mock.patch.object(self.connection, '_sql_origin', (os.getpid() + 1, threading.current_thread())):
            self.refuse('history_wrong_owner')
        self.source.close()
        self.refuse('history_unavailable')

    def test_replaced_writer_lock_refuses(self):
        self.source.state.lock.unlink()
        descriptor = os.open(self.source.state.lock, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.refuse('history_unavailable')

    def test_foreign_progress_handler_is_preserved_and_not_invoked(self):
        calls = []
        self.connection.set_progress_handler(lambda: calls.append(1) or 0, 1)
        self.refuse('history_unavailable')
        self.assertEqual([], calls)
        self.assertTrue(self.connection._sql_foreign_progress)
        self.connection.set_progress_handler(None, 0)
        self.assertIs(False, self.capture()['learning_ready'])

    def test_forged_limits_and_bad_identifiers_are_fixed_errors(self):
        invalid = deepcopy(self.limits)
        object.__setattr__(invalid, 'max_callbacks', True)
        self.refuse('history_invalid_input', lambda: self.capture(limits=invalid))
        self.refuse('history_invalid_input', lambda: self.capture(limits=object()))
        for identifier in ('../escape', '', 'a' * 161, '_'):
            self.file_id = identifier
            self.refuse('history_invalid_input')

    def test_global_pending_run_in_another_project_blocks_without_key_io(self):
        self.connection.execute('INSERT INTO host_run_bindings(project_id,handle_digest,run_id,'
            'assignment_digest,task_digest,route_digest,configuration_digest,client_version,'
            'created_at_ms,expires_at_ms,binding_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.fixture.project_b_id, 'a' * 64, 'pending', 'b' * 64, 'c' * 64, 'd' * 64,
             'e' * 64, 'fixture', 1, 2, 'pending')).close()
        with mock.patch.object(self.source.state, 'digest_key', side_effect=AssertionError('key read')):
            self.refuse('history_unavailable')

    def test_global_unreleased_unknown_capacity_is_not_adopted_or_released(self):
        other = self.fixture.project_b_id
        self.connection.execute('INSERT INTO launch_claims VALUES(?,?,?,?,?,?)',
            (other, 'a' * 64, 'unknown', 'legacy_fenced', 1, None)).close()
        self.connection.execute('INSERT INTO active_run_capacity(project_id,handle_digest,run_id,kind,'
            'claimed_at_ms) VALUES(?,?,?,?,?)', (other, 'a' * 64, 'unknown', 'legacy_unknown', 1)).close()
        before = self.connection.total_changes
        self.refuse('history_unavailable')
        self.assertEqual(before, self.connection.total_changes)
        self.assertIsNone(self.connection.execute('SELECT released_at_ms FROM active_run_capacity').fetchone()[0])

    def test_expiry_and_backward_clock_are_not_refreshed(self):
        expiry = self.connection.execute('SELECT expires_at_ms FROM stored_files').fetchone()[0]
        with mock.patch.object(self.source, '_clock_ms', side_effect=[expiry - 1, expiry]):
            self.refuse('history_result_expired')
        with mock.patch.object(self.source, '_clock_ms', side_effect=[100, 99]):
            self.refuse('history_unavailable')
        self.assertEqual(expiry, self.connection.execute('SELECT expires_at_ms FROM stored_files').fetchone()[0])

    def test_null_expiry_keeps_original_registry_meaning(self):
        self.connection.execute('UPDATE stored_files SET expires_at_ms=NULL').close()
        self.assertIs(False, self.capture()['learning_ready'])

    def test_registry_types_and_oversized_path_refuse_before_conversion(self):
        for value in ('x' * 4097, b'backup/path'):
            self.connection.execute('UPDATE stored_files SET relative_path=?', (value,)).close()
            self.refuse('history_incompatible')

    def test_arbitrary_registered_path_is_not_followed(self):
        self.connection.execute('UPDATE stored_files SET relative_path=?', ('../canary',)).close()
        self.refuse('history_unsafe_path')

    def test_symlink_hardlink_fifo_and_wrong_mode_refuse(self):
        raw = self.backup.read_bytes()
        canary = Path(self.fixture.temporary.name) / 'canary'
        canary.write_bytes(raw)
        for kind in ('symlink', 'hardlink', 'fifo', 'mode'):
            self.backup.unlink()
            if kind == 'symlink':
                self.backup.symlink_to(canary)
            elif kind == 'hardlink':
                os.link(canary, self.backup)
            elif kind == 'fifo':
                os.mkfifo(self.backup, 0o600)
            else:
                self.backup.write_bytes(raw)
                self.backup.chmod(0o644)
            with self.subTest(kind=kind):
                self.refuse('history_unsafe_path')
        self.assertEqual(raw, canary.read_bytes())

    def test_oversized_sparse_backup_refuses_before_read(self):
        with self.backup.open('r+b') as handle:
            handle.truncate(image.MAX_BACKUP_BYTES + 1)
        self.refuse('history_learning_capacity')

    def test_digest_corruption_refuses(self):
        self.backup.write_bytes(self.backup.read_bytes() + b' ')
        self.refuse('history_conflict')

    def test_schema_version_columns_and_primary_key_are_exact(self):
        self.connection.execute('PRAGMA user_version=14').close()
        self.refuse('history_incompatible')
        self.connection.execute('PRAGMA user_version=13').close()
        self.connection.execute('ALTER TABLE sessions ADD COLUMN unexpected TEXT').close()
        self.refuse('history_incompatible')

    def test_changed_primary_key_is_not_inferred_from_columns(self):
        self.connection.execute('DROP TABLE sessions').close()
        self.connection.execute('CREATE TABLE sessions(project_id TEXT NOT NULL, session_digest TEXT NOT NULL,'
            'producer TEXT NOT NULL, created_at_ms INTEGER NOT NULL, PRIMARY KEY(project_id,producer,session_digest))').close()
        self.refuse('history_incompatible')

    def test_unknown_partition_table_and_inactive_project_refuse(self):
        self.connection.execute('CREATE TABLE hidden_project_rows(project_id TEXT, content BLOB)').close()
        self.refuse('history_incompatible')
        self.connection.execute('DROP TABLE hidden_project_rows').close()
        self.connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,)).close()
        with mock.patch.object(self.source.state, 'digest_key', side_effect=AssertionError('key read')):
            self.refuse('history_conflict')

    def test_added_deleted_and_changed_central_rows_refuse(self):
        self.connection.execute('INSERT INTO hook_ingestion VALUES(?,?,?,?)', (self.project, 'tool_started', 1, 10)).close()
        self.refresh()
        self.connection.execute('UPDATE hook_ingestion SET last_accepted_at_ms=11').close()
        self.refuse('history_conflict')
        self.connection.execute('DELETE FROM hook_ingestion').close()
        self.refuse('history_conflict')
        self.connection.execute('INSERT INTO hook_ingestion VALUES(?,?,?,?)', (self.project, 'turn_started', 1, 10)).close()
        self.refuse('history_conflict')

    def test_original_row_order_unicode_and_whitespace_are_preserved(self):
        for code in ('tool_started', 'turn_started', 'turn_stop_observed'):
            self.connection.execute('INSERT INTO hook_ingestion VALUES(?,?,?,?)', (self.project, code, 1, 10)).close()
        self.file_id = 'source_\u00e9'
        self.backup = self.source.create_sanitized_backup(self.project, file_id=self.file_id,
            expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        self.refresh()
        body = self.rewrite(lambda b: b['tables']['hook_ingestion'].reverse())
        raw = self.backup.read_bytes()
        result = self.capture()
        self.assertEqual(canonical_json(body), result['body'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['backup_digest'])
        self.assertEqual(raw, self.backup.read_bytes())

    def test_boolean_counts_and_duplicate_original_keys_refuse(self):
        self.rewrite(lambda b: b['counts'].__setitem__('records', False))
        self.refuse('history_incompatible')
        self.connection.execute('INSERT INTO hook_ingestion VALUES(?,?,?,?)', (self.project, 'tool_started', 1, 10)).close()
        self.refresh()
        def duplicate(body):
            body['tables']['hook_ingestion'] *= 2
            body['counts']['hook_ingestion'] = 2
        self.rewrite(duplicate)
        self.refuse('history_unavailable')

    def test_original_manifest_versions_require_exact_integers(self):
        for field, value in (('schema_version', 2.0), ('ledger_schema_version', 13.0)):
            def changed(body):
                body['schema_version'], body['ledger_schema_version'] = 2, 13
                body[field] = value
            self.rewrite(changed)
            with self.subTest(field=field):
                self.refuse('history_incompatible')

    def test_rows_and_bytes_use_keyset_pages_without_losing_unconsumed_rows(self):
        # Synthetic relational metadata tests the row reader; it asserts no
        # retained-file or native execution authenticity for these fixture rows.
        for number in range(70):
            self.connection.execute('INSERT INTO stored_files VALUES(?,?,?,?,?,?,?)',
                (self.project, f'fixture_{number:03d}', 'artifact', str(number) + 'p' * 30_000, 'a' * 64, None, None)).close()
        self.refresh()
        pages = []
        original = image._page
        def observe(*args):
            rows = original(*args)
            pages.append((args[4], len(rows), sum(len(canonical_json(r)) for r in rows)))
            return rows
        with mock.patch.object(image, '_page', side_effect=observe):
            result = self.capture()
        self.assertEqual(71, dict(result['counts'])['stored_files'])
        self.assertTrue(all(n <= 32 and size <= image.PAGE_BYTES for _, n, size in pages))
        self.assertGreater(len([p for p in pages if p[0] == 'stored_files' and p[1]]), 8)
        plan = self.connection.execute('EXPLAIN QUERY PLAN SELECT file_id FROM stored_files '
            'WHERE project_id=? AND file_id>? ORDER BY file_id LIMIT 32', (self.project, 'fixture_032')).fetchall()
        self.assertTrue(any('SEARCH' in row[3] and 'project_id=?' in row[3] for row in plan))

    def test_read_value_budget_failure_restores_settings_and_rolls_back(self):
        for number in range(40):
            self.connection.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                (self.project, str(number).zfill(3) + 'a' * 2000, 'hook', 1)).close()
        self.refresh()
        before = self.settings()
        self.refuse('history_learning_capacity', lambda: self.capture(limits=SQLReadLimits(100_000, 1000, 1024)))
        self.assertEqual(before, self.settings())
        self.assertFalse(self.connection.in_transaction)
        self.assertIsNone(self.connection._sql_scope)
        self.assertIs(False, self.capture()['learning_ready'])

    def test_callback_budget_bounds_global_pending_scan_and_preserves_owner(self):
        for number in range(200):
            self.connection.execute('INSERT INTO host_run_bindings(project_id,handle_digest,run_id,'
                'assignment_digest,task_digest,route_digest,configuration_digest,client_version,'
                'created_at_ms,expires_at_ms,binding_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (self.fixture.project_b_id, str(number).zfill(64), str(number), 'b' * 64, 'c' * 64,
                 'd' * 64, 'e' * 64, 'fixture', 1, 2, 'consumed')).close()
        before = self.settings()
        self.refuse('history_learning_capacity', lambda: self.capture(limits=SQLReadLimits(1, 1000, 1_048_576)))
        self.assertEqual(before, self.settings())
        self.assertFalse(self.connection.in_transaction)
        self.assertIsNone(self.connection._sql_scope)
        self.assertIs(False, self.capture()['learning_ready'])

    def test_deadline_failure_preserves_settings_without_opening_snapshot(self):
        before = self.settings()
        with mock.patch('codex_agents.sql_read_budget._clock_ns', side_effect=[0, 1_000_000_000]):
            self.refuse('history_learning_capacity')
        self.assertEqual(before, self.settings())
        self.assertFalse(self.connection.in_transaction)

    def test_cancellation_closes_snapshot_cursors_and_backup_descriptor(self):
        before = self.settings()
        descriptors = []
        original = image._open_backup
        def opened(*args):
            result = original(*args)
            descriptors.append(result[1])
            return result
        with mock.patch.object(image, '_open_backup', side_effect=opened), \
                mock.patch.object(image, '_page', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.capture()
        self.assertFalse(self.connection.in_transaction)
        self.assertIsNone(self.connection._sql_scope)
        self.assertEqual(before, self.settings())
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_reentrant_clock_mutation_cannot_receive_an_image(self):
        original = self.source._clock_ms
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.connection.execute('INSERT INTO hook_ingestion VALUES(?,?,?,?)',
                    (self.project, 'tool_started', 1, 10)).close()
            return original()
        with mock.patch.object(self.source, '_clock_ms', side_effect=clock):
            self.refuse('history_conflict')

    def test_replaced_backup_and_changed_existing_key_refuse(self):
        original = image._compare
        def changed(*args):
            result = original(*args)
            replacement = self.backup.with_suffix('.replacement')
            replacement.write_bytes(self.backup.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(self.backup)
            return result
        with mock.patch.object(image, '_compare', side_effect=changed):
            self.refuse('history_conflict')
        with mock.patch.object(self.source.state, 'digest_key', side_effect=[self.key, b'x' * 32]):
            self.refuse('history_conflict')
