from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT

from codex_agents import learning_store, service_transport as transport
from codex_agents.learning_store import CollectorClient, CollectorError


class FakeSocket:
    def __init__(self, chunks=(), *, costs=(0, 0, 0), failure=None):
        self.chunks = list(chunks)
        self.costs = costs
        self.failure = failure
        self.now = 0.0
        self.closed = False
        self.timeouts = []
        self.connections = []
        self.sends = []
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, endpoint):
        self.connections.append(endpoint)
        self.now += self.costs[0]
        if self.failure == "connect":
            raise OSError("CANARY-ENDPOINT")

    def sendall(self, payload):
        self.sends.append(payload)
        self.now += self.costs[1]
        if self.failure == "send":
            raise TimeoutError("CANARY-SEND")

    def recv(self, size):
        self.read_sizes.append(size)
        self.now += self.costs[2]
        if self.failure == "receive":
            raise OSError("CANARY-REPLY")
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if len(value) > size:
            self.chunks.insert(0, value[size:])
        return value[:size]


class ServiceTransportTests(unittest.TestCase):
    def exchange(self, client, **options):
        with mock.patch.object(transport.socket, "socket", return_value=client) as create, \
                mock.patch.object(transport.time, "monotonic", side_effect=lambda: client.now):
            try:
                return transport.exchange_frame(Path("/fixture/service.sock"), b"{}", timeout=1, **options)
            finally:
                self.assertEqual(1, create.call_count)
                self.assertTrue(client.closed)

    def test_complete_frame_and_eof_compatibility(self) -> None:
        for chunks, expected in (
            ([b'{"ok":', b'true}\nunused\n'], b'{"ok":true}'),
            ([b'{"ok":true}'], b'{"ok":true}'),
            ([], b""),
        ):
            with self.subTest(chunks=chunks):
                client = FakeSocket(chunks, costs=(0.1, 0.1, 0.1))
                self.assertEqual(expected, self.exchange(client))
                self.assertEqual([b"{}\n"], client.sends)
                self.assertEqual(1, len(client.connections))
                self.assertEqual(sorted(client.timeouts, reverse=True), client.timeouts)
                self.assertTrue(all(value > 0 for value in client.timeouts))

    def test_one_deadline_covers_connect_send_and_drip_receives(self) -> None:
        for costs, expected_sends in (((1.25, 0, 0), 0), ((0, 1.25, 0), 1), ((0.25, 0.25, 0.25), 1)):
            with self.subTest(costs=costs):
                client = FakeSocket([b"a", b"\n"], costs=costs)
                with self.assertRaises(transport.ServiceTransportError) as caught:
                    self.exchange(client)
                self.assertEqual("deadline_exceeded", caught.exception.reason_code)
                self.assertEqual(expected_sends, len(client.sends))
                self.assertIsNone(caught.exception.__context__)

    def test_failed_exchange_never_retries_or_exposes_os_details(self) -> None:
        for failure, reason in (("connect", "transport_unavailable"), ("send", "deadline_exceeded"), ("receive", "transport_unavailable")):
            with self.subTest(failure=failure):
                client = FakeSocket(failure=failure)
                with self.assertRaises(transport.ServiceTransportError) as caught:
                    self.exchange(client)
                self.assertEqual((reason,), caught.exception.args)
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertLessEqual(len(client.sends), 1)
                self.assertNotIn("CANARY", str(caught.exception))
        with mock.patch.object(transport.socket, "socket", side_effect=OSError("CANARY")) as create:
            with self.assertRaises(transport.ServiceTransportError) as caught:
                transport.exchange_frame(Path("/fixture/service.sock"), b"{}", timeout=1)
        self.assertEqual(1, create.call_count)
        self.assertEqual("transport_unavailable", caught.exception.reason_code)
        self.assertIsNone(caught.exception.__context__)

    def test_invalid_or_oversized_requests_never_open_a_socket(self) -> None:
        options = [
            {"timeout": value} for value in (True, None, 0, -1, float("nan"), float("inf"), 61)
        ] + [
            {field: value} for field in ("request_limit", "response_limit")
            for value in (True, 0, -1, transport.MAX_FRAME_BYTES + 1)
        ] + [{"message": value} for value in (b"", b"{}\n{}", bytearray(b"{}"))]
        options.append({"message": b"x" * transport.MAX_FRAME_BYTES})
        for changes in options:
            with self.subTest(changes={key: type(value).__name__ for key, value in changes.items()}):
                with mock.patch.object(transport.socket, "socket") as create:
                    with self.assertRaises(transport.ServiceTransportError):
                        transport.exchange_frame(Path("/fixture/service.sock"), **{
                            "message": b"{}", "timeout": 1, **changes,
                        })
                    create.assert_not_called()

    def test_response_limit_includes_delimiter_and_bounds_read_allocation(self) -> None:
        self.assertEqual(b"1234567", self.exchange(FakeSocket([b"1234567\n"]), response_limit=8))
        for chunks in ([b"12345678\n"], [b"1234", b"56789"]):
            client = FakeSocket(chunks)
            with self.assertRaises(transport.ServiceTransportError) as caught:
                self.exchange(client, response_limit=8)
            self.assertEqual("response_too_large", caught.exception.reason_code)
            self.assertTrue(all(size <= 9 for size in client.read_sizes))

    def test_collector_adapter_preserves_codes_framing_and_runtime_checks(self) -> None:
        client = CollectorClient(Path("/fixture/service.sock"), timeout=0.2)
        reply = {
            "ok": True, "schema_version": learning_store.LEDGER_SCHEMA_VERSION,
            "runtime_version": learning_store.PACKAGE_VERSION,
            "runtime_digest": learning_store.COLLECTOR_RUNTIME_DIGEST,
        }
        with mock.patch.object(learning_store, "exchange_frame", return_value=json.dumps(reply).encode()) as exchange:
            self.assertEqual(reply, client.health())
        self.assertEqual(1, exchange.call_count)
        arguments, options = exchange.call_args
        self.assertEqual(client.socket_path, arguments[0])
        self.assertEqual({"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "health"}, json.loads(arguments[1]))
        self.assertEqual(0.2, options["timeout"])
        for reason, code, message in (
            ("request_too_large", None, "request is oversized"),
            ("response_too_large", None, "response is oversized"),
            ("deadline_exceeded", "transport_unavailable", "unavailable"),
            ("transport_unavailable", "transport_unavailable", "unavailable"),
        ):
            with self.subTest(reason=reason), mock.patch.object(
                learning_store, "exchange_frame", side_effect=transport.ServiceTransportError(reason),
            ) as exchange:
                with self.assertRaisesRegex(CollectorError, message) as caught:
                    client.health()
                self.assertEqual(code, caught.exception.code)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(1, exchange.call_count)
        for raw in (b"CANARY", b"\xff", b"[" * 10_000):
            with self.subTest(raw_length=len(raw)), mock.patch.object(learning_store, "exchange_frame", return_value=raw):
                with self.assertRaises(CollectorError) as caught:
                    client.health()
                self.assertEqual("response_invalid", caught.exception.code)
                self.assertIsNone(caught.exception.__context__)


class ServiceTransportSocketTests(unittest.TestCase):
    def test_slow_reply_cannot_extend_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="svc-wire-", dir=TEMP_ROOT) as directory:
            endpoint = Path(directory) / "s"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(endpoint))
                listener.listen(1)
                listener.settimeout(2)
                errors = []

                def serve():
                    try:
                        connection, _ = listener.accept()
                        with connection:
                            connection.settimeout(2)
                            connection.recv(8192)
                            for byte in b'{"ok":true,"padding":"' + b"x" * 80 + b'"}\n':
                                connection.sendall(bytes([byte]))
                                time.sleep(0.02)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except Exception as exc:
                        errors.append(type(exc).__name__)

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(transport.ServiceTransportError) as caught:
                        transport.exchange_frame(endpoint, b"{}", timeout=0.12)
                    self.assertEqual("deadline_exceeded", caught.exception.reason_code)
                    self.assertLess(time.monotonic() - started, 1.5)
                finally:
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
