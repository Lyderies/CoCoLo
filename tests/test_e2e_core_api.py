"""T7 S6 (scripts/e2e_checks_core_api.py, scripts/e2e_checks_core_records.py): strictness of the core API checks.

Each test shows a check cannot pass by being inert: against a stub that answers every line with a success (no
refusal, no gate, no scope), the checks that judge refusals must fail ON AN ASSERTION (run_check below counts only a
failed assertion as red; any other exception is a broken stub and is raised, so the stub is complete enough for each
check to reach its first real assertion). The faults must apply exactly once to the installed core's source, the
modules must declare exactly the requirement IDs they own, the fault comparators must demand a green baseline and a
real green-to-red change, one core's failing stop must not skip the others, and the helper wire must tell a refusal
from an answer. Written with the modules (the stub is the red-first picture of an inert core)."""

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
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_checks_core_api as api  # noqa: E402
import e2e_checks_core_records as records  # noqa: E402
import e2e_core  # noqa: E402

CORE_SOURCE = (ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "codex_agents"
               / "core_api.py").read_text()
API_OWNS = {"API-2", "API-4", "API-5", "API-6", "API-24", "API-25"}
RECORDS_OWN = {"API-7", "API-8", "API-9", "API-10", "API-11", "API-12", "API-13", "API-14", "API-15",
               "API-17", "API-18", "API-19", "API-20", "API-21", "API-22", "API-23", "TERM-13"}
# Unchecked on purpose, each `deferred` with its reason in tests/e2e/requirements.json (decision 259).
DEFERRED = {"API-1", "API-3", "API-16", "API-26", "API-27", "TERM-5", "TERM-6", "TERM-7", "TERM-17"}


def declared(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "check":
                    found.update(arg.value for arg in decorator.args if isinstance(arg, ast.Constant))
    return found


def run_check(function) -> bool:
    """True when the check passed; False only when an ASSERTION failed (CheckFailed). Any other exception is not red:
    it means the stub was too thin for the check to reach its assertion, and the test errors."""
    phase = e2e_core.Phase(function.__name__, Path("/nonexistent"))
    try:
        function(phase)
    except e2e_core.CheckFailed:
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
                                                             "scope": "owner", "seq_at": 0, "walled": [], "methods": {},
                                                             "event_kinds": [], "error_codes": [], "notifications": []}}
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


class ScopeStub(Stub):
    """A core that knows the scope matrix and the confirm step but answers every allowed call with `mode`: "ok" (a
    result), "unavailable" (an error that is not an authority error) or "silent" (no reply at all). The honest mode
    is only a control for the other two."""

    SCOPES = {scope: {m for m, _ in methods} for scope, methods in records.ALLOWED.items()}

    def __init__(self, mode):
        self.mode = mode
        super().__init__()

    def serve(self, connection):
        reader = connection.makefile("rb")
        self.connections.append(connection)
        scope = None
        for line in reader:
            request = json.loads(line)
            method, params, rid = request.get("method"), request.get("params") or {}, request.get("id")
            if method == "initialize":
                scope = params.get("token", "").split("-")[0] or None
                reply = {"result": {"protocol": "1.0", "features": ["events", "resume"], "scope": scope or "none", "seq_at": 0}}
            elif scope not in self.SCOPES or method not in self.SCOPES[scope]:
                reply = {"error": {"code": -32000, "data": {"code": "forbidden", "seq_at": 0}}}
            elif self.mode == "silent":
                continue
            elif self.mode == "unavailable":
                reply = {"error": {"code": -32000, "data": {"code": "unavailable", "seq_at": 0}}}
            elif method in api.TWO_STEP and "confirm_id" not in params:
                reply = {"error": {"code": -32000, "data": {"code": "confirm_required", "confirm_id": "c1", "seq_at": 0}}}
            else:
                reply = {"result": {"outcome": "ok", "command_id": "C-0001", "seq_at": 0}}
            try:
                connection.sendall((json.dumps({"jsonrpc": "2.0", "id": rid, **reply}) + "\n").encode())
            except OSError:
                return


class Scene(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # a failing check leaves its client sockets to the collector

    def tearDown(self):
        e2e_core._SCENES.clear()

    def stub_core(self, stub):
        """A core object complete enough for any check to reach its first assertion: a real socket, real record files
        (the producer's, hand written), a wire that opens and initializes a connection, mint, audit and stop."""
        records_dir = Path(tempfile.mkdtemp(prefix="stubrec", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(records_dir, ignore_errors=True))
        records.write_records(records_dir, records.USAGE)

        def wire(token=None, *, init=True, timeout=0.5):
            opened = api.Wire(stub.path, timeout=timeout)
            self.addCleanup(opened.close)
            if init:
                opened.init(token)
            return opened

        def mint_many(pairs):
            return [scope + "-" + "0" * 60 for scope, _ in pairs]
        core = SimpleNamespace(socket=stub.path, audit=lambda name="audit.jsonl": [], records=records_dir, alive=lambda: True,
                               wire=wire, mint_many=mint_many, mint=lambda scope, label="": mint_many([(scope, label)])[0],
                               stop=lambda: None, folder=records_dir, helper=lambda *a, **k: "{}")
        core.start = lambda *a, **k: core
        return core

    def stub_scene(self, key, *, stub=None, **extra):
        stub = stub or Stub()
        self.addCleanup(stub.close)
        core = self.stub_core(stub)
        tokens = {scope: scope + "-" + "0" * 60 for scope in ("read", "agent", "orchestrator", "confirm", "owner")}
        scene = SimpleNamespace(world=None, core=core, read=tokens["read"], tokens=tokens, **extra)
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
        for left in DEFERRED:
            self.assertNotIn(left, owned)
        doc = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())
        marked = {r["id"] for r in doc["requirements"] if r.get("deferred")}
        self.assertTrue(DEFERRED <= marked, DEFERRED - marked)  # each says why it is unchecked

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

    def test_the_fault_comparators_demand_a_green_baseline_and_a_real_green_to_red_change(self):
        for module, name, ids in ((api, "core_api_broken", api.A_IDS), (records, "core_records_broken", records.B_IDS),
                                  (records, "core_records_reads_broken", records.B_IDS),
                                  (api, "core_api_mutation_seq_broken", api.A_IDS)):
            targets = module.FAULT_TARGETS[name]
            spare = next((i for i in ids if i not in targets), None)  # None: every ID is a target of this fault
            with self.subTest(name):
                normal = {"requirements": {i: {"passed": True} for i in ids}}
                red = {"requirements": {i: {"passed": i not in targets} for i in ids}}
                self.assertTrue(module.fault_isolated(normal, red, name)["isolated"])
                self.assertFalse(module.fault_isolated(normal, normal, name)["isolated"])  # no target red
                everything_red = {"requirements": {i: {"passed": False} for i in ids}}
                self.assertFalse(module.fault_isolated(everything_red, everything_red, name)["isolated"])  # all red twice
                self.assertFalse(module.fault_isolated(everything_red, red, name)["isolated"])  # a red baseline
                one_red = {"requirements": {**normal["requirements"], targets[0]: {"passed": False}}}
                self.assertFalse(module.fault_isolated(one_red, red, name)["isolated"])  # a target that was never green
                gone = {"requirements": {i: v for i, v in red["requirements"].items() if i != ids[-1]}}
                self.assertFalse(module.fault_isolated(normal, gone, name)["isolated"])
                if spare is not None:
                    spill = {"requirements": {i: {"passed": i not in (*targets, spare)} for i in ids}}
                    self.assertEqual([spare], module.fault_isolated(normal, spill, name)["changed_outside_targets"])
                    self.assertFalse(module.fault_isolated(normal, spill, name)["isolated"])

    def test_every_id_of_each_module_is_a_target_of_some_fault_or_is_named_as_having_none(self):
        for module, ids, none in ((api, api.A_IDS, ()), (records, records.B_IDS, ())):
            proven = {i for targets in module.FAULT_TARGETS.values() for i in targets}
            self.assertEqual(set(none), set(ids) - proven)


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
        scene = self.stub_scene("core_api")
        scene.world = SimpleNamespace(base=Path("/nonexistent"))
        with mock.patch.object(api, "Core", lambda world, name, **options: scene.core):  # the check starts a core of its own
            self.assertFalse(run_check(api.api24_request_string_method_time_connection_and_subscription_caps))

    def test_the_scope_check_fails_on_a_core_that_forbids_nothing(self):
        scene = self.stub_scene("core_records", usage={"input": 1, "output": 1})
        with mock.patch.object(records, "matrix_core", lambda s: (scene.core, scene.tokens, ["Q-1", "Q-2", "Q-3"], "C-1")):
            self.assertFalse(run_check(records.api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden))

    def test_the_scope_check_needs_a_real_success_from_every_allowed_method(self):
        """A core that forbids correctly but answers every allowed method with `unavailable` (or not at all) was
        accepted by the old matrix (it only refused `forbidden`)."""
        for mode in ("unavailable", "silent"):
            with self.subTest(mode):
                scene = self.stub_scene("core_records", stub=ScopeStub(mode), usage={"input": 1, "output": 1})
                with mock.patch.object(records, "matrix_core", lambda s, scene=scene: (scene.core, scene.tokens, ["Q-1", "Q-2", "Q-3"], "C-1")):
                    self.assertFalse(run_check(records.api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden))

    def test_the_matrix_covers_changes_diff_and_events_subscribe_and_every_allowed_method_has_parameters(self):
        names = {m for m, _ in records.EVERY}
        self.assertTrue({"changes.diff", "events.subscribe"} <= names)
        ids = {"query": "Q-1", "command": "C-1", "reply": "Q-2"}
        for method, params in records.EVERY:
            self.assertIsInstance(params(ids), dict, method)

    def test_the_owner_wall_and_decision_and_authority_checks_fail_on_a_core_that_refuses_nothing(self):
        self.stub_scene("core_records", usage={"input": 1, "output": 1})
        for function in (records.api22_walled_actions_are_refused_even_with_an_owner_token,
                         records.api20_only_the_owner_role_answers_a_decision,
                         records.term13_authority_is_decided_by_the_core_not_by_what_the_client_says):
            with self.subTest(function.__name__):
                self.assertFalse(run_check(function))

    def test_an_exception_that_is_not_an_assertion_is_not_a_red_check(self):
        def broken(phase):
            raise KeyError("a stub too thin for the check")
        with self.assertRaises(KeyError):
            run_check(broken)


class TeardownTests(unittest.TestCase):
    def test_one_core_that_fails_to_stop_does_not_skip_the_others_or_the_world(self):
        stopped = []

        def failing():
            raise OSError("this core cannot stop")
        world = api.CoreWorld.__new__(api.CoreWorld)
        world.cores = [SimpleNamespace(stop=failing), SimpleNamespace(stop=lambda: stopped.append("second")),
                       SimpleNamespace(stop=lambda: stopped.append("third"))]
        with mock.patch.object(api.e2e_support.World, "__exit__", lambda self, *exc: stopped.append("world")):
            with self.assertRaises(api.e2e_support.HarnessError):
                world.__exit__(None, None, None)
        self.assertEqual(["second", "third", "world"], stopped)


if __name__ == "__main__":
    unittest.main()
