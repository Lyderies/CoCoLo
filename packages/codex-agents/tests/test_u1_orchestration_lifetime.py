"""U1: runner/core continuation through the authenticated project product protocol."""
from contextlib import contextmanager
import json
import subprocess
import sys
from unittest import mock
import unittest

from test_framework import requirements
import tests.support
from tests.support import ORCHESTRATION_ROOT
from tests import test_project_orchestration as fixtures
from codex_agents.history_contracts import HistoryError
from codex_agents.orchestration_api import OrchestrationLimits
from codex_agents import project_history, project_orchestration_report as report


class OrchestrationLifetimeTests(unittest.TestCase):
    __why_not_scenario__ = "One runner continuation scenario exercises authenticated persistence, boundary continuation and recovery; individual methods isolate its failure boundaries."
    # Reuse fixture behavior, without inheriting/rerunning another module's tests.
    for _name in ('setUp', 'open_history', 'authorize', 'make_service', 'request', 'call', 'opened', 'r6_report'):
        locals()[_name] = getattr(fixtures.ProjectOrchestrationTests, _name)
    target = staticmethod(fixtures.ProjectOrchestrationTests.target)

    def advance(self, snapshot, identity='handoff', cause='session', next_session='session-2'):
        return self.call('rollover', self.target(snapshot, cause=cause, session_id='session-1',
                                               next_session_id=next_session), identity)

    def register(self, snapshot, number):
        self.store.record_registered_run(snapshot['orchestration_handle'], 'hrn_' + f'{number:048x}',
                                         f'run-{number}', 1, instance='supervisor', now_ms=self.now)

    def open_row(self, snapshot, number):
        # Report-only fixture: production opens have sealed bindings; report reads their run identities.
        binding = json.dumps({'orchestration_handle': snapshot['orchestration_handle'], 'run_id': f'run-{number}'})
        self.store._connection.execute('PRAGMA foreign_keys=OFF')
        try:
            self.store._connection.execute('INSERT INTO project_host_opens VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.scope.project_id, 'hrn_' + f'{number:048x}', 1, binding.encode(),
                 'open-' + str(number), 'b' * 64, number + 1, self.now, 0, 'c' * 64))
        finally:
            self.store._connection.execute('PRAGMA foreign_keys=ON')

    def packet(self, snapshot):
        return self.call('packet', {'orchestration_handle': snapshot['orchestration_handle']}, 'packet')

    def assert_one_open(self):
        self.assertEqual(1, self.store._orchestration_owner()._open_count())

    @requirements('O0C-U1-1', 'O0C-U1-3', 'O0C-U1-5')
    def test_registration_boundary_rolls_at_real_512_and_continues_past_it(self):
        self.store._orchestration_limits = OrchestrationLimits(1, 12)
        first = self.opened()
        for number in range(project_history.MAX_REGISTERED_RUNS):
            self.register(first, number)
        self.register(first, 0)  # replay at full registration capacity
        changed = self.advance(first, cause='capacity', next_session='session-1')
        self.assertTrue(changed['rolled_over'])
        second = changed['current']['snapshot']
        self.register(second, 512)
        self.register(first, 0)  # retained old registration still replays after close
        self.assertEqual(512, self.r6_report(first)['admitted'])
        self.assertEqual(1, self.r6_report(second)['admitted'])
        self.assertTrue(self.advance(first, cause='capacity', next_session='session-1')['known'])
        self.assert_one_open()

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_independent_registration_report_open_and_mixed_boundaries(self):
        with mock.patch.object(project_history, 'MAX_REGISTERED_RUNS', 3), mock.patch.object(report, 'MAX_RUNS', 2):
            # Each independent report boundary: two open rows; or disjoint open/registered rows.
            for mode in ('opens', 'mixed', 'registered'):
                with self.subTest(mode=mode):
                    first = self.call('open', identity='open-' + mode)['current']['snapshot']
                    base = {'opens': 10, 'mixed': 20, 'registered': 30}[mode]
                    if mode != 'registered':
                        self.open_row(first, base)
                    if mode == 'opens':
                        self.open_row(first, base + 1)
                    else:
                        self.register(first, base + 1)
                        if mode == 'registered':
                            self.register(first, base)
                    self.assertEqual(2, self.r6_report(first)['admitted'])
                    result = self.advance(first, identity='capacity-' + mode, cause='capacity', next_session='session-1')
                    self.assertTrue(result['rolled_over'])
                    second = result['current']['snapshot']
                    self.register(second, base + 2)
                    self.assertEqual(2, self.r6_report(first)['admitted'])
                    self.assertEqual(1, self.r6_report(second)['admitted'])
                    self.call('close', self.target(second, status='completed', goal_tokens_used=None,
                                                   goal_time_used_seconds=None), 'end-' + mode)

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_overlap_does_not_double_charge_and_attempt_rows_do_count(self):
        with mock.patch.object(project_history, 'MAX_REGISTERED_RUNS', 4), mock.patch.object(report, 'MAX_RUNS', 2):
            first = self.opened()
            self.register(first, 1)
            self.open_row(first, 1)
            result = self.advance(first, cause='capacity', next_session='session-1')
            self.assertFalse(result['rolled_over'])
            self.assertEqual(first, result['current']['snapshot'])
            self.store.record_registered_run(first['orchestration_handle'], 'hrn_' + f'{1:048x}',
                                             'run-1', 2, instance='supervisor', now_ms=self.now)
            result = self.advance(first, identity='attempt-bound', cause='capacity', next_session='session-1')
            self.assertTrue(result['rolled_over'])
            self.assertEqual(1, self.r6_report(first)['admitted'])

    @requirements('O0C-U1-2', 'O0C-U1-3', 'O0C-U1-5')
    def test_events_replay_and_reserved_close_roll_repeatedly_past_limit(self):
        self.store._orchestration_limits = OrchestrationLimits(1, 3)
        current = self.opened()
        for number in range(5):
            first = current
            payload = self.target(first, goal_tokens_used=number, goal_time_used_seconds=number)
            current = self.call('checkpoint', payload, 'usage-' + str(number))['current']['snapshot']
            self.assertTrue(self.call('checkpoint', payload, 'usage-' + str(number))['known'])
            result = self.advance(current, identity='roll-' + str(number), cause='capacity', next_session='session-1')
            self.assertTrue(result['rolled_over'])
            old = result['previous']['snapshot']
            self.assertEqual(('rolled_over', 3), (old['status'], old['sequence']))
            self.assertEqual(3, report.orchestration_report(self.store, old['orchestration_id'])['checkpoint_count'])
            self.assertTrue(self.advance(current, identity='roll-' + str(number), cause='capacity',
                                         next_session='session-1')['known'])
            current = result['current']['snapshot']
            self.assert_one_open()
        self.assertEqual(6, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])

    @requirements('O0C-U1-2', 'O0C-U1-4', 'O0C-U1-5')
    def test_handoff_links_packet_and_state_survive_restart_without_file(self):
        first = self.opened()
        state = {'objective': 'Finish the next unit', 'decisions': ['310'], 'open_units': [], 'next_model': 'routine'}
        first = self.call('checkpoint', self.target(first, goal_tokens_used=200000,
                          goal_time_used_seconds=20, resume_state=state), 'state')['current']['snapshot']
        first = self.call('pause', self.target(first), 'pause')['current']['snapshot']
        self.assertEqual(state, self.packet(first)['resume_state'])
        result = self.advance(first)
        second = result['current']['snapshot']
        self.assertEqual('paused', second['status'])
        handoff = result['handoff']
        self.assertEqual((first['orchestration_handle'], second['orchestration_handle']),
                         (handoff['previous_handle'], handoff['next_handle']))
        self.assertEqual(('session-1', 'session-2'), (handoff['session_id'], handoff['next_session_id']))
        self.service.close()
        self.store = self.open_history()
        self.service = self.make_service()
        packet = self.packet(second)
        self.assertEqual(state, packet['resume_state'])
        self.assertEqual(handoff, packet['handoff'])
        self.assertEqual('rolled_over', report.orchestration_report(self.store, first['orchestration_id'])['status'])
        with mock.patch.object(self.store, '_result_now', side_effect=AssertionError('replay read clock')):
            replay = self.advance(first)
        self.assertTrue(replay['known'])
        self.assertEqual(result['receipt'], replay['receipt'])
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.advance(first, next_session='different')
        self.assert_one_open()

    @requirements('O0C-U1-2', 'O0C-U1-5')
    def test_rollbacks_and_lost_reply_never_leave_zero_or_two_open(self):
        first = self.opened()
        original = self.store._transaction
        for after_commit in (False, True):
            @contextmanager
            def fault():
                with original():
                    yield
                    if not after_commit:
                        raise OSError('interrupted before commit')
                raise OSError('lost reply')
            with mock.patch.object(self.store, '_transaction', fault), self.assertRaises(HistoryError):
                self.advance(first)
            self.assert_one_open()
            if not after_commit:
                self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
        self.assertTrue(self.advance(first)['known'])
        self.assert_one_open()

    @requirements('O0C-U1-2', 'O0C-U1-5')
    def test_process_exit_between_close_and_open_rolls_back_then_replays(self):
        first = self.opened()
        self.service.close()
        # A real abrupt process exit inside the writer transaction (no Python cleanup).
        program = '''
import json,os,sys
from pathlib import Path
import tests.support
from codex_agents.project_history import ProjectHistory
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.orchestration_api import OrchestrationRequest,OrchestrationScope,OrchestrationLimits
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
store=ProjectHistory(Path(sys.argv[1]),sys.argv[2],result_key=b'j'*32,
 result_capacity=ResultCapacity(8,4*MAX_RESULT_BODY_BYTES,60000,120000),
 result_clock_ms=lambda:1000,orchestration_limits=OrchestrationLimits(4,12))
original=OrchestrationOwner.apply
def crash(owner,request,**kwargs):
 if request.operation=='orchestration.open': os._exit(73)
 return original(owner,request,**kwargs)
OrchestrationOwner.apply=crash
req=OrchestrationRequest(1,'orchestration.rollover','handoff',OrchestrationScope(**json.loads(sys.argv[3])),json.loads(sys.argv[4]))
original(OrchestrationOwner(store),req,client_version='codex-host-v1',before_commit=lambda:None)
'''
        payload = self.target(first, cause='session', session_id='session-1', next_session_id='session-2')
        completed = subprocess.run([sys.executable, '-B', '-c', program, str(self.directory), self.scope.project_id,
                                    json.dumps(self.scope.as_data()), json.dumps(payload)], capture_output=True,
                                   cwd=ORCHESTRATION_ROOT.parents[2], timeout=10)
        self.assertEqual(73, completed.returncode, completed.stderr.decode())
        self.store = self.open_history()
        self.service = self.make_service()
        self.assert_one_open()
        self.assertEqual('active', self.call('status', {'orchestration_handle': first['orchestration_handle']})['current']['snapshot']['status'])
        self.assertFalse(self.advance(first)['known'])
        self.assertTrue(self.advance(first)['known'])
        self.assert_one_open()

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_pending_work_returns_only_internal_wait_and_retries_after_drain(self):
        from codex_agents.orchestration_store import OrchestrationOwner
        first = self.opened()
        with mock.patch.object(OrchestrationOwner, '_unfinished', return_value=True):
            waiting = self.advance(first)
        self.assertFalse(waiting['rolled_over'])
        self.assertEqual('Waiting for active work.', waiting['note'])
        self.assertEqual(first, waiting['current']['snapshot'])
        self.assertTrue(self.advance(first)['rolled_over'])
        self.assert_one_open()

    @requirements('O0C-U1-4', 'O0C-U1-5')
    def test_resume_state_exact_byte_limit_and_one_past_preserves_packet(self):
        from codex_agents.orchestration_api import MAX_RESUME_STATE_BYTES
        first = self.opened()
        state = {'s': 'x' * (MAX_RESUME_STATE_BYTES - len(b'{"s":""}'))}
        first = self.call('checkpoint', self.target(first, goal_tokens_used=0, goal_time_used_seconds=0,
                                                  resume_state=state), 'exact')['current']['snapshot']
        self.assertEqual(state, self.packet(first)['resume_state'])
        with self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
            self.call('checkpoint', self.target(first, goal_tokens_used=0, goal_time_used_seconds=0,
                                               resume_state={'s': state['s'] + 'x'}), 'past')
        self.assertEqual(state, self.packet(first)['resume_state'])
        self.assertTrue(self.advance(first)['rolled_over'])

    @requirements('O0C-U1-2', 'O0C-U1-5')
    def test_revocation_and_new_identity_cannot_fork_a_closed_predecessor(self):
        first = self.opened()
        self.authorizations = 0
        self.revoke = True
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.advance(first)
        self.assert_one_open()
        self.revoke = False
        self.advance(first)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state|history_conflict'):
            self.advance(first, identity='fork')
        self.assert_one_open()

    @requirements('O0C-U1-2', 'O0C-U1-3', 'O0C-U1-5')
    def test_exact_enrolled_runner_call_uses_one_exchange_and_reconciles_lost_reply(self):
        from types import SimpleNamespace
        from codex_agents import client_runtime, orchestration_api
        from codex_agents.service_transport import ServiceTransportError
        first = self.opened()
        options = dict(host_client='codex-host-v1', orchestration_handle=first['orchestration_handle'],
                       expected_sequence=first['sequence'], request_id='runner-handoff', session_id='session-1',
                       next_session_id='session-2', cause='session')
        def lost(endpoint, raw, **limits):
            self.assertEqual(16384, limits['response_limit'])
            self.service.handle(raw, self.token)
            raise ServiceTransportError('service_unavailable')
        with mock.patch.object(client_runtime, 'CollectorPaths'), \
             mock.patch.object(client_runtime, 'read_runtime_hint', return_value=(
                 SimpleNamespace(**self.scope.as_data()), self.directory / 'control.sock', self.directory / 'history.sock')), \
             mock.patch.object(client_runtime, 'enroll_clients') as enroll:
            with mock.patch.object(orchestration_api, 'exchange_frame', side_effect=lost) as exchange:
                with self.assertRaises(client_runtime.RuntimeOrchestrationError):
                    client_runtime.orchestration_lifetime_client(self.directory, self.directory, **options)
                self.assertEqual(1, exchange.call_count)
            with mock.patch.object(orchestration_api, 'exchange_frame', side_effect=lambda endpoint, raw, **kw:
                                   self.service.handle(raw, self.token)) as exchange:
                replay = client_runtime.orchestration_lifetime_client(self.directory, self.directory, **options)
                self.assertTrue(replay['known'])
                self.assertTrue(replay['rolled_over'])
                self.assertEqual(1, exchange.call_count)
            self.assertEqual(2, enroll.call_count)
        self.assert_one_open()

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_repeated_registration_rollovers_past_smaller_independent_registration_cap(self):
        with mock.patch.object(project_history, 'MAX_REGISTERED_RUNS', 2), mock.patch.object(report, 'MAX_RUNS', 4):
            current = self.opened()
            for number in range(7):
                result = self.advance(current, identity='before-' + str(number), cause='capacity', next_session='session-1')
                if number and number % 2 == 0:
                    self.assertTrue(result['rolled_over'])
                    self.assertEqual(2, self.r6_report(current)['admitted'])
                else:
                    self.assertFalse(result['rolled_over'])
                current = result['current']['snapshot']
                self.register(current, number)
            self.assertEqual(4, self.store._orchestration_owner().open_counts()['closed'] + 1)

    @requirements('O0C-U1-4', 'O0C-U1-5')
    def test_restart_without_handoff_generates_packet_and_rejects_corrupt_state(self):
        first = self.opened()
        state = {'objective': 'Resume this recorded objective', 'head': 'abc123', 'last_ci': 'passed'}
        first = self.call('checkpoint', self.target(first, goal_tokens_used=10, goal_time_used_seconds=1,
                                                  resume_state=state), 'state')['current']['snapshot']
        self.service.close()
        self.store = self.open_history()
        self.service = self.make_service()
        packet = self.packet(first)
        self.assertEqual(state, packet['resume_state'])
        self.assertIsNone(packet['handoff'])
        row = self.store._connection.execute('SELECT content FROM project_orchestration_checkpoints WHERE sequence=2').fetchone()
        data = json.loads(row[0])
        data['resume_state']['objective'] = 'changed behind the seal'
        self.store._connection.execute('UPDATE project_orchestration_checkpoints SET content=? WHERE sequence=2',
                                        (json.dumps(data, sort_keys=True, separators=(',', ':')).encode(),))
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.packet(first)

    @requirements('O0C-U1-2', 'O0C-U1-4', 'O0C-U1-5')
    def test_wire_rejects_changed_link_wrong_scope_and_false_rollover_receipt(self):
        from copy import deepcopy
        from codex_agents.orchestration_api import encode_orchestration_request, decode_orchestration_response
        first = self.opened()
        request = self.request('rollover', self.target(first, cause='session', session_id='one', next_session_id='two'), 'wire')
        raw = self.service.handle(encode_orchestration_request(request), self.token)
        valid = json.loads(raw)
        decode_orchestration_response(raw, request)
        for field, value in (('next_handle', first['orchestration_handle']), ('request_digest', 'f' * 64),
                             ('next_session_id', 'foreign')):
            altered = deepcopy(valid)
            altered['result']['handoff'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_orchestration_response(json.dumps(altered).encode(), request)
        altered = deepcopy(valid)
        altered['result']['current']['snapshot']['scope']['primary_session_id'] = 'foreign'
        with self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
            decode_orchestration_response(json.dumps(altered).encode(), request)
        altered = deepcopy(valid)
        altered['result']['rolled_over'] = False
        with self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
            decode_orchestration_response(json.dumps(altered).encode(), request)

    @requirements('O0C-U1-2', 'O0C-U1-5')
    def test_runner_requires_durable_explicit_identity_before_enrollment(self):
        from codex_agents import client_runtime
        with mock.patch.object(client_runtime, 'enroll_clients') as enroll, \
             mock.patch.object(client_runtime, 'read_runtime_hint', side_effect=AssertionError('invalid identity read runtime')):
            for identity in (None, '', 'not a valid identity'):
                with self.subTest(identity=identity), self.assertRaises(client_runtime.RuntimeOrchestrationError):
                    client_runtime.orchestration_lifetime_client(self.directory, self.directory,
                        host_client='codex-host-v1', orchestration_handle='orc_' + 'a' * 48,
                        expected_sequence=1, request_id=identity, session_id='one', next_session_id='two', cause='session')
            enroll.assert_not_called()

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_unchanged_capacity_reply_cannot_substitute_a_new_sequence_or_closed_state(self):
        from copy import deepcopy
        from codex_agents.orchestration_api import encode_orchestration_request, decode_orchestration_response
        first = self.opened()
        first = self.call('checkpoint', self.target(first, goal_tokens_used=0, goal_time_used_seconds=0), 'usage')['current']['snapshot']
        request = self.request('rollover', self.target(first, cause='capacity', session_id='one', next_session_id='one'), 'unchanged')
        valid = json.loads(self.service.handle(encode_orchestration_request(request), self.token))
        self.assertFalse(valid['result']['rolled_over'])
        for mutation in ('sequence', 'closed'):
            altered = deepcopy(valid)
            snapshot = altered['result']['current']['snapshot']
            if mutation == 'sequence':
                snapshot['sequence'] += 1
            else:
                snapshot['status'] = 'completed'
                snapshot['ended_at_ms'] = snapshot['observed_at_ms']
            with self.subTest(mutation=mutation), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_orchestration_response(json.dumps(altered).encode(), request)

    @requirements('O0C-U1-2', 'O0C-U1-4', 'O0C-U1-5')
    def test_paused_session_and_capacity_handoff_wait_for_owner_resume(self):
        from dataclasses import replace
        from types import SimpleNamespace
        for cause in ('session', 'capacity'):
            with self.subTest(cause=cause):
                # Open + checkpoint + pause consumes all ordinary event slots.
                self.store._orchestration_limits = OrchestrationLimits(1, 4)
                first = self.call('open', identity='open-' + cause)['current']['snapshot']
                state = {'pause_reason': 'owner reviewing results', 'inflight': []}
                first = self.call('checkpoint', self.target(first, goal_tokens_used=50,
                    goal_time_used_seconds=2, resume_state=state), 'state-' + cause)['current']['snapshot']
                first = self.call('pause', self.target(first), 'pause-' + cause)['current']['snapshot']
                result = self.advance(first, identity='handoff-' + cause, cause=cause,
                                      next_session='session-2' if cause == 'session' else 'session-1')
                second = result['current']['snapshot']
                self.assertEqual('paused', second['status'])
                self.assertEqual(first['scope'], second['scope'])
                # Reopen simulates a new provider session, without any file or instruction.
                self.service.close()
                self.store = self.open_history()
                self.service = self.make_service()
                self.assertEqual(state, self.packet(second)['resume_state'])
                binding = SimpleNamespace(**second['scope'], orchestration_handle=second['orchestration_handle'])
                with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
                    self.store._orchestration_owner().active_for(binding)
                with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                    self.call('resume', self.target(second), 'foreign-' + cause,
                              scope=replace(self.scope, primary_session_id='foreign'))
                self.assertEqual('paused', self.packet(second)['current']['snapshot']['status'])
                resumed = self.call('resume', self.target(second), 'resume-' + cause)['current']['snapshot']
                self.assertEqual('active', resumed['status'])
                self.store._orchestration_owner().active_for(binding)
                replay = self.advance(first, identity='handoff-' + cause, cause=cause,
                                      next_session='session-2' if cause == 'session' else 'session-1')
                self.assertTrue(replay['known'])
                self.assertEqual('paused', replay['receipt']['snapshot']['status'])
                self.assertEqual('active', replay['current']['snapshot']['status'])
                self.call('close', self.target(resumed, status='completed', goal_tokens_used=None,
                                              goal_time_used_seconds=None), 'end-' + cause)

    @requirements('O0C-U1-2', 'O0C-U1-4', 'O0C-U1-5')
    def test_goal_usage_totals_continue_across_repeated_rollover_and_restart(self):
        current = self.opened()
        unknown = self.advance(current, identity='unknown')
        current = unknown['current']['snapshot']
        self.assertIsNone(current['goal_tokens_used'])
        self.assertIsNone(current['goal_time_used_seconds'])
        for number in range(1, 4):
            tokens, seconds = number * 200000, number * 20
            current = self.call('checkpoint', self.target(current, goal_tokens_used=tokens,
                goal_time_used_seconds=seconds), 'usage-' + str(number))['current']['snapshot']
            result = self.advance(current, identity='accounting-' + str(number))
            current = result['current']['snapshot']
            self.assertEqual((tokens, seconds), (current['goal_tokens_used'], current['goal_time_used_seconds']))
            for snapshot in (result['previous']['snapshot'], current):
                usage = report.orchestration_report(self.store, snapshot['orchestration_id'])['usage']
                self.assertEqual((tokens, seconds), (usage['goal_total_tokens'], usage['goal_time_used_seconds']))
            with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
                self.call('checkpoint', self.target(current, goal_tokens_used=tokens - 1,
                          goal_time_used_seconds=seconds), 'reset-' + str(number))
        self.service.close()
        self.store = self.open_history()
        self.service = self.make_service()
        self.assertEqual(current, self.packet(current)['current']['snapshot'])

    @requirements('O0C-U1-2', 'O0C-U1-5')
    def test_rollover_report_distinguishes_continuation_and_keeps_all_rows(self):
        first = self.opened()
        self.register(first, 1)
        self.register(first, 2)
        self.store.record_run_failed_before_open('hrn_' + f'{2:048x}', 1, 'run_failed', now_ms=self.now)
        self.open_row(first, 1)
        first = self.call('checkpoint', self.target(first, goal_tokens_used=123,
                          goal_time_used_seconds=7), 'usage')['current']['snapshot']
        result = self.advance(first)
        previous = report.orchestration_report(self.store, first['orchestration_id'])
        self.assertEqual('rolled_over', result['previous']['snapshot']['status'])
        self.assertEqual('rolled_over', previous['status'])
        self.assertEqual(3, previous['checkpoint_count'])
        self.assertEqual(3, len(previous['checkpoints']))
        self.assertEqual((2, 1, 1), (previous['runs']['admitted'], previous['runs']['opened'],
                                  previous['runs']['failed_before_open']))
        self.assertEqual(123, previous['usage']['goal_total_tokens'])

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_capacity_sql_work_is_flat_as_unrelated_host_opens_grow(self):
        from codex_agents.orchestration_lifetime import _capacity_due
        current = self.opened()
        self.register(current, 1)
        self.open_row(current, 1)  # overlap and one other open exercise both queries
        self.open_row(current, 2)
        unrelated = {**current, 'orchestration_handle': 'orc_' + 'f' * 48}
        db = self.store._connection
        def work():
            steps = 0
            def tick():
                nonlocal steps
                steps += 1
                return 0
            db.set_progress_handler(tick, 1)
            try:
                self.assertFalse(_capacity_due(self.store._orchestration_owner(), current))
            finally:
                db.set_progress_handler(None, 0)
            return steps
        before = work()
        for number in range(10, 1010):
            self.open_row(unrelated, number)
        after = work()
        self.assertLessEqual(after, before + 20, (before, after))

    @requirements('O0C-U1-3', 'O0C-U1-5')
    def test_host_open_index_is_additive_recreated_on_reopen_without_version_change(self):
        db = self.store._connection
        version = db.execute('PRAGMA user_version').fetchone()[0]
        def index_names():
            return {row[1] for row in self.store._connection.execute('PRAGMA index_list(project_host_opens)')}
        self.assertIn('project_host_opens_orchestration', index_names())
        db.execute('DROP INDEX project_host_opens_orchestration')
        self.service.close()
        self.store.close()  # No service call yet: the lazy service does not own this fixture store.
        self.store = self.open_history()
        self.service = self.make_service()
        self.assertIn('project_host_opens_orchestration', index_names())
        self.assertEqual(version, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
        self.service.close()
        self.store.close()
        self.store = self.open_history()  # already indexed layout is also accepted
        self.service = self.make_service()
        self.assertEqual(version, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
