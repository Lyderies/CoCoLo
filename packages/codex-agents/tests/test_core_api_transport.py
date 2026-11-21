"""Core API transport, identity, framing, handshake, limits, neutrality (API-1..6, 24, 25; TERM-5)."""
import json
import os
import socket
import stat
import subprocess
import sys
import unittest

import tests.support as support
from tests.core_api_support import CoreCase


def raw(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    return s


def ask(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode())
    return json.loads(s.makefile().readline())


class SocketTests(CoreCase):
    def test_api2_socket_mode_directory_mode_and_short_path(self):  # API-2
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.socket).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(os.path.dirname(self.socket)).st_mode))
        self.assertLess(len(self.socket.encode()), 104)

    def test_api2_no_tcp_listener(self):  # API-2
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(os.getpid()), "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        self.assertEqual("", out.strip())


class UidTests(CoreCase):
    start_service = False

    def test_api3_other_uid_is_refused_before_any_method(self):  # API-3
        self.start(expected_uid=os.getuid() + 1)
        try:
            s = raw(self.socket)
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
            self.assertEqual(b"", s.recv(4096))
        except (ConnectionResetError, BrokenPipeError):
            pass


class FramingTests(CoreCase):
    def test_api4_malformed_line_gets_parse_error_and_connection_lives(self):  # API-4
        s = raw(self.socket)
        s.sendall(b"{not json\n")
        reader = s.makefile()
        reply = json.loads(reader.readline())
        self.assertEqual(-32700, reply["error"]["code"])
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                               "params": {"protocol": self.mod.PROTOCOL_VERSION}}) + "\n").encode())
        self.assertEqual(2, json.loads(reader.readline())["id"])

    def test_api4_responses_are_jsonrpc_2_with_matching_id(self):  # API-4
        s = raw(self.socket)
        reply = ask(s, {"jsonrpc": "2.0", "id": "abc", "method": "initialize", "params": {"protocol": "1.0"}})
        self.assertEqual(("2.0", "abc"), (reply["jsonrpc"], reply["id"]))
        self.assertIn("result", reply)

    def test_api4_unknown_method_is_method_not_found(self):  # API-4
        s = raw(self.socket)
        ask(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": "1.0"}})
        self.assertEqual(-32601, ask(s, {"jsonrpc": "2.0", "id": 2, "method": "nope.nothing"})["error"]["code"])


class HandshakeTests(CoreCase):
    def test_api5_initialize_returns_protocol_and_features(self):  # API-5
        result = self.mod.CoreClient(self.socket).call("initialize", {"protocol": "1.0"})
        self.assertEqual("1.0", result["protocol"])
        self.assertIsInstance(result["features"], list)

    def test_api5_major_mismatch_is_refused(self):  # API-5
        client = self.client(handshake=False)
        self.assertEqual("version_mismatch", self.error_code(client, "initialize", {"protocol": "2.0"}))

    def test_api5_method_before_initialize_is_refused(self):  # API-5
        client = self.client(handshake=False)
        self.assertEqual("not_initialized", self.error_code(client, "state.get"))


class ContractTests(CoreCase):
    def test_api6_schema_lists_methods_events_and_errors(self):  # API-6
        schema = self.client().call("schema.get")
        for name in ("state.get", "units.list", "events.subscribe", "query.ask", "context.push",
                     "unit.cancel", "decision.answer"):
            self.assertIn(name, schema["methods"])
        self.assertIn("unit.updated", schema["event_kinds"])
        for code in ("forbidden", "confirm_required", "cursor_expired", "not_found", "unavailable"):
            self.assertIn(code, schema["error_codes"])

    def test_api6_every_response_carries_seq_at(self):  # API-6
        client = self.client()
        for method in ("state.get", "units.list", "usage.get", "health.get"):
            self.assertEqual(5, client.call(method)["seq_at"], method)


class LimitTests(CoreCase):
    service_options = {"max_line_bytes": 4096, "max_connections": 3, "max_subscriptions": 1}

    def test_api24_oversize_line_is_refused_with_fixed_code(self):  # API-24
        client = self.client()
        self.assertEqual("too_large", self.error_code(client, "query.ask", {"text": "x" * 10000}))


    def test_api24_subscription_cap(self):  # API-24
        a, b = self.client(), self.client()
        a.subscribe({"from_seq": 0})
        self.assertEqual("too_many_subscriptions", self.error_code(b, "events.subscribe", {"from_seq": 0}))


class MethodTimeTests(CoreCase):
    service_options = {"method_seconds": 0.001}

    def test_api24_slow_method_times_out_with_fixed_code(self):  # API-24
        self.service.slow_for_test = 0.2  # the service exposes this hook so the limit can be proved
        self.assertEqual("timeout", self.error_code(self.client(), "state.get"))


class NeutralityTests(CoreCase):
    def test_api25_no_provider_names_in_contract_and_extra_holds_detail(self):  # API-25
        schema = json.dumps(self.client().call("schema.get")).lower()
        for provider in ("claude", "codex", "openai", "anthropic", "sonnet", "opus"):
            self.assertNotIn(provider, schema)
        unit = self.client().call("units.get", {"id": "U-0001"})["unit"]
        self.assertIn("extra", unit)
        self.assertNotIn("model", unit)  # provider detail lives only under extra
        self.assertEqual("sonnet", unit["extra"]["model"])


class SeparateProcessTests(unittest.TestCase):
    def test_term5_core_runs_headless_as_its_own_process(self):  # TERM-5, API-1
        import shutil, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp(dir=support.SHORT_TMP, prefix="core"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "records").mkdir()
        proc = subprocess.Popen([sys.executable, "-m", "codex_agents.core_api", "--records", str(d / "records"),
                                 "--socket", str(d / "o.sock")], env={**os.environ, "PYTHONPATH": str(support.ORCHESTRATION_ROOT)},
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(proc.kill)
        import time
        for _ in range(50):
            if (d / "o.sock").exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNone(proc.poll(), proc.stderr.read().decode() if proc.poll() is not None else "")
        self.assertTrue((d / "o.sock").exists())
        self.assertNotEqual(os.getpid(), proc.pid)


if __name__ == "__main__":
    unittest.main()
