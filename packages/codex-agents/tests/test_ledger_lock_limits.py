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
        self.refused(lambda: self.ledger.bind_session(self.store.project_a, 'session-busy', 'hook', 'codex-hooks-v1'))
        self.assertFalse(self.ledger._connection.in_transaction)  # no BEGIN ran

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
        self.hold()
        for budget_ms, ceiling in ((1000, learning_store.LEDGER_LOCK_WAIT_MS / 1000 + 0.4), (200, 0.6)):
            with self.subTest(budget_ms=budget_ms):
                limits = SQLReadLimits(max_callbacks=1000, max_duration_ms=budget_ms, max_value_bytes=4096)
                waited = self.refused(lambda: self.ledger._connection.read_call(limits, lambda: None))
                self.assertLess(waited, ceiling)

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
                        waits = any(isinstance(item.context_expr, ast.Attribute) and item.context_expr.attr == '_lock'
                                    for item in node.items)
                    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr == 'acquire' and isinstance(node.func.value, ast.Attribute)
                          and node.func.value.attr == '_lock'):
                        waits = not node.keywords and len(node.args) == 0  # no timeout, no blocking=False
                    if waits and function.name not in KEPT_WAITING.get(name, set()):
                        found.append(f'{name}:{node.lineno} {function.name}')
        self.assertEqual([], sorted(set(found)))


if __name__ == '__main__':
    unittest.main()
