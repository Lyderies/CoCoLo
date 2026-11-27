"""Additional S15 release rows using product acquisitions and release writers.

Each mutation suppresses one reached release and applies the identical positive
row oracle. No SQL state planting, replacement receipt or service socket.
"""
import json
import sys
import unittest
from dataclasses import replace
from unittest import mock

from test_framework import requirements
from tests import test_record_context as contexts
from tests import test_result_leases as leases
from tests import test_memory_holds as memory_fixture
from tests import test_b6_unattended_close as b6
from tests import test_project_hold_register as register
from tests import test_deadlines_retention as deadlines
from codex_agents import memory_holds as memory, project_holds_schema as holds
from codex_agents.learning_contracts import canonical_json
from tests.learning_support import assignment

WHY = 'Real store acquisitions and release transactions, replay and reopening; socket-free release-path controls.'


class ReleasePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The account fixture uses git/ssh-keygen processes, but this task may
        # never open a socket, even if a future fixture changes its setup.
        def no_sockets(event, args):
            if event.startswith('socket.'):
                raise AssertionError('S15 release rows must not open sockets: ' + event)
        sys.addaudithook(no_sockets)

    def fixture(self, cls):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def released(self, store, kind, reason):
        rows = store._connection.execute(
            'SELECT state,release_kind FROM project_holds WHERE kind=?', (kind,)).fetchall()
        self.assertEqual([('released', reason)], [tuple(row) for row in rows], kind)

    def lease_row(self, exit):
        case = self.fixture(contexts.LeaseContextTests)
        case.reserve()
        store = case.store
        if exit == 'armed_unsent':
            store.arm_result_lease(leases.lease_binding())
        if exit == 'expired':
            case.fixture.now = 61_000
            operation = store.expire_result_leases
        elif exit == 'successor':
            case.base.reopen(result_capacity=case.fixture.capacity)
            store = case.store
            def operation():
                return store.release_earlier_unarmed_leases(set())
        else:
            def operation():
                return store.release_unsent_result_lease(leases.lease_binding())

        def oracle():
            self.released(store, 'lease', 'withdrawn_unsent')
            self.released(store, 'result_custody', 'lost_with_reservation')
            self.assertEqual('ended', contexts.context_row(store, *case.attempt(1))[0])
            self.assertEqual([], contexts.context_files(case.fixture.directory))
            self.assertEqual((0, 0, 0), store._result_counts())
        return case, store, operation, oracle

    def memory_row(self, kind, reason):
        case = self.fixture(memory_fixture.MemoryFixture)
        case.register()
        if kind == 'preparation':
            case.prep()
        def operation():
            return case.release(kind, reason)
        return case, case.store, operation, lambda: self.released(case.store, kind, reason)

    def orchestration_row(self, exit):
        case = self.fixture(b6.OrchestrationStore)
        snapshot = case.open()
        if exit == 'unattended':
            case.now = b6.hold(case.store, snapshot['orchestration_handle'])[3] + 1
            operation = case.owner.end_idle
            reason = 'interrupted_unattended'
        else:
            def operation():
                return case.close(snapshot)
            reason = 'closed'
        return case, case.store, operation, lambda: self.released(case.store, 'orchestration', reason)

    def denial_row(self):
        case = self.fixture(register.DenialRegisterTests)
        raw = canonical_json(case.call())
        store = case.fixture.store
        def operation():
            return store.settle_denied_result_lease(case.fixture.scope, raw)
        return case, store, operation, lambda: self.released(store, 'lease', 'denied')

    def bind_row(self):
        case = self.fixture(contexts.LeaseContextTests)
        case.reserve()
        case.store.arm_result_lease(leases.lease_binding())

        def oracle():
            self.released(case.store, 'lease', 'bound')
            self.assertEqual([('held', None)], [tuple(row) for row in case.store._connection.execute(
                "SELECT state,release_kind FROM project_holds WHERE kind='reservation'")])
            self.assertEqual((1, 1, 0), case.store._result_counts())
        return case, case.store, case.base.bind, oracle

    def retention_row(self):
        case = self.fixture(deadlines.LeaseBackedFixture)
        case.bound()
        case.publish()
        case.now = case.reservation().retained_until_ms

        def oracle():
            self.released(case.store, 'result_custody', 'retention')
            self.assertEqual('expired', case.context_state())
            self.assertEqual([], case.fixture.files())
            self.assertEqual([], contexts.context_files(case.fixture.directory))
            self.assertEqual((0, 0, 0), case.store._result_counts())
        return case, case.store, case.store.expire_results, oracle

    def handover_row(self):
        case = self.fixture(memory_fixture.MemoryFixture)
        case.register()
        target = replace(leases.lease_binding(),
                         primary_session_id=case.scope.primary_session_id,
                         runtime_digest=case.scope.runtime_digest,
                         supervisor_instance_id=case.scope.supervisor_instance_id,
                         supervisor_generation=case.scope.supervisor_generation,
                         orchestration_handle=case.handle, run_handle=case.key[0],
                         attempt=case.key[1], run_id='run-' + case.key[0][-4:])
        case.store.reserve_result_lease(target, assignment())
        case.store.arm_result_lease(target)
        case.store.record_open_binding(target.run_handle, target.attempt, target.run_id,
                                       'e' * 64, 'codex', instance=case.instance, now_ms=case.now)
        memory.hand_over(case.store, *case.key, case.instance)

        def oracle():
            self.released(case.store, 'driver_entry', 'handed_over')
            for kind in ('lease', 'open_binding'):
                row = case.store._connection.execute(
                    'SELECT state,evidence FROM project_holds WHERE kind=?', (kind,)).fetchone()
                self.assertEqual('held', row['state'])
                self.assertEqual('sweep', json.loads(row['evidence'])['instance'])
        return case, case.store, lambda: case.release('driver_entry', 'handed_over'), oracle

    def rows(self):
        for exit in ('unsent', 'armed_unsent', 'expired', 'successor'):
            for kind in ('lease', 'result_custody'):
                yield 'lease/' + exit + '/' + kind, holds, kind, lambda e=exit: self.lease_row(e)
        for kind, reason in (('driver_entry', 'retired'), ('preparation', 'retired'),
                             ('preparation', 'submit_refused'), ('preparation', 'never_launch_local')):
            yield kind + '/' + reason, memory, kind, lambda k=kind, r=reason: self.memory_row(k, r)
        for exit in ('primary', 'unattended'):
            yield 'orchestration/' + exit, holds, 'orchestration', lambda e=exit: self.orchestration_row(e)
        yield 'lease/denied', holds, 'lease', self.denial_row
        yield 'lease/bound', holds, 'lease', self.bind_row
        yield 'result_custody/retention', holds, 'result_custody', self.retention_row
        yield 'driver_entry/handed_over', memory, 'driver_entry', self.handover_row

    @requirements('S15-RS8-RELEASE-PATHS', why_not_scenario=WHY)
    def test_release_paths_replay_without_changing_the_first_release(self):
        for name, _, _, setup in self.rows():
            with self.subTest(row=name):
                case, store, operation, oracle = setup()
                try:
                    operation()
                    oracle()
                    before = [tuple(row) for row in store._connection.execute('SELECT * FROM project_holds')]
                    operation()
                    oracle()
                    self.assertEqual(before, [tuple(row) for row in store._connection.execute('SELECT * FROM project_holds')])
                finally:
                    case.doCleanups()

    @requirements('S15-RS8-MUTATION', why_not_scenario=WHY)
    def test_each_additional_release_disabled_once_fails_the_same_row(self):
        for name, module, disabled, setup in self.rows():
            with self.subTest(row=name):
                case, store, operation, oracle = setup()
                try:
                    original = module.release_hold
                    hits = []

                    def off(connection, project, kind, *args, **kwargs):
                        if kind == disabled:
                            hits.append(kind)
                            return None
                        return original(connection, project, kind, *args, **kwargs)

                    with mock.patch.object(module, 'release_hold', off):
                        operation()
                    self.assertEqual([disabled], hits)
                    with self.assertRaises(AssertionError):
                        oracle()
                finally:
                    case.doCleanups()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ReleasePathTests)
