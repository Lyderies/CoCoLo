"""T7 S6 (scripts/e2e_checks_core_api.py, scripts/e2e_checks_core_records.py): strictness of the core API checks.

Each test shows a check cannot pass by being inert: against a stub that answers every line with a success (no
refusal, no gate, no scope), the checks that judge refusals must fail. The faults must apply exactly once to the
installed core's source, the modules must declare exactly the requirement IDs they own, and the helper wire must
tell a refusal from an answer. Written with the modules (the stub is the red-first picture of an inert core)."""

import ast
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_checks_core_api as api  # noqa: E402
import e2e_checks_core_records as records  # noqa: E402
import e2e_core  # noqa: E402

CORE_SOURCE = (ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "codex_agents"
               / "core_api.py").read_text()
API_OWNS = {"API-1", "API-2", "API-4", "API-5", "API-6", "API-24", "API-25", "TERM-5"}
RECORDS_OWN = {"API-7", "API-8", "API-9", "API-10", "API-11", "API-12", "API-13", "API-14", "API-15", "API-16",
               "API-17", "API-18", "API-19", "API-20", "API-21", "API-22", "API-23", "TERM-7", "TERM-13", "TERM-17"}


def declared(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "check":
                    found.update(arg.value for arg in decorator.args if isinstance(arg, ast.Constant))
    return found


def run_check(function) -> bool:
    phase = e2e_core.Phase(function.__name__, Path("/nonexistent"))
    try:
        function(phase)
    except e2e_core.CheckFailed:
        return False
    except Exception:  # the runner fails a check that errors, too (scripts/e2e_lifecycle.py, _execute)
        return False
    return phase.expectations > 0


class Stub:
    """A unix socket server that answers EVERY line with a success (an inert core: no gate, no refusal, no scope)."""

    def __init__(self):
        self.folder = tempfile.mkdtemp(prefix="stub", dir="/tmp")
        self.path = os.path.join(self.folder, "o.sock")
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.stopped = False
        self.connections = []
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.stopped:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()

    def serve(self, connection):
        reader = connection.makefile("rb")
        self.connections.append(connection)
        for line in reader:
            try:
                request = json.loads(line)
                rid = request.get("id")
            except ValueError:
                rid = None
            if rid is not None and not isinstance(rid, (int, str)):
                continue
            reply = {"jsonrpc": "2.0", "id": rid, "result": {"protocol": "1.0", "features": ["events", "resume"],
                                                             "scope": "owner", "seq_at": 0}}
            try:
                connection.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                return

    def close(self):
        self.stopped = True
        self.listener.close()
        for connection in self.connections:
            try:
                connection.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
            os.rmdir(self.folder)
        except OSError:
            pass


class Scene(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # a failing check leaves its client sockets to the collector

    def tearDown(self):
        e2e_core._SCENES.clear()

    def stub_scene(self, key, **extra):
        stub = Stub()
        self.addCleanup(stub.close)
        core = SimpleNamespace(socket=stub.path, audit=lambda name="audit.jsonl": [], records=Path("/nonexistent"),
                               alive=lambda: True)
        tokens = {scope: "0" * 64 for scope in ("read", "agent", "orchestrator", "confirm", "owner")}
        scene = SimpleNamespace(world=None, core=core, read="0" * 64, tokens=tokens, **extra)
        e2e_core._SCENES[key] = scene
        return scene


class DeclaredTests(unittest.TestCase):
    def test_each_module_declares_exactly_the_ids_it_owns(self):
        self.assertEqual(API_OWNS, declared(ROOT / "scripts" / "e2e_checks_core_api.py"))
        self.assertEqual(RECORDS_OWN, declared(ROOT / "scripts" / "e2e_checks_core_records.py"))
        self.assertEqual(set(api.A_IDS), API_OWNS)
        self.assertEqual(set(records.B_IDS), RECORDS_OWN)

    def test_ids_left_unchecked_are_not_declared(self):
        owned = API_OWNS | RECORDS_OWN
        for left in ("API-3", "API-26", "API-27", "TERM-6"):
            self.assertNotIn(left, owned)

    def test_every_fault_rewrite_applies_exactly_once_to_the_installed_core(self):
        for module in (api, records):
            for name, patches in module.FAULTS.items():
                for patch in patches:
                    with self.subTest(name + ": " + patch["old"][:40]):
                        self.assertEqual("codex_agents.core_api", patch["module"])
                        self.assertEqual(1, CORE_SOURCE.count(patch["old"]))
                        self.assertNotEqual(patch["old"], patch["new"])

    def test_fault_targets_are_ids_the_module_owns_and_the_fault_is_off_by_default(self):
        self.assertIsNone(api.FAULT)
        self.assertIsNone(records.FAULT)
        self.assertEqual([], api.fault_patches())
        self.assertEqual([], records.fault_patches())
        self.assertLessEqual(set(api.FAULT_TARGETS["core_api_broken"]), API_OWNS)
        self.assertLessEqual(set(records.FAULT_TARGETS["core_records_broken"]), RECORDS_OWN)

    def test_modules_do_no_io_at_import_and_never_write_sqlite(self):
        for name in ("e2e_checks_core_api.py", "e2e_checks_core_records.py"):
            tree = ast.parse((ROOT / "scripts" / name).read_text())
            for node in tree.body:  # module level: definitions, imports, constants only
                self.assertIsInstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Assign,
                                             ast.AnnAssign, ast.Expr), name)
            self.assertNotIn("sqlite3", (ROOT / "scripts" / name).read_text())

    def test_the_fault_comparator_demands_every_target_red_and_nothing_else_changed(self):
        ids = api.A_IDS
        normal = {"requirements": {i: {"passed": True} for i in ids}}
        red = {"requirements": {i: {"passed": i not in api.FAULT_TARGETS["core_api_broken"]} for i in ids}}
        self.assertTrue(api.fault_isolated(normal, red)["isolated"])
        self.assertFalse(api.fault_isolated(normal, normal)["isolated"])  # no target red
        spill = {"requirements": {i: {"passed": i not in (*api.FAULT_TARGETS["core_api_broken"], "TERM-5")} for i in ids}}
        self.assertEqual(["TERM-5"], api.fault_isolated(normal, spill)["changed_outside_targets"])
        gone = {"requirements": {i: v for i, v in red["requirements"].items() if i != "API-6"}}
        self.assertFalse(api.fault_isolated(normal, gone)["isolated"])


class WireTests(unittest.TestCase):
    def test_a_refusal_is_told_from_an_answer_and_an_inert_server_refuses_nothing(self):
        stub = Stub()
        self.addCleanup(stub.close)
        wire = api.Wire(stub.path, timeout=3)
        self.addCleanup(wire.close)
        self.assertIsNone(wire.refusal("state.get"))
        self.assertEqual("answered", records.refusal_of(wire, "state.get"))
        reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "data": {"code": "forbidden", "seq_at": 1}}}
        self.assertEqual("forbidden", api.Refused(reply).code)

    def test_a_silent_server_is_a_failed_call_not_a_pass(self):
        folder = tempfile.mkdtemp(prefix="stub", dir="/tmp")
        path = os.path.join(folder, "o.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(lambda: (listener.close(), os.unlink(path), os.rmdir(folder)))
        wire = api.Wire(path, timeout=0.5)
        self.addCleanup(wire.close)
        self.assertEqual("no_reply", wire.refusal("state.get"))


class InertCoreTests(Scene):
    def test_the_handshake_check_fails_on_a_core_that_has_no_gate_no_version_and_no_token_check(self):
        self.stub_scene("core_api")
        self.assertFalse(run_check(api.api5_handshake_version_and_gate))

    def test_the_framing_check_fails_on_a_core_that_answers_no_parse_error(self):
        self.stub_scene("core_api")
        self.assertFalse(run_check(api.api4_framing_json_rpc_lines_parse_error_and_oversize))

    def test_the_limits_check_fails_on_a_core_that_caps_nothing(self):
        self.stub_scene("core_api")
        self.assertFalse(run_check(api.api24_request_string_method_time_connection_and_subscription_caps))

    def test_the_scope_check_fails_on_a_core_that_forbids_nothing(self):
        self.stub_scene("core_records", usage={"input": 1, "output": 1})
        self.assertFalse(run_check(records.api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden))

    def test_the_owner_wall_and_decision_and_authority_checks_fail_on_a_core_that_refuses_nothing(self):
        self.stub_scene("core_records", usage={"input": 1, "output": 1})
        for function in (records.api22_walled_actions_are_refused_even_with_an_owner_token,
                         records.api20_only_the_owner_role_answers_a_decision,
                         records.term13_authority_is_decided_by_the_core_not_by_what_the_client_says):
            with self.subTest(function.__name__):
                self.assertFalse(run_check(function))


if __name__ == "__main__":
    unittest.main()
