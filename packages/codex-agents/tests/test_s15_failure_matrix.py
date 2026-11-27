"""S15 account and local-host failure boundaries, without service sockets.

The probe admission fixture is a sealed store fixture, not signature authority.
The worker rows deliberately fail before launch; no process-cleanup proof is
manufactured. Native container outcomes remain S27 carry-ins.
"""
from contextlib import contextmanager
import sys
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_run_holds as account
from tests import test_rsf_s5_probe as probes
from tests import test_codex_worker as workers
from tests import test_project_monitor as monitors
from codex_agents import learning_store
from codex_agents.account_billing_admission import PROBE_ORPHAN_GRACE_MS

WHY = 'Real account transactions, owner clocks, worker queues and local monitor cleanup; no service sockets, model launch or native-authority claim.'


class Cut(BaseException):
    pass


class Suppressed(dict):
    """Disable one actual deletion while preserving every other dictionary use."""
    def __init__(self, value, target):
        super().__init__(value)
        self.target, self.hits = target, []

    def pop(self, key, *default):
        if key == self.target:
            self.hits.append(key)
            return self[key]
        return super().pop(key, *default)

    def __delitem__(self, key):
        if key == self.target:
            self.hits.append(key)
            return
        super().__delitem__(key)


class FailureMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def no_sockets(event, args):
            if event.startswith('socket.'):
                raise AssertionError('S15 failure matrix must not open sockets')
        sys.addaudithook(no_sockets)

    def fixture(self, cls):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def account_row(self, name):
        if name.startswith('probe/'):
            case = self.fixture(probes._StoreFixture)
            original = case.reserve('matrix')
            if name == 'probe/orphaned_admission':
                case.admit(original)
                case.restart()
                case.clock = original.deadline + PROBE_ORPHAN_GRACE_MS + 1
            else:
                case.clock = original.deadline + 1
            kind, identity = 'probe', original.key
            operation = case.sweep_now
        elif name == 'run_slot/completed':
            from tests.test_account_cleanup_release import AccountCleanupReleaseTests
            from codex_agents.learning_contracts import keyed_digest
            case = self.fixture(AccountCleanupReleaseTests)
            kind, identity = 'run_slot', keyed_digest(case.ledger.state.digest_key(), case.binding.run_handle)
            operation = case.call
        else:
            case = self.fixture(account.RunHoldTests)
            if name == 'run_slot/boot_change':
                case.case.claim()
                row = case.rows()[0]
                kind, identity = 'run_slot', row['handle_digest']
                now = row['deadline_at_ms'] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
                def operation():
                    return case.sweep(now, row['clock_reference_ns'] - 1)
            else:
                project, launch = case.new_attempt()
                from codex_agents.learning_contracts import keyed_digest
                kind, identity = 'binding', keyed_digest(case.ledger.state.digest_key(), launch.run_handle)
                if name == 'binding/withdrawn':
                    def operation():
                        return case.withdraw(project, launch.run_handle)
                else:
                    expires = case.ledger._connection.execute(
                        'SELECT expires_at_ms FROM host_run_bindings WHERE handle_digest=?', (identity,)).fetchone()[0]
                    def operation():
                        return case.sweep(expires + 1, 1)

        def snapshot():
            return tuple(case.ledger._connection.execute(
                'SELECT * FROM run_holds WHERE kind=? AND identity=?', (kind, identity)).fetchone())

        def oracle():
            row = case.ledger._connection.execute(
                'SELECT state,generation FROM run_holds WHERE kind=? AND identity=?', (kind, identity)).fetchone()
            self.assertEqual(('released', 2), tuple(row))
        self.assertEqual('held', case.ledger._connection.execute(
            'SELECT state FROM run_holds WHERE kind=? AND identity=?', (kind, identity)).fetchone()[0])
        return case, operation, snapshot, oracle

    ACCOUNT_ROWS = ('binding/withdrawn', 'binding/expired', 'run_slot/boot_change', 'run_slot/completed',
                    'probe/reserved_expiry', 'probe/orphaned_admission')

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_account_holds_cross_transaction_cuts_and_lost_release_reply(self):
        for name in self.ACCOUNT_ROWS:
            for cut in ('before_effect', 'after_effect_before_commit', 'after_commit'):
                with self.subTest(row=name, cut=cut):
                    case, operation, snapshot, oracle = self.account_row(name)
                    try:
                        before = snapshot()
                        transaction = case.ledger._account_transaction
                        depth = [0]

                        @contextmanager
                        def interrupted():
                            outer = depth[0] == 0
                            depth[0] += 1
                            try:
                                with transaction():
                                    if outer and cut == 'before_effect':
                                        raise Cut()
                                    yield
                                    changed = snapshot() != before
                                    if outer and changed and cut == 'after_effect_before_commit':
                                        raise Cut()
                                if outer and changed and cut == 'after_commit':
                                    raise Cut()
                            finally:
                                depth[0] -= 1

                        with mock.patch.object(case.ledger, '_account_transaction', interrupted), self.assertRaises(Cut):
                            operation()
                        if cut != 'after_commit':
                            self.assertEqual(before, snapshot())
                            operation()
                        oracle()
                        released = snapshot()
                        operation()
                        oracle()
                        self.assertEqual(released, snapshot())
                    finally:
                        case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_account_release_trigger_disabled_fails_the_ordinary_release_oracle(self):
        for name in self.ACCOUNT_ROWS:
            with self.subTest(row=name):
                case, operation, snapshot, oracle = self.account_row(name)
                try:
                    trigger = ('run_holds_probe_released' if name.startswith('probe/') else
                               'run_holds_slot_released' if name.startswith('run_slot/') else
                               'run_holds_binding_released')
                    connection = case.ledger._connection
                    sql = connection.execute('SELECT sql FROM sqlite_master WHERE type=\'trigger\' AND name=?',
                                             (trigger,)).fetchone()[0]
                    connection.execute('DROP TRIGGER ' + trigger)
                    try:
                        operation()
                        with self.assertRaises(AssertionError):
                            oracle()
                    finally:
                        connection.execute(sql)
                finally:
                    case.doCleanups()

    def worker_row(self, disabled=None):
        case = self.fixture(workers.CodexWorkerTests)
        # Genuine original payload enters the fixed queue, but cannot decode.
        # That is a failure before launch, not a simulated cleaned child.
        prepared = case.prepare(submit=False, mutate=lambda data: data.clear())
        root, target, ticket, handle, supervisor, _ = prepared
        case.pool.submit(handle)
        case.returned(prepared)
        self.assertEqual('worker_input_invalid', case.pool.packet(handle).error_code)
        self.assertFalse((root / 'launches').exists())
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        result = case.owner.result(target, case.pool, handle)
        self.assertIs(result, case.owner.result(target, case.pool, handle))
        self.assertIn(handle, case.owner._results)
        case.monitor.observe_terminal(target)
        case.monitor.observe_cleanup(target)
        targets = {
            'execution': (case.registry, '_pending', ticket),
            'worker': (case.pool, '_jobs', handle),
            'result': (case.owner, '_results', handle),
            'monitor': (case.monitor, '_attempts', (target.run_handle, target.attempt)),
        }
        mutated = None
        if disabled:
            owner, field, key = targets[disabled]
            mutated = Suppressed(getattr(owner, field), key)
            setattr(owner, field, mutated)

        def oracle():
            self.assertEqual({}, case.registry._pending)
            self.assertEqual(set(), case.registry._keys)
            self.assertEqual({}, case.pool._jobs)
            self.assertEqual(0, case.pool._charged)
            self.assertEqual({}, case.owner._results)
            self.assertEqual({}, case.monitor._attempts)
            self.assertFalse((root / 'launches').exists())

        case.registry.acknowledge_release(ticket)  # Local fixture; no account release authority claimed.
        case.owner.retire(target, case.pool, handle)
        case.owner.forget(target)
        return case, mutated, oracle

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_actual_failure_before_launch_frees_worker_result_execution_and_monitor(self):
        case, _, oracle = self.worker_row()
        oracle()
        # A new original input can take the freed worker slot.
        handle = case.pool.prepare(b'{}')
        case.pool.withdraw_prepared(handle)
        oracle()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_local_host_deletion_disabled_fails_the_same_failure_oracle(self):
        for kind in ('execution', 'worker', 'result', 'monitor'):
            with self.subTest(kind=kind):
                case, mutated, oracle = self.worker_row(kind)
                try:
                    self.assertEqual(1, len(mutated.hits))
                    with self.assertRaises(AssertionError):
                        oracle()
                finally:
                    case.doCleanups()

    def preparation_row(self, kind, disabled=False):
        if kind == 'host_preparation':
            from tests.test_codex_preparation import CodexPreparationTests
            case = self.fixture(CodexPreparationTests)
            handle, artifact = case.prepare()
            workspace = artifact.workspace
            case.lane.discard(handle)
            case.until(lambda: case.lane.snapshot(handle)['state'] in {'cleaned', 'unknown'})
            self.assertEqual('cleaned', case.lane.snapshot(handle)['state'])
            owner = case.lane
            def operation():
                return owner.retire(handle)
        else:
            from tests.test_billing_probe_worker import BillingProbeWorkerTests
            case = self.fixture(BillingProbeWorkerTests)
            if kind == 'probe_preparation_failed':
                handle = case.pool.prepare(b'{}')
                case.until(lambda: case.pool.snapshot(handle)['state'] != 'preparing')
                self.assertEqual('failed', case.pool.snapshot(handle)['state'])
                workspace = None
            else:
                handle, prepared = case.prepare()
                workspace = prepared.workspace
                case.pool.discard(handle)
                case.until(lambda: case.pool.snapshot(handle)['state'] in {'cleaned', 'unknown'})
                self.assertEqual('cleaned', case.pool.snapshot(handle)['state'])
            owner = case.pool
            def operation():
                return owner.retire(handle)
        self.assertNotIn(None, owner._jobs)
        self.assertIn(handle, owner._jobs)
        mutated = None
        if disabled:
            mutated = Suppressed(owner._jobs, handle)
            owner._jobs = mutated

        def oracle():
            self.assertEqual({}, owner._jobs)
            self.assertTrue(all(slot.current is None for slot in owner._slots))
            if workspace is not None:
                self.assertFalse(workspace.exists())
            if kind == 'host_preparation':
                self.assertEqual((0, 0), (owner._reserved_bytes, owner._reserved_export_bytes))
                self.assertEqual({}, owner._pool._jobs)
            else:
                self.assertEqual(0, owner._charged)
                self.assertEqual({}, case.registry._pending)
        operation()
        return case, mutated, oracle

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_host_and_probe_preparation_cancellation_and_failure_remove_actual_workspaces(self):
        for kind in ('host_preparation', 'probe_preparation_cancelled', 'probe_preparation_failed'):
            with self.subTest(kind=kind):
                case, _, oracle = self.preparation_row(kind)
                try:
                    oracle()
                finally:
                    case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_preparation_retirement_disabled_fails_the_same_cleanup_oracle(self):
        for kind in ('host_preparation', 'probe_preparation_cancelled', 'probe_preparation_failed'):
            with self.subTest(kind=kind):
                case, mutated, oracle = self.preparation_row(kind, True)
                try:
                    self.assertEqual([mutated.target], mutated.hits)
                    with self.assertRaises(AssertionError):
                        oracle()
                finally:
                    case.doCleanups()

    def project_row(self, name):
        from codex_agents import project_holds_schema as holds
        if name.endswith('/fenced'):
            from tests.test_account_claim_storage import AccountClaimStorageTests
            from codex_agents.claim_denial import FenceClaimInput
            from codex_agents.learning_contracts import canonical_json
            case = self.fixture(AccountClaimStorageTests)
            lease = case.arm()
            case.store.record_open_binding(case.scope.run_handle, case.scope.attempt, case.scope.run_id,
                'e' * 64, 'codex', instance='earlier', now_ms=case.base.stamp)
            fence = FenceClaimInput(case.scope.run_handle, None, case.scope.claim_request_id, lease.lease_digest)
            closure = case.ledger._fence_claim(case.project, runtime_digest=case.scope.runtime_digest,
                caller_scope='a' * 64, fence=fence, storage_binding=case.scope, storage_evidence=case.evidence)
            closure = closure['closure']
            self.assertEqual('claim_not_open', closure['denial']['reason_code'])
            store, kind, reason = case.store, name.split('/')[0], 'fenced'
            def operation():
                return store.settle_denied_result_lease(case.scope, canonical_json(closure))
        elif name.startswith('open_binding/'):
            from tests import test_open_withdrawal as opens
            case = self.fixture(opens.OpenBindingStoreTests)
            case.record()
            target = opens.binding()
            reason = name.split('/')[1]
            case.store.mark_open_withdrawing(target.run_handle, 1, 'cancelled_before_claim',
                project_id=opens.PROJECT, runtime_digest=opens.COLLECTOR_RUNTIME_DIGEST)
            def operation():
                return case.store.release_open_binding(target.run_handle, 1, reason, receipt='6' * 64)
            store, kind = case.store, 'open_binding'
        elif name == 'reservation/completed':
            from tests.test_account_cleanup_release import AccountCleanupReleaseTests
            from codex_agents.project_release_ack import _release_obligation
            case = self.fixture(AccountCleanupReleaseTests)
            receipt = case.call()
            store = case.base.fixture.store
            lease = store.result_lease(case.binding)
            kind, reason = 'reservation', 'completed'

            def operation():
                # The exact internal release called by record_account_release.
                # This row proves its transaction/replay, not native ACK authority.
                with store._transaction():
                    _release_obligation(store, lease, receipt['receipt_digest'])
        elif name == 'result_custody/orchestration_closed':
            from tests.test_deadlines_retention import OrchestrationCloseTests
            case = self.fixture(OrchestrationCloseTests)
            case.recorded()
            store, kind, reason = case.store, 'result_custody', 'orchestration_closed'

            def operation():
                # The closure observation is controlled; expiry, unlinking and
                # custody release are the actual owner's implementation.
                with case.closed():
                    store.expire_results()
        else:
            raise AssertionError(name)

        def snapshot():
            return [tuple(row) for row in store._connection.execute('SELECT * FROM project_holds')]

        def oracle():
            rows = store._connection.execute(
                'SELECT state,release_kind FROM project_holds WHERE kind=?', (kind,)).fetchall()
            self.assertEqual([('released', reason)], [tuple(row) for row in rows])
            if kind == 'result_custody':
                from tests.test_record_context import context_files
                self.assertEqual([], context_files(case.directory))
        self.assertEqual('releasing' if name in ('open_binding/withdrawn', 'open_binding/account_final') else 'held',
            store._connection.execute('SELECT state FROM project_holds WHERE kind=?', (kind,)).fetchone()[0])
        return case, holds, kind, operation, snapshot, oracle

    PROJECT_ROWS = ('open_binding/withdrawn', 'open_binding/account_final',
                    'reservation/completed', 'result_custody/orchestration_closed',
                    'lease/fenced', 'open_binding/fenced')

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_project_withdrawal_completion_and_closed_custody_replay_keep_first_release(self):
        for name in self.PROJECT_ROWS:
            with self.subTest(row=name):
                case, _, _, operation, snapshot, oracle = self.project_row(name)
                try:
                    operation()
                    oracle()
                    first = snapshot()
                    operation()
                    oracle()
                    self.assertEqual(first, snapshot())
                finally:
                    case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_project_final_release_disabled_fails_the_same_row(self):
        for name in self.PROJECT_ROWS:
            with self.subTest(row=name):
                case, module, kind, operation, _, oracle = self.project_row(name)
                try:
                    release, hits = module.release_hold, []

                    def off(connection, project, target_kind, *args, **kwargs):
                        if target_kind == kind:
                            hits.append(kind)
                            return
                        return release(connection, project, target_kind, *args, **kwargs)

                    with mock.patch.object(module, 'release_hold', off):
                        operation()
                    self.assertEqual([kind], hits)
                    with self.assertRaises(AssertionError):
                        oracle()
                finally:
                    case.doCleanups()

    def monitor_row(self, exit, disabled=False):
        case = monitors.ProjectMonitorTests('runTest')
        clock, owner = case.make_monitor(capacity=1)
        target = monitors.binding()
        deadline = (2 if exit == 'deadline' else 100) * monitors.SECOND
        owner.register(target, hard_deadline_ns=deadline)
        if exit == 'cancel':
            self.assertEqual('cancel_requested', owner.cancel(target).stop_reason)
        elif exit == 'no_result':
            clock.at(18)
            self.assertEqual('startup_silence', owner.status(target).stop_reason)
        elif exit == 'deadline':
            clock.at(2)
            self.assertEqual('absolute_deadline', owner.status(target).stop_reason)
        elif exit == 'clock_rollback':
            clock.at(1)
            owner.status(target)
            clock.at(0)
            self.assertEqual('monitor_unavailable', owner.status(target).stop_reason)
        else:
            self.assertEqual('completed', exit)
        # Neither observation alone permits forgetting a stopped/finished row.
        with self.assertRaisesRegex(monitors.ProjectMonitorError, 'monitor_attempt_live'):
            owner.forget(target)
        owner.observe_terminal(target)
        with self.assertRaisesRegex(monitors.ProjectMonitorError, 'monitor_attempt_live'):
            owner.forget(target)
        owner.observe_cleanup(target)
        mutated = None
        if disabled:
            mutated = Suppressed(owner._attempts, (target.run_handle, target.attempt))
            owner._attempts = mutated
        owner.forget(target)

        def oracle():
            self.assertEqual({}, owner._attempts)
            with self.assertRaisesRegex(monitors.ProjectMonitorError, 'monitor_attempt_not_found'):
                owner.status(target)
        return mutated, oracle

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_monitor_exit_matrix_requires_terminal_and_cleanup_before_forgetting(self):
        for exit in ('completed', 'cancel', 'no_result', 'deadline', 'clock_rollback'):
            with self.subTest(exit=exit):
                _, oracle = self.monitor_row(exit)
                oracle()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_monitor_exit_with_forgetting_disabled_fails_the_same_oracle(self):
        for exit in ('completed', 'cancel', 'no_result', 'deadline', 'clock_rollback'):
            with self.subTest(exit=exit):
                mutated, oracle = self.monitor_row(exit, True)
                self.assertEqual([mutated.target], mutated.hits)
                with self.assertRaises(AssertionError):
                    oracle()

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_refused_open_claim_and_dispatch_do_not_acquire_downstream_holds(self):
        from tests.test_project_open_scope import ProjectOpenScopeTests
        from tests.test_account_claim_operation import AccountClaimOperationTests
        from tests.test_prepared_dispatch import PreparedDispatchTests
        case = self.fixture(ProjectOpenScopeTests)
        before = [tuple(row) for row in case.store._connection.execute('SELECT * FROM project_holds')]
        case.test_default_denial_and_foreign_scope_refuse_before_writer()
        self.assertEqual(before, [tuple(row) for row in case.store._connection.execute('SELECT * FROM project_holds')])
        case = self.fixture(AccountClaimOperationTests)
        before = [tuple(row) for row in case.ledger._connection.execute('SELECT * FROM run_holds')]
        case.test_default_denial_hook_owner_and_missing_scope_do_no_claim_work()
        self.assertEqual(before, [tuple(row) for row in case.ledger._connection.execute('SELECT * FROM run_holds')])
        self.assertEqual([], case.ledger._connection.execute('SELECT * FROM active_run_capacity').fetchall())
        case = self.fixture(PreparedDispatchTests)
        worker_before = dict(case.fixture.pool._jobs)
        case.test_default_denial_and_transport_labels_cannot_queue_or_touch_originals()
        self.assertEqual(worker_before, case.fixture.pool._jobs)
        self.assertEqual({}, case.fixture.registry._pending)
        self.assertEqual({}, case.fixture.monitor._attempts)
        self.assertEqual({}, case.lane._jobs)

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_lost_claim_reply_restarts_owner_without_another_claim_or_slot(self):
        from tests.test_account_claim_operation import AccountClaimOperationTests
        case = self.fixture(AccountClaimOperationTests)
        case.test_lost_reply_reopen_needs_no_signature_work_or_new_claim()
        self.assertEqual(1, case.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])
        self.assertEqual(1, case.ledger._connection.execute(
            "SELECT COUNT(*) FROM run_holds WHERE kind='run_slot' AND state='held'").fetchone()[0])

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_lost_and_duplicate_dispatch_replies_keep_one_original_without_new_custody(self):
        from tests.test_prepared_dispatch import PreparedDispatchTests
        case = self.fixture(PreparedDispatchTests)
        service = case.service(authorizer=case.authorize)
        first = case.call(service)  # The caller loses this reply.
        self.assertEqual('queued', first['state'])
        again = case.call(service)
        self.assertEqual({**first, 'known': True}, again)
        self.assertEqual(1, len(service._jobs))
        case.assert_unlaunched()
        # The typed fixture has no original consumption. The actual advance
        # therefore refuses, retains the uncertain request and never launches.
        service.drive()
        self.assertEqual('unknown', case.call(service)['state'])
        self.assertEqual(1, len(service._jobs))
        case.assert_unlaunched()
        self.assertEqual({}, case.lane._jobs)
        self.assertEqual({}, case.fixture.monitor._attempts)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(FailureMatrixTests)
