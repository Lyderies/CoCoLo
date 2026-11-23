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


class Observed:
    """Counts what the store holds, read-only from outside the writer."""

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

    def finish_through(self, patcher, expect_calls):
        """One run whose publication fails the given way once; it still completes its whole lifecycle."""
        context, route_path, _, run_handle, _, _ = self.opened('replayed')
        self.child_answers()
        with patcher():
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(self.executions()), 'the child is never launched again')
        self.assertEqual((1, 1, 1), self.rows(run_handle), 'one terminal, outbox and usage record')
        self.record('replayed', context, route_path, run_handle, report)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.assertEqual(0, self.store_rows()['material'])

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
            links.append(arguments[0])
            return link(*arguments, **options)

        def patcher():
            stack = contextlib.ExitStack()
            stack.enter_context(mock.patch.object(project_history.ProjectHistory, '_commit_terminal_locked', commit))
            stack.enter_context(mock.patch.object(project_history.os, 'link', counting_link))
            return stack
        self.finish_through(patcher, 2)
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
        self.finish_through(patcher, 2)
        self.assertGreaterEqual(len(calls), 2)

    def test_L3_a_replay_that_commits_a_claim_with_a_live_job_is_adopted_once_and_the_lifecycle_completes(self):
        context, route_path, _, run_handle, _, _ = self.opened('adopted')
        self.child_answers()
        with mock.patch.object(project_driver, 'EXPIRY_SECONDS', 0.2), HeldLiveCommit() as held:
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
            self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
            self.assertGreaterEqual(held.live_attempts, 1)  # the live lane tried and failed
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.record('adopted', context, route_path, run_handle, report)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        self.assertEqual(1, len(self.executions()))

    def test_L3_adopting_an_unequal_delivery_is_refused_and_an_equal_one_is_left_alone(self):
        delivery = types.SimpleNamespace(binding='b', record_id='r', result_digest='d')
        equal = types.SimpleNamespace(binding='b', record_id='r', result_digest='d')
        other = types.SimpleNamespace(binding='b', record_id='r', result_digest='e')
        pending = types.SimpleNamespace(delivery=delivery, state='committed', grant=None, selected='kept',
                                        terminal_evidence=None, reason=None, binding='b')
        lane = types.SimpleNamespace(_lock=threading.RLock())
        result_persistence.ResultPersistenceLane._install_delivery(lane, pending, equal, None)
        self.assertEqual((delivery, 'committed'), (pending.delivery, pending.state))
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
    stop_host = lost.LostRunAfterStopTests.stop_host
    start_host = lost.LostRunAfterStopTests.start_host

    def stranded_then_successor(self, *, expiry_first):
        events = []
        settle = AccountClient.settle_lost_run

        def settling(client, *arguments, **options):
            events.append(('settle', tuple(self.store_rows()['states'])))
            return settle(client, *arguments, **options)
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(
            json.dumps(lost.null_filled(lost.result())))
        with HeldLiveCommit() as held, mock.patch.object(AccountClient, 'settle_lost_run', settling):
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: held.live_attempts >= 1 and self.store_rows()['material'] == 1, seconds=30)
            self.assertEqual(['reserved'], self.store_rows()['states'])  # post-handoff, nothing committed
            if expiry_first:
                original_sweep, original_expire = project_driver.RunDriver._sweep_lost, project_driver.RunDriver._expire_results
                expired = threading.Event()

                def expire(driver):
                    original_expire(driver)
                    if driver._expiry is not None:
                        events.append(('expiry',))
                        expired.set()

                def sweep(driver):
                    if expired.is_set():
                        return original_sweep(driver)
                self.restart_host_with(expire, sweep)
            else:
                self.restart_host()
            dispatch.communicate(timeout=30)
            self.wait_for(lambda: any(event[0] == 'settle' for event in events), seconds=45)
        return events, orchestration, run_handle

    def restart_host_with(self, expire, sweep):
        patches = [mock.patch.object(project_driver.RunDriver, '_expire_results', expire),
                   mock.patch.object(project_driver.RunDriver, '_sweep_lost', sweep)]
        self.stop_host()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.start_host()

    def check(self, events, run_handle):
        settle = [event for event in events if event[0] == 'settle'][0]
        self.assertEqual(('committed',), settle[1], 'the body is committed before any settlement call: %r' % (events,))
        rows = self.store_rows()
        self.assertEqual((1, 1, 0), (rows['terminals'], rows['usage'], rows['material']))
        self.assertEqual(['committed'], rows['states'])  # never classified lost

    def test_R4prime_the_lost_sweep_before_the_expiry_pass_commits_the_body_first(self):
        events, _, run_handle = self.stranded_then_successor(expiry_first=False)
        self.check(events, run_handle)

    def test_R4prime_the_expiry_pass_before_the_lost_sweep_commits_the_body_first(self):
        events, _, run_handle = self.stranded_then_successor(expiry_first=True)
        self.assertEqual(('expiry',), events[0])
        self.check(events, run_handle)


if __name__ == '__main__':
    unittest.main()
