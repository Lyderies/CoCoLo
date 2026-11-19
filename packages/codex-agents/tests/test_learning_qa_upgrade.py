"""Exact schema12 upgrades preserve staged data without granting authority."""
from contextlib import closing
import sqlite3
import unittest
from unittest import mock

from tests import test_project_learning_corpus as fixtures
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents import project_history


class LearningQACorpusUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectLearningCorpusTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.begin()
        self.base.chunk()
        self.summary = self.base.store.finish_learning_import(
            'import-1', now_ms=120, before_commit=lambda: None)
        connection = self.base.store._connection
        # Construct a separate exact old image. Keep the current source image
        # intact, including membership metadata that did not exist in schema12.
        self.original = {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name)]
                         for name in ('history_identity', 'project_learning_imports',
                                      'project_learning_records', 'project_learning_attestations',
                                      'project_learning_import_chunks')}
        self.base.store.close()
        directory = self.base.directory.parent / 'schema12-history'
        directory.mkdir(mode=0o700)
        database = directory / 'history.sqlite3'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(project_history._SCHEMA_V12)
            for name, rows in self.original.items():
                for row in rows:
                    connection.execute('INSERT INTO ' + name + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
            connection.execute(f'PRAGMA application_id={project_history._APPLICATION_ID}')
            connection.execute('PRAGMA user_version=12')
            self.schema = connection.execute(
                'SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name').fetchall()
        database.chmod(0o600)
        self.base.directory = directory

    def read_original(self, connection):
        for name, rows in self.original.items():
            self.assertEqual(rows, [tuple(row) for row in connection.execute('SELECT * FROM ' + name)])

    def test_normal_open_cannot_upgrade_staged_v12_data(self):
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            ProjectHistory(self.base.directory, fixtures.PROJECT)
        with closing(sqlite3.connect(self.base.directory / 'history.sqlite3')) as connection, connection:
            self.assertEqual(12, connection.execute('PRAGMA user_version').fetchone()[0])
            self.read_original(connection)

    def test_unknown_v12_trigger_refuses_before_any_upgrade_write(self):
        with closing(sqlite3.connect(self.base.directory / 'history.sqlite3')) as connection, connection:
            connection.execute('CREATE TRIGGER unexpected_qa_upgrade AFTER INSERT ON project_learning_imports '
                               'BEGIN UPDATE history_identity SET next_source_sequence=999; END;')
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            ProjectHistory(self.base.directory, fixtures.PROJECT, allow_schema_upgrade=True)
        with closing(sqlite3.connect(self.base.directory / 'history.sqlite3')) as connection, connection:
            self.assertEqual(12, connection.execute('PRAGMA user_version').fetchone()[0])
            self.read_original(connection)
            self.assertIsNotNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='unexpected_qa_upgrade'").fetchone())

    def test_explicit_upgrade_rolls_back_failure_then_preserves_every_original_row(self):
        with mock.patch.object(project_history, 'LEARNING_QA_SCHEMA',
                               'CREATE TABLE qa_migration_cut(x); INVALID SQL;', create=True), \
                self.assertRaises(HistoryError):
            ProjectHistory(self.base.directory, fixtures.PROJECT, allow_schema_upgrade=True)
        with closing(sqlite3.connect(self.base.directory / 'history.sqlite3')) as connection, connection:
            self.assertEqual(12, connection.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual(self.schema, connection.execute(
                'SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name').fetchall())
            self.read_original(connection)
        store = ProjectHistory(self.base.directory, fixtures.PROJECT, allow_schema_upgrade=True)
        try:
            self.assertEqual(17, store._connection.execute('PRAGMA user_version').fetchone()[0])
            self.read_original(store._connection)
            self.assertEqual(self.summary, store.finish_learning_import(
                'import-1', now_ms=999, before_commit=lambda: None))
            self.assertIs(False, self.summary['learning_ready'])
            for name in ('project_learning_qa_imports', 'project_learning_qa_records',
                         'project_learning_qa_chunks'):
                self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0])
        finally:
            store.close()
        normal = ProjectHistory(self.base.directory, fixtures.PROJECT)
        normal.close()
