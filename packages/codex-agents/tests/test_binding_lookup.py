"""O0b B0-1 (narrowed): the open-time cap reads only live bindings as expired ones pile up (PLAN B0-1;
DESIGN-0.4.0-B0-holds.md revision 3, item 6).

Integration stage: the ledger fixture. Checked by SQLite VM steps on a read-only connection, not by timing.
"""
import unittest

from tests.test_append_lookup import AppendLookupTests

CAP_LOOKUP = ("SELECT handle_digest FROM host_run_bindings WHERE project_id=? AND binding_state='pending' "
              "AND expires_at_ms>? LIMIT 4096")
NOW = 10_000
PROBE_LOOKUP = ("SELECT COUNT(*) FROM (SELECT 1 FROM account_billing_probe_requests WHERE project_id=? "
                "AND state<>'committed' LIMIT 4097)")


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
                (have + 1, have + total, self.project, 1 if expired else 10**15))  # live by any clock

    def probes(self, total, state='committed'):
        """`total` probe requests (and their origins) for this project, in one statement each."""
        connection = self.ledger._connection
        have = connection.execute('SELECT COUNT(*) FROM account_billing_probe_requests WHERE project_id=?',
                                  (self.project,)).fetchone()[0]
        with self.ledger._lock, connection:
            for table, columns, values in (
                    ('account_billing_probe_origins', 'project_id,request_key,parameters_digest,created_at_ms,origin_seal',
                     "?, printf('%064d', i), 'p', 1, 's'"),
                    ('account_billing_probe_requests', 'project_id,request_key,parameters_digest,runtime_digest,'
                     'caller_scope,created_at_ms,deadline_at_ms,state,account_fingerprint,admission_record_id,'
                     'admission_digest,references_json,state_seal',
                     "?, printf('%064d', i), 'p', 'r', 'c', 1, 2, ?, "
                     + ("NULL, NULL, NULL, '[]', 's'" if state == 'committed' else
                        "NULL, NULL, NULL, NULL, 's'" if state == 'reserved' else
                        "printf('%064d', 0), 'rec', printf('%064d', 0), NULL, 's'"))):
                parameters = (have + 1, have + total, self.project) + (() if table.endswith('origins') else (state,))
                connection.execute(f"WITH RECURSIVE n(i) AS (SELECT ? UNION ALL SELECT i+1 FROM n WHERE i<?) "
                                   f"INSERT INTO {table}({columns}) SELECT {values} FROM n", parameters)

    def test_B0_5_the_probe_cap_reads_only_probes_in_progress_as_finished_ones_pile_up(self):
        """Review 040-b0n-code: the probe cap and its status count read only probes in progress."""
        self.probes(1, 'reserved')
        self.probes(1_000)
        small = self.steps(PROBE_LOOKUP, (self.project,))
        self.probes(20_000)
        large = self.steps(PROBE_LOOKUP, (self.project,))
        self.assertLess(large, 2 * small + 200, (small, large))

    def test_B0_status_counts_each_hold_by_its_caps_rule(self):
        """Review 040-b0n-code: mixed states, counted as each cap counts them."""
        self.bindings(1, expired=False)
        self.bindings(3)  # expired: not counted
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("UPDATE host_run_bindings SET binding_state='terminal' WHERE run_id='run-2'")
        self.probes(2, 'reserved')
        self.probes(1, 'admitted')  # past its deadline, still in progress: counted
        self.probes(5)  # finished: not counted
        live = self.ledger.account_capacity(self.project)
        self.assertEqual(1, live['pending_bindings']['live'])  # the fixture has no bindings of its own
        self.assertEqual(3, live['billing_probes']['live'])

    def test_B0_status_accepts_the_earlier_and_the_new_report(self):
        from codex_agents.client_runtime import _status_account_capacity
        old = {'observations': {'live': 1, 'limit': 4096}, 'cleanup_receipts': {'live': 0, 'limit': 4096}}
        new = {**old, 'pending_bindings': {'live': 2, 'limit': 4096}, 'billing_probes': {'live': 0, 'limit': 4096}}
        self.assertEqual(old, _status_account_capacity(old))
        self.assertEqual(new, _status_account_capacity(new))
        self.assertIsNone(_status_account_capacity({**new, 'other': {'live': 0, 'limit': 1}}))
        self.assertIsNone(_status_account_capacity({**old, 'pending_bindings': {'live': -1, 'limit': 4096}}))

    def test_B0_1_the_cap_reads_only_live_bindings_as_expired_ones_pile_up(self):
        self.bindings(1, expired=False)
        self.bindings(1_000)
        small = self.steps(CAP_LOOKUP, (self.project, NOW))
        self.bindings(20_000)
        large = self.steps(CAP_LOOKUP, (self.project, NOW))
        self.assertLess(large, 2 * small + 200, (small, large))

    def test_B0_1_a_ledger_without_the_indexes_gets_them_on_open(self):
        self.reopen()
        for name in ('host_run_bindings_live', 'billing_probes_in_progress'):
            self.ledger._connection.execute(f'DROP INDEX IF EXISTS {name}')
        self.reopen()
        names = {row[0] for row in self.ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertLessEqual({'host_run_bindings_live', 'billing_probes_in_progress'}, names)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited AP tests run in test_append_lookup."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(BindingLookupTests):
        if name in BindingLookupTests.__dict__:
            suite.addTest(BindingLookupTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
