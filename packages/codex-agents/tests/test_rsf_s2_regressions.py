"""S2 full-run regressions using local SQLite owners; no sockets or child processes."""
from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from codex_agents import learning_store, project_cleanup, project_history
from codex_agents.history_contracts import HistoryError
from codex_agents.owned_execution import ExecutionCleanup, OwnedExecutionRegistry
from tests.learning_support import event_record

PROJECT = 'prj_' + '1' * 32


class FenceRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = learning_store.LearningState(self.base / 's')
        self.directory = self.base / 'h'
        self.directory.mkdir(mode=0o700)

    def released_ledger(self):
        """The preceding layout: all project tables, no machine-local epoch."""
        ledger = learning_store.LearningLedger(self.state)
        project = self.project_id = ledger.register_project(self.directory, now_ms=1)
        ledger.append(project, event_record('s2_preserved', observed_at_ms=2), producer='hook', now_ms=2)
        rows = [tuple(row) for row in ledger._connection.execute('SELECT * FROM records')]
        ledger.close()
        with closing(sqlite3.connect(self.state.database)) as connection:
            connection.execute('DROP TABLE runtime_takeover')
            connection.execute('PRAGMA user_version=16')
        return rows

    @requirements('RSF-S2-1', 'RSF-S2-2', 'RSF-S2-4', 'RSF-S2-6')
    def test_schema16_registration_gate_waits_for_writer_then_upgrades_without_record_consent(self):
        rows = self.released_ledger()
        self.assertIsNone(learning_store.ledger_schema_behind(self.state))
        descriptor = os.open(self.state.lock, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(learning_store.LedgerError, 'another learning writer'):
                learning_store.LearningLedger(self.state)
            with closing(sqlite3.connect(self.state.database)) as connection:
                self.assertEqual(16, connection.execute('PRAGMA user_version').fetchone()[0])
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='runtime_takeover'").fetchone())
        finally:
            os.close(descriptor)
        for epoch in (1, 2):
            ledger = learning_store.LearningLedger(self.state)
            try:
                self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])
                self.assertEqual(epoch, ledger._connection.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])
                self.assertEqual(rows, [tuple(row) for row in ledger._connection.execute('SELECT * FROM records')])
                self.assertEqual(0, ledger._connection.execute(
                    "SELECT COUNT(*) FROM stored_files WHERE kind='backup'").fetchone()[0])
                self.assertEqual(self.project_id, ledger.register_project(self.directory))
            finally:
                ledger.close()

    @requirements('RSF-S2-2', 'RSF-S2-6')
    def test_schema16_explicit_upgrade_does_not_request_project_backups(self):
        rows = self.released_ledger()
        with mock.patch.object(learning_store.LearningLedger, '_require_project_migration_backups',
                               side_effect=AssertionError('epoch-only step requested a project backup')):
            ledger = learning_store.LearningLedger(self.state, allow_schema_upgrade=True)
        try:
            self.assertEqual(rows, [tuple(row) for row in ledger._connection.execute('SELECT * FROM records')])
            self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        finally:
            ledger.close()

    @requirements('RSF-S2-2', 'RSF-S2-6')
    def test_record_changing_schema15_still_requires_explicit_upgrade(self):
        self.released_ledger()
        with closing(sqlite3.connect(self.state.database)) as connection:
            connection.execute('PRAGMA user_version=15')
        self.assertEqual(15, learning_store.ledger_schema_behind(self.state))
        with self.assertRaisesRegex(learning_store.LedgerError, 'explicit drained-owner authorization'):
            learning_store.LearningLedger(self.state)

    def owners(self):
        ledger = learning_store.LearningLedger(self.state)
        self.addCleanup(ledger.close)
        history = project_history.ProjectHistory(self.directory, PROJECT)
        self.addCleanup(history.close)
        return ledger, history

    @requirements('RSF-S2-3')
    def test_nested_named_savepoints_preserve_rollback_to_and_outer_transaction(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE savepoint_values(value)')
            for explicit in (False, True):
                with self.subTest(owner=type(owner).__name__, explicit=explicit):
                    db.execute('DELETE FROM savepoint_values')
                    if explicit:
                        db.execute('BEGIN IMMEDIATE')
                    db.execute('SAVEPOINT outer_unit')
                    self.assertTrue(db.in_transaction)
                    db.execute('INSERT INTO savepoint_values VALUES(1)')
                    db.cursor().execute('SAVEPOINT inner_unit')
                    db.execute('INSERT INTO savepoint_values VALUES(2)')
                    db.execute('ROLLBACK TO inner_unit')
                    db.execute('RELEASE SAVEPOINT inner_unit')
                    self.assertEqual([1], [r[0] for r in db.execute('SELECT * FROM savepoint_values')])
                    db.execute('INSERT INTO savepoint_values VALUES(3)')
                    db.execute('ROLLBACK TRANSACTION TO SAVEPOINT outer_unit')
                    self.assertEqual([], list(db.execute('SELECT * FROM savepoint_values')))
                    db.execute('INSERT INTO savepoint_values VALUES(4)')
                    db.execute('RELEASE outer_unit')
                    self.assertEqual(explicit, db.in_transaction)
                    if explicit:
                        db.rollback()
                        self.assertEqual([], list(db.execute('SELECT * FROM savepoint_values')))
                    else:
                        self.assertEqual([4], [r[0] for r in db.execute('SELECT * FROM savepoint_values')])

    @requirements('RSF-S2-3')
    def test_savepoint_release_rechecks_epoch_and_refused_owners_can_rollback_to(self):
        for owner in self.owners():
            db = owner._connection
            epoch = db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0]
            db.execute('SAVEPOINT outer_unit')
            db.execute('SAVEPOINT inner_unit')
            sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
            db.execute('ROLLBACK TO inner_unit')
            self.assertEqual(epoch, db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])
            sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
            with self.assertRaises((learning_store.LedgerError, HistoryError)):
                db.execute('RELEASE outer_unit')
            self.assertFalse(db.in_transaction)
            self.assertEqual(epoch, db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])

    @requirements('RSF-S2-3')
    def test_cleanup_checks_live_custody_before_historical_material(self):
        registry = OwnedExecutionRegistry()
        with mock.patch.object(project_cleanup, '_original_material',
                               side_effect=HistoryError('history_incompatible')) as material:
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                project_cleanup.issue_cleanup(mock.MagicMock(), mock.sentinel.binding,
                                              registry=registry, custody=ExecutionCleanup(registry))
        material.assert_not_called()
