"""B5a: real account claim/release/closure stores with no sockets, CLI or git."""
import unittest
from unittest import mock

from tests import test_account_claim_storage as fixtures
from tests import test_bridge as bridge_fixture
from codex_agents import launch_journal
from codex_agents.account_api import AccountRequest, account_request_digest, decode_account_response
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.learning_contracts import canonical_json
from codex_agents.lost_run_contracts import SettleLostInput


class CrashedAccountTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimStorageTests(methodName='runTest')
        # Account metadata does not depend on a git repository.
        with mock.patch.object(bridge_fixture, 'git', return_value=''):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        lease = f.arm()
        self.receipt = f.call()
        self.settle = SettleLostInput(f.scope.run_handle, f.base.launch.identity_digest, f.scope.claim_request_id,
                                      self.receipt['bridge_run_id'], lease.lease_digest)
        self.request = AccountRequest(1, 'account.lost_run_closure', 'crashed-closure', f.project,
                                      f.scope.runtime_digest, self.settle.payload())
        self.directory = f.ledger.state.root / 'projects' / f.project / 'launches'
        self.directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal = launch_journal.LaunchJournal(self.directory)
        self.addCleanup(self.journal.release)
        bridge = self.receipt['bridge_run_id']
        self.journal.claimed(bridge, run_handle=f.scope.run_handle, attempt=f.scope.attempt,
                            host_client='codex-host-v1', lease=f.scope.as_data(),
                            identity_digest=f.base.launch.identity_digest, claim_request_id=f.scope.claim_request_id,
                            storage_evidence=f.evidence, workspace=None)
        self.journal.spawning(bridge)
        self.journal.launched(bridge, 2000000001, '1.0', '1' * 32)
        self.journal.watched(bridge, 2000000002, '2.0')
        from codex_agents.learning_contracts import keyed_digest
        handle = keyed_digest(f.key, f.scope.run_handle)
        with f.ledger._account_transaction():
            f.ledger._release_slot_locked(f.project, handle, 'crashed', f.base.stamp,
                                          f.ledger._capacity_clock_reference())

    def closure(self):
        f = self.fixture
        return f.base.views.lost_run_closure(f.project, runtime_digest=f.scope.runtime_digest, caller_scope='a' * 64,
                                         settle=self.settle, storage_binding=f.scope, storage_evidence=f.evidence,
                                         request_digest=account_request_digest(self.request))

    def test_crashed_account_release_alone_has_no_closure(self):
        with self.assertRaises(ClaimError) as caught:
            self.closure()
        self.assertEqual('claim_cleanup_unavailable', caught.exception.reason_code)

    def test_recorded_process_gone_gets_repeatable_crashed_closure_and_existing_wire_shape(self):
        with mock.patch.object(launch_journal, 'tree_state', return_value='gone'):
            self.journal.process_gone(self.receipt['bridge_run_id'])
        first = self.closure()
        self.assertEqual('crashed', first['release_kind'])
        self.assertEqual(first, self.closure())
        from codex_agents import reply_shapes
        self.assertTrue(reply_shapes.reply_fields_match(self.request.operation, first))
        response = canonical_json({'protocol_version': 1, 'request_id': self.request.request_id,
                                   'project_id': self.request.project_id, 'runtime_digest': self.request.runtime_digest,
                                   'request_digest': account_request_digest(self.request), 'ready': True,
                                   'result': first, 'reason_code': None})
        self.assertEqual(first, decode_account_response(response, self.request))
        self.journal.settled(self.receipt['bridge_run_id'], 'released')
        self.assertEqual(first, self.closure())

    def test_mismatched_recorded_lease_cannot_authorize_crashed_closure(self):
        bridge = self.receipt['bridge_run_id']
        with mock.patch.object(launch_journal, 'tree_state', return_value='gone'):
            self.journal.process_gone(bridge)
        entry = self.journal.entry(bridge)
        self.journal._write(bridge, {**entry, 'lease': {**entry['lease'], 'attempt': 2}})
        with self.assertRaises(ClaimError):
            self.closure()

    def test_live_instance_never_spawned_entry_cannot_authorize_crashed_closure(self):
        bridge = self.receipt['bridge_run_id']
        entry = self.journal.entry(bridge)
        self.journal._write(bridge, {**entry, 'state': 'claimed', 'child': None, 'watchdog': None, 'run_tag': None})
        with self.assertRaises(ClaimError) as caught:
            self.closure()
        self.assertEqual('claim_cleanup_unavailable', caught.exception.reason_code)

    def test_never_spawned_admission_gets_same_closure_after_journal_settlement(self):
        bridge = self.receipt['bridge_run_id']
        entry = self.journal.entry(bridge)
        self.journal._write(bridge, {**entry, 'state': 'claimed', 'child': None, 'watchdog': None, 'run_tag': None})
        self.journal.release()
        first = self.closure()
        self.assertEqual('crashed', first['release_kind'])
        self.journal = launch_journal.LaunchJournal(self.directory)
        self.addCleanup(self.journal.release)
        self.journal.settled(bridge, 'released')
        self.assertEqual(first, self.closure())


if __name__ == '__main__':
    unittest.main()
