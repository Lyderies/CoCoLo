"""F9: explain the actual release-batch SQL on a ledger built by production schema code."""
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from codex_agents.account_billing_admission import close_expired_reserved
from codex_agents.learning_store import LearningLedger, LearningState


class CapturedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.statements = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        return self.connection.execute(sql, parameters)


class ReleaseBatchQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = LearningState(self.root / 'state')
        self.ledger = LearningLedger(self.state)
        self.addCleanup(lambda: self.ledger.close())
        project_root = self.root / 'project'
        project_root.mkdir()
        self.project = self.ledger.register_project(project_root, now_ms=1)

    def seed_probe(self, identity='reserved', deadline=200, state='reserved'):
        db = self.ledger._connection
        db.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                   (self.project, identity, 'a' * 64, 1, 'origin'))
        admitted = state == 'admitted'
        db.execute('INSERT INTO account_billing_probe_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (self.project, identity, 'a' * 64, 'b' * 64, 'scope', 1, deadline, state,
                    'c' * 64 if admitted else None, 'admission' if admitted else None,
                    'd' * 64 if admitted else None, '{}' if state == 'committed' else None, 'seal'))

    def capture(self, operation):
        captured = CapturedConnection(self.ledger._connection)
        with mock.patch.object(self.ledger, '_connection', captured):
            operation()
        return captured.statements

    def explain(self, name, statements):
        for index, (sql, parameters) in enumerate(statements):
            with self.subTest(method=name, statement=index):
                plan = [row[3] for row in self.ledger._connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters)]
                print(f'{name}[{index}] SQL: {sql}\nPLAN: {plan}', flush=True)
                # EXISTS itself reports SCAN CONSTANT ROW; only table/alias scans are forbidden.
                self.assertFalse([detail for detail in plan if re.search(r'\bSCAN\b', detail)
                                  and detail != 'SCAN CONSTANT ROW'], plan)
                self.assertTrue(any('SEARCH ' in detail and 'USING ' in detail for detail in plan), plan)

    @requirements('RSA3-4')
    def test_account_holds_due_plans(self):
        with mock.patch.object(self.ledger, '_clock_ms', return_value=100), \
                mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=100):
            statements = self.capture(self.ledger.account_holds_due)
        self.assertEqual(2, len(statements))  # empty ledger exercises the orphan fallback too
        self.explain('account_holds_due', statements)

    @requirements('RSA3-4')
    def test_close_expired_reserved_plans(self):
        self.seed_probe()
        db = self.ledger._connection
        db.execute('BEGIN IMMEDIATE')
        try:
            statements = self.capture(lambda: self.assertEqual(1, close_expired_reserved(self.ledger, 1000, 64)))
            self.assertEqual(3, len(statements))  # selection and both direct updates
            self.explain('close_expired_reserved', statements)
        finally:
            db.rollback()

    def batch_snapshot(self, now, limit):
        db = self.ledger._connection
        db.execute('SAVEPOINT batch_rows')
        try:
            with mock.patch.object(self.ledger, '_clock_ms', return_value=now), \
                    mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=100):
                due = self.ledger.account_holds_due()
                released = self.ledger._release_due_locked(now, 100, limit)
                remaining = self.ledger.account_holds_due()
            self.assertEqual(due, bool(released))
            probes = [tuple(row) for row in db.execute(
                'SELECT request_key,state,references_json,state_seal FROM account_billing_probe_requests ORDER BY rowid')]
            holds = [tuple(row) for row in db.execute(
                'SELECT identity,state,generation,release_kind FROM run_holds ORDER BY rowid')]
            return due, released, remaining, probes, holds
        finally:
            db.execute('ROLLBACK TO batch_rows')
            db.execute('RELEASE batch_rows')

    @requirements('RSA3-4', 'RSA3-5')
    def test_additive_index_reopen_preserves_limited_batches_and_due_rows(self):
        # Mixed states, deadlines out of insertion order, and an exact-deadline boundary.
        for identity, deadline, state in [('z-first', 190, 'reserved'), ('a-second', 50, 'reserved'),
                                          ('edge', 200, 'reserved'), ('future', 1000, 'reserved'),
                                          ('done', 90, 'committed'), ('orphan', 80, 'admitted')]:
            self.seed_probe(identity, deadline, state)
        # Stabilize the existing open-time schema reconciliation before comparing the added index.
        self.ledger.close()
        self.ledger = LearningLedger(self.state)
        db = self.ledger._connection
        db.execute('DROP INDEX IF EXISTS billing_probe_reserved')  # same layout as unmodified 7783a899
        version = db.execute('PRAGMA user_version').fetchone()[0]
        old_schema = [tuple(row) for row in db.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
        cases = [(now, limit) for now in (49, 50, 51, 190, 191, 200, 201, 1001, 10**15)
                 for limit in (1, 2, 64)]
        before = {case: self.batch_snapshot(*case) for case in cases}
        self.assertFalse(before[49, 64][0])
        self.assertEqual((True, 3, False), before[201, 64][:3])
        self.assertEqual(['z-first'], [row[0] for row in before[201, 1][3]
                                     if row[1] == 'committed' and row[0] != 'done'])
        self.ledger.close()
        self.ledger = LearningLedger(self.state)  # normal additive schema path on an existing real ledger
        db = self.ledger._connection
        self.assertEqual(version, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(old_schema, [tuple(row) for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name<>'billing_probe_reserved' ORDER BY type,name")])
        self.assertIsNotNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='billing_probe_reserved'").fetchone())
        for case in cases:
            with self.subTest(now=case[0], limit=case[1]):
                self.assertEqual(before[case], self.batch_snapshot(*case))
