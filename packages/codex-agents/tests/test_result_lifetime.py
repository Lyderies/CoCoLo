"""0.3.4: a project's results never fill its store for good (DESIGN-0.3.4-result-capacity.md).

Integration stage: the in-process collector, account and project services run a real bridged run.
"""

import json
import sqlite3
import unittest

from tests.test_bridge import null_filled, result
from tests.test_project_driver import DriverHarness


class ResultLifetimeTests(DriverHarness):
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


    def test_034_R9_status_shows_each_project_limit_with_its_live_count(self):
        """R9: learning-status shows, for this project's store, how near each limit is."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        capacity = self.cli('learning-status')['project_capacity']
        self.assertEqual({'results', 'orchestrations', 'cleanup_reservations', 'operational_qa', 'qa_check_receipts'},
                         set(capacity))
        for name, limit in (('results', 64), ('orchestrations', 256), ('cleanup_reservations', 4096),
                            ('operational_qa', 16_384), ('qa_check_receipts', 16_384)):
            self.assertEqual(limit, capacity[name]['limit'], name)
        self.assertEqual(1, capacity['results']['live'])
        self.assertEqual(1, capacity['orchestrations']['live'])
        self.assertEqual(1, capacity['cleanup_reservations']['live'])
        self.assertEqual(0, capacity['operational_qa']['live'])


    def test_034_R9_status_shows_each_account_limit_with_its_live_count(self):
        """R9, account part: observations and unreleased cleanup receipts, with their limits."""
        context, route_path, _, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        capacity = self.cli('learning-status')['account_capacity']
        self.assertEqual({'observations', 'cleanup_receipts'}, set(capacity))
        for name in capacity:
            self.assertEqual(4096, capacity[name]['limit'], name)
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
            total = 0
            for path in self.state.root.rglob('history.sqlite3'):
                connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
                try:
                    total += connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                except sqlite3.Error:
                    pass
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


if __name__ == '__main__':
    unittest.main()
