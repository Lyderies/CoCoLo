"""M4b E1a: a time limit on every ledger-lock wait (PLAN E1a-1 to E1a-4; DESIGN-0.4.0-M4b-E1a.md).

Integration stage: the ledger fixture (its set-up starts a process). Contention comes from another
thread holding the ledger lock, coordinated by events; elapsed times are checked against monotonic
bounds with a tolerance.
"""
import ast
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
        holds = [node for node in ast.walk(commit) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == '_held']
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

    def test_E1a_2_reader_counts_are_read_under_the_readers_lock(self):
        """Review 040-m4b-e1a-code (low): every consumer reads the count through _readers()."""
        for name in ('learning_store.py', 'learning_source_image.py'):
            tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / name).read_text())
            direct = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                      and node.attr == '_active_readers' and isinstance(node.ctx, ast.Load)]
            allowed = set()
            for function in ast.walk(tree):
                if isinstance(function, ast.FunctionDef) and function.name in {'_readers', 'repository'}:
                    allowed |= {n.lineno for n in ast.walk(function)}
            self.assertEqual([], [line for line in direct if line not in allowed], name)

    def test_E1a_1_every_acquisition_is_bounded_or_listed_as_kept(self):
        """No `with <x>._lock`, `<x>._lock.acquire()` without a timeout, or alias of either, outside the
        functions E1a keeps waiting."""
        found = []
        for name in BOUNDED_MODULES:
            tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / name).read_text())
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    waits = False
                    if isinstance(node, ast.With):
                        # Review 040-m4b-e1a-code: both branches of a conditional context count too.
                        def lock_exprs(expr):
                            if isinstance(expr, ast.IfExp):
                                return lock_exprs(expr.body) + lock_exprs(expr.orelse)
                            return [expr] if isinstance(expr, ast.Attribute) and expr.attr == '_lock' else []
                        waits = any(lock_exprs(item.context_expr) for item in node.items)
                    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr == 'acquire' and isinstance(node.func.value, ast.Attribute)
                          and node.func.value.attr == '_lock'):
                        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                        blocking = node.args[0] if node.args else keywords.get('blocking')
                        nonblocking = isinstance(blocking, ast.Constant) and blocking.value is False
                        timed = len(node.args) > 1 or 'timeout' in keywords
                        waits = not nonblocking and not timed  # acquire(), acquire(True), acquire(blocking=True)
                    if waits and function.name not in KEPT_WAITING.get(name, set()):
                        found.append(f'{name}:{node.lineno} {function.name}')
        self.assertEqual([], sorted(set(found)))


if __name__ == '__main__':
    unittest.main()
