"""0.3.4: a project's results never fill its store for good (DESIGN-0.3.4-result-capacity.md).

Integration stage: the in-process collector, account and project services run a real bridged run.
"""

import json
import sqlite3
import unittest
from unittest import mock

from codex_agents import project_driver
from tests.test_bridge import null_filled, result
from tests.test_project_driver import DriverHarness

# How often the service looks for results to retire (production: every 10 s). These tests wait for that
# look, up to six times each; a shorter wait changes no rule they check, only how long they sit idle.
TEST_EXPIRY_SECONDS = 0.5


class ResultLifetimeTests(DriverHarness):
    def setUp(self):
        patcher = mock.patch.object(project_driver, 'EXPIRY_SECONDS', TEST_EXPIRY_SECONDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def result_states(self):
        """The states of this project's results, read without the service's help."""
        states = []
        for path in self.state.root.rglob('history.sqlite3'):
            connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
            try:
                states += [row[0] for row in connection.execute('SELECT state FROM result_reservations')]
            except sqlite3.Error:
                pass
            finally:
                connection.close()
        return states

    def test_034_R2_the_service_retires_a_closed_orchestrations_result_by_itself(self):
        """R2: once its orchestration closes, the service retires the result without any read; in
        0.8.29 nothing in the product ran the cleanup, so results were never retired."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual(['committed'], self.result_states())
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
        self.wait_for(lambda: self.result_states() == ['expired'], seconds=90)

    def test_034_R3_a_full_result_store_is_named_with_its_remedy(self):
        """R3: a dispatch refused because the project's result store is full says so, instead of the
        generic run_failed of 0.8.29, and says what frees space."""
        from unittest import mock
        from codex_agents import project_history
        from codex_agents.history_contracts import HistoryError
        context, route_path, _, run_handle = self.open_run()
        with mock.patch.object(project_history.ProjectHistory, '_check_result_capacity',
                               side_effect=HistoryError('history_result_capacity')):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('result_capacity_full', refused['reason_code'], refused)
        self.assertIn('close finished orchestrations', refused['error'])
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])


    def test_RC_1_space_held_by_running_results_says_to_wait(self):
        """O10 RC-1: a run refused because running results hold the space is told to wait for them,
        not that the store is full."""
        from unittest import mock
        from codex_agents import project_history
        from codex_agents.history_contracts import HistoryError
        context, route_path, _, run_handle = self.open_run()
        with mock.patch.object(project_history.ProjectHistory, '_check_result_capacity',
                               side_effect=HistoryError('history_result_capacity_busy')):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('result_capacity_busy', refused['reason_code'], refused)
        self.assertIn('running', refused['error'])
        self.assertNotIn('full', refused['error'])
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_RC_2_status_shows_the_results_in_flight_against_the_space_for_them(self):
        context, route_path, orchestration, run_handle = self.open_run()
        capacity = self.cli('learning-status')['project_capacity']
        self.assertEqual({'live': 0, 'limit': 3}, capacity['results_in_flight'])  # 3 MiB, 1 MiB each

    def test_034_R9_status_shows_each_project_limit_with_its_live_count(self):
        """R9: learning-status shows, for this project's store, how near each limit is."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        capacity = self.cli('learning-status')['project_capacity']
        # Corrected for O10 RC-2 (requirement): status also shows the results in flight.
        self.assertEqual({'results', 'results_in_flight', 'orchestrations', 'cleanup_reservations', 'operational_qa',
                          'qa_check_receipts'}, set(capacity))
        for name, limit in (('results', 64), ('orchestrations', 256), ('cleanup_reservations', 4096),
                            ('operational_qa', 16_384), ('qa_check_receipts', 16_384)):
            self.assertEqual(limit, capacity[name]['limit'], name)
        self.assertEqual(1, capacity['results']['live'])
        self.assertEqual(1, capacity['orchestrations']['live'])
        # Corrected for review 034-security: live means the account's release is not yet acknowledged,
        # which for a finished run depends on timing; the count is checked for its type.
        self.assertIs(int, type(capacity['cleanup_reservations']['live']))
        self.assertEqual(0, capacity['operational_qa']['live'])


    def test_034_R9_status_shows_each_account_limit_with_its_live_count(self):
        """R9, account part: observations and unreleased cleanup receipts, with their limits."""
        context, route_path, _, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        capacity = self.cli('learning-status')['account_capacity']
        # Corrected for O0b B0 (narrowed, requirement): status also shows the run bindings and billing
        # probes still held, against their limits.
        # Corrected for OP-3 (requirement): and the open requests in flight, against their own limit of 4.
        self.assertEqual({'observations', 'cleanup_receipts', 'pending_bindings', 'billing_probes', 'open_requests'},
                         set(capacity))
        for name in capacity:
            self.assertEqual(4 if name == 'open_requests' else 4096, capacity[name]['limit'], name)
            self.assertIs(int, type(capacity[name]['live']), name)



    def live_counts(self):
        """Live results, open orchestrations and unreleased account slots: all zero when nothing runs."""
        capacity = self.cli('learning-status')['project_capacity']
        slots = self.collector_ledger._connection.execute(
            'SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0]
        return capacity['results']['live'], capacity['orchestrations']['live'], slots

    def record(self, context, route_path, run_handle, dispatched):
        observation = {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]}
        terminal = dispatched['bridge_report'].get('terminal_state')
        if terminal is not None:
            observation['terminal_state'] = terminal
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('result.json', dispatched['result']),
                 '--observation', self.write('observation.json', observation))

    def test_034_R8_every_run_ending_returns_every_live_count_to_where_it_started(self):
        """R8: after each way a run can end, and its orchestration closes, nothing stays held: no live
        result, no open orchestration, no account slot. A run with no result needs R7 to close."""
        import dataclasses
        from unittest import mock
        from codex_agents import codex_preparation
        self.assertEqual((0, 0, 0), self.live_counts())

        def succeeded(context, route_path, orchestration, run_handle):
            self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
            (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
            self.record(context, route_path, run_handle,
                        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle))
            return 'completed'

        def cancelled(context, route_path, orchestration, run_handle):
            self.frames('codex', [{'type': 'thread.started', 'thread_id': 'quiet'}, {'sleep': 30},
                                  {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
            (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
            launched = len([i for i in self.invocations('codex') if 'exec' in i['argv']])
            process = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: len([i for i in self.invocations('codex') if 'exec' in i['argv']]) > launched,
                          seconds=30)
            self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
            output, _ = process.communicate(timeout=30)
            self.record(context, route_path, run_handle, json.loads(output))
            return 'cancelled'

        def refused_in_preparation(context, route_path, orchestration, run_handle):
            with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
                self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
            return 'failed'

        def client_changed(context, route_path, orchestration, run_handle):
            original = codex_preparation.CodexPreparationLane.prepared

            def changed(lane, handle):
                return dataclasses.replace(original(lane, handle), client_identity=('/srv/other/codex', 'f' * 64))
            with mock.patch.object(codex_preparation.CodexPreparationLane, 'prepared', autospec=True, side_effect=changed):
                self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
            return 'failed'

        for ending in (succeeded, cancelled, refused_in_preparation, client_changed):
            with self.subTest(ending=ending.__name__):
                context, route_path, orchestration, run_handle = self.open_run()
                status = ending(context, route_path, orchestration, run_handle)
                self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', status)
                self.wait_for(lambda: self.live_counts() == (0, 0, 0), seconds=60)


    def store_rows(self, *tables):
        counts = []
        for table in tables:
            total, stores = 0, list(self.state.root.rglob('history.sqlite3'))
            self.assertEqual(1, len(stores))  # Re-review 034-security-2: a missing or unreadable store fails.
            for path in stores:
                connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
                try:
                    total += connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                finally:
                    connection.close()
            counts.append(total)
        return tuple(counts)

    def test_034_R1f_a_released_attempt_is_removed_with_its_release_acknowledgement(self):
        """R1f: a real run, released by the account and recorded, of a closed orchestration: past its hard
        deadline and the grace, every row of the attempt goes, its release acknowledgement first."""
        from unittest import mock
        from codex_agents import project_history
        tables = ('result_reservations', 'result_leases', 'project_cleanup_reservations',
                  'project_cleanup_receipts', 'project_cleanup_acknowledgements')
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.record(context, route_path, run_handle, dispatched)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
        self.wait_for(lambda: self.result_states() == ['expired'], seconds=60)
        self.assertEqual((1, 1, 1, 1, 1), self.store_rows(*tables))
        import time
        later = time.time_ns() // 1_000_000 + 8 * 86_400_000  # Past the hard deadline and the grace.
        with mock.patch.object(project_history.ProjectHistory, '_result_now', return_value=later):
            self.wait_for(lambda: self.store_rows(*tables) == (0, 0, 0, 0, 0), seconds=60)


    def test_034_R1g_rows_that_cannot_go_yet_never_hold_back_ones_that_can(self):
        """R1g (review 034-security): the purge took the oldest rows first and only then checked them, so
        rows of an open orchestration at the head of every batch held back every later one for good."""
        import time
        from unittest import mock
        from codex_agents import project_history
        runs = []
        for _ in range(2):
            context, route_path, orchestration, run_handle = self.open_run()
            self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
            (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
            self.record(context, route_path, run_handle, dispatched)
            runs.append(orchestration)
        self.wait_for(lambda: self.stopped_runs.count(('driver', 'retiring', 'released')) == 2, seconds=60)
        self.cli('learning-close-orchestration', '--orchestration-handle', runs[1], '--status', 'completed')
        original = project_history.ProjectHistory.expire_results

        def one_at_a_time(history, *, limit=100):
            return original(history, limit=1)
        later = time.time_ns() // 1_000_000 + 8 * 86_400_000  # Both retire; only the closed one may go.
        with mock.patch.object(project_history.ProjectHistory, '_result_now', return_value=later), \
                mock.patch.object(project_history.ProjectHistory, 'expire_results', one_at_a_time):
            self.wait_for(lambda: self.store_rows('result_reservations') == (1,), seconds=60)


    def test_034_R1g_a_pass_goes_past_a_row_that_does_not_verify_to_the_next(self):
        """R1g (re-review 034-security-2): a changed row is kept, and a valid row after it still goes."""
        import time
        from unittest import mock
        from codex_agents import project_history
        orchestrations = []
        for _ in range(2):
            context, route_path, orchestration, run_handle = self.open_run()
            self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
            (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
            self.record(context, route_path, run_handle, dispatched)
            orchestrations.append(orchestration)
        self.wait_for(lambda: self.stopped_runs.count(('driver', 'retiring', 'released')) == 2, seconds=60)
        for orchestration in orchestrations:
            self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
        self.wait_for(lambda: self.result_states() == ['expired', 'expired'], seconds=60)
        (path,) = list(self.state.root.rglob('history.sqlite3'))
        connection = sqlite3.connect(path, timeout=5)
        try:
            with connection:  # The first row's sealed deadline is changed; its seal no longer verifies.
                connection.execute('UPDATE result_reservations SET hard_deadline_at_ms=hard_deadline_at_ms-1 '
                                   'WHERE rowid=(SELECT MIN(rowid) FROM result_reservations)')
        finally:
            connection.close()
        later = time.time_ns() // 1_000_000 + 8 * 86_400_000
        with mock.patch.object(project_history.ProjectHistory, '_result_now', return_value=later):
            self.wait_for(lambda: self.store_rows('result_reservations') == (1,), seconds=60)
        self.assertEqual((1,), self.store_rows('result_reservations'))


    def test_034_R10_the_service_ends_earlier_runtimes_orchestrations_by_itself(self):
        """R10: the project service's own cleanup asks the store to end an earlier runtime's open
        orchestrations, with its current scope."""
        from unittest import mock
        from codex_agents import orchestration_store
        from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
        scopes = []
        original = orchestration_store.OrchestrationOwner.end_earlier

        def recorded(owner, scope, limit):
            scopes.append(scope)
            return original(owner, scope, limit)
        self.open_run()  # The project service is running.
        with mock.patch.object(orchestration_store.OrchestrationOwner, 'end_earlier', recorded):
            self.wait_for(lambda: scopes, seconds=30)
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, scopes[0].runtime_digest)

    def test_O0c_P5_the_service_ends_idle_orchestrations_by_itself(self):
        """O0c P5: the project service's own cleanup pass asks the store to end idle orchestrations."""
        from unittest import mock
        from codex_agents import orchestration_store
        calls = []
        original = orchestration_store.OrchestrationOwner.end_idle

        def recorded(owner, now, limit):
            calls.append((now, limit))
            return original(owner, now, limit)
        self.open_run()  # The project service is running.
        with mock.patch.object(orchestration_store.OrchestrationOwner, 'end_idle', recorded):
            self.wait_for(lambda: calls, seconds=30)
        self.assertEqual(10, calls[0][1])


class ResultLimitTests(DriverHarness):
    """R8 with small limits, set before the services open their stores."""

    def setUp(self):
        from unittest import mock
        from codex_agents import project_service
        from codex_agents.orchestration_api import OrchestrationLimits
        from codex_agents.result_contracts import ResultCapacity
        small = project_service._result_capacity()
        small = ResultCapacity(2, small.max_reserved_bytes, small.reservation_ms, small.retention_ms)
        for patcher in (mock.patch.object(project_service, 'ORCHESTRATIONS', OrchestrationLimits(2, 4_096)),
                        mock.patch.object(project_service, '_result_capacity', return_value=small)):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def test_034_R8_more_lifecycles_than_every_limit_never_fail_on_history(self):
        """R8: with the result and orchestration limits set to 2, four complete lifecycles in a row
        (open, dispatch, record, close) all succeed. Under 0.8.29 the third failed, because the limits
        counted every row ever written."""
        for number in range(4):
            with self.subTest(lifecycle=number):
                context, route_path, orchestration, run_handle = self.open_run()
                self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
                (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
                dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
                observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
                    {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
                self.cli('learning-record-host-result', *context, '--route', route_path,
                         '--result', self.write('result.json', dispatched['result']), '--observation', observation)
                self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs[-1:], seconds=30)
                self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
                capacity = self.cli('learning-status')['project_capacity']
                self.assertEqual((2, 2), (capacity['results']['limit'], capacity['orchestrations']['limit']))



class CollectorSweepTests(DriverHarness):
    """R2/R6 reaper-runs, account side, set up before the collector starts its loop."""

    def setUp(self):
        from unittest import mock
        from codex_agents import learning_store
        self.sweeps = []
        original = learning_store.LearningLedger.sweep_account_retention

        self.later = None

        def recorded(ledger, **options):
            self.sweeps.append(options)
            if self.later is not None:
                options['now_ms'] = self.later  # The loop's own call, with the clock moved on.
            return original(ledger, **options)
        for patcher in (mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 0.2),
                        mock.patch.object(learning_store.LearningLedger, 'sweep_account_retention', recorded)):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def test_034_R6_the_running_collector_sweeps_finished_account_rows_by_itself(self):
        """Review 034-security: the earlier test only searched the source. The collector's own loop
        calls the account retention sweep, with no command asking for it."""
        self.wait_for(lambda: len(self.sweeps) >= 2, seconds=30)

    def test_034_R6_the_running_collector_removes_a_finished_observation(self):
        """Review 034-security-3: the loop's sweep is shown to remove a row, not only to be called."""
        import time

        def origins():
            return self.collector_ledger._connection.execute(
                'SELECT COUNT(*) FROM account_observation_origins').fetchone()[0]
        self.cli('provider-observe', '--provider', 'codex')
        self.assertGreaterEqual(origins(), 1)
        self.later = time.time_ns() // 1_000_000 + 2 * 86_400_000  # Past its deadline and the replay day.
        self.wait_for(lambda: origins() == 0, seconds=30)


if __name__ == '__main__':
    unittest.main()
