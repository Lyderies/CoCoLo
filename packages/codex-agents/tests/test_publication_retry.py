"""O10 D4: a result publication that fails while the service runs is retried by the run driver.

Before this, nothing in production called `ResultPersistenceLane.retry`; the driver's ready
step only waited for `committed`, so one failed publication stranded its run (packet in
memory, capacity held) until the service stopped. Now the driver retries it from the packet
it still holds, with a growing pause, until it commits or the run's own deadline ends it:
one terminal, outbox and usage record, and the child is never launched again.
"""
import sqlite3
import unittest
from unittest import mock

from tests.test_project_driver import DriverHarness
from tests import test_trial_run_driver_bugs as trial


class PublicationRetryTests(DriverHarness):
    attempt = trial.TrialRunDriverTests.attempt
    executions = trial.TrialRunDriverTests.executions
    opened = trial.TrialRunDriverTests.opened
    child_answers = trial.TrialRunDriverTests.child_answers
    standard_assignment = trial.TrialRunDriverTests.standard_assignment
    record = trial.TrialRunDriverTests.record

    def rows(self, run_handle):
        store = sqlite3.connect('file:' + str(next(self.state.root.glob('projects/*/history.sqlite3'))) + '?mode=ro',
                                uri=True)
        try:
            return (store.execute('SELECT COUNT(*) FROM terminal_outbox WHERE run_handle=?', (run_handle,)).fetchone()[0],
                    store.execute("SELECT COUNT(*) FROM records WHERE record_type='dispatch_result'").fetchone()[0],
                    store.execute("SELECT COUNT(*) FROM records WHERE record_id LIKE 'client_usage:%'").fetchone()[0])
        finally:
            store.close()

    def dispatched_through(self, failure):
        """One run whose first publication fails the given way; returns its dispatch and publication calls."""
        from codex_agents import project_history
        original, calls = project_history.ProjectHistory.publish_result, []

        def publish(history, *args, **kwargs):
            calls.append(1)
            return failure(len(calls), lambda: original(history, *args, **kwargs))
        context, route_path, _, run_handle, _, _ = self.opened('retried')
        self.child_answers()
        with mock.patch.object(project_history.ProjectHistory, 'publish_result', publish):
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(self.executions()), 'the child is never launched again')
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual((1, 1, 1), self.rows(run_handle), 'one terminal, outbox and usage record')
        # The run then finishes its whole lifecycle: terminal, cleanup, account release.
        self.record('retried', context, route_path, run_handle, report)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual((1, 1, 1), self.rows(run_handle))
        return calls

    def test_D4_a_publication_failing_before_its_commit_is_retried_and_commits_once(self):
        from codex_agents.history_contracts import HistoryError

        def failure(call, publish):
            if call == 1:
                raise HistoryError('history_service_unavailable')
            return publish()
        self.dispatched_through(failure)

    def test_D4_a_publication_failing_after_its_commit_is_retried_without_a_second_record(self):
        def failure(call, publish):
            delivery = publish()
            if call == 1:
                raise OSError('the reply was lost after the terminal committed')
            return delivery
        self.dispatched_through(failure)


class PublicationRetryStepTests(unittest.TestCase):
    """The driver's ready step on a failed publication: a growing pause, bounded by the run's deadline."""

    def setUp(self):
        from tests.test_run_driver_retries import driver, fencing_run
        self.value = driver()
        self.run = fencing_run(state='ready', receipt={'deadline_at_ms': 10_000}, binding=object())
        self.lane = self.value._runtime.persistence
        self.lane.snapshot.return_value = {'state': 'failed', 'reason_code': 'history_service_unavailable'}
        patcher = mock.patch('codex_agents.project_driver._wall_ms', return_value=5_000)
        self.wall = patcher.start()
        self.addCleanup(patcher.stop)

    def test_D4_a_failed_publication_is_retried_with_a_growing_capped_pause(self):
        from codex_agents import project_driver
        pauses = []
        for _ in range(9):
            self.value._advance(self.run)  # Failed again: the next retry is scheduled.
            pauses.append(self.run.publish_at - self.value._clock.now)
            self.value._advance(self.run)
            self.assertEqual(len(pauses) - 1, self.lane.retry.call_count, 'not before its pause')
            self.value._clock.now = self.run.publish_at
            self.value._advance(self.run)
            self.assertEqual(len(pauses), self.lane.retry.call_count)
        self.assertEqual([1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0], pauses)
        self.assertEqual(project_driver.PUBLISH_RETRY_MAX_SECONDS, max(pauses))
        self.lane.retry.assert_called_with('lease-job')
        self.assertEqual('ready', self.run.state)
        self.value._account_call.assert_not_called()

    def test_D4_a_retry_refused_by_a_closing_writer_is_asked_again_later(self):
        from codex_agents.history_contracts import HistoryError
        self.lane.retry.side_effect = HistoryError('history_service_unavailable')
        self.value._advance(self.run)
        self.value._clock.now = self.run.publish_at
        self.value._advance(self.run)
        self.value._advance(self.run)
        self.assertEqual(('ready', 2), (self.run.state, self.run.publish_attempts))

    def test_D4_the_runs_deadline_ends_the_retries_and_keeps_what_the_account_holds(self):
        self.value._advance(self.run)
        self.wall.return_value = 10_000
        self.value._clock.now = self.run.publish_at
        self.value._advance(self.run)
        self.lane.retry.assert_not_called()
        self.assertEqual(('failed', 'run_failed', 'preparation'), (self.run.state, self.run.reason, self.run.prepared))
        self.value._preparation.discard.assert_not_called()

    def test_D4_a_refusal_no_retry_can_change_stops_the_run_at_once(self):
        for reason in ('history_invalid_input', 'history_conflict', 'history_result_not_ready'):
            with self.subTest(reason=reason):
                self.setUp()
                self.lane.snapshot.return_value = {'state': 'failed', 'reason_code': reason}
                self.value._advance(self.run)
                self.lane.retry.assert_not_called()
                self.assertEqual(('failed', 'run_failed'), (self.run.state, self.run.reason))


if __name__ == '__main__':
    unittest.main()
