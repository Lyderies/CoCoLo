"""S12: production collector loop with transport edges replaced, real account jobs and SQL."""
import contextlib
import io
import os
import sqlite3
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from tests import test_account_lane_restart as lane_tests
from codex_agents import account_host, account_restart, account_billing_jobs, signature_execution
from codex_agents.account_api import decode_account_response, encode_account_request
from codex_agents.contracts import ContractError
from codex_agents.learning_store import LearningLedger
from codex_agents.process_tree import ProcessTree
from codex_agents.sql_read_budget import SQLReadError
import collector_host


class S12SettlementLimitTests(unittest.TestCase):
    __why_not_scenario__ = 'Deterministic retry-exhaustion companion to the real collector probe scenarios.'
    def job(self):
        billing = account_billing_jobs.AccountBillingJobs.__new__(account_billing_jobs.AccountBillingJobs)
        billing._executions, billing._owner = mock.Mock(), mock.Mock()
        billing._executions.child_ended.return_value = True
        billing._executions.returned.return_value = True
        billing._owner._row.return_value = {'state': 'admitted'}
        billing.health, billing._note = mock.Mock(), mock.Mock()
        job = account_billing_jobs._Job(('project',), None, (), lambda: True)
        job.reservation = SimpleNamespace(key='request')
        job.preparation = object()
        return billing, job

    @requirements('S12-N3')
    def test_record_retry_limit_denies_once_and_later_passes_do_not_retry_records(self):
        billing, job = self.job()
        billing._executions.complete_ended.side_effect = ContractError('oversized')
        # Denial itself is transiently busy: exhausting record attempts must not resume them.
        billing._owner.settle_ended.side_effect = [SQLReadError('sql_read_busy'), None]
        with mock.patch.object(account_billing_jobs.time, 'monotonic_ns', side_effect=range(0, 100_000_000_000, 2_000_000_000)):
            for _ in range(10):
                billing._settle_ended(job)
        self.assertEqual(3, billing._executions.complete_ended.call_count)
        self.assertEqual(2, billing._owner.settle_ended.call_count)
        self.assertTrue(job.ended)
        self.assertIn('probe_records_unkept', [call.args[1] for call in billing.health.note_failure.call_args_list])

    @requirements('S12-N3')
    def test_busy_record_commit_remains_retryable_past_record_failure_limit(self):
        billing, job = self.job()
        billing._executions.complete_ended.side_effect = [SQLReadError('sql_read_busy')] * 5 + [None]
        with mock.patch.object(account_billing_jobs.time, 'monotonic_ns', side_effect=range(0, 100_000_000_000, 2_000_000_000)):
            for _ in range(10):
                billing._settle_ended(job)
        self.assertEqual(6, billing._executions.complete_ended.call_count)
        billing._owner.settle_ended.assert_not_called()
        self.assertTrue(job.ended)

    @requirements('S12-N3')
    def test_oversized_record_read_exhausts_record_budget_and_settles_once(self):
        billing, job = self.job()
        billing._executions.complete_ended.side_effect = SQLReadError('sql_read_value_limit')
        with mock.patch.object(account_billing_jobs.time, 'monotonic_ns', side_effect=range(0, 100_000_000_000, 2_000_000_000)):
            for _ in range(10):
                billing._settle_ended(job)
        self.assertEqual(3, billing._executions.complete_ended.call_count)
        billing._owner.settle_ended.assert_called_once()
        self.assertTrue(job.ended)


class S12RecheckProofTests(unittest.TestCase):
    __why_not_scenario__ = 'Adverse kernel answers and custody guards complement the real collector scenarios.'
    def pair(self):
        from codex_agents.owned_execution import OwnedExecutionRegistry, OwnedExecutionKey
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(OwnedExecutionKey('prj_' + 'a' * 32, 'b' * 64, 'run:' + 'c' * 64, 'brg_' + 'd' * 32),
                                deadline_ns=time.monotonic_ns() + 10_000_000_000)
        entry = registry._pending[ticket]
        tree = ProcessTree(4242, 'a' * 32)
        tree.identified, tree.root_birth = True, '100.1'
        tree._cleanup_seen[(tree.root, tree.root_birth)] = False
        entry.state, entry.child_possible, entry.cleanup_started = 'unknown', True, True
        entry.process, entry.tree, entry.birth = SimpleNamespace(pid=4242, returncode=0), tree, tree.root_birth
        entry.worker_returned = True
        return registry, ticket, entry

    @requirements('M10', 'H1c-3')
    def test_fresh_proof_requires_group_session_and_marker_empty_and_never_signals(self):
        from codex_agents import process_tree, launch_journal
        registry, ticket, entry = self.pair()
        with mock.patch.object(process_tree, 'group_empty', return_value=True) as group, \
                mock.patch.object(launch_journal, '_session_members', return_value=False) as session, \
                mock.patch.object(process_tree, 'tagged_process_running', return_value=False) as tagged, \
                mock.patch.object(process_tree.os, 'killpg', side_effect=AssertionError('reaped root signalled')):
            group.return_value = False
            self.assertFalse(registry.ticket_ended(ticket))
            group.return_value, session.return_value = True, None
            self.assertFalse(registry.ticket_ended(ticket))
            session.return_value = True
            self.assertFalse(registry.ticket_ended(ticket))
            session.return_value, tagged.return_value = False, None
            self.assertFalse(registry.ticket_ended(ticket))
            tagged.return_value = True
            self.assertFalse(registry.ticket_ended(ticket))
            tagged.return_value = False
            self.assertTrue(registry.ticket_ended(ticket))
            self.assertEqual('cleaned', entry.state)
            tagged.assert_called_with('a' * 32, '100.1', 4242)
            self.assertTrue(registry.ticket_ended(ticket))

    @requirements('M10', 'H1c-3')
    def test_active_unreturned_or_container_execution_never_uses_local_recheck(self):
        registry, ticket, entry = self.pair()
        with mock.patch.object(ProcessTree, 'ended_after_reap', return_value=True) as proof:
            for field, value in (('state', 'active'), ('worker_returned', False), ('cleanup_started', False),
                                 ('container', object()), ('birth', 'other')):
                with self.subTest(field=field):
                    old = getattr(entry, field)
                    setattr(entry, field, value)
                    self.assertFalse(registry.ticket_ended(ticket))
                    setattr(entry, field, old)
            proof.assert_not_called()
            self.assertTrue(registry.ticket_ended(ticket))
            proof.assert_called_once_with(entry.process)

    @requirements('M10', 'H1c-3')
    def test_unidentified_tree_missing_marker_or_unreaped_root_never_proves_release(self):
        from codex_agents import process_tree
        registry, ticket, entry = self.pair()
        with mock.patch.object(process_tree, 'tagged_process_running') as tagged:
            for obj, field, value in ((entry.tree, 'identified', False), (entry.tree, 'run_tag', None),
                                      (entry.process, 'returncode', None), (entry.process, 'pid', 5151)):
                old = getattr(obj, field)
                setattr(obj, field, value)
                self.assertFalse(registry.ticket_ended(ticket))
                setattr(obj, field, old)
            tagged.assert_not_called()

    @requirements('M10', 'H1c-3')
    def test_lost_tracking_or_known_live_descendant_cannot_be_replaced_by_absent_marker(self):
        from codex_agents import process_tree, launch_journal
        for incomplete in (True, False):
            with self.subTest(incomplete=incomplete):
                registry, ticket, entry = self.pair()
                entry.tree._cleanup_incomplete = incomplete
                entry.tree._cleanup_seen[(5151, '100.2')] = False
                with mock.patch.object(process_tree, 'group_empty', return_value=True), \
                        mock.patch.object(launch_journal, '_session_members', return_value=False), \
                        mock.patch.object(process_tree, 'tagged_process_running', return_value=False), \
                        mock.patch.object(process_tree, 'process_record', return_value=(1, 5151, '100.2', False)):
                    self.assertFalse(registry.ticket_ended(ticket))
                    self.assertEqual('unknown', entry.state)


class S12SignatureBirthTests(unittest.TestCase):
    __why_not_scenario__ = 'A retained real child and adverse birth observations exercise the restart signal guard.'

    @requirements('H1c-3')
    def test_signature_restart_never_signals_a_root_with_unconfirmed_birth(self):
        process = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait(5)))
        context = signature_execution._Context(None, None, 1, threading.Event(), 1, 1)
        context.process = process
        context.tree = ProcessTree(process.pid)
        context.tree.root_birth = '100.1'
        context.tree.identified = True
        for record in (None, (os.getpid(), process.pid, '100.2', False),
                       (1, process.pid, '100.1', False)):
            with self.subTest(record=record), \
                    mock.patch.object(signature_execution, 'process_record', return_value=record), \
                    mock.patch.object(signature_execution.os, 'killpg') as group, \
                    mock.patch.object(process, 'kill') as root:
                self.assertTrue(signature_execution.unproven_children(context))
                group.assert_not_called()
                root.assert_not_called()


class S12RestartStatusTests(unittest.TestCase):
    __why_not_scenario__ = 'Malformed status callbacks complement the admitted-probe collector scenario.'

    @requirements('M10', 'H1c-1')
    def test_admitted_probe_wait_names_process_without_clearing_its_hold(self):
        notes = lane_tests.notes_of(billing=lane_tests.lane(faulted=True, kept=1))
        project = 'prj_' + '1' * 32
        idle = {'run_slots': [], 'probes': [project]}
        item = {'lane': 'probe_worker', 'pid': 4242, 'why': 'tree_not_confirmed'}
        decision = account_restart.check(lambda: notes, lambda: idle, None, now_ms=lane_tests.NOW,
                                         read_held=lambda: [item])
        self.assertEqual('wait', decision['action'])
        self.assertEqual([project], decision['held']['probes'])
        self.assertEqual([item], decision['held']['processes'])
        for bad in (None, 'probe_worker', [{'lane': 'foreign', 'pid': 4242, 'why': 'not_reaped'}]):
            with self.subTest(bad=bad):
                decision = account_restart.check(lambda: notes, lambda: idle, None, now_ms=lane_tests.NOW,
                                                 read_held=lambda: bad)
                self.assertEqual('wait', decision['action'])
                self.assertEqual([project], decision['held']['probes'])


class S12CollectorLoopTests(unittest.TestCase):
    __why_not_scenario__ = 'Production collector loop and real account jobs; only transport listeners and watchdog are replaced.'
    def setUp(self):
        self.sql = []
        original = sqlite3.connect
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(lambda statement: self.sql.append((threading.current_thread(), statement)))
            return connection
        self.enterContext(mock.patch('codex_agents.learning_store.sqlite3.connect', side_effect=connect))
        self.opening_thread = threading.current_thread()

    def billing_fixture(self):
        case = lane_tests.BillingLaneRestartTests('test_M10_the_cooldown_still_applies_after_the_restart')
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        return case

    def run_loop(self, ledger, step, notes, children, close=lambda: None):
        """Run collector_host.main, retaining its real owner, restart predicate, record and teardown.

        Transport listeners and the process-exit watchdog are replaced: this needs no sockets or PTY.
        The account adapter drives the same real AccountService used by ingress.pump_owner.
        """
        decisions, passes = [], [0]
        deadline = time.monotonic() + 20
        server = SimpleNamespace(_stop=threading.Event(), _started_at_ms=1, thread_error=None,
                                 start=lambda: None, stop=lambda: None, wait=lambda timeout: False)
        def serve():
            passes[0] += 1
            self.assertLess(time.monotonic(), deadline, 'collector loop did not settle and restart')
            step(passes[0])
        account = SimpleNamespace(serve_once=serve, health_notes=notes, unended_children=children,
                                  close=close, drained=lambda: True,
                                  held_processes=lambda: [dict(lane=name, **item) for name, items in children().items()
                                                               if name == 'probe_worker' for item in items])
        real_check = account_restart.check
        def check(*args, **kwargs):
            decision = real_check(*args, **kwargs)
            decisions.append(decision)
            return decision
        cwd = Path.cwd()
        handlers = {sig: collector_host.signal.getsignal(sig) for sig in (collector_host.signal.SIGTERM, collector_host.signal.SIGINT)}
        self.addCleanup(os.chdir, cwd)
        for sig, handler in handlers.items():
            self.addCleanup(collector_host.signal.signal, sig, handler)
        with mock.patch.object(collector_host, 'LearningState', return_value=ledger.state), \
                mock.patch.object(collector_host, 'LearningLedger', return_value=ledger), \
                mock.patch.object(collector_host, 'require_authorized_collector_runtime'), \
                mock.patch.object(collector_host, 'CollectorServer', return_value=server), \
                mock.patch.object(collector_host, 'AccountServiceHost', return_value=account), \
                mock.patch.object(collector_host, 'ShutdownWatchdog'), \
                mock.patch.object(account_restart, 'CHECK_EVERY_SECONDS', 0), \
                mock.patch.object(account_restart, 'check', side_effect=check), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, collector_host.main(['--state-root', str(ledger.state.root)]))
        self.assertGreaterEqual(passes[0], 3)
        self.assertTrue(all(d['action'] == 'wait' for d in decisions[:-1]), decisions)
        self.assertEqual('restart', decisions[-1]['action'])
        self.assertIsNotNone(account_restart.last_restart(ledger.state.root))
        return decisions

    def assert_owner_sql(self):
        self.assertGreater(len(self.sql), 50)
        self.assertEqual({self.opening_thread}, {thread for thread, _ in self.sql})

    @requirements('M8', 'H1c-1', 'H1c-3', 'S12-B0-6', 'E1b-1')
    def test_M8_signature_tree_clears_once_and_next_claim_succeeds(self):
        case = lane_tests.SignatureLaneRestartTests('test_M8_an_execution_whose_child_cannot_be_proven_gone_holds_the_restart')
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        c = case.claims
        state = c.ledger.state
        with mock.patch.object(signature_execution, '_cleanup_process', return_value=False):
            c.begin()
            case.drive_until(c.jobs, lambda: c.jobs._pool.health.view()['kept'] == 1)
        c.jobs.drive()
        pool, clear = c.jobs._pool, [False]
        retained = [slot.retained for slot in pool._slots if slot.retained is not None]
        reaped_before = [context.roots_reaped for context in retained]
        real_proof = signature_execution.unproven_children
        def proof(context):
            return real_proof(context) if clear[0] else [{'pid': context.process.pid, 'why': 'tree_not_confirmed'}]
        def step(number):
            clear[0] = number >= 3
            c.service.drive_claims()
        def close():
            for _ in range(5):
                c.service.drive_claims()
                self.assertEqual([], pool.unproven_children())
            self.assertEqual([count + 1 for count in reaped_before],
                             [context.roots_reaped for context in retained])
            self.assertEqual(0, c.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])
            c.jobs.request_close()
            c.service.close()
        with mock.patch.object(signature_execution, 'unproven_children', side_effect=proof):
            self.run_loop(c.ledger, step, lambda: lane_tests.notes_of(signature=pool.health.view(), claims=c.jobs.health.view()),
                          lambda: account_host.unended_processes(pool, None), close)
        c.ledger = LearningLedger(state, client_capabilities=c.fixture.fixture.fixture.clients)
        self.addCleanup(c.ledger.close)
        jobs, service = case.restarted()
        request = c.request('after-restart')
        for _ in range(2):
            ticket = service.begin(encode_account_request(request), c.token)
            raw = ticket if type(ticket) is bytes else case.pump(jobs, service, ticket)
            answer = decode_account_response(raw, request)
            self.assertEqual('claimed', answer['state'])
        self.assertEqual(1, c.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])
        self.assert_owner_sql()

    def probe_loop(self, *, workspace_failed):
        case = self.billing_fixture()
        clear = [False]
        initial = ProcessTree.cleanup_after_owned_wait
        def inconclusive(tree, process):
            initial(tree, process)
            return False
        def make_unknown(stack):
            stack.enter_context(mock.patch.object(ProcessTree, 'cleanup_after_owned_wait', autospec=True, side_effect=inconclusive))
            if workspace_failed:
                case.fail_workspace_removal_after_the_probe_ran(stack)
        # Keep subsequent proof inconclusive through the first two collector checks.
        real_recheck = ProcessTree.ended_after_reap
        def recheck(tree, process):
            return clear[0] and real_recheck(tree, process)
        billing = case.services['billing']
        with mock.patch.object(ProcessTree, 'ended_after_reap', autospec=True, side_effect=recheck), \
                mock.patch.object(billing._executions, 'complete_ended', wraps=billing._executions.complete_ended) as commit, \
                mock.patch.object(billing._owner, 'settle_ended', wraps=billing._owner.settle_ended) as denial:
            case.unknown_probe(make_unknown)
            self.assertEqual(1, case.admitted())
            publication = mock.Mock(return_value=False)
            for job in billing._jobs.values():
                job.before_publish = publication  # Q3: the lost caller no longer authorizes publication.
            def step(number):
                clear[0] = number >= 3
                case.services['service'].drive_claims()
                if not clear[0]:
                    self.assertEqual(1, case.admitted())
                    self.assertEqual(0, commit.call_count + denial.call_count)
            def close():
                for _ in range(6):
                    case.services['service'].drive_claims()
                self.assertEqual(0, case.admitted())
                self.assertEqual(1, commit.call_count + denial.call_count)
                case.abandon(case.services)
            self.run_loop(case.ledger, step, case.lane_notes,
                          lambda: account_host.unended_processes(case.services['claims']._pool, case.services['workers'], case.services['observations']), close)
            publication.assert_not_called()
        case.ledger = LearningLedger(case.fixture.state, client_capabilities=case.fixture.clients)
        case.fixture.ledger = case.ledger
        self.addCleanup(case.ledger.close)
        case.services = case.build()
        self.assertEqual('reused', case.take('after-restart')['probe'])
        known = case.take('held')
        self.assertEqual(('completed', True), (known['probe'], known['known']))
        self.assertEqual(0, case.admitted())
        self.assert_owner_sql()

    @requirements('M9', 'H1c-1', 'H1c-3', 'S12-B0-6', 'E1b-1')
    def test_M9_billing_tree_clears_once_and_next_probe_succeeds(self):
        self.probe_loop(workspace_failed=True)

    @requirements('M10', 'H1c-1', 'H1c-3', 'S12-B0-6', 'E1b-1')
    def test_M10_probe_tree_clears_once_and_next_probe_succeeds(self):
        self.probe_loop(workspace_failed=False)

    @requirements('M10')
    def test_Q3_admitted_never_launched_probe_still_spends_bootstrap(self):
        case = self.billing_fixture()
        case.restart_after_unknown_probe(case.fail_handoff_after_issue)
        from codex_agents import account_billing_admission
        with mock.patch.object(account_billing_admission, 'PROBE_COOLDOWN_MS', 0):
            answer = case.take('after-restart')
        self.assertEqual(('denied', 'billing_unverified'), (answer['probe'], answer['reason_code']))
        self.assertEqual(0, case.admitted())
        self.assert_owner_sql()

    @requirements('H1c-3', 'S12-B0-6', 'E1b-1')
    def test_H1c_3_every_lane_child_must_clear_before_restart(self):
        case = self.billing_fixture()
        remaining = {name: [{'pid': 4242 + index, 'why': 'tree_not_confirmed'}]
                     for index, name in enumerate(('signature', 'probe_worker', 'observations'))}
        lanes = [mock.Mock(), mock.Mock(), mock.Mock()]
        for lane, name in zip(lanes, remaining):
            lane.unproven_children.side_effect = lambda name=name: remaining[name]
        def step(number):
            if number >= 2:
                remaining[list(remaining)[number - 2]] = []
        decisions = self.run_loop(case.ledger, step,
            lambda: lane_tests.notes_of(billing=lane_tests.lane(faulted=True, kept=1)),
            lambda: account_host.unended_processes(*lanes), lambda: case.abandon(case.services))
        self.assertEqual([['observations', 'probe_worker', 'signature'], ['observations', 'probe_worker'], ['observations']],
                         [d['held']['children'] for d in decisions[:-1]])
        self.assert_owner_sql()
