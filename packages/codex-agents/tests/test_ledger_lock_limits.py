"""M4b E1a: a time limit on every ledger-lock wait (PLAN E1a-1 to E1a-4; DESIGN-0.4.0-M4b-E1a.md).

Integration stage: the ledger fixture (its set-up starts a process). Contention comes from another
thread holding the ledger lock, coordinated by events; elapsed times are checked against monotonic
bounds with a tolerance.
"""
import ast
import contextlib
import threading
import time
import unittest
from pathlib import Path

from tests import test_learning_store as store_tests
from tests.support import ORCHESTRATION_ROOT
from tests.test_collector_read_scope import event_record

from codex_agents import learning_store
from codex_agents.sql_read_budget import SQLReadError, SQLReadLimits

# E1a-1 and E1a-4 (revision 3b): the only acquisitions that keep waiting, by function. Construction
# and migration take no `with self._lock`; these are close and the collector's start and stop
# bookkeeping. The offline transfer module is not scanned (exempt by E1a-1's recorded change).
KEPT_WAITING = {
    'learning_store.py': {'close', '_release_lock', 'start'},
}
BOUNDED_MODULES = ('learning_store.py', 'account_service.py', 'account_original_open.py',
                   'account_host_open.py', 'account_run_provenance.py', 'learning_source_image.py')


class LedgerLockLimitTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger
        self.project = self.ledger.project_for_root(self.store.project_a)

    def hold(self, release_after=None):
        """Another thread holds the ledger lock until released (or for release_after seconds)."""
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(release_after if release_after is not None else 30)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        return release

    def refused(self, call):
        started = time.monotonic()
        with self.assertRaises(SQLReadError) as raised:
            call()
        self.assertEqual('sql_read_busy', raised.exception.reason_code)
        return time.monotonic() - started

    def stored(self, record_id):
        return self.ledger._connection.execute(
            'SELECT 1 FROM records WHERE project_id=? AND record_id=?', (self.project, record_id)).fetchone()

    def test_E1a_1_a_held_lock_refuses_a_write_at_the_limit_with_nothing_done(self):
        self.hold()
        waited = self.refused(lambda: self.ledger.append(self.project, event_record('busy'), producer='hook'))
        self.assertGreaterEqual(waited, learning_store.LEDGER_LOCK_WAIT_MS / 1000 - 0.05)
        self.assertLess(waited, learning_store.LEDGER_LOCK_WAIT_MS / 1000 + 0.4)
        self.assertIsNone(self.stored('busy'))  # E1a-2: nothing half-done
        self.assertFalse(self.ledger._connection.in_transaction)

    def test_E1a_1_an_account_transaction_is_refused_before_it_begins(self):
        self.hold()
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            def transaction():
                with self.ledger._account_transaction():
                    self.fail('the body must not run')
            self.refused(transaction)
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertFalse([sql for sql in statements if sql.upper().startswith('BEGIN')])  # no BEGIN ran
        self.assertEqual(1, sum(self.ledger.lock_refusals().values()))

    def test_E1a_4_re_entry_by_the_holder_is_immediate_and_never_counted(self):
        with self.ledger._lock:
            self.ledger.append(self.project, event_record('inside'), producer='hook')
        self.assertIsNotNone(self.stored('inside'))
        self.assertEqual({}, self.ledger.lock_refusals())

    def test_E1a_4_a_wait_that_succeeds_in_time_is_todays_behaviour(self):
        release = self.hold()
        threading.Timer(0.2, release.set).start()
        self.ledger.append(self.project, event_record('late-lock'), producer='hook')
        self.assertIsNotNone(self.stored('late-lock'))
        self.assertEqual({}, self.ledger.lock_refusals())

    def test_E1a_3_each_refusal_is_counted_once_by_its_caller_and_readable_while_held(self):
        self.hold()
        self.refused(lambda: self.ledger.append(self.project, event_record('c1'), producer='hook'))
        self.refused(lambda: self.ledger.append(self.project, event_record('c2'), producer='hook'))
        self.refused(lambda: self.ledger.account_capacity(self.project))
        self.assertEqual({'append': 2, 'account_capacity': 1}, self.ledger.lock_refusals())  # read while held

    def test_E1a_1_a_read_budget_over_the_limit_is_capped_and_a_shorter_one_kept(self):
        from tests.test_collector_read_scope import record_lock_timeouts
        self.hold()
        recording = record_lock_timeouts(self, self.ledger._connection)
        for budget_ms, expected in ((1000, learning_store.LEDGER_LOCK_WAIT_MS / 1000), (200, 0.2)):
            with self.subTest(budget_ms=budget_ms):
                limits = SQLReadLimits(max_callbacks=1000, max_duration_ms=budget_ms, max_value_bytes=4096)
                self.refused(lambda: self.ledger._connection.read_call(limits, lambda: None))
                self.assertAlmostEqual(expected, recording.timeouts[-1], places=3)  # the timeout asked for

    def test_E1a_2_a_reader_exit_never_waits_on_the_ledger_and_its_count_recovers(self):
        inside, leave = threading.Event(), threading.Event()
        done = []

        def reader():
            with self.ledger.repository(self.project):
                inside.set()
                leave.wait(10)
            done.append(time.monotonic())
        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(inside.wait(5))
        self.hold()
        asked = time.monotonic()
        leave.set()
        thread.join(5)
        self.assertTrue(done and done[0] - asked < 0.3)  # it did not wait for the held ledger lock
        with self.ledger._readers_lock:
            self.assertEqual(0, self.ledger._active_readers)

    def test_E1a_2_a_refused_reader_entry_leaves_no_count(self):
        self.hold()
        def enter():
            with self.ledger.repository(self.project):
                pass
        self.refused(enter)
        with self.ledger._readers_lock:
            self.assertEqual(0, self.ledger._active_readers)

    def test_E1a_2_a_refused_hold_runs_its_cleanup_before_the_refusal(self):
        self.hold()
        cleaned = []
        def enter():
            with self.ledger._held(on_refused=lambda: cleaned.append(True)):
                self.fail('the body must not run')
        self.refused(enter)
        self.assertEqual([True], cleaned)

    def test_E1a_2_a_refused_result_commit_removes_only_the_file_it_wrote(self):
        """Review 040-m4b-e1a-code: commit_host_result writes the result file before the hold, so its
        hold removes that new file when refused (the bridge fixture is needed to reach it for real; this
        checks the wiring, and the cleanup itself is the same as the commit's existing failure path)."""
        source = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / 'learning_store.py').read_text())
        commit = next(f for f in ast.walk(source) if isinstance(f, ast.FunctionDef) and f.name == 'commit_host_result')
        calls = [node for node in ast.walk(commit) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        written = min(node.lineno for node in calls if node.func.attr == '_write_worker_result')
        # Only the holds after the file is written have something to undo (the earlier ones read).
        holds = [node for node in calls if node.func.attr == '_held' and node.lineno > written]
        self.assertTrue(holds and all(any(k.arg == 'on_refused' for k in hold.keywords) for hold in holds))

    def test_E1a_1_a_sweep_cycle_gives_up_at_its_deadline_and_counts_once(self):
        server = learning_store.CollectorServer(self.ledger)
        self.hold()
        swept = []
        for name in ('sweep_worker_results', 'sweep_event_retention', 'sweep_account_retention'):
            setattr(self.ledger, name, lambda name=name: swept.append(name))
        started = time.monotonic()
        server._sweep_cycle()
        waited = time.monotonic() - started
        self.assertLess(waited, learning_store.LEDGER_LOCK_WAIT_MS / 1000 + 0.05)  # never past it
        self.assertEqual([], swept)
        self.assertEqual({'sweeps': 1}, self.ledger.lock_refusals())

    def test_E1a_3_refusals_are_published_with_collector_health(self):
        from codex_agents.collector_client import CollectorClient
        server = learning_store.CollectorServer(self.ledger)
        server.start()
        self.addCleanup(server.stop)
        release = self.hold()
        self.refused(lambda: self.ledger.account_capacity(self.project))
        reply = CollectorClient(self.store.state.socket, timeout=1).health()  # read while the lock is held
        release.set()
        self.assertEqual({'account_capacity': 1}, reply['ledger_waits'])

    def test_E1a_3_status_shows_the_refusals_as_attention(self):
        from codex_agents import client_runtime
        lines = client_runtime._ledger_waits_attention({'account_capacity': 2, 'append': 1})
        self.assertEqual(["The account's records were busy 3 times; requests were refused and can be retried."],
                         lines)
        self.assertEqual([], client_runtime._ledger_waits_attention({}))
        self.assertEqual([], client_runtime._ledger_waits_attention(None))

    def test_E1a_T4_a_failing_transaction_body_rolls_back_and_frees_the_lock(self):
        """Review 040-m4b-e1a-code-2: a body that raises leaves nothing written, no open transaction,
        and the lock free for the next caller at once."""
        connection = self.ledger._connection
        before = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        self.assertGreater(before, 0)  # something the failed body tries to remove
        with self.assertRaises(RuntimeError):
            with self.ledger._account_transaction():
                connection.execute("DELETE FROM projects")
                raise RuntimeError()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(before, connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        self.assertTrue(self.ledger._lock.acquire(timeout=0))  # free, not left held
        self.ledger._lock.release()
        self.assertEqual({}, self.ledger.lock_refusals())

    def test_RS_A_4_the_collectors_sweep_releases_due_account_holds_by_itself(self):
        """O0b RS-A-4: no client action: the running collector's sweep releases due account holds."""
        from unittest import mock
        server = learning_store.CollectorServer(self.ledger)
        with mock.patch.object(self.ledger, 'release_due_account_holds', return_value=0) as released:
            server._sweep_cycle()
        released.assert_called_once()
        self.assertEqual(64, released.call_args.kwargs.get('limit'))

    def sweep_loop(self, release, interval, enter=None):
        """The collector's sweep loop on its own thread, every sweep but the release a no-op; `cycles` counts
        the full cycles (each runs sweep_worker_results once). `enter` takes the patches (default: the test)."""
        from unittest import mock
        enter = enter or self.enterContext
        server = learning_store.CollectorServer(self.ledger)
        cycles = []
        enter(mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', interval))
        enter(mock.patch.object(self.ledger, 'sweep_worker_results',
                                side_effect=lambda: cycles.append(time.monotonic())))
        for name in ('sweep_event_retention', 'sweep_account_retention'):
            enter(mock.patch.object(self.ledger, name, return_value=0))
        enter(mock.patch.object(self.ledger, 'release_due_account_holds', side_effect=release))
        thread = threading.Thread(target=server._sweep_loop, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server._sweeps_stop.set)
        return server, thread, cycles

    def test_RS_A_F12_a_burst_of_1000_due_holds_drains_before_the_next_cycle_with_the_lock_freed_between(self):
        """O0b RS-A round 1 F12 (draft 3 item 6): while due holds remain the next batch of 64 runs promptly,
        not a whole interval later, and the ledger lock is released between batches."""
        lock, remaining, calls, between = self.ledger._lock, [1000], [], set()
        stop = threading.Event()
        cycles = []

        def release(*, limit):
            calls.append((limit, len(cycles)))
            done = min(limit, remaining[0])
            remaining[0] -= done
            return done

        def contender():  # another caller takes the lock whenever the sweep leaves it free
            while not stop.is_set():
                if lock.acquire(timeout=0.5):
                    between.add(len(calls))
                    lock.release()
                time.sleep(0.001)
        _, _, cycles = self.sweep_loop(release, 1.0)
        other = threading.Thread(target=contender, daemon=True)
        other.start()
        self.addCleanup(other.join, 5)
        self.addCleanup(stop.set)
        end = time.monotonic() + 10
        while remaining[0] and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(0, remaining[0])
        self.assertEqual([(64, 1)] * 16, calls[:16])  # all sixteen batches inside the first cycle's interval
        self.assertLessEqual(set(range(1, 16)), between)  # another caller took the lock between each batch

    def test_RS_A_F12_a_busy_cycle_is_retried_before_the_next_interval(self):
        """O0b RS-A round 1 F12: a cycle refused because the ledger is held, or skipped with a reader inside,
        is retried soon, not a whole interval later (fault matrix: busy cycles resuming)."""
        for busy in ('held', 'reader'):
            with self.subTest(busy=busy), contextlib.ExitStack() as stack:
                released, leave, entered = [], threading.Event(), threading.Event()

                def occupy():
                    with (self.ledger._lock if busy == 'held' else self.ledger.repository(self.project)):
                        entered.set()
                        leave.wait(10)
                occupant = threading.Thread(target=occupy, daemon=True)
                occupant.start()
                self.assertTrue(entered.wait(5))
                started = time.monotonic()
                server, thread, _ = self.sweep_loop(lambda *, limit: released.append(time.monotonic()) or 0,
                                                    4.0, stack.enter_context)
                time.sleep(4.0 + learning_store.LEDGER_LOCK_WAIT_MS / 1000 + 0.3)  # the first cycle was busy
                self.assertEqual([], released)
                leave.set()
                occupant.join(5)
                while not released and time.monotonic() < started + 7.5:  # the next full cycle is due at 8 s
                    time.sleep(0.01)
                self.assertTrue(released, 'the busy cycle was retried before the next interval')
                server._sweeps_stop.set()
                thread.join(5)

    def test_RS_A_F12_a_stop_ends_the_drain_at_once(self):
        """O0b RS-A round 1 F12: shutdown still stops the sweep while due holds remain."""
        calls = []
        server, thread, _ = self.sweep_loop(lambda *, limit: calls.append(limit) or limit, 0.2)
        end = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < end:
            time.sleep(0.005)
        self.assertGreaterEqual(len(calls), 3)  # draining: more remain after every batch
        server._sweeps_stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        count = len(calls)
        time.sleep(0.1)
        self.assertEqual(count, len(calls))

    def controlled_sweep_loop(self, release, clock, *, interval=60.0, until=300.0, at_wait=None, due_check=None):
        """O0b RS-A round 2 G6: the collector's sweep loop on this thread under a controlled clock (`clock[0]`,
        in seconds, from 0). Each wait advances the clock by its length, and a stop ends the run once past
        `until`; `at_wait(now, seconds)` sees each wait as it begins. Every sweep but the release is a no-op;
        `due_check`, when given, stands in for the ledger's check for due holds. Returns the full cycles'
        times and every wait's length."""
        import types
        from unittest import mock
        cycles, waits = [], []

        class Stop:  # the sweeps' stop event, its waits on the controlled clock
            flag = False

            def is_set(self):
                return self.flag

            def set(self):
                self.flag = True

            def wait(self, seconds):
                waits.append(seconds)
                if len(waits) > 100_000:  # a loop that spins without advancing the clock
                    self.flag = True
                if at_wait is not None:
                    at_wait(clock[0], seconds)
                clock[0] += max(0.0, seconds)
                if clock[0] > until:
                    self.flag = True
                return self.flag
        controlled = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time)
                                              if not name.startswith('_')})
        controlled.monotonic = lambda: clock[0]
        server = learning_store.CollectorServer(self.ledger)
        server._sweeps_stop = Stop()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(learning_store, 'time', controlled))
            stack.enter_context(mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', interval))
            stack.enter_context(mock.patch.object(self.ledger, 'sweep_worker_results',
                                                  side_effect=lambda: cycles.append(clock[0])))
            for name in ('sweep_event_retention', 'sweep_account_retention'):
                stack.enter_context(mock.patch.object(self.ledger, name, return_value=0))
            stack.enter_context(mock.patch.object(self.ledger, 'release_due_account_holds', side_effect=release))
            if due_check is not None:
                stack.enter_context(mock.patch.object(self.ledger, 'account_holds_due', create=True,
                                                      side_effect=due_check))
            server._sweep_loop()
        self.assertLessEqual(len(waits), 100_000, 'the sweep loop spun without waiting')
        self.assertIsNone(server.sweep_error)
        return cycles, waits

    def test_RS_A_G6_a_burst_arriving_while_idle_clears_within_60_seconds_of_its_arrival(self):
        """O0b RS-A round 2 G6 (draft 3 item 6; review 040-rsa-code-r2-3): 1,000 due holds that appear just
        after an idle wait begins (after the first full cycle, so the next is nearly a whole interval away)
        are all released within 60 s of their arrival on a free ledger, the gaps between batches included."""
        for armed in range(3):  # just after the first, second or third wait past the first full cycle begins
            with self.subTest(armed=armed):
                clock, remaining, arrival, batches, waits_seen = [0.0], [1000], [], [], []

                def at_wait(now, seconds):
                    if arrival or now < 60.0 or seconds <= 0.001:  # the first full cycle runs at 60 s
                        return
                    waits_seen.append(now)
                    if len(waits_seen) > armed:
                        arrival.append(now + 0.001)

                def due():
                    return bool(arrival) and clock[0] >= arrival[0] and remaining[0] > 0

                def release(*, limit):
                    if not due():
                        return 0
                    done = min(limit, remaining[0])
                    remaining[0] -= done
                    batches.append(clock[0])
                    return done
                self.controlled_sweep_loop(release, clock, until=400.0, at_wait=at_wait, due_check=due)
                self.assertTrue(arrival, 'the burst arrived')
                self.assertEqual(0, remaining[0])
                self.assertEqual(16, len(batches))  # 1,000 holds in batches of 64
                self.assertLessEqual(batches[-1] - arrival[0], 60.0,
                                     f'the last hold was released {batches[-1] - arrival[0]:.3f} s after arrival')

    def test_RS_A_G6_an_idle_ledger_with_nothing_due_is_checked_often_by_one_cheap_read(self):
        """O0b RS-A round 2 G6: while idle the due holds are checked well inside the 60-second interval; with
        nothing due a check is a read alone (no transaction, no write, no release batch beyond the full
        cycles'), and every statement it runs finds its rows by an index, never by a table scan."""
        import re
        from unittest import mock
        clock, released, checks, statements = [0.0], [], [], []
        real = getattr(self.ledger, 'account_holds_due', lambda: False)

        def check():
            checks.append(clock[0])
            return real()
        connection = self.ledger._connection
        connection.set_trace_callback(statements.append)
        self.addCleanup(connection.set_trace_callback, None)
        with mock.patch.object(self.ledger, 'account_holds_due', create=True, side_effect=check):
            cycles, _ = self.controlled_sweep_loop(lambda *, limit: released.append(clock[0]) or 0, clock,
                                                   until=300.0)
        connection.set_trace_callback(None)
        self.assertEqual(5, len(cycles))
        self.assertEqual(cycles, released)  # a release batch with each full cycle, none for an idle check
        self.assertTrue(checks, 'the idle ledger was checked for due holds')
        gaps = [later - earlier for earlier, later in zip([0.0, *checks], [*checks, 300.0])]
        self.assertLessEqual(max(gaps), 10.0)  # well inside the 60-second full sweep
        self.assertTrue(statements, 'the check reads the ledger')
        for statement in statements:
            with self.subTest(statement=statement[:80]):
                self.assertTrue(statement.lstrip().upper().startswith('SELECT'), statement)
                plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + statement).fetchall()]
                self.assertEqual([], [d for d in plan if re.fullmatch(r'SCAN \S+', d)], plan)

    def test_RS_A_G6_a_stop_during_an_idle_wait_ends_the_sweep_at_once(self):
        """O0b RS-A round 2 G6: shutdown while the sweep waits between idle checks ends it at once."""
        server, thread, cycles = self.sweep_loop(lambda *, limit: 0, 60.0)
        time.sleep(0.3)
        started = time.monotonic()
        server._sweeps_stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([], cycles)

    def test_RS_A_G6_a_held_ledger_does_not_make_idle_checks_spin_or_count_refusals(self):
        """O0b RS-A round 2 G6: while another caller holds the ledger, an idle check passes without waiting
        for the lock, counts no lock refusal and comes back at the idle pace, not in a tight loop."""
        clock, released, checks = [0.0], [], []
        leave, entered = threading.Event(), threading.Event()

        def occupy():
            with self.ledger._lock:
                entered.set()
                leave.wait(30)
        occupant = threading.Thread(target=occupy, daemon=True)
        occupant.start()
        self.addCleanup(occupant.join, 5)
        self.addCleanup(leave.set)
        self.assertTrue(entered.wait(5))
        _, waits = self.controlled_sweep_loop(lambda *, limit: released.append(limit) or 0, clock, until=59.0,
                                              due_check=lambda: checks.append(clock[0]) or True)
        self.assertEqual([], released)
        self.assertEqual([], checks)  # never run without the lock
        self.assertEqual({}, self.ledger.lock_refusals())
        self.assertLessEqual(len(waits), 30)
        self.assertEqual([], [w for w in waits if w < 1.0])

    def test_E1a_T4_maintenance_never_runs_while_a_reader_is_inside(self):
        """Review 040-m4b-e1a-code-2: a reader enters under the ledger lock and the sweep holds that lock for
        its whole cycle after checking for readers, so sweep work never overlaps a reader, however they race."""
        from unittest import mock
        server = learning_store.CollectorServer(self.ledger)
        seen, stop = [], threading.Event()

        def record(**_):  # release_due_account_holds is called with its batch limit
            seen.append(self.ledger._readers())
        patches = [mock.patch.object(self.ledger, name, side_effect=record)
                   for name in ('sweep_worker_results', 'sweep_event_retention', 'sweep_account_retention',
                                'release_due_account_holds')]
        for patch in patches:
            self.enterContext(patch)

        def reading():
            while not stop.is_set():
                with self.ledger.repository(self.project):
                    time.sleep(0.0005)
        readers = [threading.Thread(target=reading, daemon=True) for _ in range(3)]
        for thread in readers:
            thread.start()
        try:
            end = time.monotonic() + 2
            while time.monotonic() < end:
                server._sweep_cycle()
        finally:
            stop.set()
            for thread in readers:
                thread.join(5)
        self.assertTrue(seen, 'the sweep ran at least once')  # the race really happened
        self.assertEqual({0}, set(seen))  # and never with a reader inside

    def test_E1a_3_a_busy_status_still_shows_the_refusals(self):
        """Review 040-m4b-e1a-code-2 (medium): the counts matter most while the ledger is busy, when the
        project's health reply is ledger_busy."""
        from codex_agents import client_runtime
        report = {}
        self.assertFalse(client_runtime._apply_health(report, {'ledger_busy': True, 'ledger_waits': {'append': 3}}))
        self.assertEqual({'append': 3}, report['ledger_waits'])
        health = client_runtime._account_health(report)
        self.assertIn("The account's records were busy 3 times; requests were refused and can be retried.",
                      health['attention'])

    def test_E1a_2_reader_counts_are_read_under_the_readers_lock(self):
        """Review 040-m4b-e1a-code (low): every consumer reads the count through _readers()."""
        for name in ('learning_store.py', 'learning_source_image.py'):
            tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / name).read_text())
            direct = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                      and node.attr == '_active_readers' and isinstance(node.ctx, ast.Load)]
            allowed = set()
            for function in ast.walk(tree):
                if isinstance(function, ast.FunctionDef) and function.name in {'_readers', 'repository'}:
                    allowed |= {getattr(n, 'lineno', None) for n in ast.walk(function)}
            self.assertEqual([], [line for line in direct if line not in allowed], name)

    def test_E1a_1_every_acquisition_is_bounded_or_listed_as_kept(self):
        """No `with <x>._lock`, `<x>._lock.acquire()` without a bounded timeout, or alias of either, outside
        the functions E1a keeps waiting."""
        found = []
        for name in BOUNDED_MODULES:
            source = (Path(ORCHESTRATION_ROOT) / 'codex_agents' / name).read_text()
            found += [f'{name}:{where}' for where in unbounded_waits(source, KEPT_WAITING.get(name, set()))]
        self.assertEqual([], sorted(set(found)))

    def test_E1a_1_the_inventory_catches_aliases_and_unbounded_timeouts(self):
        """Review 040-m4b-e1a-code-2 (medium): the checker itself is proved on examples."""
        caught = {
            'alias acquire': 'def f(self):\n    lock = self._lock\n    lock.acquire()\n',
            'alias with': 'def f(self):\n    lock = self._lock\n    with lock:\n        pass\n',
            'negative timeout': 'def f(self):\n    self._lock.acquire(timeout=-1)\n',
            'long timeout': 'def f(self):\n    self._lock.acquire(timeout=30)\n',
            'unknown timeout': 'def f(self, t):\n    self._lock.acquire(timeout=t)\n',
            'conditional with': 'def f(self, x):\n    with self._lock if x else nothing():\n        pass\n',
            'plain with': 'def f(self):\n    with self._lock:\n        pass\n',
        }
        for label, source in caught.items():
            with self.subTest(label):
                self.assertEqual(1, len(unbounded_waits(source, set())), source)
        allowed = {
            'cap': 'def f(self):\n    self._lock.acquire(timeout=ledger_waits.wait_seconds())\n',
            'nonblocking': 'def f(self):\n    self._lock.acquire(blocking=False)\n',
            'bounded alias': 'def f(self, r):\n    lock = self._lock\n    lock.acquire(timeout=min(0.1, r))\n',
            'named wait': 'def f(self):\n    self._lock.acquire(timeout=HEALTH_LEDGER_WAIT_MS / 1000)\n',
            'kept': 'def close(self):\n    with self._lock:\n        pass\n',
        }
        for label, source in allowed.items():
            with self.subTest(label):
                self.assertEqual([], unbounded_waits(source, {'close'}), source)


def _bounded_timeout(expr):
    """A timeout E1a accepts: the shared cap, a named *_WAIT_MS in seconds, a constant within the cap, or
    the smaller of values one of which is bounded."""
    if isinstance(expr, ast.Constant):
        return type(expr.value) in (int, float) and 0 <= expr.value <= learning_store.LEDGER_LOCK_WAIT_MS / 1000
    if isinstance(expr, ast.Call):
        callee = expr.func.attr if isinstance(expr.func, ast.Attribute) else getattr(expr.func, 'id', '')
        if callee == 'wait_seconds':
            return True
        if callee == 'min':
            return any(_bounded_timeout(arg) for arg in expr.args)
        return False
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        name = expr.left.attr if isinstance(expr.left, ast.Attribute) else getattr(expr.left, 'id', '')
        return (name.endswith('_WAIT_MS') and isinstance(expr.right, ast.Constant) and expr.right.value == 1000)
    return False


def unbounded_waits(source, kept):
    """Each `line function` in `source` that can wait on a ledger lock without a bound."""
    found = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name in kept:
            continue
        aliases = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Attribute) and node.value.attr == '_lock'
                   for target in node.targets if isinstance(target, ast.Name)}

        def is_lock(expr):
            return ((isinstance(expr, ast.Attribute) and expr.attr == '_lock')
                    or (isinstance(expr, ast.Name) and expr.id in aliases))

        def lock_exprs(expr):
            if isinstance(expr, ast.IfExp):
                return lock_exprs(expr.body) + lock_exprs(expr.orelse)
            return [expr] if is_lock(expr) else []
        for node in ast.walk(function):
            waits = False
            if isinstance(node, ast.With):
                waits = any(lock_exprs(item.context_expr) for item in node.items)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'acquire' and is_lock(node.func.value)):
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                blocking = node.args[0] if node.args else keywords.get('blocking')
                nonblocking = isinstance(blocking, ast.Constant) and blocking.value is False
                timeout = node.args[1] if len(node.args) > 1 else keywords.get('timeout')
                waits = not nonblocking and (timeout is None or not _bounded_timeout(timeout))
            if waits:
                found.append(f'{node.lineno} {function.name}')
    return found

if __name__ == '__main__':
    unittest.main()
