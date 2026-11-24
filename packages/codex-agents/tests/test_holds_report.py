"""B7a store/protocol/status unit tests. No sockets or provider calls."""
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_deadlines_retention as dr
from tests import test_result_leases as leases
from tests import test_record_context as rc
from tests.learning_support import assignment, verification_outcome
from tests.test_project_history import PROJECT_A, PROJECT_B, binding
from codex_agents import client_runtime
from codex_agents.history_api import (HistoryClient, HistoryRequest, decode_history_response,
    encode_history_request, history_request_digest)
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory

RUNTIME = 'a' * 64
KINDS = {'driver_entry', 'preparation', 'lease', 'reservation', 'orchestration', 'open_binding', 'result_custody'}
MEMORY = {'driver_entry', 'preparation', 'orchestration'}
FIELDS = {'tracking', 'held', 'releasing', 'past_due', 'next_eligible', 'oldest_taken_at_ms'}
TOKEN = object()


def report(store, **options):
    from codex_agents.project_holds_report import holds_report
    return holds_report(store, runtime_digest=RUNTIME, **options)


def command(project=PROJECT_A, runtime=RUNTIME, **options):
    return HistoryRequest(1, 'history.holds_report', options.get('identity', 'holds'), project, runtime,
                          options.get('payload', {}))


def service_for(store):
    def authorize(request, token):
        if token is not TOKEN:
            return None
        return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request),
                                    'host', frozenset({'history.holds_report', 'history.capacity_report'}))
    return HistoryService(store.project_id, RUNTIME, lambda: store, authorizer=authorize, close_history=False)


class HoldsReportTests(dr.LeaseBackedFixture):
    def test_B7_1_empty_has_every_kind_and_explicit_zero_null_fields(self):
        value = report(self.store)
        self.assertEqual({'project_holds', 'result_custody', 'result_retirement_repairs'}, set(value))
        self.assertEqual(KINDS, set(value['project_holds']))
        for kind, counts in value['project_holds'].items():
            self.assertEqual(FIELDS, set(counts))
            self.assertEqual({'tracking': 'not_tracked' if kind in MEMORY else 'tracked', 'held': 0,
                'releasing': 0, 'past_due': 0 if kind in {'orchestration', 'result_custody'} else None,
                'next_eligible': None, 'oldest_taken_at_ms': None}, counts)
        self.assertEqual({'live': 0, 'context_bytes': 0, 'max_live_context_bytes': 256 * 1024 * 1024,
            'retained_until_ms': None, 'awaiting_verifier': 0, 'legacy_no_context': 0,
            'ended_by_update_unrecorded': 0}, value['result_custody'])
        self.assertEqual({'entries': [], 'truncated': False}, value['result_retirement_repairs'])

    def test_B7_1_mixed_states_evidence_deadlines_and_oldest_are_exact(self):
        self.store.reserve_result_lease(leases.lease_binding(1), assignment(), record_context=rc.simple_context(1))
        def timed_bound(number):
            self.store.reserve_result_lease(leases.lease_binding(number), assignment(), record_context=rc.simple_context(number))
            self.store.arm_result_lease(leases.lease_binding(number))
            self.base.bind(number, hard_deadline_at_ms=20000)
        self.now = 2000
        timed_bound(2)
        self.now = 3000
        timed_bound(3)
        self.store._connection.execute("UPDATE project_holds SET state='releasing' WHERE kind='reservation' AND identity=?",
                                       (binding(3).claim_id,))
        self.publish(2)
        self.now = 1_000_000
        value = report(self.store)['project_holds']
        self.assertEqual({'tracking': 'tracked', 'held': 1, 'releasing': 0, 'past_due': None,
                          'next_eligible': None, 'oldest_taken_at_ms': 1000}, value['lease'])
        self.assertEqual({'tracking': 'tracked', 'held': 1, 'releasing': 1, 'past_due': None,
                          'next_eligible': None, 'oldest_taken_at_ms': 2000}, value['reservation'])
        self.assertEqual((3, 0, 1, 123000, 1000), tuple(value['result_custody'][key] for key in
                         ('held', 'releasing', 'past_due', 'next_eligible', 'oldest_taken_at_ms')))

    def test_B7_1_clock_floor_and_read_only_no_private_file_reads(self):
        self.bound()
        self.publish()
        db = self.store._connection
        db.execute('UPDATE result_identity SET last_observed_at_ms=200000')
        self.now = 500
        before = list(db.iterdump())
        with mock.patch('builtins.open', side_effect=AssertionError('private file opened')), \
                mock.patch('os.open', side_effect=AssertionError('private file opened')):
            value = report(self.store)
        self.assertEqual(1, value['project_holds']['result_custody']['past_due'])
        self.assertEqual(before, list(db.iterdump()))

    def test_B7_2_before_after_admission_retirement_and_failed_retirement_charges(self):
        self.bound()
        before = report(self.store)['result_custody']
        self.assertEqual((1, None, 0), (before['live'], before['retained_until_ms'], before['awaiting_verifier']))
        self.assertGreater(before['context_bytes'], 0)
        self.publish()
        self.assertEqual(121000, report(self.store)['result_custody']['retained_until_ms'])
        self.record('b' * 64)
        self.now = 121000
        with mock.patch('os.unlink', side_effect=OSError('PRIVATE-IO-CANARY')):
            with self.assertRaises(HistoryError):
                self.store.expire_results()
        charged = report(self.store)['result_custody']
        self.assertEqual((1, before['context_bytes']), (charged['live'], charged['context_bytes']))
        self.store.expire_results()
        after = report(self.store)['result_custody']
        self.assertEqual((0, 0, None), (after['live'], after['context_bytes'], after['retained_until_ms']))

    def test_B7_2_awaiting_verifier_counts_workers_and_excludes_reviewed_and_verifiers(self):
        db = self.store._connection
        for number in (1, 2, 3):
            self.bound(number)
            self.publish(number)
            record = verification_outcome(f'worker-{number}')
            record['task_slice'] = 'verification-learning' if number == 3 else 'coding-medium-low'
            db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)',
                (PROJECT_A, record['record_id'], 'outcome', 'host', number + 100, 1000, 1000,
                 canonical_json(record), f'{number:064x}', 0))
            self.record(f'{number:064x}', number)
        self.assertEqual(2, report(self.store)['result_custody']['awaiting_verifier'])
        db.execute('INSERT INTO project_operational_qa VALUES(' + ','.join('?' * 16) + ')',
            (PROJECT_A, f'{1:064x}', 'v', 'w', 'v', 'check', 'scope', 'qa', b'{}', 'reviewed', 'outcome', 100, 1000, 0, 1000, 'seal'))
        self.assertEqual(1, report(self.store)['result_custody']['awaiting_verifier'])

    def test_B7_2_legacy_and_second_project_are_separate_and_private(self):
        self.bound()
        self.store._connection.execute("UPDATE project_result_contexts SET state='legacy_no_context',context_size=0,"
                                       "context_digest=NULL,host_class=NULL")
        other_path = self.fixture.directory.parent / 'PRIVATE-FOREIGN-PROJECT'
        other_path.mkdir(mode=0o700)
        other = ProjectHistory(other_path, PROJECT_B)
        self.addCleanup(other.close)
        self.assertEqual(0, report(other)['result_custody']['live'])
        value = report(self.store)
        self.assertEqual((1, 1, 0), tuple(value['result_custody'][key] for key in ('live', 'legacy_no_context', 'context_bytes')))
        for private in (PROJECT_A, PROJECT_B, binding().run_handle, binding().claim_id, 'PRIVATE', '.context-', '.result-'):
            self.assertNotIn(private, json.dumps(value))

    def test_B7_3_real_D3_directory_is_plain_operator_needed_before_retry(self):
        self.bound(1)
        self.publish(1)
        self.bound(2)
        self.publish(2)
        from codex_agents.project_result_contexts import names
        context = self.fixture.directory / names(self.store, *self.attempt(2))[1]
        context.unlink()
        context.mkdir()
        self.now = 121000
        original = __import__('os').unlink
        def fail_first(name, *args, **kwargs):
            if name in self.store._result_names(binding(1)):
                raise OSError('PRIVATE-IO')
            return original(name, *args, **kwargs)
        with mock.patch('os.unlink', side_effect=fail_first), self.assertRaises(HistoryError):
            self.store.expire_results()
        repairs = report(self.store)['result_retirement_repairs']
        self.assertFalse(repairs['truncated'])
        self.assertEqual([True, False], [entry['operator_needed'] for entry in repairs['entries']])
        for entry in repairs['entries']:
            self.assertEqual({'reason', 'action', 'operator_needed'}, set(entry))
            self.assertGreater(len(entry['action']), 10)
        self.assertNotIn(binding(2).run_handle, json.dumps(repairs))
        self.assertNotIn(context.name, json.dumps(repairs))

    def test_B7_3_more_than_twenty_repairs_are_bounded_with_truncated(self):
        db = self.store._connection
        for number in range(21):
            db.execute("INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,"
                       "lease_deadline_ms,repair_reason) VALUES(?,?,1,'repair_required',7,1000,'directory')",
                       (PROJECT_A, f'hrn_{number:048x}'))
        value = report(self.store)
        self.assertEqual(20, len(value['result_retirement_repairs']['entries']))
        self.assertTrue(value['result_retirement_repairs']['truncated'])
        self.assertEqual(21, value['result_custody']['live'])


    def test_B7_1_custody_deadline_equality_and_open_binding_evidence_only(self):
        self.bound()
        self.publish()
        db = self.store._connection
        db.execute("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,due_at_ms) "
                   "VALUES(?,'open_binding','private-binding','held',500,1)", (PROJECT_A,))
        for now, expected in ((120999, 0), (121000, 1), (121001, 1)):
            self.now = now
            value = report(self.store)['project_holds']
            self.assertEqual(expected, value['result_custody']['past_due'])
            self.assertEqual(121000, value['result_custody']['next_eligible'])
            self.assertEqual({'tracking': 'tracked', 'held': 1, 'releasing': 0, 'past_due': None,
                              'next_eligible': None, 'oldest_taken_at_ms': 500}, value['open_binding'])

    def test_B7_3_operator_needed_beyond_first_D3_page_precedes_all_retries(self):
        from codex_agents.project_result_contexts import RepairReport
        first = RepairReport([{'reason': 'retrying', 'names': ['PRIVATE']} for _ in range(20)])
        first.truncated, first.cursor = True, '1:20'
        second = RepairReport([{'reason': 'directory', 'names': ['PRIVATE-DIRECTORY']}])
        with mock.patch.object(self.store, 'result_retirement_repairs', side_effect=[first, second]) as reader:
            repairs = report(self.store)['result_retirement_repairs']
        self.assertEqual([mock.call(20, None), mock.call(20, '1:20')], reader.call_args_list)
        self.assertEqual(20, len(repairs['entries']))
        self.assertTrue(repairs['truncated'])
        self.assertTrue(repairs['entries'][0]['operator_needed'])
        self.assertNotIn('PRIVATE', json.dumps(repairs))

    def test_B7_2_started_verifier_is_no_longer_awaiting_a_verifier(self):
        self.bound()
        self.publish()
        record = verification_outcome('worker-started')
        record['task_slice'] = 'coding-medium-low'
        db = self.store._connection
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)',
            (PROJECT_A, record['record_id'], 'outcome', 'host', 101, 1000, 1000,
             canonical_json(record), 'b' * 64, 0))
        self.record('b' * 64)
        self.assertEqual(1, report(self.store)['result_custody']['awaiting_verifier'])
        verification = {'schema_version': 1, 'record_type': 'verification_binding', 'record_id': 'verify-started',
                        'run_id': 'verifier-1', 'worker_outcome_digest': 'b' * 64, 'observed_at_ms': 1000}
        self.store.append_record(canonical_json(verification), source='host', ingested_at_ms=1000)
        self.assertEqual(0, report(self.store)['result_custody']['awaiting_verifier'])


class HoldsUpdateTests(unittest.TestCase):
    def test_B7_2_ended_by_update_unrecorded_uses_verified_checkpoint_and_release(self):
        from dataclasses import replace
        from tests import test_project_orchestration as orchestration
        from codex_agents.project_holds_report import holds_report
        fixture = orchestration.ProjectOrchestrationTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        opened = fixture.opened()
        owner = fixture.store._orchestration_owner()
        self.assertEqual(1, owner.end_earlier(replace(fixture.scope, runtime_digest='b' * 64), 10))
        db = fixture.store._connection
        for number, outcome in ((1, None), (2, 'a' * 64)):
            run = f'hrn_{number:048x}'
            db.execute("INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,"
                       "lease_deadline_ms,outcome_digest) VALUES(?,?,1,'expired',0,1000,?)", (PROJECT_A, run, outcome))
            db.execute("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,release_kind) "
                       "VALUES(?,'result_custody',?,'released',1000,2000,'orchestration_closed')", (PROJECT_A, run + ':1'))
            db.execute("INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,"
                       "orchestration_handle,binding,assignment_digest,created_at_ms,hard_deadline_at_ms,"
                       "reserved_until_ms,expires_at_ms,state) VALUES(?,?,?,1,?,?,x'00','a',0,0,0,0,'expired')",
                       (PROJECT_A, f'claim-{number}', run, f'{number:064x}', opened['orchestration_handle']))
        self.assertEqual(1, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])
        self.assertEqual(0, holds_report(fixture.store, runtime_digest='a' * 64)['result_custody']['ended_by_update_unrecorded'])
        db.execute("UPDATE project_holds SET release_kind='retention'")
        self.assertEqual(0, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])
        # An adopted legacy context has no outcome association even if its old
        # primary recorded it: the durable admission receipt prevents miscounting.
        db.execute("UPDATE project_holds SET release_kind='orchestration_closed'")
        receipt = {'dispatch_result': {'bridge_run_id': 'claim-1'}}
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)',
            (PROJECT_A, 'legacy-admission', 'host_result_admission', 'host', 100, 1000, 1000,
             canonical_json(receipt), 'f' * 64, 0))
        self.assertEqual(0, holds_report(fixture.store, runtime_digest='b' * 64)['result_custody']['ended_by_update_unrecorded'])

    def test_B7_6_native_owner_scope_allows_new_operation(self):
        from codex_agents.service_host import ProjectServiceHost
        from codex_agents.hosts import OWNER_CLIENT
        host = ProjectServiceHost.__new__(ProjectServiceHost)
        enrolled = SimpleNamespace(role='host', host_client=OWNER_CLIENT)
        host._frontend = SimpleNamespace(context=lambda token: SimpleNamespace(kind='project', enrolled=enrolled))
        authorized = host._authorize_history(command(), object())
        self.assertIsNotNone(authorized)
        self.assertEqual(frozenset({'history.holds_report'}), authorized.allowed_operations)


class HoldsProtocolTests(dr.LeaseBackedFixture):
    def setUp(self):
        super().setUp()
        self.service = service_for(self.store)
        self.addCleanup(self.service.close)

    def response(self, request=None):
        request = request or command()
        return self.service.handle(encode_history_request(request), TOKEN)

    def test_B7_6_new_client_service_operation_and_capacity_shape_unchanged(self):
        client = HistoryClient(self.fixture.directory / 'unused.sock', PROJECT_A, RUNTIME, timeout=2)
        with mock.patch('codex_agents.history_api.exchange_frame', side_effect=lambda endpoint, raw, **opts:
                        self.service.handle(raw, TOKEN)):
            self.assertEqual(report(self.store), client.holds_report())
            self.assertEqual({'results', 'results_in_flight', 'orchestrations', 'cleanup_reservations',
                              'operational_qa', 'qa_check_receipts'}, set(client.capacity_report()))

    def test_B7_6_wrong_project_runtime_and_scope_are_refused(self):
        for request in (command(project=PROJECT_B), command(runtime='b' * 64)):
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                decode_history_response(self.response(request), request)
        request = command()
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            decode_history_response(self.service.handle(encode_history_request(request), None), request)

    def test_B7_6_request_is_closed(self):
        for options in ({'identity': 'capacity'}, {'payload': {'project_id': PROJECT_B}}, {'payload': {'limit': 21}}):
            with self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                command(**options)

    def test_B7_6_nested_missing_extra_types_text_bounds_and_order_refused(self):
        request = command()
        envelope = json.loads(self.response(request))
        self.assertTrue(envelope['ready'])
        base = envelope['result']
        variants = []
        def change(path, value=None, remove=False):
            variant = copy.deepcopy(base)
            target = variant
            for part in path[:-1]:
                target = target[part]
            if remove:
                del target[path[-1]]
            else:
                target[path[-1]] = value
            variants.append(variant)
        change(['private'], 'PRIVATE')
        change(['project_holds', 'lease'], remove=True)
        change(['project_holds', 'lease', 'held'], True)
        change(['project_holds', 'lease', 'past_due'], 0)
        change(['project_holds', 'driver_entry', 'tracking'], 'tracked')
        change(['project_holds', 'result_custody', 'private'], 'PRIVATE')
        change(['result_custody', 'context_bytes'], -1)
        change(['result_custody', 'retained_until_ms'], True)
        change(['result_custody', 'max_live_context_bytes'], 1)
        change(['result_retirement_repairs', 'truncated'], 1)
        change(['result_retirement_repairs', 'entries'], [{'reason': 'PRIVATE', 'action': 'PRIVATE', 'operator_needed': True}])
        from codex_agents.project_result_contexts import RepairReport
        page = RepairReport([{'reason': 'directory'}, {'reason': 'retrying'}])
        with mock.patch.object(self.store, 'result_retirement_repairs', return_value=page):
            entries = report(self.store)['result_retirement_repairs']['entries']
        change(['result_retirement_repairs', 'entries'], [entries[0]] * 21)
        change(['result_retirement_repairs', 'entries'], list(reversed(entries)))
        change(['result_retirement_repairs', 'entries'], [{**entries[0], 'names': ['PRIVATE']}])
        for value in variants:
            with self.subTest(value=value), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json({**envelope, 'result': value}), request)


class HoldsClientStatusTests(unittest.TestCase):
    def test_B7_6_client_uses_project_hint_enrollment_and_no_store_fallback(self):
        endpoint = Path('/tmp/b7a-unused.sock')
        scope = SimpleNamespace(project_id=PROJECT_A, runtime_digest=RUNTIME)
        with mock.patch.object(client_runtime, 'CollectorPaths', return_value='state'), \
                mock.patch.object(client_runtime, 'read_runtime_hint', return_value=(scope, None, endpoint)), \
                mock.patch.object(client_runtime, 'enroll_clients') as enroll, \
                mock.patch.object(HistoryClient, 'holds_report', return_value={'safe': True}) as read:
            self.assertEqual({'safe': True}, client_runtime.holds_report_client('state', 'project', host_client='host'))
            enroll.assert_called_once_with('state', 'project', scope, (endpoint,), host_client='host')
            read.assert_called_once_with()
        with mock.patch.object(client_runtime, 'read_runtime_hint', side_effect=OSError('PRIVATE')):
            self.assertIsNone(client_runtime.holds_report_client('/tmp/absent', 'project', host_client='host'))

    def test_B7_6_learning_status_prints_sections_and_nulls_when_unavailable(self):
        import cli
        safe = {'project_holds': {'lease': {'held': 2}}, 'result_custody': {'live': 3},
                'result_retirement_repairs': {'entries': [], 'truncated': False}}
        for value in (safe, None):
            with mock.patch('sys.argv', ['cli.py', 'learning-status', '--state-root', '/tmp/unused',
                                        '--project-root', '/tmp/unused-project']), \
                    mock.patch.object(cli, '_require_protected_runtime'), \
                    mock.patch.object(cli, '_require_own_roots'), \
                    mock.patch.object(cli, '_start_collector_quietly'), \
                    mock.patch.object(cli, 'status_client', return_value={'ready': True}), \
                    mock.patch.object(cli, 'capacity_report_client', return_value=None), \
                    mock.patch.object(cli, 'holds_report_client', return_value=value), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cli.main())
            printed = json.loads(stdout.getvalue())
            for name in safe:
                self.assertEqual(None if value is None else value[name], printed[name])


class HoldsLimitRegistryTests(unittest.TestCase):
    def test_B7_3_presentation_bounds_registered_with_past_limit_tests(self):
        from tests import test_lifetime_cap_guard as guard
        registry = json.loads(guard.REGISTRY.read_text())['sites']
        sites = {}
        for name in ('holds_report_contracts.py', 'project_holds_report.py'):
            sites.update(guard.cap_sites((guard.RUNTIME / name).read_text(), name))
        self.assertEqual([], guard.problems(sites, registry, guard.test_names()))


if __name__ == '__main__':
    unittest.main()
