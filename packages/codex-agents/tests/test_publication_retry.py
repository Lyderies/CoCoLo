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


if __name__ == '__main__':
    unittest.main()
