"""S14g: use production SQL and real schemas, never a simplified index fixture."""
import ast
import json
import os
from pathlib import Path
import re
import unittest

import tests.support  # noqa: F401
from test_framework import requirements
from tests import test_learning_store as account_fixture, test_result_leases as project_fixture
from codex_agents import account_lost_run, project_history, project_takeover, project_holds_schema

WHY = 'SQLite query plans and retained hold semantics require the real account and project schemas.'
FULL_KEY = 'project_id=? AND kind=? AND identity=?'


def literals(module):
    return [(n.lineno, n.value) for n in ast.walk(ast.parse(Path(module.__file__).read_text()))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def query(module, starts):
    found = [sql for _, sql in literals(module) if sql.startswith(starts)]
    if len(found) != 1:
        raise AssertionError((starts, found))
    return found[0]


def plan(db, sql, params=()):
    return [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql, params)]


class Recorder:
    def __init__(self, db):
        self.db, self.calls = db, []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.db.execute(sql, params)


class HoldQueryTests(unittest.TestCase):
    def setUp(self):
        fixture = project_fixture.ResultLeaseTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.db = self.store._connection

    def assert_full_key(self, sql, params, alias='project_holds'):
        steps = plan(self.db, sql, params)
        hold = [s for s in steps if re.match(r'(?:SCAN|SEARCH) ' + alias + r'\b', s)]
        self.assertTrue(hold, steps)
        for step in hold:
            self.assertTrue(step.startswith('SEARCH'), steps)
            self.assertIn(FULL_KEY, step, steps)

    @requirements('S14G-1', why_not_scenario=WHY)
    def test_S14G_1_custody_classification_read_and_write_use_full_key(self):
        for prefix in ('SELECT evidence FROM project_holds WHERE', 'UPDATE project_holds SET evidence=? WHERE'):
            sql = query(project_history, prefix + " kind='result_custody'") if any(
                s.startswith(prefix + " kind='result_custody'") for _, s in literals(project_history)) else query(
                    project_history, prefix + " project_id=? AND kind='result_custody'")
            with self.subTest(sql=sql):
                self.assert_full_key(sql, (None,) * sql.count('?'))

    @requirements('S14G-1', why_not_scenario=WHY)
    def test_S14G_1_takeover_report_reservation_uses_full_key(self):
        sql = next(s for _, s in literals(project_takeover) if s.startswith('SELECT * FROM project_holds WHERE')
                   and "kind='reservation'" in s)
        self.assert_full_key(sql, (None,) * sql.count('?'))

    @requirements('S14G-1', why_not_scenario=WHY)
    def test_S14G_1_takeover_retention_correlations_use_full_key(self):
        recorder = Recorder(self.db)
        project_takeover.purge_takeover(recorder, 2_000_000_000_000, transaction=self.store._transaction)
        reads = [(s, p) for s, p in recorder.calls if 'FROM project_holds h' in s or 'JOIN project_holds h' in s]
        self.assertEqual(2, len(reads))
        for sql, params in reads:
            self.assert_full_key(sql, params, 'h')

    @requirements('S14G-1', why_not_scenario=WHY)
    def test_S14G_1_source_driven_joins_use_full_key(self):
        for sql, params in ((project_history._LAST_ATTEMPT_ACTIVITY, {'project': self.store.project_id, 'handle': 'x'}),
                            (project_holds_schema._REGISTER_INCONSISTENT[4], ())):
            with self.subTest(sql=sql):
                self.assert_full_key(sql, params, 'h' if 'project_holds h' in sql else 'r')

    @requirements('S14G-1', 'S14G-3', why_not_scenario=WHY)
    def test_S14G_1_takeover_adoption_reads_only_the_processed_kinds_via_live_index(self):
        sql = query(project_takeover, "SELECT * FROM project_holds WHERE state='releasing'")
        steps = plan(self.db, sql)
        self.assertTrue(any('project_holds_kind_due (kind=? AND state=?)' in step for step in steps), steps)
        self.assertIn("kind IN ('lease','reservation')", sql)

    @requirements('S14G-1', 'S14G-3', why_not_scenario=WHY)
    def test_S14G_3_takeover_lookup_seeks_both_full_keys_and_preserves_answers(self):
        recorder = Recorder(self.db)
        project_takeover.takeover_left_unverified(recorder, self.store.project_id, 'run', 1, 'claim')
        sql, params = recorder.calls[-1]
        self.assert_full_key(sql, params, 'h')
        # Same project can have both identities in multiple kinds; wrong kind/identity pairs must not match.
        old = ("SELECT 1 FROM project_holds h WHERE h.project_id=? AND ((h.kind='reservation' AND h.identity=?) "
               "OR (h.kind='lease' AND h.identity=?)) AND " + project_takeover.unverified_hold_condition('h') + ' LIMIT 1')
        self.db.execute("INSERT INTO takeover_record VALUES('takeover','old','0','new','1','adopted',1,0,0)")
        for kind, identity in (('lease', 'run:1'), ('reservation', 'claim'), ('lease', 'claim'), ('reservation', 'run:1')):
            for state in ('held', 'releasing', 'released'):
                with self.subTest(kind=kind, identity=identity, state=state):
                    self.db.execute('DELETE FROM project_holds')
                    self.db.execute('DELETE FROM takeover_obligations')
                    self.db.execute('INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,lease_case,release_kind) '
                                    'VALUES(?,?,?,?,1,?,?)', (self.store.project_id, kind, identity, state,
                                    'armed' if kind == 'lease' else None,
                                    ('denied' if kind == 'lease' else 'lost') if state == 'released' else None))
                    self.db.execute('INSERT INTO takeover_obligations VALUES(?,?,?,?,?,?)',
                                    (kind, identity, 'takeover', 'request', 'unverified', 'digest'))
                    expected = self.db.execute(old, (self.store.project_id, 'claim', 'run:1')).fetchone() is not None
                    self.assertEqual(expected, project_takeover.takeover_left_unverified(
                        self.db, self.store.project_id, 'run', 1, 'claim'))
                    self.assertEqual(state == 'releasing' and (kind, identity) in
                                     (('lease', 'run:1'), ('reservation', 'claim')), expected)

    @requirements('S14G-2', why_not_scenario=WHY)
    def test_S14G_2_account_identity_queries_search_full_key(self):
        fixture = account_fixture.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        db = fixture.ledger._connection
        root = Path(account_lost_run.__file__).parent
        checked = 0
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                sql = node.value
                if not sql.startswith('SELECT ') or 'FROM run_holds' not in sql or 'INDEXED BY run_holds_due' in sql:
                    continue
                if not re.search(r'\b(?:r\.)?identity\s*=', sql) or sql.endswith(('WHERE ', 'AND ')):
                    continue
                # Complete literal statements; composed statements are audited separately in the table.
                try:
                    params = {name: 1 for name in re.findall(r':(\w+)', sql)} if ':' in sql else (None,) * sql.count('?')
                    steps = plan(db, sql, params)
                except Exception as error:
                    self.fail((path.name, node.lineno, sql, error))
                hold = [s for s in steps if re.match(r'(?:SCAN|SEARCH) (?:run_holds|r)\b', s)]
                self.assertTrue(hold, (sql, steps))
                for step in hold:
                    self.assertIn(FULL_KEY, step, (path.name, node.lineno, steps))
                checked += 1
        self.assertGreaterEqual(checked, 15)


class LiveIndexTests(unittest.TestCase):
    """S14G-4: held/releasing reads must be able to use the partial index project_holds_kind_due (state<>'released')."""

    def setUp(self):
        fixture = project_fixture.ResultLeaseTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.db = self.store._connection

    def assert_live_index(self, sql, params=()):
        steps = plan(self.db, sql, params)
        hold = [s for s in steps if re.match(r'(?:SCAN|SEARCH) project_holds\b', s)]
        self.assertTrue(hold, steps)
        for step in hold:
            self.assertIn('project_holds_kind_due', step, steps)

    @requirements('S14G-4', why_not_scenario=WHY)
    def test_S14G_4_takeover_finished_custody_scan_uses_live_index(self):
        sql = query(project_takeover, "SELECT evidence FROM project_holds WHERE kind='result_custody'")
        self.assert_live_index(sql)
        self.assertIn("state='held'", sql)

    @requirements('S14G-4', why_not_scenario=WHY)
    def test_S14G_4_next_hold_due_reads_use_live_index(self):
        recorder = Recorder(self.db)
        project_holds_schema.next_hold_due(recorder)
        reads = [(s, p) for s, p in recorder.calls if 'FROM project_holds' in s]
        self.assertGreaterEqual(len(reads), 5)
        for sql, params in reads:
            with self.subTest(sql=sql):
                self.assertIn("state<>'released'", sql)
                steps = plan(self.db, sql, params)
                hold = [s for s in steps if re.match(r'(?:SCAN|SEARCH) project_holds\b', s)]
                self.assertTrue(hold, steps)
                for step in hold:  # the open-binding retry (state-only) has project_holds_due; the rest project_holds_kind_due
                    self.assertRegex(step, r'USING (?:COVERING )?INDEX project_holds_(?:kind_)?due', steps)

    @requirements('S14G-4', why_not_scenario=WHY)
    def test_S14G_4_next_hold_due_answers_are_unchanged(self):
        rows = []
        for n, (kind, state, due, check) in enumerate((
                ('lease', 'held', 50, None), ('lease', 'releasing', 30, None), ('lease', 'released', 5, None),
                ('reservation', 'held', 40, None), ('reservation', 'released', 2, None),
                ('orchestration', 'held', 60, 90), ('orchestration', 'released', 1, 1),
                ('open_binding', 'releasing', None, 20), ('open_binding', 'released', None, 3))):
            rows.append((self.store.project_id, kind, 'id%d' % n, state, 1, due, check or 0,
                         'armed' if kind == 'lease' else None, {'lease': 'denied', 'reservation': 'lost', 'orchestration': 'closed',
                                                   'open_binding': 'withdrawn'}[kind] if state == 'released' else None))
        self.db.execute('DELETE FROM project_holds')
        for row in rows:
            self.db.execute('INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,due_at_ms,next_check_ms,'
                            'lease_case,release_kind) VALUES(?,?,?,?,?,?,?,?,?)', row)
        self.assertEqual(20, project_holds_schema.next_hold_due(self.db))
        self.db.execute("DELETE FROM project_holds WHERE kind='open_binding'")
        self.assertEqual(30, project_holds_schema.next_hold_due(self.db))
        self.db.execute("DELETE FROM project_holds WHERE state='releasing'")
        self.assertEqual(40, project_holds_schema.next_hold_due(self.db))


class AuditInventoryTests(unittest.TestCase):
    @requirements('S14G-2', why_not_scenario=WHY)
    def test_S14G_2_real_schema_inventory(self):
        from tests.hold_query_audit import inventory
        report = inventory()
        if os.environ.get('COCOLO_S14G_DUMP_PLANS'):
            print('S14G_PLANS=' + json.dumps(report))
        self.assertEqual([], report['unresolved'])
        self.assertGreater(len(report['queries']), 100)
        recorded = json.loads((Path(__file__).resolve().parents[3] /
                               'docs/requirements/s14g/plans.json').read_text())
        current = {(r['file'], r['sql']): r for r in report['queries']}
        self.assertEqual({(r['file'], r['sql']) for r in recorded['queries']}, set(current))
        pinned = 0
        for expected in recorded['queries']:
            actual = current[expected['file'], expected['sql']]
            aliases = {step.split()[1] for step in expected['plan'] if FULL_KEY in step}
            for alias in aliases:
                steps = [step for step in actual['plan'] if re.match(r'(?:SCAN|SEARCH) ' + alias + r'\b', step)]
                self.assertTrue(steps, actual)
                for step in steps:
                    self.assertIn(FULL_KEY, step, actual)
                pinned += len(steps)
        self.assertGreater(pinned, 100)
