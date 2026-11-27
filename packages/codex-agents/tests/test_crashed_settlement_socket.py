"""PRIMARY ONLY: real killed project host, process proof, account socket and CLI status.

This module opens sockets and initializes disposable git fixtures. Do not run in the B5a builder worktree.
"""
import json
import sqlite3
import unittest

from tests import test_lost_run_settlement as fixtures
from tests import test_project_driver as drivers
from tests import test_account_settlement_driver as settlement
from tests.test_bridge import null_filled, result
from codex_agents.account_api import AccountError
from codex_agents.learning_contracts import keyed_digest


class CrashedSocketTests(unittest.TestCase):
    def test_crashed_slot_waits_for_process_proof_then_settles_and_reports_released(self):
        h = fixtures.LostRunAfterCrashTests(methodName='runTest')
        h.setUp()
        self.addCleanup(h.doCleanups)
        context, route, orchestration, handle = h.open_run()
        h.frames('codex', [{'type': 'thread.started', 'thread_id': 'b5a'}, {'sleep': 5},
                           {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (h.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = h.separate_dispatch(context, route, handle)
        h.wait_for(lambda: h.journal_states() == ['watched'], seconds=30)
        entry = json.loads(next(h.journal().glob('brg_*.json')).read_text())
        with h.ledger._held(), h.ledger._account_transaction():
            self.assertTrue(h.ledger._release_slot_locked(h.project_id, keyed_digest(h.state.digest_key(), handle),
                'crashed', h.ledger._clock_ms(), h.ledger._capacity_clock_reference()))
        own = dict(role='project', host_client=entry['host_client'], root=h.project, project_id=h.project_id)
        with self.assertRaises(AccountError) as caught:
            settlement.SettlementHarness.call_as(h, 'lost_run_closure', entry, **own)
        self.assertEqual('claim_cleanup_unavailable', caught.exception.reason_code)
        h.host_process.kill()
        h.host_process.wait(10)
        dispatch.communicate(timeout=30)
        drivers.DriverHarness.start_project_host(h, h.project)

        def settled():
            with sqlite3.connect(f'file:{h.state.root}/projects/{h.project_id}/history.sqlite3?mode=ro',
                                 uri=True, timeout=5) as connection:
                return connection.execute('SELECT release_kind FROM project_account_settlements WHERE run_handle=?',
                                          (handle,)).fetchall() == [('crashed',)]

        h.wait_for(settled, seconds=30)
        h.wait_for(lambda: h.lost_capacity(orchestration, handle) == 'released', seconds=30)
        status = h.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', handle,
                       expected=2)
        self.assertEqual(('unknown', 'released'), (status['outcome'], status['capacity_state']))
        self.assertIn('capacity was released', status['error'])


if __name__ == '__main__':
    unittest.main()
