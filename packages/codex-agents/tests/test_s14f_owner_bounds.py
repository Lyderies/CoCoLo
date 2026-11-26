"""S14F / S9-WAIT (decision 388): owner-thread maintenance is bounded in work and in waiting."""
import sqlite3
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from tests import test_learning_store as legacy

from codex_agents import learning_store, runtime_lineage
from codex_agents.runtime_lineage import CUSTODY_PASS_ROWS, CustodyScan, live_ledger_custody

WHY = 'The bound and the exact custody answer are properties of the real SQLite statements and the real owner thread.'
NOW = 2_000_000_000_000
H = lambda n: format(n, '064x')


def history(runs, edges=3, old_runs=()):
    """Minimal real-column tables that the custody statements touch; `runs` rows each, edge i = digest H(i)."""
    db = sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE runtime_lineage(predecessor_digest TEXT PRIMARY KEY, successor_digest TEXT);
CREATE TABLE active_run_capacity(project_id TEXT, handle_digest TEXT, runtime_digest TEXT, released_at_ms INTEGER,
 PRIMARY KEY(project_id,handle_digest));
CREATE TABLE launch_inputs(project_id TEXT, handle_digest TEXT, runtime_digest TEXT, PRIMARY KEY(project_id,handle_digest));
CREATE TABLE host_run_bindings(project_id TEXT, handle_digest TEXT, expires_at_ms INTEGER, PRIMARY KEY(project_id,handle_digest));
CREATE TABLE run_holds(project_id TEXT, kind TEXT, identity TEXT, state TEXT, PRIMARY KEY(project_id,kind,identity));''')
    for i in range(edges):
        db.execute('INSERT INTO runtime_lineage VALUES(?,?)', (H(i), H(i + 1)))
    old = NOW - learning_store.OPEN_REQUEST_RETENTION_MS - 10
    for n in range(runs):  # released long ago, so none is custody
        digest = H(n % edges)
        db.execute('INSERT INTO active_run_capacity VALUES(?,?,?,?)', ('p', H(10_000 + n), digest, old))
        db.execute('INSERT INTO launch_inputs VALUES(?,?,?)', ('p', H(20_000 + n), digest))
        db.execute('INSERT INTO host_run_bindings VALUES(?,?,?)', ('p', H(20_000 + n), old))
    for n, digest in enumerate(old_runs):  # live runs and live launch inputs, spread through the history
        position = (n * 7919) % max(runs, 1)
        db.execute('UPDATE active_run_capacity SET released_at_ms=NULL, runtime_digest=? WHERE rowid=?', (digest, position + 1))
        db.execute('UPDATE host_run_bindings SET expires_at_ms=? WHERE rowid=?', (NOW + 5, position + 1))
        db.execute('UPDATE launch_inputs SET runtime_digest=? WHERE rowid=?', (digest, (position + 3) % runs + 1))
        db.execute('UPDATE host_run_bindings SET expires_at_ms=? WHERE rowid=?', (NOW + 5, (position + 3) % runs + 1))
    return db


def cycle(db, now=NOW, bound=CUSTODY_PASS_ROWS):
    scan, passes, answer = CustodyScan(), 0, None
    while answer is None:
        before = scan.visited
        answer = scan.step(db, now, bound)
        passes += 1
        yield scan, passes, answer, scan.visited - before
        if passes > 10_000:
            raise AssertionError('never converged')


class PagedCustodyTests(unittest.TestCase):
    @requirements('S14F-1', why_not_scenario=WHY)
    def test_S14F_1_each_pass_visits_at_most_the_bound_and_converges_to_the_exact_custody(self):
        live = [H(0), H(2)]  # live runs at spread positions
        db = history(5_000, old_runs=[live[0]] * 4 + [live[1]] * 3)
        exact = live_ledger_custody(db, NOW) & {row[0] for row in db.execute('SELECT predecessor_digest FROM runtime_lineage')}
        self.assertTrue(set(live) <= exact)  # the fixture's live rows are in custody; the scan must match exactly
        steps, answer = [], None
        for scan, passes, answer, visited in cycle(db):
            steps.append(visited)
        self.assertGreater(len(steps), 20)  # 10,000 rows in bounded pages, not one turn
        self.assertLessEqual(max(steps), CUSTODY_PASS_ROWS)
        self.assertEqual(exact, answer)

    @requirements('S14F-1', why_not_scenario=WHY)
    def test_S14F_1_a_live_run_is_never_missed_at_any_position_or_bound(self):
        for bound in (1, 7, 256, 100_000):
            for position_run in range(0, 40, 13):
                db = history(40, edges=2)
                db.execute('UPDATE active_run_capacity SET released_at_ms=NULL, runtime_digest=? WHERE rowid=?', (H(1), position_run + 1))
                db.execute('UPDATE launch_inputs SET runtime_digest=? WHERE rowid=?', (H(0), position_run + 1))
                db.execute('UPDATE run_holds SET state=state')
                db.execute("INSERT INTO run_holds VALUES('p','binding',?,'open')",
                           (db.execute('SELECT handle_digest FROM launch_inputs WHERE rowid=?', (position_run + 1,)).fetchone()[0],))
                *_, answer, _ = list(cycle(db, bound=bound))[-1]
                self.assertEqual({H(0), H(1)}, answer, (bound, position_run))

    @requirements('S14F-1', why_not_scenario=WHY)
    def test_S14F_1_an_edge_set_change_or_a_fallen_rowid_never_releases_a_stale_answer(self):
        db = history(600)
        scan = CustodyScan()
        self.assertIsNone(scan.step(db, NOW, 100))
        db.execute('INSERT INTO runtime_lineage VALUES(?,?)', (H(50), H(51)))  # a new edge mid-cycle
        self.assertEqual(set(), scan.step(db, NOW, 10_000))  # restarted on the new edge set and read it all
        scan = CustodyScan()
        self.assertIsNone(scan.step(db, NOW, 100))
        db.execute('DELETE FROM active_run_capacity WHERE rowid=(SELECT MAX(rowid) FROM active_run_capacity)')
        answer = scan.step(db, NOW, 100_000)
        self.assertIsNone(answer)  # the highest rowid fell: that cycle is discarded, not used to prune

    @requirements('S14F-2', why_not_scenario=WHY)
    def test_S14F_2_a_pass_on_a_large_history_stays_within_the_owner_request_bound(self):
        from codex_agents import ledger_waits
        db = history(60_000, old_runs=[H(0)])
        scan = CustodyScan()
        worst = 0.0
        while True:
            started = time.perf_counter()
            answer = scan.step(db, NOW)
            worst = max(worst, time.perf_counter() - started)
            if answer is not None:
                break
        self.assertLess(worst, ledger_waits.LEDGER_REQUEST_WAIT_MS / 1000 / 8, worst)
        self.assertEqual({H(0)}, answer)

    @requirements('S14F-3', why_not_scenario=WHY)
    def test_S14F_3_the_sweep_uses_the_paged_scan_and_open_still_computes_custody_whole(self):
        fixture = legacy.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        ledger = fixture.ledger
        with mock.patch.object(runtime_lineage, 'live_ledger_custody', side_effect=AssertionError('whole history')):
            ledger.sweep_account_retention(now_ms=NOW)
            self.assertIsInstance(ledger._custody_scan, CustodyScan)
        with mock.patch.object(runtime_lineage, 'live_ledger_custody', wraps=live_ledger_custody) as whole:
            ledger._purge_runtime_lineage(NOW)
            self.assertEqual(1, whole.call_count)


class MaintenanceReaderWaitTests(unittest.TestCase):
    class Stalled:
        def __init__(self):
            self.done, self.cancelled = threading.Event(), 0

        def cancel(self):
            self.cancelled += 1

    @requirements('S9-WAIT', why_not_scenario=WHY)
    def test_S9_WAIT_a_stalled_reader_fails_the_attempt_plainly_and_a_later_attempt_succeeds(self):
        fixture = legacy.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        ledger = fixture.ledger
        reader = self.Stalled()
        ledger._maintenance_readers.add(reader)
        with mock.patch.object(learning_store, 'MAINTENANCE_READER_WAIT_S', .2):
            started = time.monotonic()
            with self.assertRaises(learning_store.LedgerError) as caught:
                ledger._begin_exclusive_maintenance()
            self.assertLess(time.monotonic() - started, 2)
            self.assertIn('try again', str(caught.exception))
            self.assertFalse(ledger._connection.in_transaction)  # the store is unchanged
            self.assertIn(reader, ledger._maintenance_readers)
            self.assertEqual(1, reader.cancelled)
            reader.done.set()  # the worker ends
            ledger._begin_exclusive_maintenance()
            try:
                self.assertTrue(ledger._connection.in_transaction)
                ledger._connection.rollback()
            finally:
                ledger._finish_exclusive_maintenance()
        self.assertNotIn(reader, ledger._maintenance_readers)


if __name__ == '__main__':
    unittest.main()
