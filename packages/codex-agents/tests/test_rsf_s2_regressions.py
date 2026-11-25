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
from codex_agents import learning_store, project_cleanup, project_history, store_fence
from codex_agents.history_contracts import HistoryError
from codex_agents.owned_execution import ExecutionCleanup, OwnedExecutionRegistry
from codex_agents.sql_read_budget import SQLReadError, SQLReadLimits
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
    def test_with_select_reports_read_budget_without_checking_fence(self):
        sql = ('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) '
               'SELECT SUM(x) FROM n')
        limits = SQLReadLimits(max_callbacks=1, max_duration_ms=1000, max_value_bytes=65_536)
        for owner in (learning_store.LearningLedger(self.state),):
            self.addCleanup(owner.close)
            db = owner._connection
            for transaction in (False, True):
                with self.subTest(owner=type(owner).__name__, transaction=transaction):
                    if transaction:
                        db.execute('BEGIN IMMEDIATE')
                    read = db.transaction_read_call if transaction else db.read_call
                    try:
                        with mock.patch.object(owner._fence, 'check', wraps=owner._fence.check) as check:
                            with self.assertRaises(SQLReadError) as caught:
                                read(limits, lambda: db.execute(sql).fetchone())
                            self.assertEqual('sql_read_work_limit', caught.exception.reason_code)
                            check.assert_not_called()
                    finally:
                        db.rollback()

    @requirements('RSF-S2-3')
    def test_with_write_keywords_stay_fenced_after_takeover(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE with_values(value)')
            insert = 'WITH n(x) AS (SELECT 1) INSERT INTO with_values SELECT x FROM n'
            db.execute(insert)
            self.assertEqual([1], [row[0] for row in db.execute('SELECT * FROM with_values')])
            db.execute('DELETE FROM with_values')
            sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
            for sql in (insert, *(f"WITH n(x) AS (SELECT '{word}') SELECT x FROM n"
                                  for word in ('insert', 'UPDATE', 'delete', 'REPLACE'))):
                with self.subTest(owner=type(owner).__name__, sql=sql):
                    with self.assertRaisesRegex((learning_store.LedgerError, HistoryError), 'replaced'):
                        db.execute(sql)
                    self.assertFalse(db.in_transaction)
                    self.assertEqual([], list(db.execute('SELECT * FROM with_values')))

    @requirements('RSF-S2-3')
    def test_interrupted_epoch_read_preserves_sqlite_error_for_read_budget(self):
        limits = SQLReadLimits(max_callbacks=1, max_duration_ms=1000, max_value_bytes=65_536)
        for owner in (learning_store.LearningLedger(self.state),):
            self.addCleanup(owner.close)
            db = owner._connection
            interrupted = []

            def epoch_read():
                try:
                    owner._fence.check(db, full=False)
                except sqlite3.OperationalError as error:
                    interrupted.append(error)
                    raise

            with self.subTest(owner=type(owner).__name__), \
                    mock.patch('codex_agents.sql_read_budget.SQL_READ_PROGRESS_INTERVAL', 1):
                with self.assertRaises(SQLReadError) as caught:
                    db.read_call(limits, epoch_read)
                self.assertEqual('sql_read_work_limit', caught.exception.reason_code)
                self.assertEqual(1, len(interrupted))
                self.assertEqual('interrupted', str(interrupted[0]))
                self.assertEqual(sqlite3.SQLITE_INTERRUPT, interrupted[0].sqlite_errorcode)

    @requirements('RSF-S2-3')
    def test_other_sqlite_epoch_read_error_keeps_ownership_refusal(self):
        for owner in self.owners():
            db = owner._connection
            sqlite3.Connection.execute(db, 'DROP TABLE runtime_takeover')
            with self.subTest(owner=type(owner).__name__):
                with self.assertRaisesRegex((learning_store.LedgerError, HistoryError), 'replaced'):
                    owner._fence.check(db, full=False)

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

    @requirements('RSF-S2-6')
    def test_epoch_is_exact_global_metadata_not_project_partitioned_local_data(self):
        self.assertNotIn('runtime_takeover', learning_store.LOCAL_TABLE_COLUMNS)
        self.assertEqual(store_fence.EPOCH_COLUMNS, learning_store.OTHER_TABLE_COLUMNS['runtime_takeover'])

    @requirements('RSF-S2-3')
    def test_sqlite_release_error_preserves_outer_transaction_and_earlier_writes(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE release_values(value)')
            for execute in (db.execute, db.cursor().execute):
                with self.subTest(owner=type(owner).__name__, execute=execute):
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO release_values VALUES(1)')
                    db.execute('SAVEPOINT retained')
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'no such savepoint'):
                        execute('RELEASE missing_savepoint')
                    self.assertTrue(db.in_transaction)
                    self.assertEqual([1], [r[0] for r in db.execute('SELECT * FROM release_values')])
                    db.execute('ROLLBACK TO retained')
                    db.rollback()
                    self.assertEqual([], list(db.execute('SELECT * FROM release_values')))

    @requirements('RSF-S2-3')
    def test_sqlite_commit_error_preserves_deferred_foreign_key_transaction(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE fence_parent(id PRIMARY KEY)')
            db.execute('CREATE TEMP TABLE fence_child(id REFERENCES fence_parent(id) DEFERRABLE INITIALLY DEFERRED)')
            for commit in (db.commit, lambda: db.execute('COMMIT')):
                with self.subTest(owner=type(owner).__name__, commit=commit):
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO fence_parent VALUES(1)')
                    db.execute('INSERT INTO fence_child VALUES(2)')
                    with self.assertRaises(sqlite3.IntegrityError):
                        commit()
                    self.assertTrue(db.in_transaction)
                    self.assertEqual([1], [r[0] for r in db.execute('SELECT * FROM fence_parent')])
                    db.rollback()
                    self.assertEqual([], list(db.execute('SELECT * FROM fence_parent')))

    @requirements('RSF-S2-3')
    def test_maintenance_rechecks_after_epoch_transaction_before_durable_operation(self):
        for owner in self.owners():
            db = owner._connection
            epoch = owner._fence.epoch
            for sql in ('VACUUM', 'PRAGMA journal_mode=DELETE', 'PRAGMA journal_mode(DELETE)'):
                with self.subTest(owner=type(owner).__name__, sql=sql):
                    statements, revoked = [], False
                    def trace(statement):
                        nonlocal revoked
                        statements.append(statement)
                        if statement == 'COMMIT' and not revoked:
                            revoked = True
                            sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
                    db.set_trace_callback(trace)
                    try:
                        with self.assertRaises((learning_store.LedgerError, HistoryError)):
                            db.execute(sql)
                        self.assertTrue(revoked)
                        self.assertNotIn(sql, statements)
                        self.assertFalse(db.in_transaction)
                    finally:
                        db.set_trace_callback(None)
                        sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=?', (epoch,))

    @requirements('RSF-S2-3')
    def test_maintenance_still_checks_after_operation(self):
        for owner in self.owners():
            db = owner._connection
            statements = []
            def trace(statement):
                statements.append(statement)
                if statement == 'PRAGMA journal_mode=DELETE':
                    sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
            db.set_trace_callback(trace)
            try:
                with self.assertRaises((learning_store.LedgerError, HistoryError)):
                    db.execute('PRAGMA journal_mode=DELETE')
                self.assertIn('PRAGMA journal_mode=DELETE', statements)
                self.assertFalse(db.in_transaction)
            finally:
                db.set_trace_callback(None)

    @requirements('RSF-S2-3', 'RSF-S2-6')
    def test_pure_history_reads_skip_fence_probe_and_epoch_queries(self):
        _, history = self.owners()
        statements = []
        history._connection.set_trace_callback(statements.append)
        with mock.patch.object(history._fence, 'check', side_effect=AssertionError('read checked fence')), \
                mock.patch.object(history._fence, 'lock', side_effect=AssertionError('read probed lock')):
            self.assertEqual(0, history.learning_outcome_revision())
            self.assertEqual((), history.learning_outcome_page(0))
            self.assertEqual((), history.pending_terminals())
            self.assertEqual([], history.open_withdrawals())
            self.assertEqual({'ended_by_update_unrecorded': 0}, history.runtime_close_status())
            with self.assertRaisesRegex(HistoryError, '^history_not_found$'):
                history.record('event:missing')
        self.assertFalse(any('runtime_takeover' in sql for sql in statements))

    @requirements('RSF-S2-3', 'RSF-S2-6')
    def test_pure_learning_reads_skip_nested_helper_fences(self):
        from tests.test_project_learning_identity import ProjectLearningIdentityTests
        from tests.test_project_learning_corpus import ProjectLearningCorpusTests
        identity = ProjectLearningIdentityTests()
        identity.setUp()
        self.addCleanup(identity.doCleanups)
        expected = identity.prepare()
        corpus = ProjectLearningCorpusTests()
        corpus.setUp()
        self.addCleanup(corpus.doCleanups)
        corpus.begin()
        corpus.chunk()
        for case, reads in ((identity, (
                (identity.domain, identity.store.learning_key_domain_digest), (expected, identity.prepare))),
                (corpus, ((tuple(corpus.records), lambda: corpus.store.learning_import_records('import-1')),
                          (tuple(corpus.attestations), lambda: corpus.store.learning_import_attestations('import-1'))))):
            statements = []
            case.store._connection.set_trace_callback(statements.append)
            with mock.patch.object(case.store._fence, 'check', side_effect=AssertionError('read checked fence')):
                for expected, read in reads:
                    self.assertEqual(expected, read())
            self.assertFalse(any('runtime_takeover' in sql for sql in statements))

    @requirements('RSF-S2-3')
    def test_500_statement_transaction_uses_two_full_probes_and_checks_epoch_between(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE probe_values(value)')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            unit = owner._write_unit if isinstance(owner, learning_store.LearningLedger) else owner._transaction
            for transaction in (False, True):
                with self.subTest(owner=type(owner).__name__, owner_unit=transaction):
                    statements = []
                    db.set_trace_callback(statements.append)
                    with mock.patch.object(owner._fence, 'lock', wraps=owner._fence.lock) as probes, \
                            mock.patch.object(store_fence.os, 'open', wraps=os.open) as opens:
                        if transaction:
                            with unit():
                                for index in range(500):
                                    if index % 100 == 0:
                                        db.execute('PRAGMA user_version=' + str(version))
                                    db.execute('INSERT INTO probe_values VALUES(?)', (index,))
                        else:
                            db.execute('BEGIN IMMEDIATE')
                            for index in range(500):
                                if index % 100 == 0:
                                    db.execute('PRAGMA user_version=' + str(version))
                                if index == 250:
                                    db.execute('SAVEPOINT middle')
                                db.execute('INSERT INTO probe_values VALUES(?)', (index,))
                            db.execute('RELEASE middle')
                            db.commit()
                    db.set_trace_callback(None)
                    self.assertEqual(2, probes.call_count)
                    self.assertLessEqual(opens.call_count, 2)
                    self.assertGreaterEqual(sum(sql.startswith('SELECT epoch,new_digest') for sql in statements), 502)
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO probe_values VALUES(9999)')
            sqlite3.Connection.execute(db, 'UPDATE runtime_takeover SET epoch=epoch+1')
            with self.assertRaises((learning_store.LedgerError, HistoryError)):
                db.execute('INSERT INTO probe_values VALUES(10000)')
            db.rollback()
            self.assertEqual(1000, db.execute('SELECT COUNT(*) FROM probe_values').fetchone()[0])

    @requirements('RSF-S2-3')
    def test_executescript_does_not_implicitly_commit_owner_transaction(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE script_values(value)')
            for script in (db.executescript, db.cursor().executescript):
                with self.subTest(owner=type(owner).__name__, script=script):
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO script_values VALUES(1)')
                    script("INSERT INTO script_values VALUES('semi;colon'); INSERT INTO script_values VALUES(2);")
                    self.assertTrue(db.in_transaction)
                    self.assertEqual([1, 'semi;colon', 2], [r[0] for r in db.execute('SELECT * FROM script_values')])
                    with self.assertRaises(sqlite3.OperationalError):
                        script('INSERT INTO script_values VALUES(3); INVALID SQL;')
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(4, db.execute('SELECT COUNT(*) FROM script_values').fetchone()[0])
                    db.rollback()
                    self.assertEqual([], list(db.execute('SELECT * FROM script_values')))

    @requirements('RSF-S2-3')
    def test_script_commit_and_outermost_release_probe_lock_before_commit(self):
        for owner in self.owners():
            db = owner._connection
            db.execute('CREATE TEMP TABLE commit_values(value)')
            descriptor = owner._fence.descriptor
            for script in (False, True):
                with self.subTest(owner=type(owner).__name__, script=script):
                    try:
                        if script:
                            db.execute('BEGIN IMMEDIATE')
                        else:
                            db.execute('SAVEPOINT "outer;unit"')
                            db.execute('SAVEPOINT "OUTER;UNIT"')
                            db.execute('RELEASE "OUTER;UNIT"')
                        db.execute('INSERT INTO commit_values VALUES(1)')
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        with self.assertRaises((learning_store.LedgerError, HistoryError)):
                            if script:
                                db.executescript('COMMIT;')
                            else:
                                db.execute('RELEASE "outer;unit";')
                        self.assertFalse(db.in_transaction)
                        self.assertEqual([], list(db.execute('SELECT * FROM commit_values')))
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        db.rollback()
                        db.execute('DELETE FROM commit_values')
