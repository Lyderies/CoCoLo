from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.learning_support import event_record, verification_outcome
from tests.test_project_learning_identity import fixture_outcome
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.project_history import ProjectHistory
from codex_agents import project_history as history

PROJECT = 'prj_' + 'a' * 32


class LearningOutcomePageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'history'
        self.directory.mkdir(mode=0o700)
        self.store = ProjectHistory(self.directory, PROJECT)
        self.addCleanup(self.store.close)

    def append(self, index, *, wide=False):
        outcome = verification_outcome('snapshot-' + str(index))
        if wide:
            outcome['evidence_digests'] = [f'{i:064x}' for i in range(64)]
            outcome['skill_digests'] = [f'{i + 100:064x}' for i in range(64)]
            configuration = {name: outcome[name] for name in (
                'model', 'effort', 'role', 'topology', 'max_parallel', 'verification_required',
                'verifier_digest', 'bounds_digest', 'package_digest', 'policy_digest', 'agent_digest',
                'skill_digests', 'prompt_digest', 'client_digest',
            )}
            outcome['configuration_digest'] = content_digest(configuration)
        return fixture_outcome(self.store, outcome, 1000 + index)

    def test_exact_bytes_and_sequence_pagination_without_owner_json_or_hash_work(self):
        entries = []
        for index in range(70):
            self.store.append_record(canonical_json(event_record('noise-' + str(index))), source='hook', ingested_at_ms=1000)
            entries.append(self.append(index, wide=True))
        expected = tuple((e.source_sequence, e.record_id, e.content_digest, e.content) for e in entries)
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        with mock.patch.object(history, 'parse_bounded_json', side_effect=AssertionError('writer decoded bulk JSON')), \
                mock.patch.object(history, 'canonical_json', side_effect=AssertionError('writer encoded bulk JSON')), \
                mock.patch.object(history.hashlib, 'sha256', side_effect=AssertionError('writer hashed bulk data')):
            revision = self.store.learning_outcome_revision()
            seen, after = [], 0
            while True:
                page = self.store.learning_outcome_page(revision, after_sequence=after)
                if not page:
                    break
                self.assertLessEqual(len(page), 32)
                self.assertLessEqual(sum(len(item[3]) for item in page), history.MAX_LEARNING_PAGE_BYTES)
                self.assertFalse(self.store._connection.in_transaction)
                seen.extend(page)
                after = page[-1][0]
        self.store._connection.set_trace_callback(None)
        self.assertEqual(expected, tuple(seen))
        self.assertEqual(entries[-1].source_sequence, revision)
        self.assertFalse(any(s.startswith(('BEGIN', 'COMMIT')) for s in statements))
        self.assertTrue(all('INDEXED BY records_learning_outcomes' in s for s in statements))

    def test_unrelated_activity_and_exact_replay_preserve_revision_but_new_outcome_conflicts(self):
        self.assertEqual(0, self.store.learning_outcome_revision())
        self.assertEqual((), self.store.learning_outcome_page(0))
        first = self.append(0)
        revision = self.store.learning_outcome_revision()
        self.store.append_record(canonical_json(event_record('new-event')), source='hook', ingested_at_ms=2000)
        self.assertEqual(revision, self.store.learning_outcome_revision())
        self.assertEqual(first, self.append(0))
        self.assertEqual(revision, self.store.learning_outcome_revision())
        self.assertEqual(first.content, self.store.learning_outcome_page(revision)[0][3])
        self.append(1)
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.store.learning_outcome_page(revision)

    def test_bounded_index_seek_owner_affinity_and_no_fallback_if_index_disappears(self):
        self.append(0)
        revision = self.store.learning_outcome_revision()
        for options in ({'after_sequence': -1}, {'after_sequence': True}, {'after_sequence': revision + 1},
                        {'limit': True}, {'limit': 0}, {'limit': 33}):
            with self.assertRaisesRegex(HistoryError, '^history_invalid_input$'):
                self.store.learning_outcome_page(revision, **options)
        with self.assertRaisesRegex(HistoryError, '^history_invalid_input$'):
            self.store.learning_outcome_page(True)
        errors = []
        def foreign():
            for method in (self.store.learning_outcome_revision, lambda: self.store.learning_outcome_page(revision)):
                try:
                    method()
                except HistoryError as error:
                    errors.append(error.reason_code)
        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        self.assertEqual(['history_wrong_owner'] * 2, errors)
        with self.store._transaction():
            with self.assertRaisesRegex(HistoryError, '^history_result_unavailable$'):
                self.store.learning_outcome_page(revision)
            self.assertEqual(revision, self.store.learning_outcome_revision())
        self.store._connection.execute('DROP INDEX records_learning_outcomes')
        with self.assertRaisesRegex(HistoryError, '^history_unavailable$') as caught:
            self.store.learning_outcome_page(revision)
        self.assertIsNone(caught.exception.__context__)

    def test_invalid_blob_storage_classes_or_oversized_blob_are_refused_before_conversion(self):
        entry = self.append(0)
        revision = self.store.learning_outcome_revision()
        for value in (1 << 60, 'SYNTHETIC-PRIVATE', b'x' * 65_537, b''):
            self.store._connection.execute('UPDATE records SET content=? WHERE record_id=?', (value, entry.record_id))
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.store.learning_outcome_page(revision)
        self.store._connection.execute('UPDATE records SET content=? WHERE record_id=?', (entry.content, entry.record_id))
        self.assertEqual(entry.content, self.store.learning_outcome_page(revision)[0][3])

    def test_raw_view_does_not_validate_or_upgrade_record_and_migration_authority(self):
        entry = self.append(0)
        raw = b'{"CANARY":"synthetic corrupt record"}'
        self.store._connection.execute('UPDATE records SET content=? WHERE record_id=?', (raw, entry.record_id))
        page = self.store.learning_outcome_page(self.store.learning_outcome_revision())
        self.assertEqual((entry.source_sequence, entry.record_id, entry.content_digest, raw), page[0])
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(), page[0][2])
        # The worker must check these expected bytes/digests; a raw page is not
        # a complete corpus, verified provenance or an available learning cycle.


class LearningOutcomePageUpgradeTests(unittest.TestCase):
    def test_exact_v11_upgrade_requires_drained_owner_and_rolls_back_failed_index_creation(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            directory = Path(temporary) / 'history'
            directory.mkdir(mode=0o700)
            database = directory / 'history.sqlite3'
            connection = sqlite3.connect(database)
            connection.executescript(history._SCHEMA_V11)
            record = verification_outcome('old-v11-outcome')
            raw = canonical_json(record)
            original = (PROJECT, record['record_id'], 'outcome', 'verifier', 1, 1, 10,
                        raw, hashlib.sha256(raw).hexdigest(), 0)
            connection.execute('INSERT INTO history_identity VALUES(?,2,1)', (PROJECT,))
            connection.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)', original)
            connection.execute(f'PRAGMA application_id={history._APPLICATION_ID}')
            connection.execute('PRAGMA user_version=11')
            connection.commit()
            connection.close()
            database.chmod(0o600)
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                ProjectHistory(directory, PROJECT)
            with mock.patch.object(history, 'LEARNING_OUTCOME_INDEX', 'CREATE TABLE index_cut(x); INVALID SQL;'), \
                    self.assertRaises(HistoryError):
                ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            connection = sqlite3.connect(database)
            self.assertEqual(11, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='index_cut'").fetchone())
            self.assertEqual(original, connection.execute('SELECT * FROM records').fetchone())
            connection.close()
            store = ProjectHistory(directory, PROJECT, allow_schema_upgrade=True)
            try:
                self.assertEqual(20, store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20
                self.assertEqual(original, tuple(store._connection.execute('SELECT * FROM records').fetchone()))
                self.assertEqual(1, store.learning_outcome_revision())
                self.assertEqual(raw, store.learning_outcome_page(1)[0][3])
            finally:
                store.close()
