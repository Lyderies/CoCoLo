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


if __name__ == '__main__':
    unittest.main()
