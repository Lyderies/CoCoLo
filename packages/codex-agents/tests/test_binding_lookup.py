"""O0b B0-1 (narrowed): the open-time cap reads only live bindings as expired ones pile up (PLAN B0-1;
DESIGN-0.4.0-B0-holds.md revision 3, item 6).

Integration stage: the ledger fixture. Checked by SQLite VM steps on a read-only connection, not by timing.
"""
import unittest

from tests.test_append_lookup import AppendLookupTests

CAP_LOOKUP = ("SELECT handle_digest FROM host_run_bindings WHERE project_id=? AND binding_state='pending' "
              "AND expires_at_ms>? LIMIT 4096")
NOW = 10_000


class BindingLookupTests(AppendLookupTests):
    def bindings(self, total, *, expired=True):
        """`total` pending bindings for this project, expired (or live), written in one statement."""
        connection = self.ledger._connection
        have = connection.execute('SELECT COUNT(*) FROM host_run_bindings WHERE project_id=?',
                                  (self.project,)).fetchone()[0]
        with self.ledger._lock, connection:
            connection.execute(
                "WITH RECURSIVE n(i) AS (SELECT ? UNION ALL SELECT i+1 FROM n WHERE i<?) "
                "INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) "
                "SELECT ?, printf('%064d', i), 'run-' || i, 'a', 't', 'r', 'c', 'bridge-v1', 1, ?, 'pending' FROM n",
                (have + 1, have + total, self.project, 1 if expired else NOW + 86_400_000))

    def test_B0_1_the_cap_reads_only_live_bindings_as_expired_ones_pile_up(self):
        self.bindings(1, expired=False)
        self.bindings(1_000)
        small = self.steps(CAP_LOOKUP, (self.project, NOW))
        self.bindings(20_000)
        large = self.steps(CAP_LOOKUP, (self.project, NOW))
        self.assertLess(large, 2 * small + 200, (small, large))

    def test_B0_1_a_ledger_without_the_index_gets_it_on_open(self):
        self.reopen()
        self.ledger._connection.execute('DROP INDEX IF EXISTS host_run_bindings_live')
        self.reopen()
        names = {row[0] for row in self.ledger._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='host_run_bindings'")}
        self.assertIn('host_run_bindings_live', names)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited AP tests run in test_append_lookup."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(BindingLookupTests):
        if name in BindingLookupTests.__dict__:
            suite.addTest(BindingLookupTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
