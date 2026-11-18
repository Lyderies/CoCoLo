"""0.3.4 R1g and the lease identity check: the result purge's control flow, on a stand-in store.

The seal checks are replaced by plain values, so these tests show only the order of the purge's
decisions: where its cursor stops, and which rows it deletes (reviews 034-security-2 and -3).
"""

import contextlib
import sqlite3
import unittest
from types import SimpleNamespace

import tests.support  # noqa: F401
from codex_agents.project_history import ProjectHistory


class StandIn:
    project_id = 'prj_' + 'a' * 32

    def __init__(self, reservations, leases=()):
        self._connection = sqlite3.connect(':memory:')
        self._connection.execute('CREATE TABLE result_reservations(project_id,claim_id,state,hard_deadline_at_ms)')
        self._connection.execute('CREATE TABLE result_leases(project_id,claim_id,state,reserved_until_ms,run_handle,attempt)')
        for claim in reservations:
            self._connection.execute("INSERT INTO result_reservations VALUES(?,?,'expired',0)", (self.project_id, claim))
        for claim, run_handle, attempt in leases:
            self._connection.execute("INSERT INTO result_leases VALUES(?,?,'bound',0,?,?)",
                                     (self.project_id, claim, run_handle, attempt))
        self._purge_cursors = {'reservations': None, 'leases': None}
        self.deleted = []

    def _reservation(self, row):
        claim = self._connection.execute('SELECT claim_id FROM result_reservations WHERE rowid=?',
                                         (row[0],)).fetchone()[0] if row is not None else None
        return SimpleNamespace(state='expired', hard_deadline_at_ms=0,
                               binding=SimpleNamespace(run_handle='hrn_' + claim, attempt=1, claim_id=claim))

    def _lease(self, row):
        return SimpleNamespace(state='bound', binding=SimpleNamespace(run_handle=row[4], attempt=row[5]))

    def _orchestration_owner(self):
        return SimpleNamespace(closed_for=lambda binding: True)

    def _attempt_released(self, lease):
        return True

    @contextlib.contextmanager
    def _transaction(self):
        yield

    def _delete_attempt_rows(self, run_handle, attempt, claim_id):
        self.deleted.append(claim_id)
        self._connection.execute('DELETE FROM result_reservations WHERE claim_id=?', (claim_id,))


def purge(store, limit):
    # SELECT rowid,... reads the same rows; the stand-in looks them up by rowid.
    original = store._connection.execute

    class Rows:
        def execute(self, sql, parameters=()):
            if sql.startswith('SELECT * FROM result_reservations'):
                sql = sql.replace('SELECT *', 'SELECT rowid,*')
            return original(sql, parameters)

        def __getattr__(self, name):
            return getattr(original.__self__, name)
    connection, store._connection = store._connection, Rows()
    try:
        return ProjectHistory._purge_finished_results(store, 10_000_000, limit)
    finally:
        store._connection = connection


class ResultPurgeFlowTests(unittest.TestCase):
    def test_a_pass_stopped_by_its_limit_leaves_the_next_row_for_the_next_pass(self):
        """Re-review 034-security-2: the cursor moved onto a row before the limit check, so that row was
        passed over until the table wrapped around."""
        store = StandIn(['clm_1', 'clm_2', 'clm_3'])
        self.assertEqual(1, purge(store, 1))
        self.assertEqual(1, store._purge_cursors['reservations'])
        self.assertEqual(1, purge(store, 1))
        self.assertEqual(['clm_1', 'clm_2'], store.deleted)  # The second pass took the very next row.

    def test_a_lease_of_another_attempt_never_lets_a_reservation_go(self):
        """Review 034-security-3: the lease found by claim must be the reservation's own attempt."""
        store = StandIn(['clm_1', 'clm_2'], leases=[('clm_1', 'hrn_other', 1), ('clm_2', 'hrn_clm_2', 1)])
        self.assertEqual(1, purge(store, 10))
        self.assertEqual(['clm_2'], store.deleted)


if __name__ == '__main__':
    unittest.main()
