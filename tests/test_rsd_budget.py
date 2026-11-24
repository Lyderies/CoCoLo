"""RS-D-9 S2/S3: production query work at zero and 100,000 retired tombstones.

The real store emits the SQL. Only its selection statements are replayed against
synthetic rows: these tests measure SQLite work, independently of file I/O,
unrelated purge passes, and service scheduling. No service boundary is changed.
"""
from contextlib import ExitStack
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(1, str(ROOT / 'packages/codex-agents/native-package/.codex/orchestration'))

from test_framework import requirements
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents import project_result_contexts
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

PROJECT = 'prj_' + 'a' * 32
OUTCOME = 'a' * 64
WHY = ('SQLite query selection is a store component contract; real store methods supply the statements, '
       'and progress handlers and query plans count work without service or filesystem timing.')


def production_queries(store):
    """Capture both cursor shapes from actual calls, with no patched product path."""
    statements = []
    store._connection.set_trace_callback(statements.append)
    try:
        store._result_expiry_cursor = 0
        store.expire_results()
        store._context_sweep_cursor = 0
        project_result_contexts.sweep(store, 10)
        try:
            store.stored_worker_context(OUTCOME, host_class='codex')
        except HistoryError as exc:
            if exc.reason_code != 'history_not_authorized':
                raise
    finally:
        store._connection.set_trace_callback(None)
    return {
        'retirement': list(dict.fromkeys(sql for sql in statements
                                         if sql.startswith('SELECT rowid FROM result_reservations ')
                                         and "state!='expired' AND CASE" in sql)),
        'lookup': list(dict.fromkeys(sql for sql in statements
                                     if sql.startswith('SELECT * FROM project_result_contexts INDEXED BY '
                                                       'project_result_contexts_outcome '))),
        'sweep': list(dict.fromkeys(sql for sql in statements
                                    if sql.startswith('SELECT c.rowid,c.run_handle,c.attempt '))),
    }


def seed(connection, tombstones):
    """One transaction, bulk inserts; history precedes live rows to expose LIMIT scans."""
    def identities():
        for n in range(tombstones):
            yield n, True
        for n in range(100_000, 100_010):
            yield n, False  # identical live identities in both stores

    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.executemany(
            'INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,'
            'orchestration_handle,binding,assignment_digest,created_at_ms,hard_deadline_at_ms,'
            'reserved_until_ms,expires_at_ms,state) VALUES(?,?,?,1,?,?,?, ?,0,1,1,1,?)',
            ((PROJECT, f'claim-{n}', f'run-{n}', f'{n:064x}', 'orchestration', b'{}', 'digest',
              'expired' if retired else 'committed') for n, retired in identities()))
        connection.executemany(
            'INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,'
            'lease_deadline_ms,outcome_digest) VALUES(?,?,1,?,0,1,?)',
            ((PROJECT, f'context-{n}', 'expired' if retired else 'retiring', f'{n:064x}')
             for n, retired in identities()))
        connection.executemany(
            'INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,release_kind) '
            "VALUES(?,'result_custody',?,'released',0,0,'retention')",
            ((PROJECT, f'context-{n}:1') for n, _ in identities()))
        connection.execute(
            'INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,'
            "lease_deadline_ms,outcome_digest) VALUES(?,'worker',1,'recorded',1,1,?)", (PROJECT, OUTCOME))
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise


def measured(connection, sql):
    # Warm statement preparation so the handler counts execution only, one VM instruction per tick.
    connection.execute(sql).fetchall()
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        rows = [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.set_progress_handler(None, 0)
    return steps, rows


class RsdBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.databases = {}
        for count in (0, 100_000):
            directory = Path(cls.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
            store = ProjectHistory(directory, PROJECT, result_key=b'r' * 32,
                                   result_capacity=ResultCapacity(16, 16 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                                   result_clock_ms=lambda: 1000)
            cls.resources.callback(store.close)
            queries = production_queries(store)
            seed(store._connection, count)
            cls.databases[count] = (store._connection, queries)

    def equal_work(self, operation, expected_rows):
        measurements = []
        for count, (connection, queries) in self.databases.items():
            self.assertEqual(1 if operation == 'lookup' else 2, len(queries[operation]))
            current = []
            for sql in queries[operation]:
                steps, rows = measured(connection, sql)
                self.assertEqual(expected_rows, len(rows), (operation, count, sql))
                self.assertGreater(steps, 0)
                current.append(steps)
            measurements.append(current)
        self.assertEqual(*measurements, (operation, 'VM steps at 0 and 100000 tombstones', measurements))

    def partial_plans(self, operation, table):
        for count, (connection, queries) in self.databases.items():
            partial = {row[1] for row in connection.execute('PRAGMA index_list(' + table + ')') if row[4]}
            for sql in queries[operation]:
                plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql)]
                used = {match.group(1) for detail in plan
                        if (match := re.search(r'USING (?:COVERING )?INDEX (\w+)', detail))}
                self.assertTrue(partial & used, (operation, count, 'partial index required', plan))
                for detail in plan:
                    if re.search(r'\bSCAN (?:result_reservations|project_result_contexts|c|s)\b', detail):
                        self.assertTrue(any(name in detail for name in partial), (operation, count, plan))

    @requirements('RS-D-9-S2', why_not_scenario=WHY)
    def test_S2_retirement_equal_work(self):
        self.equal_work('retirement', 10)

    @requirements('RS-D-9-S2', why_not_scenario=WHY)
    def test_S2_lookup_equal_work(self):
        self.equal_work('lookup', 1)

    @requirements('RS-D-9-S2', why_not_scenario=WHY)
    def test_S2_sweep_equal_work(self):
        self.equal_work('sweep', 10)

    @requirements('RS-D-9-S3', why_not_scenario=WHY)
    def test_S3_retirement_partial_plan(self):
        self.partial_plans('retirement', 'result_reservations')

    @requirements('RS-D-9-S3', why_not_scenario=WHY)
    def test_S3_lookup_partial_plan(self):
        self.partial_plans('lookup', 'project_result_contexts')

    @requirements('RS-D-9-S3', why_not_scenario=WHY)
    def test_S3_sweep_partial_plan(self):
        self.partial_plans('sweep', 'project_result_contexts')


if __name__ == '__main__':
    unittest.main()
