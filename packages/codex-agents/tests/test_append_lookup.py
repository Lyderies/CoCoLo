"""O10 AP: an append no longer reads all of its project's records (PLAN AP1 to AP3;
DESIGN-0.4.0-AP-append.md).

Integration stage: the ledger fixture (its set-up starts a process). AP1 is checked by the query plan, not by timing.
"""
import unittest

from tests import test_learning_store as store_tests
from tests.test_collector_read_scope import event_record

from codex_agents import collector_contracts
from codex_agents.learning_store import LearningLedger


class AppendLookupTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger
        self.project = self.ledger.project_for_root(self.store.project_a)

    def append_statements(self):
        seen = []
        self.ledger._connection.set_trace_callback(seen.append)
        try:
            self.ledger.append(self.project, event_record('probe'), producer='hook')
        finally:
            self.ledger._connection.set_trace_callback(None)
        return [sql for sql in seen if 'MAX(' in sql and 'records' in sql]

    def test_AP1_the_append_lookup_reads_only_index_ends(self):
        lookups = self.append_statements()
        self.assertTrue(lookups)
        for sql in lookups:
            plan = [row[-1] for row in self.ledger._connection.execute('EXPLAIN QUERY PLAN ' + sql)]
            reads = [step for step in plan if 'records' in step]
            self.assertTrue(reads, plan)
            for step in reads:  # a covering index answers MAX from its end; anything else reads rows
                self.assertIn('COVERING INDEX', step, plan)

    def test_AP2_sequence_and_late_are_exactly_as_before(self):
        def expected(observed):
            row = self.ledger._connection.execute(
                'SELECT COALESCE(MAX(source_sequence),0), COALESCE(MAX(observed_at_ms),0) FROM records '
                'WHERE project_id=?', (self.project,)).fetchone()
            return int(row[0]) + 1, int(observed < int(row[1]))  # today's rule, computed the old way
        for index, observed in enumerate((5_000, 9_000, 7_000, 9_000, 12_000, 1_000)):
            sequence, late = expected(observed)
            self.ledger.append(self.project, event_record(f'ap2_{index}', observed_at_ms=observed), producer='hook')
            row = self.ledger._connection.execute(
                'SELECT source_sequence, late FROM records WHERE project_id=? AND record_id=?',
                (self.project, f'ap2_{index}')).fetchone()
            self.assertEqual((sequence, late), (int(row[0]), int(row[1])), observed)

    def test_AP3_a_ledger_without_the_index_gets_it_on_open_with_no_schema_change(self):
        self.ledger._connection.execute('DROP INDEX IF EXISTS records_observed_at')  # as an older version leaves it
        version = collector_contracts.LEDGER_SCHEMA_VERSION
        self.ledger.close()
        self.ledger = LearningLedger(self.store.state)
        self.store.ledger = self.ledger
        names = {row[0] for row in self.ledger._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='records'")}
        self.assertIn('records_observed_at', names)
        self.assertEqual(14, version)  # AP3: additive, the schema version is unchanged
        self.assertTrue(self.ledger.append(self.project, event_record('after'), producer='hook')['stored'])


if __name__ == '__main__':
    unittest.main()
