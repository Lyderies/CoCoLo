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
import os
import shutil
import sqlite3
import tempfile
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

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # TESTSPEED3-3: the separate dispatches of one class share one bytecode cache outside the source tree.
        cls.dispatch_pycache = tempfile.mkdtemp(prefix='settle-pyc-')
        cls.addClassCleanup(shutil.rmtree, cls.dispatch_pycache, ignore_errors=True)

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

    def account_claims(self):
        """The account's admitted claims, read on a connection of the test's own (the collector thread owns its
        ledger's connection: a read from here while it reads raises sql_read_wrong_owner)."""
        connection = sqlite3.connect(f'file:{self.state.database}?mode=ro', uri=True, timeout=5)
        try:
            return connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0]
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

    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3')
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
class ConfirmFixSweepTests(unittest.TestCase):
    """B4s confirming check (decision 154): the sweep's pruning, backoff and per-entry isolation, and the workspace
    removal's answer, on a real launch journal with the driver's sweep alone (no services)."""

    def setUp(self):
        import tempfile
        from codex_agents.launch_journal import LaunchJournal
        from tests.support import TEMP_ROOT
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.journal = LaunchJournal(self.base / 'launches')
        self.addCleanup(self.journal.release)
        driver = object.__new__(project_driver.RunDriver)
        driver._journal, driver._clock = self.journal, lambda: 1000.0
        driver._earlier_admitted, driver._fenced = {}, {}
        self.driver = driver

    def settled(self, number, *, aged=True):
        from tests.test_launch_journal import claimed
        bridge = 'brg_' + f'{number:032x}'
        claimed(self.journal, bridge)
        self.journal.settled(bridge, 'settled', now_ms=1 if aged else None)
        return bridge

    def files(self):
        return sorted(path.name[:-5] for path in self.journal.directory.glob('brg_*.json'))

    def test_c_prune_removes_only_entries_recovery_inspected_in_the_same_pass(self):
        """NEW HIGH (pruning): an aged `settled` entry recovery did not see in this pass is never pruned."""
        seen, unseen = self.settled(1), self.settled(2)
        self.driver._settled_checked = frozenset({seen})
        listed = [self.journal.entry(seen)]
        with mock.patch.object(self.journal, 'entries', return_value=listed):
            self.driver._settle_lost(False)
        self.assertEqual([unseen], self.files())

    def test_c_settled_checked_drops_pruned_keys_and_is_capped(self):
        """NEW HIGH (pruning): `_settled_checked` loses each pruned key and never grows past its cap."""
        seen = self.settled(1)
        self.driver._settled_checked = frozenset({seen} | {'brg_' + f'{n:032x}' for n in range(100, 100 + 5000)})
        with mock.patch.object(self.journal, 'entries', return_value=[self.journal.entry(seen)]):
            self.driver._settle_lost(False)
        self.assertEqual([], self.files())
        self.assertNotIn(seen, self.driver._settled_checked)
        self.assertLessEqual(len(self.driver._settled_checked), project_driver.MAX_SETTLED_CHECKED)

    def test_c_a_long_failing_recovery_backs_off_at_the_cap_without_overflow(self):
        """NEW HIGH (backoff): 1,024+ failures give the capped delay; the exponent is capped before multiplying."""
        bridge = self.settled(1)
        entry = self.journal.entry(bridge)
        self.driver._settled_retry = {bridge: (1024, 0.0)}
        self.driver._settled_checked = frozenset()
        self.driver._recover_settled(entry)  # its lease cannot be read: a failure
        self.assertEqual((1025, 1000.0 + project_driver.FENCE_RETRY_MAX_SECONDS), self.driver._settled_retry[bridge])
        self.assertEqual(project_driver.FENCE_RETRY_MAX_SECONDS, project_driver.RunDriver._earlier_delay(5000))

    def test_L14_1_an_old_settled_entry_is_released_in_run_status_only_once_its_recovery_is_done(self):
        """s2fix round 2 (L14.1): while the project store's settlement of an older journal's run is still being
        recovered, learning-run-status says it is held, as the report counts it pending: never `released` early."""
        bridge = self.settled(1, aged=False)
        handle = self.journal.entry(bridge)['run_handle']
        self.driver._recover_settled = lambda entry: False
        self.assertEqual('held', self.driver._settle_lost(False)[handle][1])
        self.driver._recover_settled = lambda entry: True
        self.assertEqual('released', self.driver._settle_lost(False)[handle][1])

    def test_c_one_entry_failing_never_aborts_the_sweep_for_later_entries(self):
        """NEW HIGH (backoff): per-entry isolation; an exception for one entry leaves the later ones swept."""
        first, second = self.settled(1, aged=False), self.settled(2, aged=False)
        calls = []

        def recover(entry):
            calls.append(entry['bridge_run_id'])
            if entry['bridge_run_id'] == first:
                raise OverflowError('one entry')
            return True
        self.driver._recover_settled = recover
        lost = self.driver._settle_lost(False)
        self.assertEqual([first, second], calls)
        self.assertEqual({'released'}, {state for _, state in lost.values()})

    def scratch(self):
        from codex_agents import bridge
        patcher = mock.patch.object(bridge, 'WORKSPACE_PARENT', str(self.base / 'scratch'))
        patcher.start()
        self.addCleanup(patcher.stop)
        container = self.base / 'scratch' / bridge.SCRATCH_CONTAINER
        container.mkdir(parents=True)
        (self.base / 'outside' / 'prepare-kept').mkdir(parents=True)
        return container

    def test_c_M2_a_symlink_at_the_workspace_path_is_not_reported_removed(self):
        """MED 2: an existing symlink at the recorded workspace path is left and reported not removed."""
        container = self.scratch()
        (container / 'prepare-link').symlink_to(self.base / 'outside' / 'prepare-kept')
        self.assertFalse(project_driver.RunDriver._remove_workspace(str(container / 'prepare-link')))
        self.assertTrue((container / 'prepare-link').is_symlink())

    def test_c_M2_a_wrong_owner_or_non_folder_entry_is_not_reported_removed(self):
        """MED 2: an entry of another owner, or one that is not a folder, is left and reported not removed."""
        container = self.scratch()
        (container / 'prepare-file').write_text('x')
        self.assertFalse(project_driver.RunDriver._remove_workspace(str(container / 'prepare-file')))
        (container / 'prepare-other').mkdir()
        with mock.patch.object(project_driver.os, 'geteuid', return_value=os.geteuid() + 1):
            self.assertFalse(project_driver.RunDriver._remove_workspace(str(container / 'prepare-other')))
        self.assertTrue((container / 'prepare-other').is_dir())
        self.assertTrue(project_driver.RunDriver._remove_workspace(str(container / 'prepare-gone')))


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

    @requirements('RS-B-B4', 'TESTSPEED3-1', 'TESTSPEED3-2', 'TESTSPEED3-3')
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
