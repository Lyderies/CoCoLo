"""Connected project frontend, real APIs/inert workers, fake native enrollment."""
import json
from pathlib import Path
import selectors
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from test_framework import requirements  # noqa: E402
from tests import test_project_request_queue as fixtures
from tests.learning_support import event_record

from codex_agents.history_api import HistoryClient, decode_history_response, encode_history_request
from codex_agents.history_contracts import HistoryError
from codex_agents import project_ingress
from codex_agents.project_history import ProjectHistory
from codex_agents.project_ingress import FRONTEND_SLOT_BYTES, ProjectIngress, ProjectIngressLimits
from codex_agents.service_transport import MAX_FRAME_BYTES

SECOND = 1_000_000_000
CONTEXT = fixtures.CONTEXT


class ProjectIngressTests(unittest.TestCase):
    def setUp(self):
        self.api = fixtures.ProjectRequestLaneTests(methodName="runTest")
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        self.lane, self.worker = self.api.lane, self.api.worker
        self.publication_allowed = True
        self.phases = []
        self.limits = ProjectIngressLimits(3, 3, 3 * SECOND, 1_000_000)

    def authorize(self, raw, context, phase):
        self.phases.append((phase, threading.current_thread()))
        if context is not CONTEXT or phase not in {"request", "publication"}:
            return False
        request = json.loads(raw)
        return (request.get("project_id") == self.api.project
                and request.get("runtime_digest") == self.api.runtime
                and (phase != "publication" or self.publication_allowed))

    def start(self, **changes):
        options = {"authorizer": self.authorize}
        options.update(changes)
        ingress = ProjectIngress(self.lane, self.limits, **options)
        self.addCleanup(self.stop, ingress)
        ingress.bootstrap()
        return ingress

    def stop(self, ingress):
        ingress.close()
        if ingress._thread.ident is not None:
            ingress._thread.join(6)
        self.assertFalse(ingress._thread.is_alive())

    def pair(self, ingress, *, context=CONTEXT):
        server, client = socket.socketpair()
        client.settimeout(5)
        try:
            token = ingress.attach(server, context)
        except BaseException:
            server.close()
            client.close()
            raise
        self.addCleanup(client.close)
        return client, token

    def send(self, client, command):
        client.sendall(encode_history_request(command) + b"\n")

    def read(self, client, command):
        raw = bytearray()
        while not raw.endswith(b"\n") and len(raw) < MAX_FRAME_BYTES:
            chunk = client.recv(min(8192, MAX_FRAME_BYTES - len(raw)))
            self.assertTrue(chunk, "fixture connection ended before its response")
            raw.extend(chunk)
        self.assertTrue(raw.endswith(b"\n"))
        return decode_history_response(bytes(raw[:-1]), command)

    def closed(self, client):
        try:
            self.assertEqual(client.recv(1), b"")
        except ConnectionResetError:
            pass

    def wait(self, condition):
        end = time.monotonic() + 5
        while not condition():
            if time.monotonic() >= end:
                self.fail("fixture frontend progress timeout")
            time.sleep(.001)

    def test_actual_history_client_and_original_writer_use_separate_threads(self):
        ingress = self.start()
        endpoint = Path(self.worker.temporary.name) / "project-api.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(endpoint))
        listener.listen(1)
        listener.settimeout(5)
        failures = []
        def accept():
            try:
                connection, _ = listener.accept()
                ingress.attach(connection, CONTEXT)
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=accept)
        thread.start()
        client = HistoryClient(endpoint, self.api.project, self.api.runtime, timeout=5)
        try:
            self.assertEqual(client.record_event(event_record("real-client"))["source_sequence"], 1)
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.api.composition.factory_threads, [self.lane._writer])
        self.assertTrue(all(thread is ingress._thread for _, thread in self.phases))
        self.assertIn("request", [phase for phase, _ in self.phases])
        self.assertIn("publication", [phase for phase, _ in self.phases])
        self.assertNotEqual(ingress._thread, self.lane._writer)

    def test_slow_partial_sender_never_occupies_project_writer(self):
        ingress = self.start()
        slow, _ = self.pair(ingress)
        slow.sendall(b"{")
        fast, _ = self.pair(ingress)
        command = self.api.command("fast-after-partial")
        self.send(fast, command)
        self.assertEqual(self.read(fast, command)["source_sequence"], 1)
        self.assertEqual(self.api.composition.factory.call_count, 1)
        self.assertFalse(ingress.snapshot()["faulted"])

    def test_complete_request_with_send_half_closed_still_receives_original_reply(self):
        ingress = self.start()
        client, _ = self.pair(ingress)
        command = self.api.command("send-half-closed")
        self.send(client, command)
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(self.read(client, command)["source_sequence"], 1)
        self.wait(lambda: ingress.snapshot()["connections"] == 0)
        self.assertEqual(self.api.composition.factory.call_count, 1)

    def test_inherited_process_denies_before_touching_inherited_lock(self):
        ingress = ProjectIngress(self.lane, self.limits, authorizer=self.authorize)
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        wrong_process = SimpleNamespace(getpid=lambda: ingress._pid + 1)
        with mock.patch.object(project_ingress, "os", wrong_process):
            for call in (ingress.bootstrap, ingress.close, ingress.snapshot,
                         lambda: ingress.attach(server, CONTEXT), lambda: ingress.detach(object())):
                with self.assertRaisesRegex(HistoryError, "history_wrong_owner"):
                    call()
            # Simulate an inherited locked mutex without forking or touching any
            # actual native identity/containment boundary.
            self.assertTrue(ingress._lock.acquire(timeout=1))
            try:
                thread = threading.Thread(target=ingress._front, daemon=True)
                thread.start()
                thread.join(.5)
                inherited_wait = thread.is_alive()
            finally:
                ingress._lock.release()
                ingress._closing = True
                thread.join(3)
            self.assertFalse(inherited_wait, "wrong-process frontend waited on an inherited lock")
        self.assertFalse(thread.is_alive())

    def test_partial_sender_cannot_refresh_absolute_connection_deadline(self):
        self.limits = ProjectIngressLimits(3, 3, SECOND // 4, 1_000_000)
        ingress = self.start()
        client, _ = self.pair(ingress)
        for _ in range(30):
            if ingress.snapshot()["connections"] == 0:
                break
            try:
                client.sendall(b"{")
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(.02)
        else:
            self.fail("partial input refreshed the absolute connection deadline")
        self.closed(client)
        self.assertEqual(self.api.composition.factory.call_count, 0)

    def test_default_denial_does_not_open_store(self):
        ingress = self.start(authorizer=None)
        client, _ = self.pair(ingress)
        self.send(client, self.api.command())
        self.closed(client)
        self.assertEqual(self.api.composition.factory.call_count, 0)
        self.assertEqual(self.lane.request_snapshot()["retained"], 0)

    def test_wrong_context_and_truthy_authorizer_do_not_grant_access(self):
        ingress = self.start(authorizer=lambda *args: "authorized")
        client, _ = self.pair(ingress, context=object())
        self.send(client, self.api.command())
        self.closed(client)
        self.assertEqual(self.api.composition.factory.call_count, 0)

    def test_oversized_and_multiple_frames_close_before_writer_work(self):
        ingress = self.start()
        for raw in (b"{}\n{}\n", b"x" * MAX_FRAME_BYTES):
            client, _ = self.pair(ingress)
            try:
                client.sendall(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.closed(client)
        self.assertEqual(self.api.composition.factory.call_count, 0)

    def test_connection_quota_refusal_retains_callers_descriptor_custody(self):
        ingress = self.start()
        peers = [self.pair(ingress) for _ in range(self.limits.connections)]
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        before = server.getblocking()
        with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            ingress.attach(server, CONTEXT)
        self.assertGreaterEqual(server.fileno(), 0)
        self.assertEqual(server.getblocking(), before)
        ingress.detach(peers[0][1])
        self.wait(lambda: ingress.snapshot()["connections"] == self.limits.connections - 1)

    def test_one_ingress_owns_lane_for_its_lifetime(self):
        ingress = self.start()
        with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            ProjectIngress(self.lane, self.limits, authorizer=self.authorize)
        self.stop(ingress)
        with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            ProjectIngress(self.lane, self.limits, authorizer=self.authorize)
        with self.assertRaises(HistoryError):
            ingress.bootstrap()

    def test_publication_revocation_withholds_reply_after_original_commit(self):
        ingress = self.start()
        original = ProjectHistory.append_record
        def commit_then_revoke(store, *args, **kwargs):
            result = original(store, *args, **kwargs)
            self.publication_allowed = False
            return result
        with mock.patch.object(ProjectHistory, "append_record", new=commit_then_revoke):
            client, _ = self.pair(ingress)
            command = self.api.command("committed-before-revocation")
            self.send(client, command)
            self.closed(client)
        self.stop(ingress)
        recovered = decode_history_response(self.api.response(self.api.queue(command)), command)
        self.assertEqual(recovered["source_sequence"], 1)
        self.assertEqual(self.api.composition.factory.call_count, 1)

    def test_authorization_rechecked_after_buffering_and_before_first_send(self):
        publications = []
        def authorize(raw, context, phase):
            if phase == "publication":
                publications.append(True)
                return len(publications) == 1
            return self.authorize(raw, context, phase)
        ingress = self.start(authorizer=authorize)
        client, _ = self.pair(ingress)
        self.send(client, self.api.command("buffered-no-send"))
        self.closed(client)
        self.assertEqual(len(publications), 2)

    def test_disconnect_during_write_retains_quota_until_actual_writer_return(self):
        ingress = self.start()
        prepared = self.api.composition.execution(rows=[{"pause": "release"}])
        self.worker.until(lambda: (prepared[0] / "launches").exists())
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.append_record
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise AssertionError("fixture database gate expired")
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, "append_record", new=blocked):
            client, _ = self.pair(ingress)
            self.send(client, self.api.command("disconnected-commit"))
            self.assertTrue(entered.wait(5))
            start = time.monotonic()
            cancelled = self.worker.request(prepared[1])
            self.assertEqual(cancelled.result["cancellation_state"], "stop_requested")
            self.assertLess(time.monotonic() - start, 2)
            self.worker.returned(prepared)
            client.close()
            # EOF alone cannot distinguish full disconnect from a valid send
            # half-close. It cannot free a still-executing writer's charge.
            self.assertEqual(self.lane.request_snapshot()["retained"], 1)
            resume.set()
            self.wait(lambda: self.lane.request_snapshot()["retained"] == 0)
            self.wait(lambda: ingress.snapshot()["connections"] == 0)
        self.assertFalse(ingress.snapshot()["faulted"])

    def test_close_is_nonblocking_and_does_not_close_original_writer(self):
        ingress = self.start()
        client, _ = self.pair(ingress)
        command = self.api.command("before-frontend-close")
        self.send(client, command)
        self.assertEqual(self.read(client, command)["source_sequence"], 1)
        start = time.monotonic()
        ingress.close()
        self.assertLess(time.monotonic() - start, .5)
        self.wait(lambda: ingress.snapshot()["stopped"])
        later = self.api.command("after-frontend-close")
        result = decode_history_response(self.api.response(self.api.queue(later)), later)
        self.assertEqual(result["source_sequence"], 2)
        self.assertEqual(self.api.composition.factory.call_count, 1)

    def test_frontend_fault_closes_peers_and_cannot_restart(self):
        class StopFrontend(BaseException):
            pass
        def fatal(*args):
            raise StopFrontend()
        ingress = self.start(authorizer=fatal)
        client, _ = self.pair(ingress)
        self.send(client, self.api.command())
        self.closed(client)
        self.wait(lambda: ingress.snapshot()["stopped"])
        self.assertTrue(ingress.snapshot()["faulted"])
        with self.assertRaises(HistoryError):
            ingress.bootstrap()
        self.assertEqual(self.api.composition.factory.call_count, 0)


    @requirements('S13d-1', why_not_scenario='Counts the frontend thread\'s own wake-ups; no client scenario is involved.')
    def test_S13d_1_an_ingress_with_no_connection_does_not_wake_every_poll_interval(self):
        selects = []

        class Counting(selectors.DefaultSelector):
            def select(self, timeout=None):
                selects.append(timeout)
                return super().select(timeout)

        with mock.patch.object(project_ingress.selectors, "DefaultSelector", Counting):
            ingress = self.start()
            time.sleep(1.0)
            idle_wakes = len(selects)
            client, _ = self.pair(ingress)  # a connection that arrives after a quiet spell is still taken at once
            deadline = time.monotonic() + 3
            while ingress.snapshot()["connections"] != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(1, ingress.snapshot()["connections"])
        client.close()
        # The poll interval is 1 ms: a thread that polls with nothing attached wakes about 1000 times a second.
        self.assertLess(idle_wakes, 60, idle_wakes)


class ProjectIngressLimitTests(unittest.TestCase):
    def test_invalid_counts_types_timeouts_and_poll_limits_refuse(self):
        for values in ((True, 1, SECOND, 1), (0, 1, SECOND, 1), (65, 1, SECOND, 1),
                       (2, 3, SECOND, 1), (2, 1, True, 1), (2, 1, SECOND, True),
                       (2, 1, 0, 1), (2, 1, 10, 11), (2, 1, SECOND, 100_000_001)):
            with self.assertRaises(HistoryError):
                ProjectIngressLimits(*values)
        self.assertEqual(ProjectIngressLimits(2, 1, SECOND, 1).retained_bytes, 2 * FRONTEND_SLOT_BYTES)
