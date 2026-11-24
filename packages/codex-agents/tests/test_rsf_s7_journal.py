"""RSF-S7-4: expiry of real v1/v2 journal entries cannot erase store replay."""
import json
from dataclasses import replace

from tests.rsf_s7_support import ProjectFixture
from codex_agents.launch_journal import LaunchJournal, TOMBSTONE_MS
from codex_agents.history_contracts import HistoryError
from codex_agents.lost_run_contracts import SettleLostInput


class JournalExpiryReplayTests(ProjectFixture):
    def test_committed_body_expiry_and_journal_expiry_preserve_the_original_answer(self):
        from tests.test_publication_replay import claim_binding
        self.bound()
        self.commit()
        self.open(arbitrate=False)
        payload = self.payload()
        raw = self.closure(kind='released_unacknowledged')
        first = self.record_settlement(raw=raw)
        answer = self.open(arbitrate=False)
        self.assertTrue(self.fixture.files())
        target = claim_binding(1)
        journal = LaunchJournal(self.fixture.directory / 'body-expiry-launches')
        self.addCleanup(journal.release)
        journal.claimed(target.claim_id, run_handle=target.run_handle, attempt=target.attempt,
            host_client='codex-host-v1', lease=self.lease().as_data(), identity_digest='d' * 64,
            claim_request_id=self.lease().claim_request_id, storage_evidence=b'{}', workspace=None)
        journal.settled(target.claim_id, 'released', now_ms=1)
        self.migrate()
        self.now += self.retention_ms + 1
        self.assertGreaterEqual(self.store.expire_results(), 1)
        self.assertEqual([], self.fixture.files())
        self.assertEqual(frozenset({target.claim_id}), journal.prune(now_ms=2 + TOMBSTONE_MS))
        self.prune()
        self.reopen()
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(answer, self.store.open_account_settlement(*self.attempt(), self.request_id(), payload,
                                                                       runtime_digest='a' * 64))
            self.assertEqual({**first, 'known': True}, self.store.record_account_settlement(self.lease(), raw))
        with self.assertRaises(HistoryError):
            self.store.open_account_settlement(*self.attempt(), self.request_id(), {**payload, 'lease_digest': 'e' * 64},
                                               runtime_digest='a' * 64)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], journal.entries())

    def test_v1_and_v2_expiry_leave_the_original_reply_and_identity_in_the_store(self):
        self.bound()
        self.open()
        raw = self.closure()
        first = self.record_settlement(raw=raw)
        answer = self.open(arbitrate=False)
        closure = json.loads(raw)
        payload = SettleLostInput(self.lease().run_handle, 'd' * 64, self.lease().claim_request_id,
            closure['bridge_run_id'], closure['lease_digest']).payload()
        directory = self.fixture.directory / 'launches'
        journal = LaunchJournal(directory)
        self.addCleanup(journal.release)
        arguments = dict(run_handle=self.lease().run_handle, attempt=1, host_client='codex-host-v1',
            lease=self.lease().as_data(), identity_digest='d' * 64, claim_request_id=self.lease().claim_request_id,
            storage_evidence=b'{}', workspace=None)
        journal.claimed(closure['bridge_run_id'], **arguments)
        token = 'lnc_' + '1' * 32
        project = self.lease().project_id
        journal.intent(token, project_id=project, container_name='cocolo-run-' + '1' * 32,
            labels={'cocolo.project': project, 'cocolo.run': token, 'cocolo.kind': 'run'}, **arguments)
        journal.settled(closure['bridge_run_id'], 'settled', now_ms=1)
        journal.settled(token, 'fenced', now_ms=1)
        journal.release()
        journal = LaunchJournal(directory)
        self.addCleanup(journal.release)
        self.assertEqual((1, 2), (journal.entry(closure['bridge_run_id'])['version'], journal.entry(token)['version']))
        self.assertEqual(frozenset(), journal.prune(now_ms=1 + TOMBSTONE_MS))
        self.assertEqual(frozenset({closure['bridge_run_id'], token}), journal.prune(now_ms=2 + TOMBSTONE_MS))
        self.assertIsNone(journal.entry(token))
        self.assertIsNone(journal.entry(closure['bridge_run_id']))
        self.migrate()
        self.prune()
        self.reopen()
        before = self.snapshot()
        self.assertEqual(answer, self.store.open_account_settlement(*self.attempt(), self.request_id(), payload,
                                                                   runtime_digest='a' * 64))
        self.assertEqual({**first, 'known': True}, self.store.record_account_settlement(self.lease(), raw))
        with self.assertRaises(HistoryError):
            self.store.open_account_settlement(*self.attempt(), self.request_id(), {**payload, 'identity_digest': 'e' * 64},
                                               runtime_digest='a' * 64)
        with self.assertRaises(HistoryError):
            self.store.record_account_settlement(replace(self.lease(), supervisor_generation=2), raw)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], journal.entries())
