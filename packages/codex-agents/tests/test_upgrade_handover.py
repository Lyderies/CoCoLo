"""UPG: setup of this build hands over from a RUNNING earlier collector (owner-seen failure, 0.8.55).

Real processes: the earlier collector is a git archive of an earlier release (0.8.55 8d4a7ce3, 0.8.58
1c293c44) whose own `learning-register` starts it on a temporary state root; this build's
`learning-register` must stop it and register. UPG-5 checks this build's own collector the same way.
Needs git and those commits in this repository.
"""
import fcntl
import json
import os
import socket
import threading
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from unittest import mock

from codex_agents import collector_client
from codex_agents.collector_client import CollectorError, PACKAGE_VERSION
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.learning_store import CollectorMaintenance, LearningState
from codex_agents.collector_client import CollectorClient, canonical_json, exchange_frame
from codex_agents.collector_contracts import COLLECTOR_MUTATING_OPERATIONS

HERE = Path(__file__).resolve().parent
ORCHESTRATION = HERE.parent / "native-package" / ".codex" / "orchestration"
CLI_RELATIVE = "packages/codex-agents/native-package/.codex/orchestration/cli.py"


class Fixture:
    def prepare(self):
        self.base = Path(tempfile.mkdtemp(prefix="upg", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = self.base / "s"
        self.state.mkdir(mode=0o700)
        self.project = self.base / "p"
        self.project.mkdir()
        self.socket = self.state / "collector.sock"
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("CODEX_")},
                    "TMPDIR": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}
        self.addCleanup(self.stop_collector)

    def run_cli(self, cli, *extra):
        return subprocess.run(
            [sys.executable, "-B", str(cli), "learning-register",
             "--state-root", str(self.state), "--project-root", str(self.project), *extra],
            env=self.env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def frame(self, request):
        return json.loads(exchange_frame(self.socket, canonical_json(request), timeout=3.0,
                                         request_limit=4096, response_limit=16_384))

    def stop_collector(self):
        """Stop whatever collector is left with a stop it accepts, check it was accepted, wait for it to exit."""
        try:
            health = self.frame({"protocol": 1, "op": "health"})
        except Exception:
            # Not answering is not stopped: a loaded collector answers slowly while it still holds the writer lock.
            self.wait_writer_lock_free()
            return
        accepted = False
        for extra in ({"runtime_digest": health.get("runtime_digest")}, {}):
            try:
                if self.frame({"protocol": 1, "op": "shutdown_if_drained", **extra}).get("ok") is True:
                    accepted = True
                    break
            except Exception:
                pass
        self.assertTrue(accepted, "the leftover collector did not accept the drained stop")
        end = time.monotonic() + 15
        while self.socket.exists() or self.alive():
            self.assertLess(time.monotonic(), end, "the leftover collector did not exit")
            time.sleep(.1)
        self.wait_writer_lock_free()

    def wait_writer_lock_free(self, seconds=30):
        """Decision 248: a slow or failed health reply is not "stopped". The collector holds the state root's writer
        lock (collector.lock) until it has exited, so the only proof it is gone is taking that lock."""
        path = self.state / "collector.lock"
        end = time.monotonic() + seconds
        while True:
            try:
                descriptor = os.open(path, os.O_RDWR)
            except FileNotFoundError:
                return  # no collector ever created it
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return
            except OSError:
                pass
            finally:
                os.close(descriptor)
            self.assertLess(time.monotonic(), end,
                            f"the leftover collector still holds the state root's writer lock after {seconds} s")
            time.sleep(.1)

    def alive(self):
        try:
            self.reported_version()
            return True
        except Exception:
            return False

    def reported_version(self):
        return self.frame({"protocol": 1, "op": "health"})["runtime_version"]


class EarlierCollectorMixin(Fixture):
    OLD_COMMIT = ""
    OLD_VERSION = ""

    def setUp(self):
        top = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        if subprocess.run(["git", "-C", top, "cat-file", "-e", self.OLD_COMMIT + "^{commit}"]).returncode != 0:
            self.skipTest("the earlier release commit is not in this repository")
        self.prepare()
        archive = subprocess.run(["git", "-C", top, "archive", self.OLD_COMMIT, "packages/codex-agents/native-package"],
                                 capture_output=True, check=True).stdout
        old = self.base / "old"
        old.mkdir()
        subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
        # The earlier release's own setup registers the project and starts its collector, as it did live.
        started = self.run_cli(old / CLI_RELATIVE)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)

    def test_registering_stops_a_running_earlier_collector_and_completes(self):
        self.assertEqual(self.OLD_VERSION, self.reported_version())
        # Decision 352 (S14b-6): the schema-16 ledger's 17 -> 18 step (with its retention indexes) needs the owner's consent.
        done = self.run_cli(ORCHESTRATION / "cli.py", "--upgrade-ledger")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report["ready"], report)
        self.assertTrue(report["project_id"].startswith("prj_"))
        # UPG-4: the replacement is this build's collector and takes this build's own post-hand-over calls.
        health = self.frame({"protocol": 1, "op": "health"})
        self.assertEqual(PACKAGE_VERSION, health["runtime_version"])
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, health["runtime_digest"])
        client = CollectorClient(self.socket)
        self.assertIsInstance(client.learning_moved_if_empty(self.project), bool)
        self.assertEqual(report["project_id"], client.register(self.project))


class Earlier0855Tests(EarlierCollectorMixin, unittest.TestCase):
    """UPG-1: 0.8.55 rejects the stop's runtime_digest key (operation_invalid)."""
    OLD_COMMIT = "8d4a7ce3"
    OLD_VERSION = "0.8.55"


class Earlier0858Tests(EarlierCollectorMixin, unittest.TestCase):
    """UPG-6: 0.8.58 refuses the stop of another runtime (runtime_upgrade_required); its own digest is echoed once."""
    OLD_COMMIT = "1c293c44"
    OLD_VERSION = "0.8.58"


class StandInCollector:
    """A socket-level stand-in answering exactly the old wire shapes (a busy real 0.8.5x collector needs a real run).

    `script(request, seen)` returns the reply for one request; every request is recorded."""

    def __init__(self, path, script):
        self.requests = []
        self.script = script
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.settimeout(.2)
        self.stopping = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping:
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with connection:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                connection.sendall(json.dumps(self.script(request, self.requests)).encode() + b"\n")

    def close(self):
        self.stopping = True
        self.thread.join(5)
        self.server.close()

    def stops(self):
        return [r for r in self.requests if r["op"] == "shutdown_if_drained"]


class HandoverRefusalTests(unittest.TestCase):
    """The compatibility retries of the hand-over stop refuse, and stay bounded (review r1 MED).

    Stand-in server used (not a real busy collector: a busy one needs a driven run)."""
    DIGEST = "cd" * 32

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="upr", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        (self.base / "s").mkdir(mode=0o700)
        self.state = LearningState(self.base / "s")
        self.project = self.base / "p"
        self.project.mkdir()

    def health(self, version, digest=DIGEST):
        return {"ok": True, "runtime_version": version, "schema_version": 1, "runtime_digest": digest}

    def acquire(self, script, *, owner=True):
        stand_in = StandInCollector(self.state.socket, script)
        self.addCleanup(stand_in.close)
        with mock.patch("codex_agents.learning_store._start_collector_locked") as start, \
                mock.patch("codex_agents.learning_store._wait_for_collector_shutdown") as waited:
            try:
                CollectorMaintenance.acquire(self.state, ORCHESTRATION / "cli.py", self.project,
                                             owner_registration=owner)
            except CollectorError as error:
                outcome = error
            else:
                outcome = None
            self.assertEqual(0, start.call_count, "a refused hand-over must not restart or register")
            self.assertEqual(0, waited.call_count)
        self.assertEqual([], [r for r in stand_in.requests if r["op"] not in ("health", "shutdown_if_drained")])
        return outcome, stand_in

    def test_the_bare_retry_to_a_busy_collector_refuses_with_collector_busy(self):
        def script(request, seen):
            if request["op"] == "health":
                return self.health("0.8.55")
            return {"ok": False, "code": "operation_invalid" if "runtime_digest" in request else "collector_busy"}
        outcome, stand_in = self.acquire(script)
        self.assertEqual("collector_busy", outcome.code)
        self.assertEqual([True, False], ["runtime_digest" in r for r in stand_in.stops()])

    def test_the_echoed_retry_to_a_busy_collector_refuses_with_collector_busy(self):
        def script(request, seen):
            if request["op"] == "health":
                return self.health("0.8.58")
            if request["runtime_digest"] == self.DIGEST:
                return {"ok": False, "code": "collector_busy"}
            return {"ok": False, "code": "runtime_upgrade_required"}
        outcome, stand_in = self.acquire(script)
        self.assertEqual("collector_busy", outcome.code)
        self.assertEqual([COLLECTOR_RUNTIME_DIGEST, self.DIGEST], [r["runtime_digest"] for r in stand_in.stops()])

    def test_each_compatibility_retry_happens_at_most_once(self):
        for version, code in (("0.8.55", "operation_invalid"), ("0.8.58", "runtime_upgrade_required")):
            with self.subTest(version=version):
                def script(request, seen):
                    return self.health(version) if request["op"] == "health" else {"ok": False, "code": code}
                outcome, stand_in = self.acquire(script)
                self.assertEqual(code, outcome.code)
                self.assertEqual(2, len(stand_in.stops()))
                stand_in.close()
                self.state.socket.unlink()

    def test_a_collector_that_reports_this_version_or_newer_is_never_retried(self):
        for code in ("operation_invalid", "runtime_upgrade_required"):
            with self.subTest(code=code):
                def script(request, seen):
                    if request["op"] == "health":
                        # Older at first (the stop is attempted), this build's version when asked again.
                        return self.health("0.8.58" if len(seen) == 1 else PACKAGE_VERSION)
                    return {"ok": False, "code": code}
                outcome, stand_in = self.acquire(script)
                self.assertEqual(code, outcome.code)
                self.assertEqual(1, len(stand_in.stops()))
                stand_in.close()
                self.state.socket.unlink()

    def test_without_a_reported_digest_the_echo_fails_closed(self):
        def script(request, seen):
            if request["op"] == "health":
                return self.health("0.8.58", digest="not-a-digest")
            return {"ok": False, "code": "runtime_upgrade_required"}
        outcome, stand_in = self.acquire(script)
        self.assertEqual("runtime_upgrade_required", outcome.code)
        self.assertEqual(1, len(stand_in.stops()))

    def test_a_caller_that_is_not_the_owner_registration_never_retries(self):
        def script(request, seen):
            return self.health("0.8.58") if request["op"] == "health" else {"ok": False, "code": "runtime_upgrade_required"}
        outcome, stand_in = self.acquire(script, owner=False)
        self.assertIsInstance(outcome, CollectorError)  # refused (here its authorization check), no stop sent
        self.assertEqual(0, len(stand_in.stops()))


class SameBuildCollectorTests(Fixture, unittest.TestCase):
    """UPG-2 and UPG-5 against this build's own collector, a real process."""
    FOREIGN = "ab" * 32

    def setUp(self):
        self.prepare()
        done = self.run_cli(ORCHESTRATION / "cli.py")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def wait_stopped(self):
        end = time.monotonic() + 15
        while self.alive():
            self.assertLess(time.monotonic(), end, "the collector did not stop")
            time.sleep(.1)

    def test_UPG_5_other_mutating_operations_keep_the_exact_digest_gate(self):
        for request in ({"op": "shutdown", "runtime_digest": self.FOREIGN},
                        {"op": "shutdown"},
                        {"op": "register", "project_root": str(self.project), "runtime_digest": self.FOREIGN},
                        {"op": "learning_moved_if_empty", "project_root": str(self.project),
                         "runtime_digest": self.FOREIGN},
                        {"op": "ingest", "runtime_digest": self.FOREIGN}):
            self.assertEqual("runtime_upgrade_required", self.frame({"protocol": 1, **request}).get("code"), request)
        # A malformed digest on the drained stop is refused too, and nothing stops.
        self.assertFalse(self.frame({"protocol": 1, "op": "shutdown_if_drained", "runtime_digest": "not-a-digest"})["ok"])
        self.assertTrue(self.alive())

    def test_UPG_5_the_drained_stop_from_another_runtime_stops_it(self):
        self.assertEqual({"ok": True}, self.frame(
            {"protocol": 1, "op": "shutdown_if_drained", "runtime_digest": self.FOREIGN}))
        self.wait_stopped()

    def test_UPG_5_the_drained_stop_without_a_digest_stops_it(self):
        self.assertEqual({"ok": True}, self.frame({"protocol": 1, "op": "shutdown_if_drained"}))
        self.wait_stopped()

    def test_UPG_2_this_builds_client_still_sends_its_digest_with_every_mutating_operation(self):
        for operation in ("shutdown", "shutdown_if_drained", "register", "learning_moved_if_empty"):
            self.assertIn(operation, COLLECTOR_MUTATING_OPERATIONS)
        sent = []
        original = collector_client.exchange_frame
        collector_client.exchange_frame = lambda path, payload, **options: sent.append(json.loads(payload)) or b'{"ok":true}'
        try:
            CollectorClient(self.socket).shutdown(only_if_drained=True)
        finally:
            collector_client.exchange_frame = original
        self.assertEqual(collector_client.COLLECTOR_RUNTIME_DIGEST, sent[0]["runtime_digest"])


if __name__ == "__main__":
    unittest.main()
