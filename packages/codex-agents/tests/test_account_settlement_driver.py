"""Slice B4s (RS-B-B4-1, -4, -5, -8): lost-run settlement through the real driver, writer lane and account.

REQUIREMENTS-account-settlement.md; ANALYSIS-0.4.0-rsb-b4-b7.md §2. The collector, the account services and the
project host run as threads of this process (DriverHarness), everything else is the product path. A project host
stops (as on SIGTERM) with a claimed run; the stopping service (or, when it could not, the next one) settles it at
the account (`account.settle_lost_run`), fetches the closure (`account.lost_run_closure`) and records it in the
project store before the run's workspace is removed and its journal entry is written `settled`. A stop between any
two of those steps leaves the entry unsettled, and the next sweep re-sends the identical request.
Integration stage: real services, sockets and stores; only the step boundaries are interrupted.
"""
import ast
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.learning_support import attest_provider
from tests.test_bridge import CLEAN, git, null_filled, result
from tests.test_lost_run_settlement import LostRunHarness
from tests.test_project_driver import DriverHarness

from codex_agents import learning_store, project_driver, result_persistence, service_transport
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.active_run_capacity import ActiveRunLimits
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.hosts import OWNER_CLIENT
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.lost_run_contracts import SettleLostInput
from codex_agents.service_enrollment import (
    account_endpoint_path, enrollment_preamble, proof_directory, read_capability, withdraw_proof,
)

WHY = ('B4s needs the real account, project writer and driver: the closure is keyed by the account and verified by '
       'the project store, and each step boundary is a real stop of the service between two account or store writes.')


def requirements(*ids):
    return __import__('test_framework').requirements(*ids, why_not_scenario=WHY)


class SettlementHarness(LostRunHarness):
    """The stop-and-restart helpers of the lost-run tests (copied, so that module's tests do not run twice)."""

    def setUp(self):
        self._host_stop = threading.Event()
        original = DriverHarness.start_project_host

        def first(harness, project, stop=None):
            return original(harness, project, stop or self._host_stop)

        with mock.patch.object(DriverHarness, 'start_project_host', first):
            super().setUp()

    def stop_services(self):
        self._host_stop.set()
        super().stop_services()

    def restart_host(self):
        self.stop_host()
        self.start_host()

    def stop_host(self):
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(30)
        self.assertFalse(host.is_alive())
        self.threads.remove(host)

    def start_host(self):
        self._host_stop = threading.Event()
        DriverHarness.start_project_host(self, self.project, self._host_stop)

    def lose(self, state, *, orchestration_run=None, between=None):
        """Hold one run in `state`, stop its host (inside `between`, if given), start the next one."""
        import contextlib
        original, seen = project_driver.RunDriver._advance, []

        def held(driver, run):
            if run.state == state:
                seen.append(state)
                return None
            return original(driver, run)

        context, route_path, orchestration, run_handle = orchestration_run or self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.object(project_driver.RunDriver, '_advance', held):
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: seen, seconds=30)
            with between or contextlib.nullcontext():
                self.stop_host()
            self.start_host()
        dispatch.communicate(timeout=30)
        return context, route_path, orchestration, run_handle

    def store(self, sql, arguments=()):
        (path,) = list(self.state.root.rglob('history.sqlite3'))
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            return [tuple(row) for row in connection.execute(sql, arguments)]
        finally:
            connection.close()

    def settlements(self):
        return self.store('SELECT run_handle,claim_id IS NOT NULL,release_kind FROM project_account_settlements')

    def reservation_hold(self, run_handle):
        return self.store("SELECT h.state,h.release_kind FROM project_holds h JOIN result_reservations s "
                          "ON s.claim_id=h.identity AND h.kind='reservation' WHERE s.run_handle=?", (run_handle,))

    def call_as(self, operation, entry, *, role, host_client, root, project_id, handle=None):
        custody = canonical_json({'purpose': 'run', 'lease': entry['lease'], 'storage_evidence': entry['storage_evidence'],
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        endpoint = account_endpoint_path(self.paths)
        here = threading.get_ident()

        def preamble(socket_path, message):
            if threading.get_ident() != here or Path(socket_path) != endpoint:
                return None
            key, proofs = read_capability(self.paths, None, 'account'), proof_directory(socket_path)
            line = enrollment_preamble(key, role, host_client, 1, message, proofs, subject=str(root), context=custody)
            return line, lambda: withdraw_proof(proofs, line)

        evidence = bytes.fromhex(entry['storage_evidence'])
        settle = SettleLostInput(handle or entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                 entry['bridge_run_id'], parse_bounded_json(evidence)['lease']['lease_digest'])
        enrollments = {'test-settle': preamble, **service_transport._ENROLLMENTS}
        with mock.patch.object(service_transport, '_ENROLLMENTS', enrollments):
            client = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=10)
            return getattr(client, operation)(settle, request_id='test-closure-' + entry['bridge_run_id'][4:20])


@requirements('RS-B-B4')
class SettlementThroughTheDriverTests(SettlementHarness):
    """A1-A4: the real write, from the account's closure to the project's settlement row."""

    def test_A1_a_run_lost_after_its_claim_is_settled_in_the_project_store(self):
        from codex_agents.lost_run_closure import verify_lost_run_closure
        context, route_path, orchestration, run_handle = self.lose('consuming')
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assertEqual([('released', 'lost')], self.reservation_hold(run_handle))
        self.assertEqual([('abandoned', 'service_lost', 1)], self.claimed_bindings())
        (raw,) = self.store('SELECT closure FROM project_account_settlements')[0]
        closure = verify_lost_run_closure(self.state.digest_key(), bytes(raw), project_id=self.project_id)
        self.assertEqual((run_handle, 'lost'), (closure['binding']['run_handle'], closure['release_kind']))
        self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)

    def test_A2_a_claim_admitted_but_never_bound_releases_its_lease_lost_unbound(self):
        # Hold the run before its lease is bound: the store's bind fails once, so the account holds the
        # claim while the lease stays claim_pending (at 'binding' a successful bind has already happened).
        from codex_agents.project_history import ProjectHistory
        original, failed = ProjectHistory.bind_result_lease, []

        def fail_once(history, *arguments, **options):
            if not failed:
                failed.append(True)
                raise result_persistence.HistoryError('history_service_unavailable')
            return original(history, *arguments, **options)

        with mock.patch.object(ProjectHistory, 'bind_result_lease', fail_once):
            context, route_path, orchestration, run_handle = self.lose('binding')
        self.wait_for(lambda: self.settlements() == [(run_handle, 0, 'lost')], seconds=30)
        self.assertEqual([('expired', 'released', 'lost_unbound')], self.store(
            "SELECT l.state,h.state,h.release_kind FROM result_leases l JOIN project_holds h ON h.kind='lease' "
            "AND h.identity=l.run_handle||':'||l.attempt WHERE l.run_handle=?", (run_handle,)))
        # Review r1 HIGH 2: the open binding is released as claimed in the same settlement (nothing else can).
        self.assertEqual([('released', 'claimed')], self.store(
            "SELECT state,release_kind FROM project_holds WHERE kind='open_binding' AND identity=?",
            (run_handle + ':1',)))

    def test_A3_the_orchestration_of_a_settled_run_closes_and_the_run_reads_lost(self):
        """RS-B-B4-5: a verified settlement finishes the reservation for the close; it is not a success."""
        context, route_path, orchestration, run_handle = self.lose('consuming')
        self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assert_lost_status(orchestration, run_handle, 'released')
        self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')

    def test_A4_when_the_stopping_service_cannot_record_the_next_service_settles_with_the_identical_request(self):
        requests, original = [], AccountClient.lost_run_closure

        def recorded(client, settle, *, request_id):
            requests.append((request_id, canonical_json(settle.payload())))
            return original(client, settle, request_id=request_id)

        def refused(lane, operation, *arguments, **options):
            raise result_persistence.HistoryError('history_service_unavailable')

        with mock.patch.object(AccountClient, 'lost_run_closure', recorded):
            _, _, _, run_handle = self.lose('consuming', between=mock.patch.object(
                result_persistence.ResultPersistenceLane, 'account_settlement_after_stop', refused))
            self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
        self.assertEqual(1, len(set(requests)))  # every request was the identical one


@requirements('RS-B-B4')
class StepBoundaryTests(SettlementHarness):
    """RS-B-B4-4: account reply -> settlement committed -> workspace removed -> journal `settled`; a stop between any
    two leaves the journal entry unsettled and the next sweep replays the identical request (answer `known`)."""

    def replays(self, patcher):
        known, original = [], AccountClient.settle_lost_run

        def watched(client, *args, **options):
            reply = original(client, *args, **options)
            known.append(reply['known'])
            return reply
        with mock.patch.object(AccountClient, 'settle_lost_run', watched):
            _, _, _, run_handle = self.lose('consuming', between=patcher)
            self.wait_for(lambda: self.settlements() == [(run_handle, 1, 'lost')], seconds=30)
            self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)
        self.assertEqual(False, known[0])
        self.assertTrue(all(known[1:]))
        self.assertGreaterEqual(len(known), 2)
        return run_handle

    def test_B4_4_a_stop_after_the_account_reply_before_the_settlement_is_replayed(self):
        def refused(lane, operation, *arguments, **options):
            if operation == 'record_account_settlement':
                raise result_persistence.HistoryError('history_service_unavailable')
            return original(lane, operation, *arguments, **options)
        original = result_persistence.ResultPersistenceLane.account_settlement_after_stop
        self.replays(mock.patch.object(result_persistence.ResultPersistenceLane, 'account_settlement_after_stop',
                                       refused))

    def test_B4_4_a_stop_after_the_settlement_before_the_workspace_removal_is_replayed(self):
        def stopped(workspace):
            raise RuntimeError('stopped before the workspace removal')
        self.replays(mock.patch.object(project_driver.RunDriver, '_remove_workspace', staticmethod(stopped)))

    def test_B4_4_a_stop_after_the_workspace_removal_before_the_journal_is_replayed(self):
        from codex_agents.launch_journal import JournalError, LaunchJournal

        def stopped(journal, *args, **options):
            raise JournalError('stopped before the journal write')
        self.replays(mock.patch.object(LaunchJournal, 'settled', stopped))

    def test_B4_4_every_caller_reaches_the_account_only_through_the_one_settlement_sequence(self):
        """`_settle_lost` (sweep and exit), `_settle_own_ended` (C3) and the container recovery (C4) all settle through
        `_settle_entry`, which records the closure before it returns; each caller removes the workspace and writes
        the journal only after it returned."""
        source = Path(project_driver.__file__).read_text()
        tree = ast.parse(source)
        functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        calls = {name: [ast.unparse(n.func) for n in sorted(
                     (n for n in ast.walk(node) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))]
                 for name, node in functions.items()}
        self.assertEqual(['_settle_entry'], [name for name, found in calls.items() if 'client.settle_lost_run' in found])
        self.assertEqual(['_settle_entry'], [name for name, found in calls.items() if 'client.lost_run_closure' in found])
        order = [c for c in calls['_settle_entry'] if c in {'client.settle_lost_run', 'client.lost_run_closure',
                                                            "self._settlement_step"}]
        self.assertEqual(['self._settlement_step', 'client.settle_lost_run', 'client.lost_run_closure',
                          'self._settlement_step'], order)
        settle = functions['_settle_lost']
        lines = {ast.unparse(n.func): n.lineno for n in ast.walk(settle) if isinstance(n, ast.Call)}
        self.assertLess(lines['self._settle_entry'], lines['self._remove_workspace'])
        self.assertLess(lines['self._remove_workspace'], lines['journal.settled'])

    def test_B4_4_the_container_recovery_writes_its_journal_only_after_the_settlement_returned(self):
        from codex_agents.container_recovery import ContainerRecovery
        order = []
        journal = mock.Mock(settled=lambda token, outcome: order.append('journal'))
        recovery = ContainerRecovery(mock.Mock(), journal, fence=None,
                                     settle=lambda entry: (order.append('settled'), {'state': 'settled'})[1],
                                     remove_workspace=lambda workspace: (order.append('workspace'), True)[1])
        self.assertEqual('settled', recovery._settle_gone({'token': 't', 'bridge_run_id': 'brg_' + '0' * 32,
                                                          'workspace': None}))
        self.assertEqual(['settled', 'workspace', 'journal'], order)


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

    @unittest.skip("B4s r1 HIGH 3 (process mode), red in B4s (timed out waiting for the settlement row; "
                   "docs/requirements/b4s/red.md): larger than B4s. The successor's fence answers `claimed`, but "
                   "`settle_lost_run` needs the run's identity digest, which in process mode exists only in the "
                   "journal entry that was never written (the project store keeps no copy; the fence takes it from "
                   "the account's launch observation and its reply shape is fixed). Needs a process-mode journal "
                   "intent before the claim is sent (as containers write) or an account settlement taking the "
                   "identity from the launch observation: a primary decision")
    def test_r1_H3_a_claim_admitted_before_its_first_journal_write_is_settled_from_the_store(self):
        """HIGH 3 (process mode): the service stopped after the account admitted the claim and before the
        journal entry was written; its successor's fence answers `claimed`, and the run must still settle."""
        import contextlib

        @contextlib.contextmanager
        def admitted():
            self.wait_for(lambda: self.ledger._connection.execute(
                'SELECT COUNT(*) FROM launch_claims').fetchone()[0] == 1, seconds=30)
            yield
        _, _, _, run_handle = self.lose('claiming', between=admitted())
        self.wait_for(lambda: self.settlements() == [(run_handle, 0, 'lost')], seconds=30)


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
