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
