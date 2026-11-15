"""Original schema13 staging needs exact replay to recover chunk membership."""
from contextlib import closing
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.learning_support import verification_outcome
from codex_agents import project_history as history
from codex_agents import project_learning_corpus as corpus
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory

PROJECT = 'prj_' + 'a' * 32


class LearningChunkUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / 'history'
        self.directory.mkdir(mode=0o700)
        self.database = self.directory / 'history.sqlite3'
        record = verification_outcome('old13-original')
        raw = canonical_json(record)
        self.records = [dict(content=raw, source='verifier', source_sequence=5,
                             observed_at_ms=record['observed_at_ms'], ingested_at_ms=20,
                             expires_at_ms=None, late=False)]
        digest = corpus.chunk_digest(self.records, [])
        chain = corpus.next_chain_digest(corpus.EMPTY_CHAIN_DIGEST, digest)
        manifest = dict(schema_version=1, scope='learning-record-staging-v1', project_id=PROJECT,
                        source_identity_digest='d' * 64, key_domain_digest='e' * 64,
                        record_count=1, attestation_count=0, total_bytes=len(raw), chain_digest=chain)
        manifest_digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
        self.receipt = dict(import_id='old-import', manifest_digest=manifest_digest, chunk_index=0,
                            chunk_digest=digest, record_count=1, attestation_count=0,
                            total_bytes=len(raw), chain_digest=chain, learning_ready=False)
        schema = getattr(history, '_SCHEMA_V13', history._SCHEMA)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript(schema)
            connection.execute('INSERT INTO history_identity VALUES(?,1,1)', (PROJECT,))
            connection.execute('INSERT INTO project_learning_imports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (PROJECT, 'old-import', manifest_digest, canonical_json(manifest), 1, 1, 0,
                 len(raw), 5, chain, 'staged', 100, 110))
            connection.execute('INSERT INTO project_learning_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (PROJECT, 'old-import', record['record_id'], 'outcome', 'verifier', 5,
                 record['observed_at_ms'], 20, None, 0, raw, hashlib.sha256(raw).hexdigest()))
            connection.execute('INSERT INTO project_learning_import_chunks VALUES(?,?,?,?,?)',
                (PROJECT, 'old-import', 0, digest, canonical_json(self.receipt)))
            connection.execute(f'PRAGMA application_id={history._APPLICATION_ID}')
            connection.execute('PRAGMA user_version=13')
            self.original = self.rows(connection)
        self.database.chmod(0o600)

    @staticmethod
    def rows(connection):
        return {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name)]
                for name in ('history_identity', 'project_learning_imports', 'project_learning_records',
                             'project_learning_attestations', 'project_learning_import_chunks')}

    def test_normal_open_refuses_old_staging_without_changing_it(self):
        store = None
        try:
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                store = ProjectHistory(self.directory, PROJECT)
        finally:
            if store is not None:
                store.close()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(13, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual(self.original, self.rows(connection))

    def test_interrupted_upgrade_rolls_back_then_exact_replay_backfills_only_membership(self):
        store = None
        try:
            with mock.patch.object(history, 'LEARNING_CHUNK_SCHEMA',
                                   'CREATE TABLE chunk_upgrade_cut(x); INVALID SQL;', create=True), \
                    self.assertRaises(HistoryError):
                store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        finally:
            if store is not None:
                store.close()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(13, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='chunk_upgrade_cut'").fetchone())
            self.assertEqual(self.original, self.rows(connection))
        store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        try:
            self.assertEqual(16, store._connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual(self.original, self.rows(store._connection))
            with self.assertRaisesRegex(HistoryError, '^history_learning_incomplete$'):
                store.learning_import_chunk('old-import', 0)
            self.assertEqual(self.receipt, store.stage_learning_import_chunk(
                'old-import', 0, self.records, [], now_ms=1, before_commit=lambda: None))
            restored = store.learning_import_chunk('old-import', 0)
            self.assertEqual(tuple(self.records), restored['records'])
            self.assertEqual((), restored['attestations'])
            self.assertEqual(self.receipt, restored['receipt'])
            self.assertIs(False, restored['receipt']['learning_ready'])
            self.assertEqual(self.original, self.rows(store._connection))
            self.assertEqual(1, store._connection.execute(
                'SELECT COUNT(*) FROM project_learning_chunk_memberships').fetchone()[0])
        finally:
            store.close()

    def test_unknown_old_trigger_refuses_before_any_upgrade_write(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TRIGGER unexpected_chunk_upgrade AFTER INSERT ON project_learning_imports '
                               'BEGIN UPDATE history_identity SET next_source_sequence=999; END;')
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(13, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual(self.original, self.rows(connection))

    def test_revoked_known_replay_rolls_back_new_membership_without_touching_original_rows(self):
        store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        calls = []

        def revoke():
            calls.append(1)
            if len(calls) == 2:
                self.assertEqual(1, store._connection.execute(
                    'SELECT COUNT(*) FROM project_learning_chunk_memberships').fetchone()[0])
                raise HistoryError('history_not_authorized')

        try:
            with self.assertRaisesRegex(HistoryError, '^history_not_authorized$'):
                store.stage_learning_import_chunk('old-import', 0, self.records, [],
                                                  now_ms=1, before_commit=revoke)
            self.assertEqual(2, len(calls))
            self.assertEqual(0, store._connection.execute(
                'SELECT COUNT(*) FROM project_learning_chunk_memberships').fetchone()[0])
            self.assertEqual(self.original, self.rows(store._connection))
            with self.assertRaisesRegex(HistoryError, '^history_learning_incomplete$'):
                store.learning_import_chunk('old-import', 0)
        finally:
            store.close()

    def test_old_receipt_cannot_backfill_membership_over_changed_stored_companions(self):
        store = ProjectHistory(self.directory, PROJECT, allow_schema_upgrade=True)
        mutations = (
            ('source', 'primary'), ('source_sequence', 6), ('ingested_at_ms', 21),
            ('expires_at_ms', 0), ('late', 1), ('content_digest', 'f' * 64),
            ('content', b'private-corrupt-record'), ('content', b'x' * 65_537),
        )
        original = self.original['project_learning_records'][0]
        try:
            for column, value in mutations:
                with self.subTest(column=column, size=len(value) if isinstance(value, bytes) else None):
                    store._connection.execute(
                        'UPDATE project_learning_records SET ' + column + '=?', (value,))
                    corrupted = self.rows(store._connection)
                    try:
                        with self.assertRaisesRegex(HistoryError, '^history_(incompatible|conflict)$') as caught:
                            store.stage_learning_import_chunk('old-import', 0, self.records, [],
                                                              now_ms=1, before_commit=lambda: None)
                        self.assertNotIn('private-corrupt', str(caught.exception))
                        self.assertEqual(corrupted, self.rows(store._connection))
                        self.assertEqual(0, store._connection.execute(
                            'SELECT COUNT(*) FROM project_learning_chunk_memberships').fetchone()[0])
                    finally:
                        store._connection.execute('DELETE FROM project_learning_records')
                        store._connection.execute(
                            'INSERT INTO project_learning_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', original)
            self.assertEqual(self.original, self.rows(store._connection))
        finally:
            store.close()
