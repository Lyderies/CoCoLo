"""Batch 4 (RSF-S7-1 with RS-B-B5a-3/4): a crashed hold retains B5a's process proof as its one bounded evidence value.

The merge of RS-F S7 (retained closure evidence) and RS-B B5a (crashed proof in the released hold's evidence): the proof
is never overwritten by the closure or by S7's attempt-pruning backfill, the identical open request after the attempt
purge is still answered `settled` from the tombstone within S7's replay window, refused after it, and the purge pass
clears the proof with the other expired evidence. Real temporary project stores, no service or sockets.
"""
import json
import unittest

from tests import test_account_settlement as fixtures
from tests import test_crashed_settlement as crashed
from tests import test_publication_replay as replay
from codex_agents import project_history
from codex_agents.history_contracts import HistoryError


@__import__('test_framework').requirements(
    'RS-B-B5a',
    why_not_scenario='Real temporary project stores and protected launch journals; no service or sockets.')
class CrashedEvidenceReplayTests(fixtures.SettlementFixture):
    journal = crashed.CrashedSettlementTests.journal

    def evidence(self):
        return self.store._connection.execute(
            "SELECT evidence FROM project_holds WHERE kind='reservation'").fetchone()[0]

    def purged_crash(self):
        self.bound()
        opened = self.open()
        self.request = self.payload()  # the original complete request (its lease row goes with the purge)
        journal = self.journal()
        raw = self.closure(kind='crashed')
        first = self.record_settlement(raw=raw)
        proof = self.evidence()
        journal.remove(replay.claim_binding().claim_id)
        self.store.expire_results()
        self.now += self.retention_ms + 1
        self.store.expire_results()
        with self.store._transaction():
            target = replay.claim_binding()
            self.store._delete_attempt_rows(target.run_handle, target.attempt, target.claim_id)
        return opened, raw, first, proof

    def test_crashed_proof_survives_pruning_and_the_open_replays_settled_in_the_window(self):
        opened, raw, first, proof = self.purged_crash()
        self.assertEqual('gone', json.loads(proof)['state'])
        self.assertEqual(proof, self.evidence())  # neither the closure nor S7's backfill replaced it
        before = self.snapshot()
        target = replay.claim_binding()
        again = self.store.open_account_settlement(target.run_handle, target.attempt, self.request_id(),
                                                   self.request, runtime_digest=fixtures.RUNTIME)
        self.assertEqual({'state': 'settled', 'request_id': self.request_id(),
                          'request_digest': opened['request_digest'], 'release_kind': 'crashed'}, again)
        self.assertEqual({**first, 'known': True}, self.record_settlement(raw=raw))
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.store.open_account_settlement(target.run_handle, target.attempt, 'lost-other', self.request,
                                               runtime_digest=fixtures.RUNTIME)
        self.assertEqual(before, self.snapshot())

    def test_after_the_replay_window_the_crashed_tombstone_refuses_and_the_purge_clears_its_proof(self):
        _, raw, _, _ = self.purged_crash()
        released = self.store._connection.execute(
            "SELECT released_at_ms FROM project_holds WHERE kind='reservation'").fetchone()[0]
        self.now = released + project_history.SETTLEMENT_REPLAY_WINDOW_MS + 1
        target = replay.claim_binding()
        with self.assertRaisesRegex(HistoryError, '^history_result_not_reserved$'):
            self.store.open_account_settlement(target.run_handle, target.attempt, self.request_id(), self.request,
                                               runtime_digest=fixtures.RUNTIME)
        with self.assertRaisesRegex(HistoryError, '^history_result_not_reserved$'):
            self.record_settlement(raw=raw)
        self.store.expire_results()
        self.assertIsNone(self.evidence())


if __name__ == '__main__':
    unittest.main()
