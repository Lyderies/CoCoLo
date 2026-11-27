"""B5a driver ordering and plain status, with real store/journal and an in-memory account adapter."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_account_settlement as fixtures
from tests import test_crashed_settlement as crashed
from tests import test_publication_replay as replay
from codex_agents import client_runtime, launch_journal, project_driver


class CrashedDriverTests(fixtures.SettlementFixture):
    def driver(self):
        self.bound()
        journal = crashed.CrashedSettlementTests.journal(self, gone=False)
        entry = journal.entry(replay.claim_binding().claim_id)
        journal._write(entry['bridge_run_id'], {**entry, 'storage_evidence': fixtures.canonical_json(
            {'lease': {'lease_digest': self.store.result_lease(self.lease()).lease_digest}}).hex()})
        driver = object.__new__(project_driver.RunDriver)
        driver._journal = journal
        driver._scope = SimpleNamespace(project_id=self.store.project_id)
        driver._call = SimpleNamespace()
        driver._account = Path('/unused-no-socket')
        driver._containers = None
        driver._container_recovery = lambda: None
        driver._settled_retry = {}
        driver._settled_checked = frozenset()
        driver._remove_workspace = mock.Mock(return_value=True)
        request_id = 'lost-' + hashlib.sha256(replay.claim_binding().claim_id.encode()).hexdigest()[:32]
        driver._arbitrate_publication = lambda lease, entry: self.store.arbitrate_publication(
            lease.run_handle, lease.attempt, request_id)
        driver._settlement_step = lambda operation, *args, **kwargs: getattr(self.store, operation)(*args, **kwargs)
        return driver

    def account(self, journal):
        account = mock.Mock()
        requests = []

        def settle(value, *, request_id):
            self.assertEqual('gone', journal.entry(value.bridge_run_id)['state'])
            requests.append((request_id, value.payload()))
            return {'state': 'released', 'known': len(requests) > 1}

        def closure(value, *, request_id):
            opened = self.store.open_account_settlement(value.run_handle, 1, request_id, value.payload(),
                                                        runtime_digest=project_driver.COLLECTOR_RUNTIME_DIGEST)
            return json.loads(self.closure(kind='crashed', request_digest=opened['request_digest']))

        account.settle_lost_run.side_effect = settle
        account.lost_run_closure.side_effect = closure
        return account, requests

    def test_sweep_records_gone_then_closure_before_workspace_and_settled_journal(self):
        driver = self.driver()
        account, requests = self.account(driver._journal)

        def workspace(path):
            self.assertEqual('crashed', self.settlement()[1])
            self.assertEqual('gone', driver._journal.entry(replay.claim_binding().claim_id)['state'])
            return True

        driver._remove_workspace.side_effect = workspace
        with mock.patch.object(project_driver, 'tree_state', return_value='gone'), \
                mock.patch.object(launch_journal, 'tree_state', return_value='gone'), \
                mock.patch.object(project_driver, 'AccountClient', return_value=account):
            lost = driver._settle_lost(True)
        self.assertEqual(('codex-host-v1', 'released'), lost[self.lease().run_handle])
        self.assertEqual('settled', driver._journal.entry(replay.claim_binding().claim_id)['state'])
        self.assertEqual(1, len(requests))

    def test_stop_after_account_reply_before_record_replays_identical_request(self):
        driver = self.driver()
        account, requests = self.account(driver._journal)
        original = driver._settlement_step

        def stopped(operation, *args, **kwargs):
            if operation == 'record_account_settlement':
                raise fixtures.HistoryError('history_service_unavailable')
            return original(operation, *args, **kwargs)

        driver._settlement_step = stopped
        with mock.patch.object(project_driver, 'tree_state', return_value='gone'), \
                mock.patch.object(launch_journal, 'tree_state', return_value='gone'), \
                mock.patch.object(project_driver, 'AccountClient', return_value=account):
            self.assertEqual(('codex-host-v1', 'held'), driver._settle_lost(True)[self.lease().run_handle])
            self.assertIsNone(self.settlement())
            driver._remove_workspace.assert_not_called()
            driver._settlement_step = original
            driver._settle_lost(True)
        self.assertEqual(2, len(requests))
        self.assertEqual(requests[0], requests[1])
        self.assertEqual('crashed', self.settlement()[1])

    def test_failed_proof_recording_never_calls_account_or_removes_workspace(self):
        driver = self.driver()
        account, _ = self.account(driver._journal)
        with mock.patch.object(project_driver, 'tree_state', return_value='gone'), \
                mock.patch.object(launch_journal, 'tree_state', return_value='unknown'), \
                mock.patch.object(project_driver, 'AccountClient', return_value=account):
            self.assertEqual(('codex-host-v1', 'held'), driver._settle_lost(True)[self.lease().run_handle])
        account.settle_lost_run.assert_not_called()
        driver._remove_workspace.assert_not_called()
        self.assertIsNone(self.settlement())

    def test_stop_after_project_commit_reuses_receipt_before_workspace_and_journal(self):
        driver = self.driver()
        account, _ = self.account(driver._journal)
        driver._remove_workspace.return_value = False
        with mock.patch.object(project_driver, 'tree_state', return_value='gone'), \
                mock.patch.object(launch_journal, 'tree_state', return_value='gone'), \
                mock.patch.object(project_driver, 'AccountClient', return_value=account):
            self.assertEqual(('codex-host-v1', 'held'), driver._settle_lost(True)[self.lease().run_handle])
            self.assertEqual('crashed', self.settlement()[1])
            self.assertEqual('gone', driver._journal.entry(replay.claim_binding().claim_id)['state'])
            before = self.snapshot()
            account.settle_lost_run.side_effect = RuntimeError('account unavailable after local commit')
            driver._remove_workspace.return_value = True
            self.assertEqual(('codex-host-v1', 'released'), driver._settle_lost(True)[self.lease().run_handle])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, account.lost_run_closure.call_count)
        self.assertEqual('settled', driver._journal.entry(replay.claim_binding().claim_id)['state'])

    def test_fence_admission_is_recorded_before_unbound_crash_settlement(self):
        self.armed()
        journal = launch_journal.LaunchJournal(self.store._directory / 'launches')
        self.addCleanup(journal.release)
        binding, bridge, token = self.lease(), replay.claim_binding().claim_id, launch_journal.new_process_token()
        journal.process_intent(token, run_handle=binding.run_handle, attempt=binding.attempt,
                               host_client='codex-host-v1', lease=binding.as_data(), identity_digest=fixtures.IDENTITY,
                               claim_request_id=binding.claim_request_id, storage_evidence=b'{}', workspace=None)
        driver = object.__new__(project_driver.RunDriver)
        driver._journal = journal
        driver._earlier_admitted = {binding.claim_request_id: {'state': 'claimed', 'bridge_run_id': bridge}}
        driver._intent_fences = {}

        def settle(entry):
            self.assertEqual('claimed', journal.entry(bridge)['state'])
            self.open()
            self.record_settlement(kind='crashed')
            return {'state': 'released'}

        def workspace(path):
            self.assertEqual((None, 'crashed'), self.settlement()[:2])
            return True

        driver._settle_entry, driver._remove_workspace = settle, workspace
        self.assertEqual('settled', driver._settle_intent(journal.entry(token), False))
        self.assertEqual('settled', journal.entry(bridge)['state'])
        self.assertEqual('settled', journal.entry(token)['state'])
        self.consistent()

    def test_recovered_admission_keeps_stopped_instance_for_next_sweep_after_failure(self):
        self.armed()
        old = launch_journal.LaunchJournal(self.store._directory / 'launches')
        self.addCleanup(old.release)
        binding, bridge, token = self.lease(), replay.claim_binding().claim_id, launch_journal.new_process_token()
        old.process_intent(token, run_handle=binding.run_handle, attempt=binding.attempt,
                           host_client='codex-host-v1', lease=binding.as_data(), identity_digest=fixtures.IDENTITY,
                           claim_request_id=binding.claim_request_id, storage_evidence=b'{}', workspace=None)
        entry, stopped_instance = old.entry(token), old.instance
        old.release()
        journal = launch_journal.LaunchJournal(self.store._directory / 'launches')
        self.addCleanup(journal.release)
        driver = object.__new__(project_driver.RunDriver)
        driver._journal = journal
        driver._earlier_admitted = {binding.claim_request_id: {'state': 'claimed', 'bridge_run_id': bridge}}
        driver._settle_entry = mock.Mock(side_effect=fixtures.HistoryError('history_service_unavailable'))
        self.assertIsNone(driver._settle_intent(entry, False))
        self.assertEqual(stopped_instance, journal.entry(bridge)['instance'])
        self.assertNotEqual(journal.instance, journal.entry(bridge)['instance'])


class CrashedStatusTests(unittest.TestCase):
    def test_lost_capacity_status_explains_held_and_released_in_plain_words(self):
        for reason, words in (('run_lost_held', 'capacity stays held'), ('run_lost', 'capacity was released')):
            with self.subTest(reason=reason), \
                    mock.patch.object(client_runtime, 'CollectorPaths'), \
                    mock.patch.object(client_runtime, 'read_runtime_hint', return_value=(None, '/unused/control', None)), \
                    mock.patch.object(client_runtime, 'enroll_clients', return_value={}), \
                    mock.patch.object(client_runtime, '_run_driver', return_value={'state': 'failed', 'reason_code': reason}):
                answer = client_runtime._lost_run(Path('/unused'), Path('/project'), 'hrn_' + 'a' * 48, 'codex-host-v1')
                self.assertIn(words, answer['error'])
                self.assertEqual('unknown', answer['outcome'])
                self.assertEqual({'ready', 'reason_code', 'terminal_state', 'outcome', 'capacity_state', 'error'},
                                 set(answer))


if __name__ == '__main__':
    unittest.main()
