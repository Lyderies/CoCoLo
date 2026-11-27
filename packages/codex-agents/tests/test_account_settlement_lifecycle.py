"""Slice B4s (RS-B-B4-1, -8): closure authority, status agreement and the lost-lifecycle limits through the real driver.

Split out of test_account_settlement_driver (TESTSPEED-3, 3) with the same harness (imported, not copied): only the
run's own project role gets its closure and only once it is settled; learning status and the report agree on a
settled lost run; three lost lifecycles settle, close and purge and a fourth runs to the end.
Integration stage: real services, sockets and stores.
"""
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.learning_support import attest_provider
from tests.test_account_settlement_driver import SettlementHarness, WHY, requirements
from tests.test_bridge import CLEAN, git, null_filled, result

from codex_agents import learning_store, project_driver
from codex_agents.account_api import AccountError
from codex_agents.active_run_capacity import ActiveRunLimits
from codex_agents.hosts import OWNER_CLIENT
from codex_agents.learning_contracts import canonical_json

@requirements('RS-B-B4')
class ClosureAuthorityTests(SettlementHarness):
    """RS-B-B4-1 / RS-B-B4-6: only the run's own project role gets its closure, and only once it is settled."""

    def admit(self, provider):
        self.second = Path(self.temporary.name) / 'second'
        self.second.mkdir()
        (self.second / 'source.txt').write_text('second marker\n')
        git(self.second, 'init', '-q')
        git(self.second, 'config', 'user.email', 'tests@example.invalid')
        git(self.second, 'config', 'user.name', 'Bridge Tests')
        git(self.second, 'add', '.')
        git(self.second, 'commit', '-qm', 'second')
        fingerprint = super().admit(provider)
        self.second_id = self.ledger.register_project(self.second, now_ms=1)
        identity = self.ledger.observe_provider(self.second_id, provider, project_root=self.tools, environ=CLEAN)[0]
        attest_provider(self.ledger, self.second_id, self.owner_key, provider, identity['account_fingerprint'])
        return fingerprint

    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3', 'TESTSPEED3-4')
    def test_B4_1_only_the_runs_own_project_role_gets_its_closure_once_settled(self):
        from codex_agents.lost_run_closure import verify_lost_run_closure
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'held'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: self.journal_states() == ['watched'], seconds=30)
        entry = json.loads(next(self.journal().glob('brg_*.json')).read_text())
        own = {'role': 'project', 'host_client': entry['host_client'], 'root': self.project, 'project_id': self.project_id}
        refused = []
        for options in ({**own, 'role': 'account'}, {**own, 'role': 'account', 'host_client': OWNER_CLIENT},
                        {**own, 'root': self.second, 'project_id': self.second_id}, {**own, 'root': self.second}):
            with self.assertRaises(AccountError) as caught:
                self.call_as('lost_run_closure', entry, **options)
            refused.append(caught.exception.reason_code)
        self.assertEqual(['account_not_authorized'] * 4, refused)
        with self.assertRaises(AccountError) as caught:  # not settled: no closure exists yet
            self.call_as('lost_run_closure', entry, **own)
        self.assertEqual('claim_cleanup_unavailable', caught.exception.reason_code)
        other = 'hrn_' + ('0' * 47 + '1' if not entry['run_handle'].endswith('1') else '0' * 48)
        with self.assertRaises(AccountError):  # another handle with this run's custody
            self.call_as('lost_run_closure', entry, **own, handle=other)
        self.assertEqual('settled', self.call_as('settle_lost_run', entry, **own)['state'])
        closure = self.call_as('lost_run_closure', entry, **own)
        self.assertEqual(closure, self.call_as('lost_run_closure', entry, **own))  # the same answer each time
        checked = verify_lost_run_closure(self.state.digest_key(), canonical_json(closure), project_id=self.project_id)
        self.assertEqual(('lost', entry['bridge_run_id']), (checked['release_kind'], checked['bridge_run_id']))
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        dispatch.communicate(timeout=30)


class StatusAgreementTests(SettlementHarness):
    """T7 S2 finding 2 (L14.1): learning-status, learning-run-status and learning-orchestration-report read the same
    truth. The orchestrations are the project store's (its service), and a lost run the account settled is ended,
    abandoned, in the report as in learning-status, never pending or active."""

    def lifecycle_counts(self):
        return self.cli_any('learning-status')['ingestion']['managed_lifecycle']

    def orchestration_report(self, orchestration):
        status = self.cli('learning-orchestration-status', '--orchestration-handle', orchestration)
        identity = status['current']['snapshot']['orchestration_id']
        return self.cli('learning-orchestration-report', '--orchestration-id', identity)

    @__import__('test_framework').requirements('L14.1', why_not_scenario=WHY)
    def test_L14_1_learning_status_counts_the_projects_orchestrations_by_state(self):
        names = ('orchestrations_active', 'orchestrations_paused', 'orchestrations_closed')
        self.assertEqual((0, 0, 0), tuple(self.lifecycle_counts()[name] for name in names))
        first = self.cli('learning-open-orchestration')['orchestration_handle']
        self.assertEqual((1, 0, 0), tuple(self.lifecycle_counts()[name] for name in names))
        second = self.cli('learning-open-orchestration')['orchestration_handle']
        self.cli('learning-pause-orchestration', '--orchestration-handle', second)
        self.assertEqual((1, 1, 0), tuple(self.lifecycle_counts()[name] for name in names))
        self.cli('learning-close-orchestration', '--orchestration-handle', first, '--status', 'completed')
        self.assertEqual((0, 1, 1), tuple(self.lifecycle_counts()[name] for name in names))

    @__import__('test_framework').requirements('L14.1', why_not_scenario=WHY)
    def test_L14_1_a_settled_lost_run_reads_abandoned_in_the_report_as_in_learning_status(self):
        context, route_path, orchestration, run_handle = self.lose('consuming')
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assert_lost_status(orchestration, run_handle, 'released')
        managed = self.lifecycle_counts()
        self.assertEqual((0, 1), (managed['host_runs_pending'], managed['host_runs_abandoned']))
        runs = self.orchestration_report(orchestration)['runs']
        self.assertEqual({'opened': 1, 'pending': 0, 'current_active': 0, 'abandoned': 1, 'terminal': 0},
                         {name: runs[name] for name in ('opened', 'pending', 'current_active', 'abandoned', 'terminal')})
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        report = self.orchestration_report(orchestration)
        self.assertEqual(('failed', 0, 0, 1), (report['status'], report['runs']['pending'],
                                               report['runs']['current_active'], report['runs']['abandoned']))


@requirements('RS-B-B4')
class LostLifecycleLimitTests(SettlementHarness):
    """RS-B-B4-8 (O0c P2-P4, ANALYSIS-0.4.0-lifetime-caps.md:180-184), in the shape of 0.3.4's ResultLimitTests: every
    bound 2; three lifecycles lost after their claim are settled, close and purge; a fourth runs to the end."""

    def setUp(self):
        from codex_agents import project_service
        from codex_agents.orchestration_api import OrchestrationLimits
        from codex_agents.result_contracts import ResultCapacity
        small = project_service._result_capacity()
        small = ResultCapacity(2, small.max_reserved_bytes, small.reservation_ms, small.retention_ms)
        for patcher in (mock.patch.object(project_service, 'ORCHESTRATIONS', OrchestrationLimits(2, 4_096)),
                        mock.patch.object(project_service, '_result_capacity', return_value=small),
                        mock.patch.object(project_driver, 'EXPIRY_SECONDS', 0.5),
                        mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(2, 2))):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def rows(self):
        return tuple(self.store(f'SELECT COUNT(*) FROM {table}')[0][0] for table in (
            'result_reservations', 'result_leases', 'project_account_settlements'))

    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3', 'TESTSPEED3-4', 'TESTSPEED3-5')
    def test_B4_8_three_lost_lifecycles_settle_close_and_purge_and_a_fourth_completes(self):
        from codex_agents import project_history
        for number in range(3):
            with self.subTest(lost=number):
                _, _, orchestration, run_handle = self.lose('consuming')
                self.wait_for(lambda: (run_handle, 1, 'lost') in self.settlements(), seconds=30)
                self.wait_for(lambda: self.store('SELECT state FROM result_reservations WHERE run_handle=?',
                                                 (run_handle,)) == [('expired',)], seconds=30)
                self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        later = time.time_ns() // 1_000_000 + 8 * 86_400_000  # past every hard deadline and the grace
        with mock.patch.object(project_history.ProjectHistory, '_result_now', return_value=later):
            self.wait_for(lambda: self.rows() == (0, 0, 0), seconds=60)
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs[-1:], seconds=30)
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')


if __name__ == '__main__':
    unittest.main()
