"""O10 AP: the append's sequence-and-late lookup no longer reads all of its project's records (PLAN AP1
to AP3; DESIGN-0.4.0-AP-append.md revision 2).

Integration stage: the ledger fixture (its set-up starts a process). AP1 is checked by counting SQLite
VM steps on a read-only connection at two project sizes, not by timing or plan text.
"""
import sqlite3
import unittest

from tests import test_learning_store as store_tests
from tests.test_collector_read_scope import event_record

from codex_agents.learning_store import LearningLedger

OLD_LOOKUP = ('SELECT COALESCE(MAX(source_sequence),0), COALESCE(MAX(observed_at_ms),0) '
              'FROM records WHERE project_id=?')


class AppendLookupTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger
        self.project = self.ledger.project_for_root(self.store.project_a)

    def append(self, record_id, project=None, **fields):
        return self.ledger.append(project or self.project, event_record(record_id, **fields), producer='hook')

    def lookups(self):
        """The statements one append runs to find the next sequence and the latest observed time."""
        seen = []
        self.ledger._connection.set_trace_callback(seen.append)
        try:
            self.append(f'probe_{id(seen)}')
        finally:
            self.ledger._connection.set_trace_callback(None)
        return [sql for sql in seen if 'MAX(' in sql.upper() and 'RECORDS' in sql.upper()]

    def steps(self, sql, parameters=()):
        reader = sqlite3.connect(self.store.state.database.as_uri() + '?mode=ro', uri=True)
        try:
            counted = [0]

            def tick():
                counted[0] += 1
                return 0
            reader.set_progress_handler(tick, 1)
            reader.execute(sql, parameters).fetchall()
            return counted[0]
        finally:
            reader.close()

    def fill(self, total):
        """Copies of one record until the project has `total` records (fast, in one statement)."""
        if not self.ledger._connection.execute(
                'SELECT 1 FROM records WHERE project_id=? AND record_id=?', (self.project, 'seed')).fetchone():
            self.append('seed')
        connection = self.ledger._connection
        with self.ledger._lock, connection:
            have, top = connection.execute(
                'SELECT COUNT(*), MAX(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()
            connection.execute(
                'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<?) '
                'INSERT INTO records SELECT project_id, ?||(?+x), record_type, source, ?+x, observed_at_ms+x, '
                'ingested_at_ms, expires_at_ms, content, content_digest, late FROM records, n '
                'WHERE project_id=? AND record_id=?', (total - have, 'fill_', have, top, self.project, 'seed'))

    def test_AP1_the_lookup_reads_an_index_end_however_many_records_the_project_has(self):
        self.fill(1_000)
        small = [self.steps(sql) for sql in self.lookups()]
        old_small = self.steps(OLD_LOOKUP, (self.project,))
        self.fill(20_000)
        large = [self.steps(sql) for sql in self.lookups()]
        old_large = self.steps(OLD_LOOKUP, (self.project,))
        self.assertGreater(old_large, 5 * old_small)  # the negative control: today's lookup grows
        self.assertTrue(small and len(small) == len(large))
        for before, after in zip(small, large):
            self.assertLess(after, 2 * before + 200, (small, large))

    def test_AP2_sequence_and_late_are_exactly_as_before(self):
        def expected(project, observed):
            row = self.ledger._connection.execute(OLD_LOOKUP, (project,)).fetchone()
            return int(row[0]) + 1, int(observed < int(row[1]))  # today's rule, computed the old way

        def check(project, record_id, observed):
            sequence, late = expected(project, observed)
            self.append(record_id, project, observed_at_ms=observed)
            row = self.ledger._connection.execute(
                'SELECT source_sequence, late FROM records WHERE project_id=? AND record_id=?',
                (project, record_id)).fetchone()
            self.assertEqual((sequence, late), (int(row[0]), int(row[1])), (record_id, observed))
            return int(row[1])

        # 8,000 right after 7,000 is late: the maximum (9,000), not the latest record's time, decides.
        lates = [check(self.project, f'ap2_{index}', observed)
                 for index, observed in enumerate((5_000, 9_000, 7_000, 8_000, 9_000, 12_000, 1_000))]
        self.assertEqual(1, lates[3])
        other = self.ledger.project_for_root(self.store.project_b)
        empty = self.ledger._connection.execute(
            'SELECT COUNT(*) FROM records WHERE project_id=?', (other,)).fetchone()[0]
        if empty == 0:  # an empty project: sequence 1, and observed time 0 is not late
            self.assertEqual(0, check(other, 'ap2_empty', 0))
            self.assertEqual(1, self.ledger._connection.execute(
                'SELECT source_sequence FROM records WHERE project_id=? AND record_id=?',
                (other, 'ap2_empty')).fetchone()[0])
        check(other, 'ap2_other_high', 99_000)  # another project's larger maximum does not reach this one
        self.assertEqual(0, check(self.project, 'ap2_after_other', 13_000))

    def test_AP3_a_ledger_without_the_index_gets_it_on_open_and_nothing_else_changes(self):
        connection = self.ledger._connection
        connection.execute('DROP INDEX IF EXISTS records_observed_at')  # as an older version leaves it

        def schema():
            return sorted(tuple(row) for row in self.ledger._connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name != 'records_observed_at'"))
        before, version = schema(), connection.execute('PRAGMA user_version').fetchone()[0]
        self.ledger.close()
        self.ledger = LearningLedger(self.store.state)
        self.store.ledger = self.ledger
        names = {row[0] for row in self.ledger._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='records'")}
        self.assertIn('records_observed_at', names)
        self.assertEqual(version, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, schema())  # AP3: additive, nothing else in the schema changed
        self.assertTrue(self.append('after')['stored'])


if __name__ == '__main__':
    unittest.main()
