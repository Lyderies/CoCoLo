"""Slice B4s review round 1 and its confirming check (RS-B-B4-4, -5): lost-run recovery through the real driver.

Split out of test_account_settlement_driver (TESTSPEED-3: that module sat at its 60 s scenario budget under full-run
load); the harness, the helpers and the requirement decoration are that module's, unchanged. These are the
review-round tests: a settlement the account purged first, a recorded settlement finished from the local receipt, a
claim admitted before its first journal write, an intent whose claim never reached the account, workspace removal
that fails, and a second stop after the lease was fenced or the intent was settled. Integration stage: real
services, sockets and stores; only the step boundaries are interrupted.
"""
import ast
import hashlib
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_account_settlement_driver import SettlementHarness, requirements
from tests.test_bridge import null_filled, result

from codex_agents import project_driver, result_persistence
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.lost_run_contracts import SettleLostInput

@requirements('RS-B-B4')
class ReviewRoundOneDriverTests(SettlementHarness):
    """B4s security review r1 (Codex, NOT-READY): the driver and account findings, each red before its fix."""

    def far(self):
        return time.time_ns() // 1_000_000 + 40 * 86_400_000  # past the binding TTL and the open retention

    def purge_account(self):
        """The account's retention runs while the project is down: the run's binding goes, its tombstone stays."""
        self.assertGreaterEqual(self.ledger.sweep_account_retention(now_ms=self.far()), 1)
        self.assertEqual([], self.ledger._connection.execute('SELECT 1 FROM host_run_bindings').fetchall())

    def blocked(self, operation):
        """The project writer refuses one settlement step (on the live lane and after a stop) while `gate` is set."""
        gate, live = threading.Event(), result_persistence.ResultPersistenceLane.account_settlement
        after = result_persistence.ResultPersistenceLane.account_settlement_after_stop
        gate.set()

        def refusing(original):
            def step(lane, name, *arguments, **options):
                if name == operation and gate.is_set():
                    raise result_persistence.HistoryError('history_service_unavailable')
                return original(lane, name, *arguments, **options)
            return step
        patches = [mock.patch.object(result_persistence.ResultPersistenceLane, 'account_settlement', refusing(live)),
                   mock.patch.object(result_persistence.ResultPersistenceLane, 'account_settlement_after_stop',
                                     refusing(after))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return gate

    def test_r1_H1_a_settlement_the_account_purged_before_the_project_recorded_it_still_finishes(self):
        """HIGH 1: the account settled the run and purged its binding while the project could not record the
        closure; the account's tombstone keeps the replay evidence, so the identical request still gets it."""
        gate = self.blocked('record_account_settlement')
        _, _, _, run_handle = self.lose('consuming')
        self.wait_for(lambda: self.claimed_bindings() == [('abandoned', 'service_lost', 1)], seconds=30)
        self.purge_account()
        gate.clear()
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assertEqual([('released', 'lost')], self.reservation_hold(run_handle))
        self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)

    def test_r1_H1_a_recorded_settlement_finishes_its_journal_from_the_local_receipt_without_the_account(self):
        """HIGH 1: the project recorded the closure but stopped before its journal; the account then purged the
        run, which it had settled before its tombstone kept replay evidence, so it can no longer answer. The
        verified local settlement finishes the journal without the account's answer."""
        from codex_agents.launch_journal import JournalError, LaunchJournal
        gate, original, calls = threading.Event(), LaunchJournal.settled, []
        gate.set()

        def journal(journal, *arguments, **options):
            if gate.is_set():
                raise JournalError('stopped before the journal write')
            return original(journal, *arguments, **options)
        settle = AccountClient.settle_lost_run

        def counted(client, *arguments, **options):
            calls.append(gate.is_set())
            return settle(client, *arguments, **options)
        with mock.patch.object(LaunchJournal, 'settled', journal), \
                mock.patch.object(AccountClient, 'settle_lost_run', counted):
            _, _, _, run_handle = self.lose('consuming')
            self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
            self.purge_account()
            with self.ledger._held(), self.ledger._account_transaction():  # settled before the tombstone kept it
                self.ledger._connection.execute("UPDATE run_holds SET receipt=NULL,evidence=NULL WHERE kind='binding'")
            gate.clear()
            self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)
        self.assertTrue(calls and calls[0])  # the stopping service settled it at the account first

    def test_r1_M1_a_slot_released_as_crashed_has_no_released_unacknowledged_closure(self):
        """MED 1: the closure reads the slot's recorded release kind; a timed `crashed` release is B5's (refused)."""
        from codex_agents.learning_contracts import keyed_digest
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'held'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: self.journal_states() == ['watched'], seconds=30)
        entry = json.loads(next(self.journal().glob('brg_*.json')).read_text())
        own = {'role': 'project', 'host_client': entry['host_client'], 'root': self.project, 'project_id': self.project_id}
        handle = keyed_digest(self.state.digest_key(), entry['run_handle'])
        ledger = self.ledger
        with ledger._held(), ledger._account_transaction():
            self.assertTrue(ledger._release_slot_locked(self.project_id, handle, 'crashed', ledger._clock_ms(),
                                                        ledger._capacity_clock_reference()))
        self.assertEqual('released', self.call_as('settle_lost_run', entry, **own)['state'])
        with self.assertRaises(AccountError) as caught:
            self.call_as('lost_run_closure', entry, **own)
        self.assertEqual('claim_cleanup_unavailable', caught.exception.reason_code)
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        dispatch.communicate(timeout=30)

    def test_r1_M2_a_workspace_that_could_not_be_removed_keeps_the_journal_unsettled(self):
        """MED 2 (sweep and exit path): the journal is `settled` only once the workspace is confirmed gone. At the
        stop the run's workspace is still there after its settlement and its removal fails (the folder stays)."""
        import contextlib
        from codex_agents import host_paths
        original, workspaces = project_driver.RunDriver._settle_entry, []

        def settled_with_workspace(driver, entry):
            reply = original(driver, entry)
            Path(entry['workspace']).mkdir(mode=0o700, exist_ok=True)
            workspaces.append(entry['workspace'])
            return reply
        stop = contextlib.ExitStack()
        stop.enter_context(mock.patch.object(project_driver.RunDriver, '_settle_entry', settled_with_workspace))
        stop.enter_context(mock.patch.object(host_paths, 'remove_private_directory', lambda path: None))
        _, _, _, run_handle = self.lose('consuming', between=stop)
        self.assertTrue(workspaces)
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.wait_for(lambda: not any(Path(w).exists() for w in workspaces), seconds=30)
        self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)

    def test_r1_M2_the_container_recovery_writes_no_journal_while_the_workspace_remains(self):
        """MED 2 (container recovery path)."""
        from codex_agents.container_recovery import ContainerRecovery
        order = []
        journal = mock.Mock(settled=lambda token, outcome: order.append('journal'))
        recovery = ContainerRecovery(mock.Mock(), journal, fence=None,
                                     settle=lambda entry: (order.append('settled'), {'state': 'settled'})[1],
                                     remove_workspace=lambda workspace: order.append('workspace') and False,
                                     clock=lambda: 0.0)
        self.assertIsNone(recovery._settle_gone({'token': 't', 'bridge_run_id': 'brg_' + '0' * 32,
                                                 'workspace': '/w'}))
        self.assertEqual(['settled', 'workspace'], order)

    def test_r1_M2_the_live_container_path_settles_its_journal_only_after_the_workspace_is_gone(self):
        """MED 2 (C3 live path, `_settle_own_ended`): the workspace removal gates the journal write."""
        source = Path(project_driver.__file__).read_text()
        function = [node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.FunctionDef) and node.name == '_settle_own_ended'][0]
        lines = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                lines.setdefault(ast.unparse(node.func), node.lineno)
        self.assertIn('self._remove_workspace', lines)
        self.assertLess(lines['self._remove_workspace'], lines['self._journal.settled'])
        gate = [n for n in ast.walk(function) if isinstance(n, ast.If)
                and 'self._remove_workspace' in ast.unparse(n.test)]
        self.assertTrue(gate)  # its answer decides whether the journal is written

    def test_r1_H3_an_old_settled_journal_with_an_outstanding_project_hold_is_recovered(self):
        """HIGH 3 (upgrade): a journal written `settled` before project settlement existed (0.8.54: the account
        settled, the reservation hold stayed `releasing`) is recovered from the store before it is pruned."""
        def old_settle(driver, entry):  # 8d4a7ce3's _settle_entry: arbitration, then the account only
            from codex_agents.result_contracts import ResultLeaseBinding
            import hashlib
            lease = ResultLeaseBinding(**entry['lease'])
            driver._arbitrate_publication(lease, entry)
            evidence = bytes.fromhex(entry['storage_evidence'])
            settle = SettleLostInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                     entry['bridge_run_id'], parse_bounded_json(evidence)['lease']['lease_digest'])
            custody = canonical_json({'purpose': 'run', 'lease': entry['lease'],
                                      'storage_evidence': entry['storage_evidence'], 'terminal_evidence': None,
                                      'cleanup_evidence': None})
            call = driver._call
            call.role, call.host_client, call.context = 'project', entry['host_client'], custody
            try:
                return AccountClient(driver._account, driver._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                     timeout=10).settle_lost_run(settle, request_id='lost-' + hashlib.sha256(
                                         entry['bridge_run_id'].encode()).hexdigest()[:32])
            finally:
                call.role = call.host_client = call.context = None
        _, _, _, run_handle = self.lose('consuming', between=mock.patch.object(
            project_driver.RunDriver, '_settle_entry', old_settle))
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assertEqual([('released', 'lost')], self.reservation_hold(run_handle))

    def test_r1_H3_a_claim_admitted_before_its_first_journal_write_is_settled_from_the_store(self):
        """HIGH 3 (process mode): the service stopped after the account admitted the claim and before the
        journal entry was written; its successor's fence answers `claimed`, and the run must still settle."""
        import contextlib

        @contextlib.contextmanager
        def admitted():
            self.wait_for(lambda: self.account_claims() == 1, seconds=30)
            yield
        _, _, _, run_handle = self.lose('claiming', between=admitted())
        self.wait_for(lambda: self.settlements() == [(run_handle, 0, 'lost')], seconds=30)

    def intents(self):
        values = []
        for path in sorted(self.journal().glob('pit_*.json')):
            try:
                values.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                values.append(None)
        return values

    def test_r1_H3_an_intent_whose_claim_never_reached_the_account_is_released_and_nothing_is_counted(self):
        """HIGH 3 (process mode, decision 146): the service stopped after its write-ahead intent and before its
        claim reached the account. The successor fences the intent's claim request (the account never admitted
        it): the intent is settled `fenced`, the account counts nothing and the lease is released."""
        from codex_agents.account_api import AccountClient as Client
        sent = []

        def never_sent(client, *arguments, **options):
            sent.append([(v or {}).get('state') for v in self.intents()])  # on disk before the claim leaves
            return {'state': 'unavailable'}
        with mock.patch.object(Client, 'claim_launch', never_sent):
            _, _, _, run_handle = self.lose('claiming')
        self.assertEqual([['intent']], sent)
        self.wait_for(lambda: [(v or {}).get('state') for v in self.intents()] == ['settled'], seconds=30)
        self.assertEqual('fenced', self.intents()[0]['outcome'])
        self.assertEqual(run_handle, self.intents()[0]['run_handle'])
        self.wait_for(lambda: self.store('SELECT state FROM result_leases') == [('expired',)], seconds=30)
        self.assertEqual({('lease', 'released', 'fenced'), ('open_binding', 'released', 'fenced')}, set(self.store(
            "SELECT kind,state,release_kind FROM project_holds WHERE kind IN ('lease','open_binding')")))
        self.assertEqual(0, self.account_claims())
        self.assertEqual([], self.settlements())
        self.assertEqual([], self.journal_states())

    # B4s confirming check (decision 154): HIGH 3, a second stop before the intent's journal completion.
    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3', 'TESTSPEED3-4', 'TESTSPEED3-5')
    def test_c_H3_a_second_stop_after_the_lease_was_fenced_still_settles_the_intent_and_its_workspace(self):
        """HIGH 3: the successor fenced the intent's claim (the lease expired) and stopped before it could remove
        the workspace and settle the intent; the third service no longer finds a `claim_pending` lease, so it
        must fence from the outstanding intent itself (the identical request) and settle the intent `fenced`."""
        from codex_agents.account_api import AccountClient as Client
        attempts = []

        def kept(workspace):
            attempts.append(workspace)
            return False
        with mock.patch.object(Client, 'claim_launch', lambda client, *a, **o: {'state': 'unavailable'}), \
                mock.patch.object(project_driver.RunDriver, '_remove_workspace', staticmethod(kept)):
            _, _, _, run_handle = self.lose('claiming')
            self.wait_for(lambda: self.store('SELECT state FROM result_leases') == [('expired',)], seconds=30)
            seen = len(attempts)
            self.wait_for(lambda: len(attempts) > seen, seconds=30)  # the successor reached the intent's workspace
            self.stop_host()
        self.assertEqual(['intent'], [(v or {}).get('state') for v in self.intents()])
        workspace = self.intents()[0]['workspace']
        self.assertIsNotNone(workspace)
        Path(workspace).mkdir(mode=0o700, parents=True, exist_ok=True)  # still there after the second stop
        self.start_host()
        self.wait_for(lambda: [(v or {}).get('state') for v in self.intents()] == ['settled'], seconds=30)
        self.assertEqual('fenced', self.intents()[0]['outcome'])
        self.assertFalse(Path(workspace).exists())
        self.assertEqual(0, self.account_claims())
        self.assertEqual([], self.settlements())

    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3', 'TESTSPEED3-4', 'TESTSPEED3-5')
    def test_c_H3_a_second_stop_after_an_admitted_intent_was_settled_at_the_account_still_settles_it(self):
        """HIGH 3: the successor learned the claim was admitted and settled it (its lease expired with the
        settlement), then stopped before the intent's journal completion; the third service settles the intent."""
        import contextlib

        @contextlib.contextmanager
        def admitted():
            self.wait_for(lambda: self.account_claims() == 1, seconds=30)
            yield
        with mock.patch.object(project_driver.RunDriver, '_remove_workspace', staticmethod(lambda workspace: False)):
            _, _, _, run_handle = self.lose('claiming', between=admitted())
            self.wait_for(lambda: self.settlements() == [(run_handle, 0, 'lost')], seconds=30)
            self.wait_for(lambda: self.store('SELECT state FROM result_leases') == [('expired',)], seconds=30)
            self.stop_host()
        self.assertEqual(['intent'], [(v or {}).get('state') for v in self.intents()])
        self.start_host()
        self.wait_for(lambda: [(v or {}).get('state') for v in self.intents()] == ['settled'], seconds=30)
        self.assertEqual([(run_handle, 0, 'lost')], self.settlements())

    def test_c_H3_an_intent_whose_claimed_entry_exists_is_taken_over_by_that_entry(self):
        """HIGH 3 (duplicate-entry takeover): a stop between the `claimed` entry and the intent's removal leaves
        both; the successor removes the intent and settles the run once, through the `claimed` entry."""
        with mock.patch.object(project_driver.RunDriver, '_forget_intent', lambda driver, run: None):
            _, _, _, run_handle = self.lose('consuming')
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.wait_for(lambda: self.intents() == [], seconds=30)
        self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)
        self.assertEqual([(run_handle, 1, 'lost')], self.settlements())


if __name__ == '__main__':
    unittest.main()
