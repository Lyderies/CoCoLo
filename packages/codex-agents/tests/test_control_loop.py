from __future__ import annotations

import socket
import threading
import time
import unittest
from dataclasses import asdict, replace
from unittest import mock

import tests.support  # noqa: F401
from tests.test_liveness_clock import FakeClock, SECOND
from tests.test_project_monitor import binding, limits, scope

from codex_agents.control_api import (
    MAX_CONTROL_FRAME_BYTES, ControlError, ControlScope, control_request,
    decode_control_response, encode_control_request,
)
from codex_agents.control_loop import ControlLoop, ControlLoopLimits
from codex_agents.control_service import ControlAuthorization, ControlService
from codex_agents.project_monitor import ProjectMonitor
from codex_agents.service_protocol import ServiceTarget, service_request_digest

CONTEXT = object()  # Fixture-only adapter input, never native caller proof.


class ControlLoopTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.monitor = ProjectMonitor(scope(), limits(), max_tracked_attempts=4, clock_ns=self.clock)
        self.monitor.register(binding(), hard_deadline_ns=100 * SECOND)
        self.scope = ControlScope(**asdict(scope()))
        self.service = ControlService(self.monitor, authorizer=self.authorize, clock_ns=self.clock)
        self.addCleanup(self.service.close)
        self.limits = ControlLoopLimits(8, 1, 10 * SECOND, 1_000_000)
        self.loop = ControlLoop(self.monitor, self.service, self.limits)
        self.addCleanup(self.loop.close)

    def authorize(self, request, context):
        if context is CONTEXT:
            return ControlAuthorization(self.scope, request.target, service_request_digest(request),
                                        frozenset({"project.status", "project.cancel"}))

    def request(self, operation="project.status"):
        return control_request(self.scope, operation,
                               ServiceTarget(binding().orchestration_handle, binding().run_handle, 1))

    def pair(self, *, context=CONTEXT, loop=None):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        (loop or self.loop).attach(server, context)
        client.setblocking(False)
        return server, client

    def send(self, client, request=None):
        client.sendall(encode_control_request(request or self.request()) + b"\n")

    def receive(self, client, request=None):
        incoming = bytearray()
        for _ in range(64):
            self.loop.run_once()
            try:
                part = client.recv(MAX_CONTROL_FRAME_BYTES)
            except BlockingIOError:
                continue
            incoming.extend(part)
            if b"\n" in incoming:
                self.assertTrue(incoming.endswith(b"\n"))
                self.assertLessEqual(len(incoming), MAX_CONTROL_FRAME_BYTES)
                return decode_control_response(bytes(incoming[:-1]), request or self.request())
            if not part:
                self.fail("peer closed without a response")
        self.fail("response did not finish within bounded fixture cycles")

    def test_partial_sender_does_not_delay_another_cancel_or_local_silence(self):
        _, slow = self.pair()
        slow.send(b'{"operation":')
        _, active = self.pair()
        cancel = self.request("project.cancel")
        self.send(active, cancel)
        view = self.receive(active, cancel)
        self.assertEqual("stop_requested", view.result["cancellation_state"])
        self.assertFalse(view.result["terminal_observed"])
        self.assertFalse(view.result["cleanup_confirmed"])
        self.monitor.register(binding(2), hard_deadline_ns=100 * SECOND)
        self.clock.at(18)
        self.loop.run_once()
        self.assertEqual("startup_silence", self.monitor._attempts[(binding(2).run_handle, 1)].stop_reason)
        stopped = self.monitor.status(binding(2))
        self.assertEqual("startup_silence", stopped.stop_reason)
        self.assertFalse(stopped.terminal_observed)
        self.assertFalse(stopped.cleanup_confirmed)

    def test_fixed_deadline_is_not_refreshed_by_partial_input(self):
        server, client = self.pair()
        client.send(b"{")
        self.loop.run_once()
        self.clock.at(9)
        client.send(b'"')
        self.loop.run_once()
        self.clock.at(10)
        self.assertEqual(0, self.loop.run_once())
        self.assertEqual(-1, server.fileno())

    def test_deadline_is_rechecked_after_readiness_before_routing(self):
        server, client = self.pair()
        self.send(client)
        original = self.loop._step
        def advance(connection, events):
            self.clock.at(10)
            return original(connection, events)
        with mock.patch.object(self.loop, "_step", side_effect=advance), mock.patch.object(
            self.service, "handle", side_effect=AssertionError("expired request routed")
        ):
            self.assertEqual(0, self.loop.run_once())
        self.assertEqual(-1, server.fileno())

    def test_expired_cancel_reply_does_not_erase_an_already_latched_stop(self):
        def authorize(request, context):
            self.clock.at(10)
            return self.authorize(request, context)
        self.service._authorizer = authorize
        server, client = self.pair()
        self.send(client, self.request("project.cancel"))
        self.assertEqual(0, self.loop.run_once())
        self.assertEqual(-1, server.fileno())
        self.assertEqual(b"", client.recv(MAX_CONTROL_FRAME_BYTES))
        self.assertEqual("cancel_requested", self.monitor._attempts[(binding().run_handle, 1)].stop_reason)

    def test_all_ready_peers_make_progress_with_one_step_per_cycle(self):
        clients = [self.pair()[1] for _ in range(8)]
        for client in clients:
            self.send(client)
        for _ in range(16):
            self.loop.run_once()
        self.assertEqual(0, self.loop.run_once())
        for client in clients:
            raw = client.recv(MAX_CONTROL_FRAME_BYTES)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual("starting", decode_control_response(raw[:-1], self.request()).result["health"])

    def test_reply_backpressure_does_not_delay_cancel_or_timers(self):
        server, blocked = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(blocked.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        server.setblocking(False)
        # Fixture fault injection: fill the outbound kernel buffer first.
        for _ in range(256):
            try:
                server.send(b"x" * 1024)
            except BlockingIOError:
                break
        else:
            self.fail("bounded fixture could not create backpressure")
        self.loop.attach(server, CONTEXT)
        blocked.sendall(encode_control_request(self.request()) + b"\n")
        self.loop.run_once()
        self.assertIsNotNone(self.loop._peers[server].outgoing)
        _, active = self.pair()
        started = time.monotonic()
        cancel = self.request("project.cancel")
        self.send(active, cancel)
        self.assertEqual("stop_requested", self.receive(active, cancel).result["cancellation_state"])
        self.assertLess(time.monotonic() - started, 2)
        self.clock.at(10)
        self.assertEqual(0, self.loop.run_once())
        self.assertEqual(-1, server.fileno())

    def test_oversized_and_pipelined_input_closes_without_invoking_handler(self):
        cases = (b"x" * MAX_CONTROL_FRAME_BYTES, encode_control_request(self.request()) + b"\n{}\n")
        for body in cases:
            server, client = self.pair()
            with mock.patch.object(self.service, "handle", side_effect=AssertionError("must not route")):
                sent = 0
                for _ in range(32):
                    try:
                        sent += client.send(body[sent:])
                    except BlockingIOError:
                        pass
                    self.loop.run_once()
                    if sent == len(body):
                        break
                self.assertEqual(len(body), sent)
            self.assertEqual(-1, server.fileno())

    def test_valid_ready_request_flood_cannot_starve_an_unrequested_attempt_timer(self):
        self.loop.close()
        self.loop = ControlLoop(self.monitor, self.service, replace(self.limits, connection_timeout_ns=60 * SECOND))
        self.addCleanup(self.loop.close)
        self.monitor.register(binding(2), hard_deadline_ns=100 * SECOND)
        clients = [self.pair()[1] for _ in range(8)]
        for client in clients:
            self.send(client)
        self.clock.at(18)
        self.loop.run_once()
        self.assertEqual(8, len(self.loop._peers))
        # Read the latched decision directly: a status call would itself poll.
        self.assertEqual("startup_silence", self.monitor._attempts[(binding(2).run_handle, 1)].stop_reason)

    def test_closed_unknown_or_unauthenticated_peer_never_gets_authority(self):
        server, client = self.pair(context={"role": "primary"})
        self.send(client)
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.receive(client)
        self.assertEqual(-1, server.fileno())
        server, client = self.pair()
        client.close()
        self.assertEqual(0, self.loop.run_once())
        self.assertEqual(-1, server.fileno())

    def test_full_capacity_refusal_leaves_socket_with_caller(self):
        for _ in range(8):
            self.pair()
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.loop.attach(server, CONTEXT)
        self.assertTrue(server.getblocking())
        self.assertGreaterEqual(server.fileno(), 0)

    def test_clock_failure_closes_peers_and_latches_monitor_stop(self):
        server, _ = self.pair()
        self.clock.value = ValueError("PRIVATE-CLOCK")
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.loop.run_once()
        self.assertEqual(-1, server.fileno())
        self.clock.at(1)
        self.assertEqual("monitor_unavailable", self.monitor.status(binding()).stop_reason)
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.loop.run_once()

    def test_interrupted_clock_closes_peers_preserving_interrupt_and_stop(self):
        server, _ = self.pair()
        with mock.patch.object(self.monitor, "_clock", side_effect=[0, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.loop.run_once()
        self.assertEqual(-1, server.fileno())
        self.clock.at(1)
        self.assertEqual("monitor_unavailable", self.monitor.status(binding()).stop_reason)

    def test_reentrant_authorization_cannot_enter_or_close_loop(self):
        def authorize(request, context):
            for operation in (self.loop.run_once, self.loop.close):
                with self.assertRaisesRegex(ControlError, "control_unavailable"):
                    operation()
            return self.authorize(request, context)
        self.service._authorizer = authorize
        _, client = self.pair()
        self.send(client)
        self.assertEqual("starting", self.receive(client).result["health"])

    def test_foreign_process_or_thread_refuses_before_socket_cleanup(self):
        server, _ = self.pair()
        with mock.patch("codex_agents.control_loop.os.getpid", return_value=self.loop._pid + 1):
            for operation in (self.loop.run_once, self.loop.close):
                with self.assertRaisesRegex(ControlError, "control_wrong_owner"):
                    operation()
        failures = []
        def foreign():
            try:
                self.loop.close()
            except ControlError as error:
                failures.append(error.reason_code)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["control_wrong_owner"], failures)
        self.assertGreaterEqual(server.fileno(), 0)
        self.loop.close()
        self.loop.close()
        self.assertEqual("starting", self.monitor.status(binding()).health)

    def test_limits_and_connection_type_are_strict_and_explicit(self):
        for field, value in (("max_connections", True), ("max_connections", 257), ("steps_per_cycle", 9),
                              ("connection_timeout_ns", 0), ("max_wait_ns", SECOND + 1)):
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "control_invalid_input"):
                replace(self.limits, **{field: value})
        with self.assertRaisesRegex(ControlError, "control_invalid_input"):
            self.loop.attach(3, CONTEXT)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        with self.assertRaisesRegex(ControlError, "control_invalid_input"):
            self.loop.attach(listener, CONTEXT)

    def test_equality_overriding_foreign_thread_cannot_operate_or_close(self):
        server, _ = self.pair()
        extra, client = socket.socketpair()
        self.addCleanup(extra.close)
        self.addCleanup(client.close)
        outcomes = []
        loop = self.loop
        class EqualThread(threading.Thread):
            __hash__ = object.__hash__
            def __eq__(self, other):
                return True
            def run(self):
                for operation in (lambda: loop.attach(extra, CONTEXT), loop.run_once, loop.close):
                    try:
                        operation()
                    except ControlError as error:
                        outcomes.append(error.reason_code)
                    except Exception as error:
                        outcomes.append(type(error).__name__)
                    else:
                        outcomes.append("allowed")
        worker = EqualThread()
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["control_wrong_owner"] * 3, outcomes)
        self.assertGreaterEqual(server.fileno(), 0)
        self.assertTrue(extra.getblocking())

    def test_constructor_checks_service_thread_identity_without_equality(self):
        class EqualThread(threading.Thread):
            __hash__ = object.__hash__
            def __eq__(self, other):
                return True
        with mock.patch.object(self.service, "_thread", EqualThread()):
            with self.assertRaisesRegex(ControlError, "control_wrong_owner"):
                candidate = ControlLoop(self.monitor, self.service, self.limits)
                self.addCleanup(candidate.close)

    def test_attach_clock_callback_cannot_reenter_public_operations(self):
        server, _ = self.pair()
        added, client = socket.socketpair()
        nested, nested_client = socket.socketpair()
        for connection in (added, client, nested, nested_client):
            self.addCleanup(connection.close)
        outcomes = []
        def clock():
            for operation in (self.loop.close, self.loop.run_once, lambda: self.loop.attach(nested, CONTEXT)):
                try:
                    operation()
                except ControlError as error:
                    outcomes.append(error.reason_code)
                else:
                    outcomes.append("allowed")
            return 0
        with mock.patch.object(self.monitor, "_clock", clock):
            self.loop.attach(added, CONTEXT)
        self.assertEqual(["control_unavailable"] * 3, outcomes)
        self.assertGreaterEqual(server.fileno(), 0)
        self.assertTrue(nested.getblocking())
        self.assertFalse(self.monitor._clock_failed)
        self.assertEqual(2, len(self.loop._peers))

    def test_context_finalizer_cannot_reenter_close(self):
        loop = self.loop
        outcomes = []
        class Finalizer:
            def __del__(self):
                try:
                    loop.close()
                except ControlError as error:
                    outcomes.append(error.reason_code)
        first, _ = self.pair(context=Finalizer())
        second, _ = self.pair()
        loop.close()
        self.assertEqual(["control_unavailable"], outcomes)
        self.assertEqual((-1, -1), (first.fileno(), second.fileno()))

    def test_failed_attach_preserves_timeout_and_refuses_an_aliased_descriptor(self):
        for timeout in (None, 0, 0.25):
            server, client = socket.socketpair()
            self.addCleanup(server.close)
            self.addCleanup(client.close)
            server.settimeout(timeout)
            with self.subTest(timeout=timeout), mock.patch.object(
                self.loop._selector, "register", side_effect=OSError("synthetic registration failure")
            ):
                with self.assertRaisesRegex(ControlError, "control_invalid_input"):
                    self.loop.attach(server, CONTEXT)
                self.assertEqual(timeout, server.gettimeout())
                self.assertEqual(0, len(self.loop._peers))
                self.assertFalse(self.loop._running)
        original, _ = self.pair()
        alias = socket.socket(fileno=original.fileno())
        try:
            with self.assertRaisesRegex(ControlError, "control_invalid_input"):
                self.loop.attach(alias, CONTEXT)
            self.assertIsNone(alias.gettimeout())
            self.assertGreaterEqual(original.fileno(), 0)
        finally:
            alias.detach()  # The original loop owns this fd; do not double-close.

    def test_partial_registration_failure_rolls_back_without_touching_existing_peer(self):
        original, _ = self.pair()
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        server.settimeout(0.25)
        register = self.loop._selector.register
        def partial(connection, events):
            register(connection, events)
            raise OSError("synthetic failure after registration")
        with mock.patch.object(self.loop._selector, "register", side_effect=partial):
            with self.assertRaisesRegex(ControlError, "control_invalid_input"):
                self.loop.attach(server, CONTEXT)
        self.assertEqual(0.25, server.gettimeout())
        self.assertEqual(1, len(self.loop._selector.get_map()))
        self.assertGreaterEqual(original.fileno(), 0)
        self.loop.attach(server, CONTEXT)
        client.setblocking(False)
        self.send(client)
        self.assertEqual("starting", self.receive(client).result["health"])

    def test_interrupted_registration_restores_caller_state_and_preserves_interrupt(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        with mock.patch.object(self.loop._selector, "register", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.loop.attach(server, CONTEXT)
        self.assertIsNone(server.gettimeout())
        self.assertGreaterEqual(server.fileno(), 0)
        self.assertEqual(0, len(self.loop._peers))
        self.assertFalse(self.loop._running)

    def test_attach_clock_failure_closes_old_peers_without_taking_new_socket(self):
        original, _ = self.pair()
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        self.clock.value = ValueError("PRIVATE-CLOCK")
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.loop.attach(server, CONTEXT)
        self.assertEqual(-1, original.fileno())
        self.assertIsNone(server.gettimeout())
        self.assertGreaterEqual(server.fileno(), 0)
        self.assertFalse(self.loop._running)
