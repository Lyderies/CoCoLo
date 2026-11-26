"""S14F / S9-WAIT (decision 388): owner-thread maintenance is bounded in work and in waiting."""
import contextlib
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


def edge_map(db):
    return dict(db.execute('SELECT predecessor_digest,successor_digest FROM runtime_lineage').fetchall())


def whole_keys(db, now):
    """The whole-scan answer the paged one must cover: custody restricted to lineage edge keys."""
    return live_ledger_custody(db, now) & edge_map(db).keys()


def must_keep(db, now, current):
    """The edges the whole computation protects at `now`: every edge key reachable from custody before the current
    runtime (a reachability search written independently of the code's walk)."""
    edges, keep = edge_map(db), set()
    frontier = [d for d in live_ledger_custody(db, now) if d in edges and d != current]
    while frontier:
        digest = frontier.pop()
        if digest in keep:
            continue
        keep.add(digest)
        following = edges[digest]
        if following in edges and following != current:
            frontier.append(following)
    return keep


def checked_advance(test, db, scan, now, current, bound=CUSTODY_PASS_ROWS, race=None):
    """One owner pass that may prune; nothing the whole computation protects at that moment may be deleted.

    `race` (S14F-8) commits from another connection after the pass-start checks, just before the owner takes the
    write lock, exactly like the r2c interleaving; what the whole computation protects then must survive too.
    """
    keep, before = must_keep(db, now, current), edge_map(db).keys()
    transaction = None
    if race is not None:
        @contextlib.contextmanager
        def transaction():
            race()
            keep.update(must_keep(db, now, current))
            db.execute('BEGIN IMMEDIATE')
            try:
                yield
                db.commit()
            except BaseException:
                db.rollback()
                raise
    removed = scan.advance(db, now, current, bound=bound, transaction=transaction)
    deleted = before - edge_map(db).keys()
    test.assertFalse(deleted & keep, (now, sorted(deleted & keep)))
    test.assertEqual(len(deleted), removed)
    test.assertLessEqual(scan.work, bound)
    return removed


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
        # S14F-6 (r1 LOW): a delete only shrinks custody, so the cycle continues and equals the whole answer;
        # a row that reappears behind the cursor is caught by the change watch (test_S14F_6_*), not by max rowids.
        self.assertEqual(whole_keys(db, NOW), answer)

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


RET = learning_store.OPEN_REQUEST_RETENTION_MS
CURRENT = H(999_999)


def one_edge(rows=257):
    """The r1 reproduction: edge A->current, `rows` released history rows of A (and launch inputs)."""
    db = history(rows, edges=1)
    db.execute('UPDATE runtime_lineage SET successor_digest=?', (CURRENT,))
    return db


class CustodyClockTests(unittest.TestCase):
    @requirements('S14F-4', why_not_scenario=WHY)
    def test_S14F_4_a_clock_rollback_mid_cycle_never_completes_short_of_the_whole_custody(self):
        a = H(0)
        db = one_edge()
        db.execute('UPDATE active_run_capacity SET released_at_ms=? WHERE rowid=1', (NOW - RET - 1,))
        self.assertNotIn(a, whole_keys(db, NOW))  # not custody at T ...
        self.assertIn(a, whole_keys(db, NOW - 2))  # ... but custody at T-2 ms
        scan, clock, answer = CustodyScan(), iter([NOW] + [NOW - 2] * 50), None
        while answer is None:
            now = next(clock)
            answer = scan.step(db, now)
        self.assertTrue(whole_keys(db, now) <= answer, (answer, now))
        # and through the pruning owner pass: A's edge is never deleted while T-2 custody names it
        db = one_edge()
        db.execute('UPDATE active_run_capacity SET released_at_ms=? WHERE rowid=1', (NOW - RET - 1,))
        scan = CustodyScan()
        checked_advance(self, db, scan, NOW, CURRENT)
        for _ in range(20):
            checked_advance(self, db, scan, NOW - 2, CURRENT)
        self.assertIn(a, edge_map(db))

    @requirements('S14F-4', why_not_scenario=WHY)
    def test_S14F_4_a_forward_jump_keeps_the_cycle_start_cutoff_and_still_converges(self):
        a = H(0)
        db = one_edge()
        db.execute('UPDATE active_run_capacity SET released_at_ms=? WHERE rowid=257', (NOW - RET + 5,))
        self.assertIn(a, whole_keys(db, NOW))
        self.assertNotIn(a, whole_keys(db, NOW + 10))
        scan, answer, passes = CustodyScan(), None, 0
        clock = iter([NOW] + [NOW + 10 + n for n in range(50)])
        while answer is None:
            now = next(clock)
            answer = scan.step(db, now)
            passes += 1
        self.assertLess(passes, 10)  # a forward clock never restarts the cycle
        self.assertIn(a, answer)  # judged at the cycle-start cutoff: the more protective one
        self.assertTrue(whole_keys(db, now) <= answer)
        # pruning: kept through the cycle, released by the next cycle once it truly ended
        scan = CustodyScan()
        for n in range(30):
            checked_advance(self, db, scan, NOW + (0 if n == 0 else 10 + n), CURRENT)
        self.assertNotIn(a, edge_map(db))


def chain(edges, live_from, runs=0):
    """A long overlapping chain H(i)->H(i+1) ending at CURRENT; every digest from `live_from` has a live run."""
    db = history(runs, edges=1)
    db.execute('DELETE FROM runtime_lineage')
    for i in range(edges):
        db.execute('INSERT INTO runtime_lineage VALUES(?,?)', (H(i), H(i + 1) if i + 1 < edges else CURRENT))
    for i in range(live_from, edges):
        db.execute('INSERT INTO active_run_capacity VALUES(?,?,?,NULL)', ('p', H(50_000 + i), H(i)))
    return db


class CustodyWorkTests(unittest.TestCase):
    @requirements('S14F-5', why_not_scenario=WHY)
    def test_S14F_5_every_pass_bounds_its_total_work_on_a_long_overlapping_chain(self):
        from codex_agents import ledger_waits
        db = chain(2_000, live_from=1_000, runs=300)
        expected = edge_map(db).keys() & must_keep(db, NOW, CURRENT)
        self.assertEqual(1_000, len(expected))
        scan, passes, worst, removed = CustodyScan(), 0, 0.0, 0
        while scan.cycles == 0:
            started = time.perf_counter()
            removed += checked_advance(self, db, scan, NOW, CURRENT)
            worst = max(worst, time.perf_counter() - started)
            passes += 1
            self.assertLess(passes, 1_000)
        self.assertGreater(passes, 15)  # 2,000 edges read, 2,600 rows, 1,000 walked, 2,000 judged: many passes
        self.assertLess(worst, ledger_waits.LEDGER_REQUEST_WAIT_MS / 1000 / 8, worst)
        self.assertEqual(1_000, removed)
        self.assertEqual(expected, edge_map(db).keys())  # equals the whole-scan answer

    @requirements('S14F-5', why_not_scenario=WHY)
    def test_S14F_5_the_whole_prune_walks_each_edge_once_on_overlapping_chains(self):
        db = chain(4_000, live_from=0)
        custody = live_ledger_custody(db, NOW)
        started = time.perf_counter()
        self.assertEqual(0, runtime_lineage.prune_lineage(db, custody, CURRENT))
        self.assertLess(time.perf_counter() - started, 0.25)  # linear, not 8 million steps
        self.assertEqual(4_000, len(edge_map(db)))


def file_copy(test, memory, name):
    """A real database file (two connections share it, unlike :memory:)."""
    import os
    import shutil
    import tempfile
    from tests.support import TEMP_ROOT
    directory = tempfile.mkdtemp(dir=TEMP_ROOT)
    test.addCleanup(shutil.rmtree, directory, True)
    path = os.path.join(directory, name)
    target = sqlite3.connect(path, isolation_level=None)
    memory.backup(target)
    target.close()
    memory.close()
    return path


class CountingSet(set):
    """Counts the items any full iteration (a copy such as list(s)) walks."""
    iterated = 0

    def __iter__(self):
        for item in super().__iter__():
            CountingSet.iterated += 1
            yield item


class EdgeMap(dict):
    """A weak-referenceable edge map, to see when the scan lets it go."""


class CustodyRaceAndResetTests(unittest.TestCase):
    @requirements('S14F-8', why_not_scenario=WHY)
    def test_S14F_8_another_connection_committing_custody_before_the_write_lock_keeps_the_edge(self):
        a = H(0)
        for mode in ('wal', 'delete'):
            memory = one_edge(40)
            memory.commit()
            path = file_copy(self, memory, mode + '-race.db')
            db = sqlite3.connect(path, isolation_level=None)
            other = sqlite3.connect(path, isolation_level=None)
            self.addCleanup(db.close)
            self.addCleanup(other.close)
            db.execute('PRAGMA journal_mode=' + mode)
            self.assertNotIn(a, whole_keys(db, NOW))  # no custody: the cycle will want to delete A's edge

            def race():  # r2c: the pass-start checks have passed; now another connection commits a live run of A
                other.execute('INSERT INTO active_run_capacity VALUES(?,?,?,NULL)', ('p', H(66_666), a))

            raced = []

            def once():  # called by the owner's transaction hook: only the deleting pass reaches it
                if not raced:
                    raced.append(True)
                    race()

            scan = CustodyScan()
            for _ in range(50):
                checked_advance(self, db, scan, NOW, CURRENT, bound=30, race=once)
                if scan.cycles:
                    break
            self.assertTrue(raced)
            self.assertIn(a, edge_map(db))  # kept by the abandoned prune and by the restarted cycle
            self.assertEqual(1, scan.cycles)

    @requirements('S14F-9', why_not_scenario=WHY)
    def test_S14F_9_the_completion_walk_never_copies_the_custody_keys(self):
        db = chain(2_000, live_from=0)  # 2,000 custody keys, every one an edge key
        scan = CustodyScan()
        while scan.stage in ('edges', 'rows'):
            checked_advance(self, db, scan, NOW, CURRENT)
            if scan.stage == 'rows' and not isinstance(scan.found, CountingSet):
                scan.found = CountingSet(scan.found)  # instrument before history completes
        self.assertIsInstance(scan.found, CountingSet)
        cycle_sized = {'edges', 'order', 'found', 'protected', 'drop'}  # held across passes, drained within budgets
        while scan.cycles == 0:
            CountingSet.iterated = 0
            before = len(scan.found)
            checked_advance(self, db, scan, NOW, CURRENT)
            self.assertLessEqual(CountingSet.iterated + scan.work, CUSTODY_PASS_ROWS, CountingSet.iterated)
            if scan.found is not None and scan.cycles == 0:
                self.assertLessEqual(before - len(scan.found), CUSTODY_PASS_ROWS)
            copies = {name: len(value) for name, value in vars(scan).items()
                      if name not in cycle_sized and isinstance(value, (list, set, dict, tuple))
                      and len(value) > CUSTODY_PASS_ROWS}
            self.assertEqual({}, copies)  # no per-pass copy of the custody keys (r2c: list(self.found))
        self.assertEqual(2_000, len(edge_map(db)))

    @requirements('S14F-9', why_not_scenario=WHY)
    def test_S14F_9_a_reset_never_discards_cycle_sized_containers_in_one_pass(self):
        import weakref
        for how in ('invalidated', 'completed'):
            db = chain(2_000, live_from=0)
            scan = CustodyScan()
            while scan.stage in ('edges', 'rows'):
                checked_advance(self, db, scan, NOW, CURRENT)
            scan.edges = EdgeMap(scan.edges)  # a cycle-sized container (2,000 edges)
            old = weakref.ref(scan.edges)
            self.assertEqual(2_000, len(old()))
            if how == 'invalidated':
                db.execute('INSERT INTO runtime_lineage VALUES(?,?)', (H(70_000), H(70_001)))  # watch fires
            else:
                while scan.cycles == 0:
                    checked_advance(self, db, scan, NOW, CURRENT)
            sizes = []
            for _ in range(40):  # the old container drains at most a budget's worth per pass
                checked_advance(self, db, scan, NOW, CURRENT)
                sizes.append(len(old()) if old() is not None else 0)
                if not sizes[-1]:
                    break
            self.assertGreater(sizes[0], 0, how)  # not dropped wholesale in the resetting pass
            self.assertEqual(0, sizes[-1], how)  # but drained in the end


class CustodyInvalidationTests(unittest.TestCase):
    def remaining_bound(self, db, used):
        return 3 + 2 * db.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0] - used

    @requirements('S14F-6', why_not_scenario=WHY)
    def test_S14F_6_a_delete_and_reinsert_behind_the_cursor_restoring_the_max_rowid_is_still_found(self):
        db = history(600)
        scan = CustodyScan()
        self.assertIsNone(scan.step(db, NOW, 3 + 600 + 100))  # active_run_capacity done, launch_inputs part-way
        top = db.execute('SELECT MAX(rowid) FROM active_run_capacity').fetchone()[0]
        db.execute('DELETE FROM active_run_capacity WHERE rowid=?', (top,))
        db.execute('INSERT INTO active_run_capacity VALUES(?,?,?,NULL)', ('p', H(77_777), H(1)))
        self.assertEqual(top, db.execute('SELECT MAX(rowid) FROM active_run_capacity').fetchone()[0])
        answer = None
        while answer is None:
            answer = scan.step(db, NOW, 10_000)
        self.assertEqual({H(1)}, answer)
        self.assertEqual(whole_keys(db, NOW), answer)

    @requirements('S14F-6', why_not_scenario=WHY)
    def test_S14F_6_a_vacuum_or_another_connection_commit_mid_cycle_discards_the_cycle(self):
        import os
        import shutil
        import tempfile
        from tests.support import TEMP_ROOT
        directory = tempfile.mkdtemp(dir=TEMP_ROOT)
        self.addCleanup(shutil.rmtree, directory, True)
        for change in ('vacuum', 'other connection'):
            path = os.path.join(directory, change.replace(' ', '-') + '.db')
            db = history(600)
            db.commit()
            target = sqlite3.connect(path, isolation_level=None)
            db.backup(target)
            db.close()
            db = target
            scan = CustodyScan()
            self.assertIsNone(scan.step(db, NOW, 100))
            if change == 'vacuum':
                db.execute('VACUUM')
            else:
                other = sqlite3.connect(path, isolation_level=None)
                other.execute("INSERT INTO run_holds VALUES('q','probe','x','held')")
                other.close()
            # exactly the rest of the old cycle's work: a cycle that was not discarded would complete here
            self.assertIsNone(scan.step(db, NOW, self.remaining_bound(db, 100)), change)
            answer = None
            while answer is None:
                answer = scan.step(db, NOW)
            self.assertEqual(whole_keys(db, NOW), answer, change)
            db.close()

    @requirements('S14F-6', why_not_scenario=WHY)
    def test_S14F_6_ledger_maintenance_vacuum_resets_the_owner_scan(self):
        fixture = legacy.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        ledger = fixture.ledger
        ledger.sweep_account_retention(now_ms=NOW)
        scan = ledger._custody_scan
        scan.start = NOW  # as if part-way through a cycle
        ledger._begin_exclusive_maintenance()
        ledger._connection.rollback()
        ledger._finish_exclusive_maintenance()
        self.assertIsNone(scan.start)


class CustodyPropertyTests(unittest.TestCase):
    """Random interleavings between passes; whenever a pass prunes, it keeps everything the whole answer keeps."""

    def interleave(self, seed, paged_prune):
        import random
        rng = random.Random(seed)
        memory = history(0, edges=1)
        memory.execute('DELETE FROM runtime_lineage')
        memory.commit()
        path = file_copy(self, memory, f'interleave-{seed}-{paged_prune}.db')
        db = sqlite3.connect(path, isolation_level=None)
        other = sqlite3.connect(path, isolation_level=None)  # S14F-8: a second real connection on the same file
        self.addCleanup(db.close)
        self.addCleanup(other.close)
        db.execute('PRAGMA journal_mode=WAL')  # as the ledger; no fsync (test speed only)
        for connection in (db, other):
            connection.execute('PRAGMA synchronous=OFF')
        digests = [H(500 + n) for n in range(8)]
        for n in range(5):
            db.execute('INSERT INTO runtime_lineage VALUES(?,?)', (digests[n], digests[n + 1]))
        clock, serial, scan, race = NOW, 0, CustodyScan(), None

        def other_commit():
            nonlocal serial
            serial += 1
            other.execute('INSERT INTO active_run_capacity VALUES(?,?,?,NULL)',
                          ('p', H(80_000 + serial), rng.choice(digests)))

        def near():
            return clock - RET + rng.randint(-6, 6)

        for _ in range(250):
            for _ in range(rng.randint(0, 3)):
                serial += 1
                op = rng.randrange(12)
                digest = rng.choice(digests + [CURRENT])
                if op == 0:
                    db.execute('INSERT INTO active_run_capacity VALUES(?,?,?,?)',
                               ('p', H(90_000 + serial), digest, rng.choice([None, near()])))
                elif op == 1:
                    db.execute('UPDATE active_run_capacity SET released_at_ms=? WHERE rowid=(SELECT rowid FROM '
                               'active_run_capacity WHERE released_at_ms IS NULL ORDER BY random() LIMIT 1)', (clock,))
                elif op == 2:
                    db.execute('DELETE FROM active_run_capacity WHERE rowid=(SELECT MAX(rowid) FROM active_run_capacity)')
                    db.execute('INSERT INTO active_run_capacity VALUES(?,?,?,NULL)', ('p', H(90_000 + serial), digest))
                elif op == 3:
                    db.execute('DELETE FROM active_run_capacity WHERE rowid=(SELECT rowid FROM active_run_capacity '
                               'ORDER BY random() LIMIT 1)')
                elif op == 4:
                    handle = H(95_000 + serial)
                    db.execute('INSERT INTO launch_inputs VALUES(?,?,?)', ('p', handle, digest))
                    db.execute('INSERT INTO host_run_bindings VALUES(?,?,?)', ('p', handle, near()))
                elif op == 5:
                    db.execute('UPDATE host_run_bindings SET expires_at_ms=? WHERE rowid=(SELECT rowid FROM '
                               'host_run_bindings ORDER BY random() LIMIT 1)', (near(),))
                elif op == 6:
                    row = db.execute('SELECT handle_digest FROM launch_inputs ORDER BY random() LIMIT 1').fetchone()
                    if row:
                        db.execute("INSERT OR REPLACE INTO run_holds VALUES('p',?,?,?)",
                                   (rng.choice(['binding', 'run_slot', 'probe']), row[0],
                                    rng.choice(['held', 'released'])))
                elif op == 7 and len(digests) < 12:
                    new = H(500 + len(digests))
                    db.execute('INSERT OR IGNORE INTO runtime_lineage VALUES(?,?)', (rng.choice(digests), new))
                    digests.append(new)
                elif op == 8:
                    clock += rng.randint(-5, 5)
                elif op == 9 and rng.random() < .2:
                    db.execute('VACUUM')
                elif op == 10:
                    other_commit()  # between passes
                elif op == 11:
                    race = other_commit  # inside the next pass, after its checks, before its write lock
            bound = rng.randint(1, 12)
            if paged_prune:
                checked_advance(self, db, scan, clock, CURRENT, bound, race=race)
                race = None
            else:
                answer = scan.step(db, clock, bound)
                if answer is not None:
                    self.assertTrue(whole_keys(db, clock) <= answer, (seed, clock, whole_keys(db, clock) - answer))
        if paged_prune:  # quiet: two whole cycles later the paged prune equals the whole one
            completed = scan.cycles
            for _ in range(5_000):
                if scan.cycles >= completed + 2:
                    break
                checked_advance(self, db, scan, clock, CURRENT, 7)
            self.assertGreaterEqual(scan.cycles, completed + 2)
            self.assertEqual(edge_map(db).keys(), edge_map(db).keys() & must_keep(db, clock, CURRENT), seed)

    @requirements('S14F-7', why_not_scenario=WHY)
    def test_S14F_7_random_interleavings_never_let_the_paged_answer_fall_short_of_the_whole(self):
        for seed in range(12):
            self.interleave(seed, paged_prune=False)
            self.interleave(seed, paged_prune=True)


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
