"""Actual socket delivery; synthetic frontend contexts are not native proof."""
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.test_account_claim_jobs as fixtures
from codex_agents.account_api import AccountClient, AccountError, decode_account_response, encode_account_request
from codex_agents.account_claim_contracts import ClaimError, LaunchClaimInput
from codex_agents.account_ingress import AccountIngress, AccountIngressLimits


class AccountIngressLimitTests(unittest.TestCase):
    def test_closed_fixed_limits(self):
        valid = dict(ordinary_connections=4, health_connections=1, steps_per_cycle=5, owner_steps_per_cycle=2,
                     connection_timeout_ns=5_000_000_000, poll_interval_ns=2_000_000)
        AccountIngressLimits(**valid)
        for key, value in [("ordinary_connections", True), ("ordinary_connections", 256),
                           ("health_connections", 0), ("steps_per_cycle", 6), ("owner_steps_per_cycle", 6),
                           ("connection_timeout_ns", 60_000_000_001), ("poll_interval_ns", 100_000_001)]:
            with self.subTest(key=key), self.assertRaises(AccountError):
                AccountIngressLimits(**{**valid, key: value})


@unittest.skipUnless(fixtures.MACOS_WORKER, "fixed macOS worker guard; no Linux policy selected")
class AccountIngressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimJobTests(
            "test_actual_deferred_claim_verifies_off_owner_and_uses_original_atomic_receipt")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.service, self.jobs = self.fixture.service, self.fixture.jobs
        self.ingress = None
        self.threads = []
        self.stop = threading.Event()
        self.listener = None

    def tearDown(self):
        self.stop.set()
        for gate in self.fixture.gates:
            gate.set()
        if self.listener is not None:
            self.listener.close()
        if self.ingress is not None:
            self.ingress.request_close()
            self.wait(self.ingress.drain, pump=False)
            self.assertTrue(self.ingress.snapshot()["closed"])
        else:
            self.jobs.request_close()
            self.wait(self.jobs.drain, pump=False)
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "owned fixture frontend/client did not stop")

    def start(self, *, ordinary=4, timeout_ns=5_000_000_000):
        self.ingress = AccountIngress(self.service, AccountIngressLimits(
            ordinary, 1, ordinary + 1, 2, timeout_ns, 2_000_000))
        self.ingress.bootstrap()
        return self.ingress

    def wait(self, predicate, *, pump=True):
        end = time.monotonic() + 8
        while not predicate():
            if pump:
                self.ingress.pump_owner()
            self.assertLess(time.monotonic(), end, "bounded fixture progress expired")
            time.sleep(.002)

    def request(self, identity="wire", **kwargs):
        return self.fixture.request(identity, **kwargs)

    def pair(self, request=None, *, health=False, authorized=True):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        try:
            self.ingress.attach(server, self.fixture.token if authorized else object(), health=health)
        except BaseException:
            server.close()
            raise
        client.settimeout(2)
        if request is not None:
            client.sendall(encode_account_request(request) + b"\n")
        return client

    def read(self, client, request, *, pump=True):
        raw = bytearray()
        client.setblocking(False)

        def received():
            try:
                chunk = client.recv(16_384)
            except BlockingIOError:
                return False
            self.assertTrue(chunk, "socket closed before a reply")
            raw.extend(chunk)
            return b"\n" in raw

        self.wait(received, pump=pump)
        frame, trailing = bytes(raw).split(b"\n", 1)
        self.assertEqual(b"", trailing)
        return decode_account_response(frame, request)

    def test_partial_request_saturation_keeps_reserved_health_off_owner(self):
        self.start(ordinary=2, timeout_ns=300_000_000)
        first, second = self.pair(), self.pair()
        first.sendall(b'{"protocol_version":')
        second.sendall(b"{")
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.pair()
        health = self.request("reserved", operation="account.health")
        with mock.patch.object(self.service._views, "project", side_effect=AssertionError("health SQL")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("extra SQLite")), \
                mock.patch.object(threading.Thread, "start", side_effect=AssertionError("request thread")), \
                mock.patch.object(threading.Thread, "join", side_effect=AssertionError("request join")):
            self.assertEqual({"transport_alive": True, "database_checked": False},
                             self.read(self.pair(health, health=True), health, pump=False))
            # No owner pump, no traffic-based deadline extension.
            self.wait(lambda: self.ingress.snapshot()["ordinary"] == 0, pump=False)
        self.assertEqual(b"", first.recv(1))
        self.assertEqual(b"", second.recv(1))

    def test_reserved_lane_does_not_grant_operation_or_caller_authority(self):
        self.start()
        claim = self.pair(self.request(), health=True)
        claim.settimeout(2)
        self.assertEqual(b"", claim.recv(1))
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
        health = self.request("unauthorized", operation="account.health")
        with self.assertRaisesRegex(AccountError, "account_not_authorized"):
            self.read(self.pair(health, health=True, authorized=False), health, pump=False)

    def test_disconnected_handoff_keeps_slot_until_owner_ack(self):
        self.start(ordinary=1)
        request = self.request(operation="account.project")
        client = self.pair(request)
        self.wait(lambda: self.ingress.snapshot()["queued"] == 1, pump=False)
        client.close()
        self.wait(lambda: self.ingress.snapshot()["disconnected"] == 1, pump=False)
        self.assertEqual(1, self.ingress.snapshot()["ordinary"])
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.pair()
        with mock.patch.object(self.service, "begin", side_effect=AssertionError("disconnected request")):
            self.ingress.pump_owner()
        self.assertEqual(0, self.ingress.snapshot()["ordinary"])

    def test_duplicate_subscriber_disconnect_does_not_cancel_other(self):
        self.start()
        request = self.request("shared")
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            first, second = self.pair(request), self.pair(request)
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 2 and started.is_set())
            self.assertEqual(1, self.jobs.snapshot()["tickets"])
            first.close()
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 1)
            self.assertEqual(1, self.jobs.snapshot()["pending"])
            gate.set()
            self.assertEqual("claimed", self.read(second, request)["state"])
        self.assertEqual(1, self.fixture.fixture.count("launch_claims"))
        self.assertEqual(1, self.fixture.fixture.count("launch_receipts"))
        self.wait(lambda: self.ingress.snapshot()["ordinary"] == 0)

    def test_last_disconnect_keeps_active_custody_until_actual_return(self):
        self.start()
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher, mock.patch.object(self.service._views, "_claim_from_signature_job",
                                       side_effect=AssertionError("cancelled claim committed")):
            client = self.pair(self.request("disconnect"))
            self.wait(lambda: started.is_set())
            client.close()
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 0)
            self.assertEqual(1, self.ingress.snapshot()["groups"])
            self.assertEqual(1, self.jobs.snapshot()["pending"])
            for _ in range(5):
                self.ingress.pump_owner()
            self.assertEqual(1, self.jobs.snapshot()["tickets"])
            gate.set()
            self.wait(lambda: self.ingress.snapshot()["groups"] == 0)
        self.fixture.fixture.assert_no_claim()
        self.assertEqual(0, self.jobs.snapshot()["tickets"])

    def test_changed_pending_parameters_conflict_and_reply_is_bound(self):
        self.start()
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            original = self.request("same-id")
            first = self.pair(original)
            self.wait(lambda: started.is_set())
            changed = self.request("same-id", launch=LaunchClaimInput(self.fixture.launch.run_handle, "b" * 64))
            with self.assertRaisesRegex(AccountError, "claim_conflict"):
                self.read(self.pair(changed), changed)
            self.assertEqual(1, self.jobs.snapshot()["tickets"])
            gate.set()
            self.assertEqual("claimed", self.read(first, original)["state"])

    def test_real_account_client_second_project_progresses_while_first_waits(self):
        project, launch = self.fixture.other_project()
        self.start()
        # A temporary fixture listener supplies sockets; production binds none.
        temporary = self.fixture.fixture.fixture.fixture.temporary
        path = Path(temporary.name) / "account-ingress.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.listener.settimeout(.02)
        errors = []

        def frontend():
            while not self.stop.is_set():
                try:
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if not self.stop.is_set():
                        errors.append("fixture accept failed")
                    return
                try:
                    self.ingress.attach(connection, self.fixture.token)
                except BaseException:
                    connection.close()
                    errors.append("fixture attach failed")

        front = threading.Thread(target=frontend)
        self.threads.append(front)
        front.start()
        release, finished = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        result = {}

        def client():
            release.wait(8)
            try:
                result["reply"] = AccountClient(path, project, self.service._runtime, timeout=5).claim_launch(
                    launch.run_handle, launch.identity_digest, request_id="second-project")
            except BaseException as error:
                result["error"] = type(error).__name__
            finally:
                finished.set()

        thread = threading.Thread(target=client)
        self.threads.append(thread)
        thread.start()
        owner, popen = threading.current_thread(), subprocess.Popen
        helper_threads = []

        def off_owner(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread())
            helper_threads.append(threading.current_thread())
            return popen(*args, **kwargs)

        gate, started, patcher = self.fixture.gate_first_project()
        with patcher, mock.patch("sqlite3.connect", side_effect=AssertionError("extra SQLite")), \
                mock.patch.object(threading.Thread, "start", side_effect=AssertionError("request thread")), \
                mock.patch.object(threading.Thread, "join", side_effect=AssertionError("request join")), \
                mock.patch.object(subprocess, "Popen", side_effect=off_owner), \
                mock.patch.object(self.fixture.ledger, "_prepare_attestations", side_effect=AssertionError("sync verifier")):
            first_request = self.request("first-project")
            first = self.pair(first_request)
            self.wait(started.is_set)
            release.set()
            self.wait(finished.is_set)
            self.assertNotIn("error", result)
            self.assertEqual("claimed", result["reply"]["state"])
            self.assertEqual(1, self.jobs.snapshot()["pending"])
            health = self.request("during-signature", operation="account.health")
            self.assertFalse(self.read(self.pair(health, health=True), health, pump=False)["database_checked"])
            gate.set()
            self.assertEqual("claimed", self.read(first, first_request)["state"])
        self.assertTrue(helper_threads)
        self.assertFalse(errors)

    def test_kernel_reply_backpressure_leaves_other_requests_and_health_available(self):
        self.start(timeout_ns=500_000_000)
        server, stalled = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(stalled.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        server.setblocking(False)
        queued = 0
        while True:
            try:
                queued += server.send(b"x" * 4096)
            except BlockingIOError:
                break
        self.assertGreater(queued, 0)
        self.ingress.attach(server, self.fixture.token)
        stalled.sendall(encode_account_request(self.request("slow-reader", operation="account.project")) + b"\n")
        self.wait(lambda: self.ingress.snapshot()["queued"] == 1, pump=False)
        with mock.patch("sqlite3.connect", side_effect=AssertionError("extra SQLite")):
            self.ingress.pump_owner()
            other = self.request("other", operation="account.project")
            self.assertIs(type(self.read(self.pair(other), other)), dict)
            health = self.request("health", operation="account.health")
            self.assertTrue(self.read(self.pair(health, health=True), health, pump=False)["transport_alive"])
            self.wait(lambda: self.ingress.snapshot()["ordinary"] == 0)

    def test_owner_interrupt_fences_admission_but_health_and_public_drain_remain(self):
        self.start()
        self.pair(self.request(operation="account.project"))
        self.wait(lambda: self.ingress.snapshot()["queued"] == 1, pump=False)
        with mock.patch.object(self.service, "begin", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.ingress.pump_owner()
        self.assertTrue(self.ingress.snapshot()["faulted"])
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.pair()
        health = self.request("fault-health", operation="account.health")
        self.assertTrue(self.read(self.pair(health, health=True), health, pump=False)["transport_alive"])
        self.fixture.fixture.assert_no_claim()

    def test_shutdown_waits_for_actual_worker_return_without_offer_commit_or_join(self):
        self.start()
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            self.pair(self.request("shutdown"))
            self.wait(started.is_set)
            with mock.patch.object(self.jobs._pool, "offer_next", side_effect=AssertionError("shutdown offer")), \
                    mock.patch.object(self.service._views, "_claim_from_signature_job", side_effect=AssertionError("shutdown commit")), \
                    mock.patch.object(threading.Thread, "join", side_effect=AssertionError("shutdown join")):
                self.ingress.request_close()
                self.wait(lambda: self.ingress.snapshot()["front_stopped"], pump=False)
                self.assertFalse(self.ingress.drain())
                self.assertEqual(1, self.jobs.snapshot()["tickets"])
                gate.set()
                self.wait(self.ingress.drain, pump=False)
        self.assertTrue(self.ingress.snapshot()["closed"])
        self.fixture.fixture.assert_no_claim()
        # The database and service have independent lifetimes.
        self.assertIs(type(self.service._views.project(self.fixture.project)), dict)

    def test_both_duplicate_subscribers_receive_the_same_committed_reply(self):
        self.start()
        request = self.request("both")
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            first, second = self.pair(request), self.pair(request)
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 2 and started.is_set())
            gate.set()
            result = self.read(first, request)
            self.assertEqual(result, self.read(second, request))
        self.assertEqual("claimed", result["state"])
        self.assertEqual(1, self.fixture.fixture.count("launch_receipts"))

    def test_lost_committed_reply_retry_retrieves_known_without_signature_work(self):
        self.start()
        request = self.request("lost")
        first = self.pair(request)
        self.wait(lambda: self.fixture.fixture.count("launch_claims") == 1 and self.jobs.snapshot()["tickets"] == 0)
        first.close()  # Intentionally consume no reply.
        self.wait(lambda: self.ingress.snapshot()["ordinary"] == 0)
        with mock.patch.object(self.jobs._intake, "capture", side_effect=AssertionError("known receipt recaptured")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("known receipt helper")):
            known = self.read(self.pair(request), request)
            self.assertEqual("claimed", known["state"])
            self.assertTrue(known["known"])
        self.assertEqual(1, self.fixture.fixture.count("launch_claims"))
        self.assertEqual(1, self.fixture.fixture.count("launch_receipts"))

    def test_detached_active_group_and_queued_subscriber_remain_quota_charged(self):
        self.start()
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            clients = [self.pair(self.request(identity)) for identity in ("detached-1", "detached-2")]
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 2 and started.is_set())
            # Intake deliberately permits one selected worker per project.
            # Keep the queued request subscribed: cancelling it can release it
            # immediately, whereas the active disconnected one must stay held.
            clients[0].close()
            self.wait(lambda: self.ingress.snapshot()["subscribers"] == 1)
            self.assertEqual(2, self.ingress.snapshot()["groups"])
            self.assertEqual(2, self.jobs.snapshot()["tickets"])
            busy = self.request("over-quota")
            with self.assertRaisesRegex(AccountError, "claim_busy"):
                self.read(self.pair(busy), busy)
            gate.set()
            self.assertEqual("claimed", self.read(clients[1], self.request("detached-2"))["state"])
            self.wait(lambda: self.ingress.snapshot()["groups"] == 0)

    def test_bound_coordinator_refuses_other_delivery_consumers(self):
        self.start()
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            AccountIngress(self.service, self.ingress._limits)
        request = self.request("outside")
        raw = self.service.begin(encode_account_request(request), self.fixture.token)
        with self.assertRaisesRegex(AccountError, "claim_unavailable"):
            decode_account_response(raw, request)
        gate, started, patcher = self.fixture.gate_first_project()
        with patcher:
            client = self.pair(self.request("bound"))
            self.wait(started.is_set)
            ticket = next(iter(self.ingress._groups))
            for method in (self.jobs.take, self.jobs.cancel, self.service.take_claim_reply):
                with self.assertRaisesRegex(ClaimError, "claim_unavailable"):
                    method(ticket)
            gate.set()
            self.assertEqual("claimed", self.read(client, self.request("bound"))["state"])

    def test_attachment_failure_keeps_mode_and_descriptor_with_frontend(self):
        self.start(ordinary=1)
        self.pair()
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        server.settimeout(.7)
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.ingress.attach(server, self.fixture.token)
        self.assertEqual(.7, server.gettimeout())
        self.assertGreaterEqual(server.fileno(), 0)
        server.sendall(b"owned")
        self.assertEqual(b"owned", client.recv(5))

    def test_malformed_or_extra_frames_do_not_reach_owner(self):
        self.start()
        for frame in (b"{}\n", b"x" * 16_384, encode_account_request(self.request()) + b"\n{}\n"):
            client = self.pair()
            client.sendall(frame)
            with mock.patch.object(self.service, "begin", side_effect=AssertionError("bad frame executed")):
                self.wait(lambda: self.ingress.snapshot()["ordinary"] == 0)
            self.assertEqual(b"", client.recv(1))
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
