"""RS-B-B5a: real project stores, durable process evidence, no sockets or git."""
import dataclasses
import json
import unittest
from unittest import mock

from tests import test_account_settlement as fixtures
from tests import test_publication_replay as replay
from codex_agents import launch_journal, project_holds_schema
from codex_agents.history_contracts import HistoryError


def requirements(*ids):
    return __import__('test_framework').requirements(
        *ids, why_not_scenario='Real temporary project stores and protected launch journals; no service or sockets.')


@requirements('RS-B-B5a')
class CrashedSettlementTests(fixtures.SettlementFixture):
    def journal(self, number=1, *, gone=True, changes=None):
        journal = launch_journal.LaunchJournal(self.store._directory / 'launches')
        self.addCleanup(journal.release)
        binding = self.lease(number)
        bridge = replay.claim_binding(number).claim_id
        journal.claimed(bridge, run_handle=binding.run_handle, attempt=binding.attempt, host_client='codex-host-v1',
                        lease=binding.as_data(), identity_digest=fixtures.IDENTITY,
                        claim_request_id=binding.claim_request_id, storage_evidence=b'{}', workspace=None)
        journal.spawning(bridge)
        journal.launched(bridge, 2000000001, '1.0', '1' * 32)
        journal.watched(bridge, 2000000002, '2.0')
        if gone:
            with mock.patch.object(launch_journal, 'tree_state', return_value='gone'):
                journal.process_gone(bridge)
        if changes:
            journal._write(bridge, {**journal.entry(bridge), **changes})
        return journal

    def test_V3_crashed_closure_without_process_evidence_never_releases_on_age(self):
        self.bound()
        self.open()
        raw = self.closure(kind='crashed')
        self.now += 100 * 86_400_000
        before = self.snapshot()
        with self.assertRaises(HistoryError) as caught:
            self.record_settlement(raw=raw)
        self.assertEqual('history_conflict', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())

    def test_V7_pruning_the_proof_first_is_refused_without_writes(self):
        self.bound()
        self.open()
        journal = self.journal()
        journal.remove(replay.claim_binding().claim_id)
        raw, before = self.closure(kind='crashed'), self.snapshot()
        with self.assertRaises(HistoryError):
            self.record_settlement(raw=raw)
        self.assertEqual(before, self.snapshot())

    def test_X4_uncommitted_crash_retires_and_records_proof_exactly_once(self):
        self.bound()
        opened = self.open()
        journal = self.journal()
        raw, counts = self.closure(kind='crashed'), self.counts()
        first = self.record_settlement(raw=raw)
        self.assertEqual(('crashed', False), (first['release_kind'], first['known']))
        self.assertEqual(('released', 'crashed', first['receipt_digest']),
                         self.hold('reservation', replay.claim_binding().claim_id)[:3])
        evidence = self.store._connection.execute(
            "SELECT evidence FROM project_holds WHERE kind='reservation'").fetchone()[0]
        self.assertEqual('gone', json.loads(evidence)['state'])
        self.assertEqual('retiring', self.row_state())
        self.assertEqual(('released', 'lost_with_reservation'), self.custody())
        self.assertEqual(counts, self.counts())
        self.assertEqual(opened['request_digest'], self.hold('reservation', replay.claim_binding().claim_id)[4])
        journal.remove(replay.claim_binding().claim_id)
        self.successor()
        before = self.snapshot()
        self.assertEqual({**first, 'known': True}, self.record_settlement(raw=raw))
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.store._attempt_settled(self.store.result_lease(self.lease())))
        self.consistent()

    def test_X4_committed_crash_preserves_body_and_custody(self):
        self.bound()
        self.commit()
        self.open()
        self.journal()
        material = self.material()
        self.record_settlement(kind='crashed')
        self.assertEqual('committed', self.row_state())
        self.assertEqual(material, self.material())
        self.assertEqual(('held', None), self.custody())
        self.consistent()

    def test_crashed_tombstone_replays_after_attempt_purge_without_journal(self):
        self.bound()
        self.open()
        journal = self.journal()
        raw = self.closure(kind='crashed')
        first = self.record_settlement(raw=raw)
        journal.remove(replay.claim_binding().claim_id)
        self.store.expire_results()
        self.now += self.retention_ms + 1
        self.store.expire_results()
        with self.store._transaction():
            target = replay.claim_binding()
            self.store._delete_attempt_rows(target.run_handle, target.attempt, target.claim_id)
        before = self.snapshot()
        self.assertEqual({**first, 'known': True}, self.record_settlement(raw=raw))
        self.assertEqual(before, self.snapshot())

    def test_wrong_journal_identity_and_unconfirmed_watched_entry_are_refused(self):
        self.bound()
        self.open()
        journal = self.journal(gone=False)
        bridge = replay.claim_binding().claim_id
        raw = self.closure(kind='crashed')
        original = journal.entry(bridge)
        other = dataclasses.replace(self.lease(), attempt=2).as_data()
        cases = ({}, {'state': 'gone', 'lease': other}, {'state': 'gone', 'attempt': 2},
                 {'state': 'gone', 'claim_request_id': 'another-request'})
        for changes in cases:
            with self.subTest(changes=changes):
                journal._write(bridge, {**original, **changes})
                before = self.snapshot()
                with self.assertRaises(HistoryError):
                    self.record_settlement(raw=raw)
                self.assertEqual(before, self.snapshot())

    def test_authenticated_crashed_closure_must_match_request_and_key(self):
        self.bound()
        self.open()
        self.journal()
        before = self.snapshot()
        for changes in ({'request_digest': 'f' * 64}, {'key': b'x' * 32},
                        {'bridge_run_id': 'brg_' + 'f' * 32}, {'lease_digest': 'e' * 64}):
            with self.subTest(changes=sorted(changes)), self.assertRaises(HistoryError):
                self.record_settlement(kind='crashed', **changes)
            self.assertEqual(before, self.snapshot())

    def test_stop_after_account_reply_replays_same_outbox_and_kept_proof(self):
        self.bound()
        first = self.open()
        self.journal()
        raw = self.closure(kind='crashed')
        self.successor()
        self.assertEqual(first, self.open())
        self.assertFalse(self.record_settlement(raw=raw)['known'])
        self.assertTrue(self.record_settlement(raw=raw)['known'])

    def test_admitted_never_spawned_claim_keeps_b4s_lost_unbound_path(self):
        self.armed()
        self.open()
        self.record_settlement()
        identity = project_holds_schema.attempt_identity(*self.attempt())
        self.assertEqual(('released', 'lost_unbound'), self.hold('lease', identity)[:2])
        self.assertEqual('expired', self.store.result_lease(self.lease()).state)
        self.consistent()

    def test_unreadable_or_container_evidence_cannot_release_a_crashed_hold(self):
        self.bound()
        self.open()
        journal = self.journal()
        raw = self.closure(kind='crashed')
        path = journal._path(replay.claim_binding().claim_id)
        path.chmod(0o644)
        before = self.snapshot()
        with self.assertRaises(HistoryError):
            self.record_settlement(raw=raw)
        self.assertEqual(before, self.snapshot())
        path.chmod(0o600)
        with mock.patch.object(launch_journal, '_read_entry_or_none', return_value={'version': 2}):
            with self.assertRaises(HistoryError):
                self.record_settlement(raw=raw)
        self.assertEqual(before, self.snapshot())

    def test_already_settled_process_entry_retains_evidence_for_first_project_record(self):
        self.bound()
        self.open()
        journal = self.journal()
        journal.settled(replay.claim_binding().claim_id, 'released')
        self.record_settlement(kind='crashed')
        self.assertEqual('crashed', self.settlement()[1])
        self.consistent()

    def test_A5_L6b_crashed_admitted_unbound_claim_releases_lease_only_with_never_spawned_evidence(self):
        self.armed()
        journal = launch_journal.LaunchJournal(self.store._directory / 'launches')
        self.addCleanup(journal.release)
        binding, bridge = self.lease(), replay.claim_binding().claim_id
        journal.claimed(bridge, run_handle=binding.run_handle, attempt=binding.attempt, host_client='codex-host-v1',
                        lease=binding.as_data(), identity_digest=fixtures.IDENTITY,
                        claim_request_id=binding.claim_request_id, storage_evidence=b'{}', workspace=None)
        self.open()
        identity = project_holds_schema.attempt_identity(*self.attempt())
        case = self.store._connection.execute("SELECT lease_case FROM project_holds WHERE kind='lease' AND identity=?",
                                               (identity,)).fetchone()[0]
        self.assertEqual('admitted_unbound', case)
        self.record_settlement(kind='crashed')
        self.assertEqual(('released', 'lost_unbound'), self.hold('lease', identity)[:2])
        self.assertEqual('expired', self.store.result_lease(binding).state)
        self.assertEqual((None, 'crashed'), self.settlement()[:2])
        self.consistent()

    def test_bound_reservation_cannot_use_the_never_spawned_lease_exception(self):
        self.bound()
        self.open()
        journal = self.journal()
        bridge = replay.claim_binding().claim_id
        journal._write(bridge, {**journal.entry(bridge), 'state': 'claimed',
                               'child': None, 'watchdog': None, 'run_tag': None})
        before = self.snapshot()
        with self.assertRaises(HistoryError):
            self.record_settlement(kind='crashed')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
