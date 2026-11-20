"""S1-H1a-6: the collector's health route answers under ledger contention (PLAN H1a-6a to H1a-6d;
DESIGN-0.4.0-H1a6-health.md).

Integration stage: an in-process CollectorServer on a real ledger and socket.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import learning_store
from codex_agents.collector_client import CollectorClient
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, authorize_collector_runtime

PROJECT_FIELDS = {'provider_attestations', 'collector_health', 'account_capacity', 'account_slots', 'ingestion'}


class HealthContentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        self.project = self.base / 'project'
        (self.project / '.git').mkdir(parents=True)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.ledger.register_project(self.project)

    def serve(self):
        server = CollectorServer(self.ledger)
        server.start()
        self.addCleanup(server.stop)
        return server, CollectorClient(self.state.socket, timeout=1)

    def hold_lock(self, seconds):
        """Another thread holds the ledger lock, as the account owner can."""
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(seconds)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        return release

    def test_H1a_6a_a_held_ledger_lock_gives_a_prompt_reply_marked_busy_without_the_project_fields(self):
        _, client = self.serve()
        self.hold_lock(10)
        started = time.monotonic()
        reply = client.health(self.project)
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.2)  # it waited its 250 ms for the lock (held throughout)
        self.assertLess(waited, 0.5)
        self.assertIs(True, reply.get('ledger_busy'))
        self.assertFalse(PROJECT_FIELDS & set(reply), reply)  # none half-read
        self.assertIn('runtime_digest', reply)  # read without the ledger, as today

    def test_H1a_6d_with_the_lock_free_the_reply_is_unchanged(self):
        _, client = self.serve()
        reply = client.health(self.project)
        self.assertNotIn('ledger_busy', reply)
        self.assertLessEqual(PROJECT_FIELDS, set(reply))
        project_id = self.ledger.project_for_root(self.project)
        self.assertEqual(self.ledger.provider_attestation_status(project_id), reply['provider_attestations'])
        self.assertEqual(self.ledger.health_snapshot(project_id), reply['collector_health'])
        self.assertEqual(self.ledger.account_capacity(project_id), reply['account_capacity'])
        self.assertEqual(self.ledger.account_slot_health(project_id), reply['account_slots'])
        from codex_agents.learning_store import client_health_snapshot
        self.assertEqual(self.ledger.ingestion_snapshot(
            project_id, client_failures=client_health_snapshot(self.ledger.state, self.project)), reply['ingestion'])

    def test_H1a_6a_a_lock_released_within_the_wait_gives_the_full_reply(self):
        _, client = self.serve()
        release = self.hold_lock(10)
        threading.Timer(0.1, release.set).start()
        reply = client.health(self.project)
        self.assertNotIn('ledger_busy', reply)
        self.assertLessEqual(PROJECT_FIELDS, set(reply))

    def test_H1a_6a_an_unregistered_project_leaves_the_lock_free(self):
        from codex_agents.collector_contracts import CollectorError
        _, client = self.serve()
        other = self.base / 'other'
        (other / '.git').mkdir(parents=True)
        with self.assertRaises(CollectorError):
            client.health(other)
        taken = []
        thread = threading.Thread(target=lambda: taken.append(self.ledger._lock.acquire(timeout=1)
                                                              and self.ledger._lock.release() is None))
        thread.start()
        thread.join(5)
        self.assertEqual([True], taken)

    def test_H1a_6b_health_is_answered_while_a_slow_sweep_runs_on_its_own_thread(self):
        running, threads = threading.Event(), []
        original = self.ledger.sweep_worker_results

        def slow_sweep():
            threads.append(threading.current_thread().name)
            with self.ledger._lock:
                running.set()
                time.sleep(2)
            return original()
        with mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 0.05), \
                mock.patch.object(self.ledger, 'sweep_worker_results', slow_sweep):
            _, client = self.serve()
            self.assertTrue(running.wait(5))
            started = time.monotonic()
            bare = client.health()  # as collector_runtime and the digest check ask it
            with_project = client.health(self.project)
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn('runtime_digest', bare)
        self.assertIs(True, with_project.get('ledger_busy'))
        self.assertEqual(['collector-sweeps'], sorted(set(threads)))

    def test_H1a_6b_the_sweeps_keep_their_cadence_and_stop_with_the_collector(self):
        calls = []
        with mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 0.05), \
                mock.patch.object(self.ledger, 'sweep_event_retention', lambda: calls.append(time.monotonic()) or 0):
            server, client = self.serve()
            deadline = time.monotonic() + 5
            while len(calls) < 3:
                self.assertLess(time.monotonic(), deadline, 'the sweeps did not run again')
                time.sleep(0.02)
            server.stop()
        self.assertFalse([t for t in threading.enumerate() if t.name == 'collector-sweeps' and t.is_alive()])


    def patched_sweeps(self, calls, interval=0.05, slow=0.0, fail_first=False):
        def record(name):
            def sweep():
                calls.append((name, time.monotonic()))
                if fail_first and len(calls) == 1:
                    raise RuntimeError('unexpected')
                time.sleep(slow)
                return 0
            return sweep
        return [mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', interval)] + [
            mock.patch.object(self.ledger, name, record(name))
            for name in ('sweep_worker_results', 'sweep_event_retention', 'sweep_account_retention')]

    def run_with(self, patches, body):
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return body()

    @staticmethod
    def capture(failed, call):
        try:
            call()
        except Exception as error:
            failed.append(error)

    def test_H1a_6b_no_sweep_runs_while_a_transaction_is_active(self):
        calls = []

        def body():
            with self.ledger._lock:
                self.ledger._connection.execute("BEGIN")
            try:
                self.serve()
                time.sleep(0.5)
                self.assertEqual([], calls)
            finally:
                with self.ledger._lock:
                    self.ledger._connection.rollback()
            deadline = time.monotonic() + 5
            while not calls:
                self.assertLess(time.monotonic(), deadline, 'the sweeps did not resume after the rollback')
                time.sleep(0.02)
        self.run_with(self.patched_sweeps(calls), body)

    def test_G1_a_stop_cancels_the_sweeps_even_while_a_request_is_being_handled(self):
        calls, entered, release = [], threading.Event(), threading.Event()

        def blocked(*_):
            entered.set()
            release.wait(10)
            return {}

        def body():
            server, client = self.serve()
            asking = threading.Thread(target=lambda: self.capture([], lambda: client.health(self.project)))
            asking.start()
            self.assertTrue(entered.wait(5))  # the request loop is inside a handler now
            stopper = threading.Thread(target=server.stop)
            stopper.start()
            time.sleep(1.0)  # two cycles would have come due
            self.assertEqual([], calls)
            release.set()
            asking.join(10)
            stopper.join(10)
        self.run_with(self.patched_sweeps(calls, interval=0.4)
                      + [mock.patch.object(learning_store, 'client_health_snapshot', blocked)], body)

    def test_G2_a_sweep_outliving_the_join_is_reported_by_stop_and_wait(self):
        from codex_agents.collector_contracts import CollectorError
        started, done = threading.Event(), threading.Event()

        def stuck():
            started.set()
            time.sleep(6.5)  # past the 5 s join, ignoring the stop
            done.set()
            return 0

        def body():
            server, _ = self.serve()
            self.assertTrue(started.wait(5))
            with self.assertRaises(CollectorError):
                server.stop()
            self.assertFalse(server.wait(0))  # not stopped while the sweep still uses the ledger
            self.assertTrue(done.wait(5))
            deadline = time.monotonic() + 2
            while not server.wait(0):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        self.run_with([mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 0.05),
                       mock.patch.object(self.ledger, 'sweep_worker_results', stuck)], body)

    def test_H1a_6b_no_sweep_runs_while_a_reader_is_active(self):
        calls = []

        def body():
            with self.ledger._lock:
                self.ledger._active_readers += 1
            server, _ = self.serve()
            time.sleep(0.5)
            self.assertEqual([], calls)  # worker results included (review 040-h1a6-design)
            with self.ledger._lock:
                self.ledger._active_readers -= 1
            deadline = time.monotonic() + 5
            while not calls:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertEqual('sweep_worker_results', calls[0][0])  # the old order
        self.run_with(self.patched_sweeps(calls), body)

    def test_H1a_6b_a_cycle_waiting_for_the_lock_ends_on_stop_without_sweeping(self):
        calls = []

        def body():
            server, _ = self.serve()
            release = self.hold_lock(30)
            time.sleep(0.3)  # the cycle is waiting for the lock now
            # stop() itself ends by taking the lock to release the collector's claim (as always), so it
            # runs on a thread; what H1a-6b needs is the waiting cycle ending at once, without sweeping.
            failed = []
            stopper = threading.Thread(target=lambda: self.capture(failed, server.stop))
            stopper.start()
            try:
                deadline = time.monotonic() + 2
                while server._sweeps.is_alive():
                    self.assertLess(time.monotonic(), deadline, 'the sweep cycle kept waiting for the lock')
                    time.sleep(0.02)
            finally:
                release.set()
                stopper.join(10)
            self.assertFalse(stopper.is_alive())
            self.assertEqual([], failed)  # the stop itself succeeded
            self.assertEqual([], calls)
        self.run_with(self.patched_sweeps(calls), body)

    def test_H1a_6b_cadence_counts_from_each_cycles_start(self):
        calls = []

        def body():
            self.serve()
            deadline = time.monotonic() + 10
            while len([c for c in calls if c[0] == 'sweep_worker_results']) < 4:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            starts = [at for name, at in calls if name == 'sweep_worker_results']
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            # Each cycle takes 0.45 s (three sweeps of 0.15 s); counted from its start the gap is about
            # 0.6 s, counted from its end it would be about 1.05 s.
            self.assertLess(sum(gaps) / len(gaps), 0.85, gaps)
            self.assertGreater(sum(gaps) / len(gaps), 0.5, gaps)  # and never more often than the interval
        self.run_with(self.patched_sweeps(calls, interval=0.6, slow=0.15), body)

    def test_H1a_6b_an_unexpected_error_is_recorded_and_the_next_cycle_runs(self):
        calls = []

        def body():
            server, client = self.serve()
            deadline = time.monotonic() + 5
            while len([c for c in calls if c[0] == 'sweep_worker_results']) < 2:
                self.assertLess(time.monotonic(), deadline, 'the sweeps did not run again')
                time.sleep(0.02)
            self.assertEqual('RuntimeError', server.sweep_error)
            self.assertIn('runtime_digest', client.health())
        self.run_with(self.patched_sweeps(calls, fail_first=True), body)


class StatusLedgerBusyTests(unittest.TestCase):
    def owner_ready(self):
        """The account answers the owner-key read, so only the collector's reading can stop ready."""
        from codex_agents import client_runtime
        owner = mock.Mock()
        owner.owner_key.return_value = {'state': 'present', 'fingerprint': 'f', 'replacements': 0,
                                        'last_replaced_at_ms': None}
        return [mock.patch.object(client_runtime, '_account_target', return_value=('prj_' + 'a' * 32, 'e')),
                mock.patch.object(client_runtime, 'enroll_clients'),
                mock.patch.object(client_runtime, 'AccountClient', return_value=owner)]

    def test_H1a_6c_a_busy_second_reading_after_starting_is_never_ready(self):
        from codex_agents import client_runtime
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        state = LearningState(base / 'state')
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / 'cli.py')
        project = base / 'project'
        (project / '.git').mkdir(parents=True)
        ledger = LearningLedger(state)
        ledger.register_project(project)
        server = CollectorServer(ledger)
        server.account_services = 'starting'
        server.start()
        first = CollectorClient(state.socket, timeout=1).health(project)
        server.stop()
        ledger.close()
        busy = {key: value for key, value in first.items() if key not in PROJECT_FIELDS} | {'ledger_busy': True}
        readings = iter([first, busy])
        patches = self.owner_ready() + [
            mock.patch.object(client_runtime.CollectorClient, 'health', side_effect=lambda *a: next(readings)),
            mock.patch.object(client_runtime.CollectorClient, 'learning_migration',
                              side_effect=client_runtime.CollectorError('x'))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        report = client_runtime.status_client(state.root, project)
        self.assertEqual((True, False, 'status_ledger_busy'),
                         (report['active'], report['ready'], report['reason_code']))
        self.assert_project_fields_unavailable(report)  # G3: never the first reading's values

    def assert_project_fields_unavailable(self, report):
        from codex_agents import client_runtime
        # registered_project_state comes from the account's answer, not the ledger, so it may say active.
        ingestion = {k: v for k, v in report['ingestion'].items() if k != 'registered_project_state'}
        unread = {k: v for k, v in client_runtime._status_ingestion_unavailable().items()
                  if k != 'registered_project_state'}
        self.assertEqual(unread, ingestion)
        self.assertEqual({p: {'state': 'unavailable'} for p in ('codex', 'claude')}, report['provider_attestations'])
        self.assertIsNone(report['account_capacity'])
        self.assertIsNone(report.get('collector_health'))
        self.assertIsNone(report.get('account_slots'))

    def test_H1a_6c_status_says_active_and_busy_never_unreachable(self):
        from codex_agents import client_runtime, refusal_steps
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        state = LearningState(base / 'state')
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / 'cli.py')
        project = base / 'project'
        (project / '.git').mkdir(parents=True)
        busy = {'ok': True, 'ledger_busy': True, 'schema_version': learning_store.LEDGER_SCHEMA_VERSION,
                'runtime_version': learning_store.PACKAGE_VERSION, 'runtime_digest': learning_store.COLLECTOR_RUNTIME_DIGEST,
                'started_at_ms': 1, 'previous_exit': None, 'startup_phases': None, 'account_services': 'running',
                'account_restart': None, 'account_restart_last': None}
        for owner_ok in (False, True):
            with self.subTest(owner_ready=owner_ok), \
                    mock.patch.object(client_runtime.CollectorClient, 'health', return_value=busy), \
                    mock.patch.object(client_runtime.CollectorClient, 'learning_migration',
                                      side_effect=client_runtime.CollectorError('x')):
                patches = self.owner_ready() if owner_ok else []
                for patch in patches:
                    patch.start()
                try:
                    report = client_runtime.status_client(state.root, project)
                finally:
                    for patch in patches:
                        patch.stop()
                self.assertEqual((True, False, 'status_ledger_busy'),
                                 (report['active'], report['ready'], report['reason_code']))
                self.assert_project_fields_unavailable(report)
        self.assertIn('again in a moment', refusal_steps.next_step('status_ledger_busy'))


if __name__ == '__main__':
    unittest.main()
