"""Slice D4 (RS-D R4', L1-L3 and the production retry), through the real run driver and writer lane.

DESIGN-0.4.0-RS-D.md draft 3 s1, draft 4 s3 (live lanes complete their own publication), shared rule S-3.
- R4': the real driver `step` order (the lost sweep before the expiry pass, and the reverse) with a post-handoff
  publication of a stopped earlier instance: the body is committed before any `settle_lost_run` call.
- L1, L2: a live service retries a failure after the directory fsync (the commit transaction fails) and a failure after
  the terminal commit (the delivery attestation fails): one terminal, outbox and usage record, then the full lifecycle.
- L3: a replay that commits a claim with a live job is adopted by the lane once (`adopt_delivery`); an unequal delivery
  is refused; the lifecycle completes.
Integration stage: a real collector, account and project host in temporary folders (the driver harness of the D4/O10
publication-retry tests and the lost-run stop tests); the publication is held by patching the writer's commit.
"""
import contextlib
import json
import sqlite3
import threading
import time
from threading import local as ThreadContext
import types
import unittest
from unittest import mock

from tests import test_lost_run_settlement as lost
from tests import test_publication_retry as retry
from tests import test_trial_run_driver_bugs as trial
from tests.test_project_driver import DriverHarness

from codex_agents import project_driver, project_history, result_persistence
from codex_agents.account_api import AccountClient
from codex_agents.history_contracts import HistoryError

WHY = ('D4 drives a real collector, account and project host in temporary folders; the writer commit and the account '
       'settlement are observed at their real call sites.')


MODULE_END = None


def setUpModule():
    global MODULE_END
    MODULE_END = time.monotonic() + 50  # reserve ten seconds of the shared 60 s budget for cleanup


class Observed:
    """Counts what the store holds, read-only from outside the writer."""

    def remaining(self):
        remaining = MODULE_END - time.monotonic()
        self.assertGreater(remaining, 0, 'D4 driver module exhausted its shared 50 s work deadline')
        return remaining

    def wait_for(self, condition, seconds=20):
        return super().wait_for(condition, seconds=min(seconds, self.remaining()))

    def stop_services(self):
        self.stop.set()
        for thread in reversed(self.threads):
            thread.join(max(0, MODULE_END + 10 - time.monotonic()))
            self.assertFalse(thread.is_alive(), thread.name + ' exceeded the shared 60 s module budget')
        self.assertEqual([], self.failures)
        workers = {'account-observation', 'signature-fixed-worker', 'codex-agents-collector', 'account-ingress'}
        self.assertEqual([], [t.name for t in threading.enumerate() if t.name in workers and t.is_alive()])

    def _end_services(self, stop):
        """The separately registered cleanup shares teardown's module deadline."""
        stop.set()
        for thread in reversed(self.threads):
            thread.join(max(0, MODULE_END + 10 - time.monotonic()))

    def dispatch_result(self, context, route_path, run_handle):
        process = self.separate_dispatch(context, route_path, run_handle)
        try:
            output, errors = process.communicate(timeout=self.remaining())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)  # reap this dispatch even on timeout or assertion failure
        return process.returncode, json.loads(output)

    def store_rows(self):
        path = next(self.state.root.glob('projects/*/history.sqlite3'))
        store = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
        try:
            return {
                'terminals': store.execute('SELECT COUNT(*) FROM terminal_outbox').fetchone()[0],
                'usage': store.execute("SELECT COUNT(*) FROM records WHERE record_id LIKE 'client_usage:%'").fetchone()[0],
                'states': [row[0] for row in store.execute('SELECT state FROM result_reservations')],
                'material': store.execute('SELECT COUNT(*) FROM project_result_publications').fetchone()[0],
            }
        finally:
            store.close()


class HeldLiveCommit:
    """While held, the writer's own `commit_result` (the live lane) fails inside its commit transaction, after the
    body's directory fsync; a store replay (outside `commit_result`) is not affected."""

    def __init__(self):
        self.on = ThreadContext()
        self.blocked = threading.Event()
        self.live_attempts = 0
        self.failed = threading.Event()
        self.binding = None

    def __enter__(self):
        commit_result, terminal = project_history.ProjectHistory.commit_result, project_history.ProjectHistory._commit_terminal_locked
        held = self

        def live(history, *arguments, **options):
            held.on.live, held.live_attempts = True, held.live_attempts + 1
            try:
                return commit_result(history, *arguments, **options)
            finally:
                held.on.live = False

        def commit(history, *arguments, **options):
            if getattr(held.on, 'live', False) and held.blocked.is_set():
                held.binding = arguments[2]
                held.failed.set()  # this exact post-fsync terminal fault actually fired
                raise HistoryError('history_service_unavailable')
            return terminal(history, *arguments, **options)
        self.patches = [mock.patch.object(project_history.ProjectHistory, 'commit_result', live),
                        mock.patch.object(project_history.ProjectHistory, '_commit_terminal_locked', commit)]
        for patch in self.patches:
            patch.start()
        self.blocked.set()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()
        return False


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class LiveRetryLifecycleTests(Observed, DriverHarness):
    attempt = trial.TrialRunDriverTests.attempt
    executions = trial.TrialRunDriverTests.executions
    opened = trial.TrialRunDriverTests.opened
    child_answers = trial.TrialRunDriverTests.child_answers
    standard_assignment = trial.TrialRunDriverTests.standard_assignment
    record = trial.TrialRunDriverTests.record
    rows = retry.PublicationRetryTests.rows

    def setUp(self):
        self.live_packets = []
        publish_live = result_persistence.ResultPersistenceLane._publish

        def publish(lane, pending):
            self.live_packets.append((pending.selected, pending.observed_at_ms))
            return publish_live(lane, pending)

        # _run caches bound operation callbacks when the writer starts. Install
        # this observer before the harness starts the host, not around dispatch.
        patcher = mock.patch.object(result_persistence.ResultPersistenceLane, '_publish', publish)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def finish_through(self, patcher):
        """One run whose publication fails the given way once; it still completes its whole lifecycle."""
        context, route_path, _, run_handle, _, _ = self.opened('replayed')
        self.child_answers()
        calls, retries, packets = [], [], self.live_packets
        commit_result = project_history.ProjectHistory.commit_result
        retry_live = result_persistence.ResultPersistenceLane.retry

        def commit(history, content, binding, *args, **kwargs):
            calls.append(history._result_row(binding)[1].state)
            return commit_result(history, content, binding, *args, **kwargs)

        def retry_job(lane, handle):
            retries.append(handle)
            return retry_live(lane, handle)

        with patcher(), mock.patch.object(project_history.ProjectHistory, 'replay_publications', return_value=[]), \
                mock.patch.object(project_history.ProjectHistory, 'commit_result', commit), \
                mock.patch.object(result_persistence.ResultPersistenceLane, 'retry', retry_job):
            code, report = self.dispatch_result(context, route_path, run_handle)
        self.assertGreaterEqual(len(retries), 1, 'the driver must request its live retry')
        self.assertGreaterEqual(len(packets), 2, 'the live publish lane must run again')
        for packet, observed in packets[1:]:
            self.assertIs(packets[0][0], packet)
            self.assertEqual(packets[0][1], observed)
        self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(self.executions()), 'the child is never launched again')
        self.assertEqual((1, 1, 1), self.rows(run_handle), 'one terminal, outbox and usage record')
        self.record('replayed', context, route_path, run_handle, report)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.assertEqual(0, self.store_rows()['material'])
        return calls

    def test_L1_a_failure_after_the_directory_fsync_is_retried_from_the_final_file_and_completes_the_lifecycle(self):
        calls = []
        original = project_history.ProjectHistory._commit_terminal_locked
        links = []
        link = project_history.os.link

        def commit(history, *arguments, **options):
            calls.append(1)
            if len(calls) == 1:
                raise HistoryError('history_service_unavailable')  # the commit transaction fails
            return original(history, *arguments, **options)

        def counting_link(*arguments, **options):
            if str(arguments[0]).startswith('.result-'):  # a result body's own final link, not other files'
                links.append(arguments[0])
            return link(*arguments, **options)

        def patcher():
            stack = contextlib.ExitStack()
            stack.enter_context(mock.patch.object(project_history.ProjectHistory, '_commit_terminal_locked', commit))
            stack.enter_context(mock.patch.object(project_history.os, 'link', counting_link))
            return stack
        live_states = self.finish_through(patcher)
        self.assertEqual(['reserved', 'reserved'], live_states[:2])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(1, len(links), 'the retry reuses the final file; no second body is written')

    def test_L2_a_failure_after_the_terminal_commit_takes_the_committed_branch_and_completes_the_lifecycle(self):
        calls = []
        original = project_history.ProjectHistory.attest_terminal_delivery

        def attest(history, *arguments, **options):
            calls.append(1)
            if len(calls) == 1:
                raise HistoryError('history_service_unavailable')  # the evidence attestation fails
            return original(history, *arguments, **options)

        def patcher():
            return mock.patch.object(project_history.ProjectHistory, 'attest_terminal_delivery', attest)
        live_states = self.finish_through(patcher)
        self.assertEqual(['reserved', 'committed'], live_states[:2])
        self.assertGreaterEqual(len(calls), 2)

    def test_L3_a_replay_that_commits_a_claim_with_a_live_job_is_adopted_once_and_the_lifecycle_completes(self):
        context, route_path, _, run_handle, _, _ = self.opened('adopted')
        self.child_answers()
        installations = []
        install = result_persistence.ResultPersistenceLane._install_delivery

        def installed(lane, pending, delivery, evidence):
            before = pending.delivery
            install(lane, pending, delivery, evidence)
            if before is None and pending.delivery is not None:
                installations.append(pending.binding.claim_id)

        with mock.patch.object(project_driver, 'EXPIRY_SECONDS', 0.2), HeldLiveCommit() as held, \
                mock.patch.object(result_persistence.ResultPersistenceLane, '_install_delivery', installed):
            code, report = self.dispatch_result(context, route_path, run_handle)
            self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
            self.assertGreaterEqual(held.live_attempts, 1)  # the live lane tried and failed
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(installations))
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.record('adopted', context, route_path, run_handle, report)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.assertEqual(1, len(self.executions()))

    def test_L3_adopting_an_unequal_delivery_is_refused_and_an_equal_one_is_left_alone(self):
        delivery = types.SimpleNamespace(binding='b', record_id='r', result_digest='d')
        equal = types.SimpleNamespace(binding='b', record_id='r', result_digest='d')
        other = types.SimpleNamespace(binding='b', record_id='r', result_digest='e')
        evidence, selected = object(), object()
        pending = types.SimpleNamespace(delivery=delivery, state='committed', grant=None, selected=selected,
                                        terminal_evidence=evidence, reason=None, binding='b')
        lane = types.SimpleNamespace(_lock=threading.RLock())
        result_persistence.ResultPersistenceLane._install_delivery(lane, pending, equal, None)
        self.assertEqual((delivery, 'committed'), (pending.delivery, pending.state))
        self.assertIs(delivery, pending.delivery)
        self.assertIs(evidence, pending.terminal_evidence)
        self.assertIs(selected, pending.selected)
        with self.assertRaises(HistoryError) as caught:
            result_persistence.ResultPersistenceLane._install_delivery(lane, pending, other, None)
        self.assertEqual('history_conflict', caught.exception.reason_code)
        fresh = types.SimpleNamespace(delivery=None, state='failed', grant=None, selected='kept',
                                      terminal_evidence=None, reason='history_service_unavailable', binding='b')
        result_persistence.ResultPersistenceLane._install_delivery(lane, fresh, delivery, b'evidence')
        self.assertEqual((delivery, 'committed', None, b'evidence', None),
                         (fresh.delivery, fresh.state, fresh.reason, fresh.terminal_evidence, fresh.selected))


@__import__('test_framework').requirements('RS-D-D4', why_not_scenario=WHY)
class StoppedInstanceOrderTests(Observed, lost.LostRunHarness):
    """R4': the earlier instance stopped after its handoff, before its commit; the successor's sweeps run in either
    order, and the body is committed before the account is asked to settle the attempt."""
    def setUp(self):
        self._host_stop = threading.Event()
        original = DriverHarness.start_project_host

        def first(harness, project, stop=None):
            return original(harness, project, stop or self._host_stop)

        with mock.patch.object(DriverHarness, 'start_project_host', first):
            super().setUp()

    def stop_services(self):
        self._host_stop.set()
        super().stop_services()

    restart_host = lost.LostRunAfterStopTests.restart_host
    def stop_host(self):
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(min(8, self.remaining()))
        self.assertFalse(host.is_alive(), 'the stopped host must join within the module deadline')
        self.threads.remove(host)
    start_host = lost.LostRunAfterStopTests.start_host

    def stop_without_settling(self):
        """The host stops the way a crash leaves it: its own exit settles nothing, so the next host settles."""
        original = project_driver.RunDriver.settle_on_exit
        with mock.patch.object(project_driver.RunDriver, 'settle_on_exit',
                               lambda driver, settle=True: original(driver, settle=False)):
            self.stop_host()

    def stranded_then_successor(self, mode):
        """`exit`: the stopping service settles at its own exit; `sweep_first` and `expiry_first`: the successor's
        lost sweep and expiry pass, in that order or the reverse."""
        events = []
        settle = AccountClient.settle_lost_run
        context, route_path, orchestration, run_handle = self.open_run()

        def settling(client, value, **options):
            event = ['settle', None, value.run_handle, value]
            events.append(event)  # record identity before anything the sweep could catch
            event[1] = tuple(self.store_rows()['states'])
            reply = settle(client, value, **options)
            events.append(('settled', value.run_handle, reply))
            return reply

        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(lost.null_filled(lost.result())))
        with HeldLiveCommit() as held, mock.patch.object(AccountClient, 'settle_lost_run', settling):
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            try:
                self.wait_for(held.failed.is_set)
                self.assertEqual(['reserved'], self.store_rows()['states'])
                if mode == 'exit':
                    self.stop_host()
                    # No successor exists yet: this settlement belongs to the exiting service.
                    self.assertTrue(any(e[0] == 'settled' and e[1] == run_handle for e in events), events)
                else:
                    self.stop_without_settling()
                    self.assertEqual([], events)
                    sweep_done, expiry_done = threading.Event(), threading.Event()
                    sweep = project_driver.RunDriver._settle_lost
                    earlier = result_persistence.ResultPersistenceLane._earlier
                    expire_step = project_driver.RunDriver._expire_results
                    sweep_step = project_driver.RunDriver._sweep_lost

                    def swept(driver, *args, **kwargs):
                        events.append(('sweep_start',))
                        result = sweep(driver, *args, **kwargs)
                        events.append(('sweep_done',))
                        sweep_done.set()
                        return result

                    def expired(lane, request):
                        earlier(lane, request)
                        if request.kind == 'expire':
                            events.append(('expiry_done', request.state))
                            if request.state == 'done':
                                expiry_done.set()

                    def expire_in_order(driver):
                        if mode == 'expiry_first' or sweep_done.is_set():
                            return expire_step(driver)

                    def sweep_in_order(driver):
                        if mode == 'sweep_first' or expiry_done.is_set():
                            return sweep_step(driver)

                    with mock.patch.object(project_driver.RunDriver, '_settle_lost', swept), \
                            mock.patch.object(result_persistence.ResultPersistenceLane, '_earlier', expired), \
                            mock.patch.object(project_driver.RunDriver, '_expire_results', expire_in_order), \
                            mock.patch.object(project_driver.RunDriver, '_sweep_lost', sweep_in_order), \
                            mock.patch.object(project_driver, 'EXPIRY_SECONDS', 0.2):
                        self.start_host()
                        self.wait_for(lambda: sweep_done.is_set() and expiry_done.is_set()
                                      and self.counted() == 0)
                        names = [event[0] for event in events]
                        first, second = (('expiry_done', 'sweep_start') if mode == 'expiry_first'
                                         else ('sweep_done', 'expiry_done'))
                        self.assertLess(names.index(first), names.index(second), events)
                        if mode == 'sweep_first':
                            first_sweep = events[names.index('sweep_start') + 1:names.index('sweep_done')]
                            self.assertTrue(any(e[0] == 'settled' and e[1] == run_handle for e in first_sweep),
                                            'target settlement must return inside the first sweep: %r' % (events,))
                dispatch.communicate(timeout=self.remaining())
                self.wait_for(lambda: self.counted() == 0 and
                              any(e[0] == 'settled' and e[1] == run_handle for e in events))
            finally:
                if dispatch.poll() is None:
                    dispatch.kill()
                    dispatch.communicate(timeout=2)
        # These assertions belong to the test thread: the sweep catches callback
        # exceptions, which must never erase a wrong-target call from the proof.
        for event in events:
            if event[0] == 'settle':
                self.assertEqual(run_handle, event[2])
                self.assertEqual(held.binding.claim_id, event[3].bridge_run_id)
        return events, orchestration, run_handle

    def check(self, events, run_handle):
        calls = [event for event in events if event[0] == 'settle']
        self.assertTrue(calls, events)
        for event in calls:
            self.assertEqual(('committed',), event[1], 'every settlement must follow commitment: %r' % (events,))
            self.assertEqual(run_handle, event[2])
        completed = [event for event in events if event[0] == 'settled' and event[1] == run_handle]
        self.assertEqual(len(calls), len(completed), 'observe every target settlement through its return')
        self.assertTrue(completed, 'the target attempt must finish settlement')
        rows = self.store_rows()
        self.assertEqual((1, 1, 0), (rows['terminals'], rows['usage'], rows['material']))
        self.assertEqual(['committed'], rows['states'])

    def test_the_exiting_service_arbitrates_on_a_store_of_its_own_before_it_settles(self):
        events, _, run_handle = self.stranded_then_successor('exit')
        self.check(events, run_handle)

    def test_R4prime_the_lost_sweep_before_the_expiry_pass_commits_the_body_first(self):
        events, _, run_handle = self.stranded_then_successor('sweep_first')
        self.check(events, run_handle)

    def test_R4prime_the_expiry_pass_before_the_lost_sweep_commits_the_body_first(self):
        events, _, run_handle = self.stranded_then_successor('expiry_first')
        self.assertEqual('expiry_done', events[0][0])
        self.check(events, run_handle)


if __name__ == '__main__':
    unittest.main()
