"""Inert Claude probe process protocol; no real provider/account admission."""
from copy import copy
import json
import sqlite3
import threading
import time
import unittest
from unittest import mock

from tests import test_bridge as fixtures
from codex_agents import billing_probe_worker as workers, bridge
from codex_agents.learning_contracts import parse_bounded_json_value
from codex_agents.owned_execution import ExecutionCustodyError, OwnedExecutionRegistry


class BillingProbeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def probe(self, frames):
        self.fixture.frames('claude', frames)
        return bridge.claude_billing_probe(fixtures.CLEAN, project_root=self.fixture.project,
                                          untrusted=(self.fixture.project,))

    def test_nonzero_exit_cannot_refresh_clean_billing_even_after_a_success_frame(self):
        (self.fixture.tools/'claude-exit-code').write_text('1')
        infos, reason = self.probe([fixtures.PROBE_INIT, fixtures.FAVOURABLE, fixtures.result_frame('OK')])
        self.assertEqual(1, len(infos))
        self.assertIsNotNone(reason)

    def test_missing_error_or_duplicate_terminal_result_cannot_refresh_clean_billing(self):
        success = fixtures.result_frame('OK')
        error = {**success, 'is_error': True, 'subtype': 'error_during_execution'}
        for terminal in ([], [error], [success, success]):
            with self.subTest(terminal=terminal):
                infos, reason = self.probe([fixtures.PROBE_INIT, fixtures.FAVOURABLE, *terminal])
                self.assertEqual(1, len(infos))
                self.assertIsNotNone(reason)

    def test_tool_use_refuses_and_keeps_prior_restrictions(self):
        tool = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash'}]}}
        infos, reason = self.probe([fixtures.PROBE_INIT, fixtures.FAVOURABLE, tool, fixtures.result_frame('OK')])
        self.assertEqual(1, len(infos))
        self.assertEqual('probe_tool_not_allowed', reason)

    def test_complete_minimal_turn_retains_every_rate_limit_event(self):
        second = {**fixtures.FAVOURABLE, 'rate_limit_info': {
            **fixtures.FAVOURABLE['rate_limit_info'], 'utilization': .3}}
        infos, reason = self.probe([fixtures.PROBE_INIT, fixtures.FAVOURABLE, second,
                                    fixtures.result_frame('OK')])
        self.assertEqual(2, len(infos))
        self.assertIsNone(reason)

    def test_second_init_or_top_level_tool_event_cannot_refresh_billing(self):
        for frame, expected in ((fixtures.PROBE_INIT, 'probe_init_duplicate'),
                                ({'type': 'tool_use', 'name': 'Bash'}, 'probe_tool_not_allowed')):
            with self.subTest(frame=frame['type']):
                infos, reason = self.probe([fixtures.PROBE_INIT, fixtures.FAVOURABLE, frame,
                                            fixtures.result_frame('OK')])
                self.assertEqual(1, len(infos))
                self.assertEqual(expected, reason)


class BillingProbeWorkerTests(unittest.TestCase):
    """Actual inert Popen/tree/watchdog custody; no native or account release claim."""
    def setUp(self):
        self.fixture = fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.identity = self.fixture.ledger.observe_provider(self.fixture.project_id, 'claude',
            project_root=self.fixture.project, environ=fixtures.CLEAN)[0]
        self.registry = OwnedExecutionRegistry()
        self.pool = workers.BillingProbeWorkers(self.registry,
            workers.ProbeWorkerLimits(1, workers.PROBE_SLOT_BYTES))
        self.addCleanup(self.stop)

    def stop(self):
        self.pool.close()
        for slot in self.pool._slots:
            slot.thread.join(6)
            self.assertFalse(slot.thread.is_alive(), 'inert probe worker did not stop')

    def until(self, predicate):
        end = time.monotonic()+8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.005)
        self.fail('inert probe operation did not finish')

    def prepare(self, frames=None):
        self.fixture.frames('claude', frames if frames is not None else
            [fixtures.PROBE_INIT, fixtures.FAVOURABLE, fixtures.result_frame('OK')])
        payload = workers.probe_payload(self.fixture.project_id, 'a'*64, self.fixture.project,
            self.identity, deadline_ns=time.monotonic_ns()+30_000_000_000,
            created_at_ms=time.time_ns()//1_000_000)
        handle = self.pool.prepare(payload)
        self.until(lambda: self.pool.snapshot(handle)['state'] != 'preparing')
        prepared = self.pool.prepared(handle)
        return handle, prepared

    def launch(self, frames=None):
        handle, prepared = self.prepare(frames)
        ticket = self.registry.issue(prepared.key, deadline_ns=prepared.deadline_ns,
                                     run_handle=prepared.run_handle)
        self.pool.bind(handle, ticket)
        self.pool.submit(handle)
        return handle, prepared, ticket, self.pool._jobs[handle].supervisor

    def returned(self, handle):
        self.until(lambda: self.pool.snapshot(handle)['state'] in {'returned', 'unknown'})
        self.assertEqual('returned', self.pool.snapshot(handle)['state'])
        return self.pool.packet(handle)

    def test_real_process_return_requires_original_cleanup_before_local_retirement(self):
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('worker SQL')):
            handle, prepared, ticket, supervisor = self.launch()
            packet = self.returned(handle)
        self.assertTrue(packet.workspace_removed)
        self.assertFalse(prepared.workspace.exists())
        self.assertIsNone(packet.reason_code)
        value = parse_bounded_json_value(packet.body, max_bytes=workers.MAX_PROBE_PACKET_BYTES)
        self.assertEqual(['provider_headroom', 'provider_billing'], [r['record_type'] for r in value['records']])
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.assertTrue(supervisor.snapshot()['worker_returned'])
        proof = self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())
        self.assertEqual(prepared.key, self.registry.cleanup_snapshot(proof)['key'])
        with self.assertRaises(ExecutionCustodyError):
            self.registry.cleanup_snapshot(copy(proof))
        with self.assertRaisesRegex(workers.ProbeWorkerError, 'probe_worker_busy'):
            self.pool.retire(handle)
        self.assertEqual(workers.PROBE_SLOT_BYTES, self.pool._charged)
        # Local registry acknowledgement is not durable account settlement.
        self.registry.acknowledge_release(ticket)
        self.pool.retire(handle)
        self.assertEqual(0, self.pool._charged)
        for invocation in self.fixture.invocations('claude'):
            if invocation['argv'][0] == '-p':
                self.fixture.assert_process_gone(invocation)

    def test_cancel_during_held_process_is_prompt_and_keeps_reservation_until_return(self):
        handle, _, ticket, supervisor = self.launch([fixtures.PROBE_INIT, {'sleep': 30}])
        self.until(lambda: supervisor.snapshot()['state'] == 'active')
        start = time.monotonic()
        supervisor.request_stop()
        self.assertLess(time.monotonic()-start, .2)
        self.assertEqual(workers.PROBE_SLOT_BYTES, self.pool._charged)
        packet = self.returned(handle)
        self.assertEqual('probe_incomplete', packet.reason_code)
        self.assertEqual([], json.loads(packet.body)['records'])
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())

    def test_returned_failed_process_keeps_restrictive_events_and_drops_clean_refresh(self):
        (self.fixture.tools/'claude-exit-code').write_text('1')
        warning = {**fixtures.FAVOURABLE, 'rate_limit_info': {
            **fixtures.FAVOURABLE['rate_limit_info'], 'status': 'allowed_warning', 'utilization': .9}}
        handle, _, _, _ = self.launch([fixtures.PROBE_INIT, fixtures.FAVOURABLE,
                                        warning, fixtures.result_frame('OK')])
        packet = self.returned(handle)
        self.assertEqual('probe_incomplete', packet.reason_code)
        records = json.loads(packet.body)['records']
        self.assertTrue(any(r['record_type'] == 'provider_headroom' and r['status'] == 'allowed_warning'
                            for r in records))
        self.assertFalse(any(r['record_type'] == 'provider_billing' for r in records))

    def test_stream_read_failure_keeps_observed_restriction_after_actual_cleanup(self):
        warning = {**fixtures.FAVOURABLE, 'rate_limit_info': {
            **fixtures.FAVOURABLE['rate_limit_info'], 'status': 'allowed_warning', 'utilization': .9}}
        original_read, original_classify = bridge.os.read, bridge.claude_rate_limit_records
        armed, injected, event_descriptor = False, False, None

        def classify(info, **arguments):
            nonlocal armed
            result = original_classify(info, **arguments)
            if (info == warning['rate_limit_info'] and not injected
                    and threading.current_thread() is self.pool._slots[0].thread):
                armed = True
            return result

        def read(descriptor, *arguments):
            nonlocal armed, injected, event_descriptor
            if (armed and descriptor == event_descriptor
                    and threading.current_thread() is self.pool._slots[0].thread):
                armed, injected = False, True
                raise OSError('PRIVATE-STREAM-FAILURE')
            chunk = original_read(descriptor, *arguments)
            if (b'"rate_limit_event"' in chunk
                    and threading.current_thread() is self.pool._slots[0].thread):
                event_descriptor = descriptor
            return chunk

        with mock.patch.object(bridge, 'claude_rate_limit_records', new=classify), \
                mock.patch.object(bridge.os, 'read', new=read):
            handle, prepared, ticket, supervisor = self.launch([
                fixtures.PROBE_INIT, fixtures.FAVOURABLE, warning,
                {'sleep': .1}, fixtures.result_frame('OK'),
            ])
            packet = self.returned(handle)
        self.assertTrue(injected)
        self.assertTrue(packet.workspace_removed)
        self.assertFalse(prepared.workspace.exists())
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.assertTrue(supervisor.snapshot()['worker_returned'])
        self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())
        self.registry.acknowledge_release(ticket)
        self.pool.retire(handle)
        self.assertEqual(0, self.pool._charged)
        self.assertEqual('probe_incomplete', packet.reason_code)
        value = json.loads(packet.body)
        self.assertTrue(any(r['record_type'] == 'provider_headroom' and r['status'] == 'allowed_warning'
                            for r in value['records']))
        self.assertFalse(any(r['record_type'] == 'provider_billing' for r in value['records']))
        self.assertNotIn('PRIVATE-STREAM-FAILURE', packet.body.decode())

    def test_paid_usage_stops_before_another_frame_and_preserves_its_restriction(self):
        start = time.monotonic()
        handle, _, _, supervisor = self.launch([fixtures.PROBE_INIT, fixtures.OVERAGE,
            {'sleep': 3}, fixtures.FAVOURABLE, fixtures.result_frame('OK')])
        packet = self.returned(handle)
        self.assertEqual('probe_incomplete', packet.reason_code)
        self.assertLess(time.monotonic()-start, 2.5)
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.assertTrue(any(r['record_type'] == 'provider_billing' and r['paid_usage_usable'] is True
                            for r in json.loads(packet.body)['records']))

    def test_provider_rejection_stops_before_another_frame_and_preserves_its_restriction(self):
        rejection = {**fixtures.FAVOURABLE, 'rate_limit_info': {
            **fixtures.FAVOURABLE['rate_limit_info'], 'status': 'rejected'}}
        start = time.monotonic()
        handle, _, _, supervisor = self.launch([fixtures.PROBE_INIT, rejection,
            {'sleep': 3}, fixtures.FAVOURABLE, fixtures.result_frame('OK')])
        packet = self.returned(handle)
        self.assertEqual('probe_incomplete', packet.reason_code)
        self.assertLess(time.monotonic()-start, 2.5)
        self.assertTrue(supervisor.snapshot()['cleanup_confirmed'])
        self.assertTrue(any(r['record_type'] == 'provider_headroom' and r['status'] == 'rejected'
                            for r in json.loads(packet.body)['records']))

    def test_mutated_prepared_key_refuses_before_handoff(self):
        handle, prepared = self.prepare()
        object.__setattr__(prepared.key, 'bridge_run_id', 'brg_'+'b'*32)
        with self.assertRaises(workers.ProbeWorkerError):
            self.pool.prepared(handle)
        self.assertFalse(any(i['argv'][0] == '-p' for i in self.fixture.invocations('claude')))
        self.pool.discard(handle)
        self.until(lambda: self.pool.snapshot(handle)['state'] == 'cleaned')
        self.pool.retire(handle)

    def test_mutated_public_preparation_after_handoff_cannot_change_original_arguments(self):
        handle, prepared = self.prepare()
        ticket = self.registry.issue(prepared.key, deadline_ns=prepared.deadline_ns,
                                     run_handle=prepared.run_handle)
        self.pool.bind(handle, ticket)
        object.__setattr__(prepared, 'arguments', ('/untrusted/replacement',))
        self.pool.submit(handle)
        packet = self.returned(handle)
        self.assertIsNone(packet.reason_code)
        self.assertTrue(any(i['argv'][0] == '-p' for i in self.fixture.invocations('claude')))

    def test_changed_client_before_spawn_stays_unknown_and_charged(self):
        handle, prepared = self.prepare()
        ticket = self.registry.issue(prepared.key, deadline_ns=prepared.deadline_ns,
                                     run_handle=prepared.run_handle)
        self.pool.bind(handle, ticket)
        with mock.patch.object(workers, 'resolve_client', return_value=(None, None)):
            self.pool.submit(handle)
            self.returned(handle)
        supervisor = self.pool._jobs[handle].supervisor
        self.assertFalse(supervisor.snapshot()['cleanup_confirmed'])
        with self.assertRaises(ExecutionCustodyError):
            self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())
        with self.assertRaises(workers.ProbeWorkerError):
            self.pool.retire(handle)
        self.assertEqual(workers.PROBE_SLOT_BYTES, self.pool._charged)

    def test_unknown_tree_tracking_cannot_supply_cleanup(self):
        with mock.patch.object(bridge, 'process_table', return_value=None):
            handle, _, ticket, supervisor = self.launch()
            self.returned(handle)
        self.assertFalse(supervisor.snapshot()['cleanup_confirmed'])
        with self.assertRaises(ExecutionCustodyError):
            self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())
        self.assertEqual(workers.PROBE_SLOT_BYTES, self.pool._charged)

    def test_packet_mutation_is_detected_after_original_return(self):
        handle, _, _, _ = self.launch()
        packet = self.returned(handle)
        object.__setattr__(packet, 'body', b'{"records":[]}')
        with self.assertRaises(workers.ProbeWorkerError):
            self.pool.packet(handle)

    def test_changed_original_execution_key_after_handoff_cannot_complete_worker(self):
        handle, prepared = self.prepare()
        ticket = self.registry.issue(prepared.key, deadline_ns=prepared.deadline_ns,
                                     run_handle=prepared.run_handle)
        self.pool.bind(handle, ticket)
        object.__setattr__(prepared.key, 'bridge_run_id', 'brg_'+'c'*32)
        self.pool.submit(handle)
        self.until(lambda: self.pool.snapshot(handle)['state'] == 'unknown')
        with self.assertRaises(ExecutionCustodyError):
            self.registry.retain_cleanup(ticket, self.pool, handle, consumer=threading.current_thread())
        with self.assertRaises(workers.ProbeWorkerError):
            self.pool.packet(handle)
        self.assertEqual(workers.PROBE_SLOT_BYTES, self.pool._charged)

    def payload(self):
        return workers.probe_payload(self.fixture.project_id, 'a'*64, self.fixture.project, self.identity,
                                     deadline_ns=time.monotonic_ns()+30_000_000_000,
                                     created_at_ms=time.time_ns()//1_000_000)

    def test_H1a_normal_work_is_busy_and_never_kept_or_held(self):
        handle, _ = self.prepare()
        view = self.pool.health.view()
        self.assertEqual((1, 0, False, None), (view['busy'], view['kept'], view['faulted'], view['held_since_ms']))
        self.pool.discard(handle)
        self.until(lambda: self.pool.snapshot(handle)['state'] == 'cleaned')
        self.pool.retire(handle)
        view = self.pool.health.view()
        self.assertEqual((0, 0, None), (view['busy'], view['kept'], view['held_since_ms']))

    def test_H1a_an_unknown_preparation_is_kept_and_held_without_a_fault(self):
        with mock.patch.object(workers, '_prepare', return_value=(None, None, True)):
            handle = self.pool.prepare(self.payload())
            self.until(lambda: self.pool.snapshot(handle)['state'] == 'unknown')
        view = self.pool.health.view()
        self.assertEqual((1, False), (view['kept'], view['faulted']))
        self.assertIs(int, type(view['held_since_ms']))
        self.assertEqual(('probe_preparation_unknown', 'invariant'),
                         (view['last_failure']['cause'], view['last_failure']['type']))

    def test_H1a_a_worker_exception_is_faulted_kept_and_held_with_its_type(self):
        with mock.patch.object(workers, '_prepare', side_effect=RuntimeError('a secret path /x')):
            handle = self.pool.prepare(self.payload())
            self.until(lambda: self.pool.snapshot(handle)['state'] == 'unknown')
        view = self.pool.health.view()
        self.assertEqual((True, 1), (view['faulted'], view['kept']))
        self.assertIs(int, type(view['held_since_ms']))
        self.assertEqual(('probe_worker_failed', 'RuntimeError'),
                         (view['last_failure']['cause'], view['last_failure']['type']))
        self.assertNotIn('secret', repr(view))

    def test_H1a_a_worker_fault_is_published_while_the_registry_lock_is_held(self):
        # Round-5 verifier: the note must not wait for a lock a blocked owner may hold. The worker is
        # inside its preparation when the lock is taken, then fails.
        started, go = threading.Event(), threading.Event()

        def failing(payload):
            started.set()
            go.wait(5)
            raise RuntimeError('fixture')
        with mock.patch.object(workers, '_prepare', side_effect=failing):
            self.pool.prepare(self.payload())
            self.assertTrue(started.wait(5))
            with self.registry._lock:
                go.set()
                self.until(lambda: self.pool.health.view()['faulted'])
                self.assertEqual(1, self.pool.health.view()['kept'])

    def test_C7_a_preparation_failure_caught_inside_the_helper_keeps_its_type(self):
        from codex_agents import bridge
        with mock.patch.object(bridge, 'private_workspace', side_effect=PermissionError('a secret path')):
            handle = self.pool.prepare(self.payload())
            self.until(lambda: self.pool.snapshot(handle)['state'] in {'failed', 'unknown'})
        failure = self.pool.health.view()['last_failure']
        self.assertEqual('PermissionError', failure['type'])
        self.assertIn(failure['cause'], {'probe_preparation_failed', 'probe_preparation_unknown'})
        self.assertNotIn('secret', repr(self.pool.health.view()))

    def test_C6_an_unknown_preparation_is_published_while_the_registry_lock_is_held(self):
        started, go = threading.Event(), threading.Event()

        def unknown(payload):
            started.set()
            go.wait(5)
            return None, None, True
        with mock.patch.object(workers, '_prepare', side_effect=unknown):
            self.pool.prepare(self.payload())
            self.assertTrue(started.wait(5))
            with self.registry._lock:
                go.set()
                self.until(lambda: self.pool.health.view()['kept'] == 1)
                self.assertIs(int, type(self.pool.health.view()['held_since_ms']))

    def test_unbound_preparation_holds_slot_until_actual_discard(self):
        handle, prepared = self.prepare()
        with self.assertRaisesRegex(workers.ProbeWorkerError, 'probe_worker_full'):
            self.pool.prepare(b'{}')
        with self.assertRaisesRegex(workers.ProbeWorkerError, 'probe_worker_busy'):
            self.pool.retire(handle)
        self.pool.discard(handle)
        self.until(lambda: self.pool.snapshot(handle)['state'] == 'cleaned')
        self.assertFalse(prepared.workspace.exists())
        self.pool.retire(handle)
        self.assertEqual(0, self.pool._charged)

    def test_same_client_path_with_changed_binary_digest_refuses_before_preparation(self):
        from pathlib import Path
        client = Path(self.identity['client_path'])
        client.write_bytes(client.read_bytes()+b'\n# changed inert client image\n')
        payload = workers.probe_payload(self.fixture.project_id, 'd'*64, self.fixture.project,
            self.identity, deadline_ns=time.monotonic_ns()+10_000_000_000,
            created_at_ms=time.time_ns()//1_000_000)
        handle = self.pool.prepare(payload)
        self.until(lambda: self.pool.snapshot(handle)['state'] != 'preparing')
        self.assertEqual('failed', self.pool.snapshot(handle)['state'])
        self.assertFalse(any(i['argv'][0] == '-p' for i in self.fixture.invocations('claude')))
        self.pool.retire(handle)
