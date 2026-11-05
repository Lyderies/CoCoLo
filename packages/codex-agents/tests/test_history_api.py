from __future__ import annotations

import hashlib
import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.history_api import (
    HISTORY_OPERATIONS, HISTORY_PROTOCOL_VERSION, MAX_HISTORY_REPLY_BYTES,
    HistoryClient, HistoryRequest, decode_history_request, decode_history_response,
    encode_history_request, history_request_digest,
)
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory
from tests.learning_support import event_record, verification_outcome
from tests.test_project_history import PROJECT_A, PROJECT_B, binding, terminal

RUNTIME = "a" * 64


def request(operation="history.record_event", *, project=PROJECT_A, runtime=RUNTIME, payload=None, identity=None):
    payload = {"record": event_record()} if payload is None else payload
    if identity is None:
        identity = payload["record"]["record_id"] if "record" in payload else "read"
    return HistoryRequest(HISTORY_PROTOCOL_VERSION, operation, identity, project, runtime, payload)


def fixture_authorizer(command, context):
    # A fixture-owned Python object, not a JSON claim or native enrollment proof.
    if context is not FIXTURE_CONTEXT:
        return None
    return HistoryAuthorization(command.project_id, command.runtime_digest, history_request_digest(command),
                                "host", HISTORY_OPERATIONS)


FIXTURE_CONTEXT = object()


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base / "history"
        self.directory.mkdir(mode=0o700)
        self.opened = []

        def open_history():
            self.opened.append(True)
            return ProjectHistory(self.directory, PROJECT_A)

        self.factory = open_history
        self.service = HistoryService(PROJECT_A, RUNTIME, self.factory,
                                      authorizer=fixture_authorizer, clock_ms=lambda: 1000)
        self.addCleanup(self.service.close)

    def call(self, command, *, context=FIXTURE_CONTEXT):
        return decode_history_response(self.service.handle(encode_history_request(command), context), command)

    def test_default_denial_wrong_context_project_and_runtime_never_open_a_store(self):
        default = HistoryService(PROJECT_A, RUNTIME, self.factory)
        self.addCleanup(default.close)
        command = request()
        for service, command, context in (
            (default, command, FIXTURE_CONTEXT), (self.service, command, {"role": "host", "authorized": True}),
            (self.service, request(project=PROJECT_B), FIXTURE_CONTEXT),
            (self.service, request(runtime="b" * 64), FIXTURE_CONTEXT),
        ):
            reply = json.loads(service.handle(encode_history_request(command), context))
            self.assertFalse(reply["ready"])
            self.assertEqual("history_not_authorized", reply["reason_code"])
        self.assertEqual([], self.opened)
        self.assertEqual([], list(self.directory.iterdir()))

    def test_request_ids_are_the_immutable_business_identity_and_changed_data_conflicts(self):
        command = request()
        first = self.call(command)
        self.assertEqual(first, self.call(command))
        changed = {**event_record(), "observed_at_ms": 1001}
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call(request(payload={"record": changed}))
        self.assertEqual(1, first["source_sequence"])
        with self.assertRaisesRegex(HistoryError, "history_invalid_request"):
            request(identity="a-different-request-id")
        read = self.call(request("history.read_record", identity="read", payload={"record_id": event_record()["record_id"]}))
        self.assertEqual(canonical_json(event_record()), canonical_json(read["record"]))
        self.assertEqual(first, read["metadata"])

    def test_named_record_operations_round_trip_their_exact_records(self):
        intent = {
            "schema_version": 1, "record_type": "dispatch_intent", "record_id": "intent-api",
            "run_id": "run-api", "bridge_run_id": "brg_" + "a" * 32, "provider": "codex",
            "model": "gpt-6-astra", "effort": "high", "observed_at_ms": 1000, "deadline_at_ms": 2000,
        }
        verifier = {"schema_version": 1, "record_type": "verification_binding", "record_id": "verify-api",
                    "run_id": "run-verifier", "worker_outcome_digest": "b" * 64, "observed_at_ms": 1000}
        client = HistoryClient(self.base / "unused.sock", PROJECT_A, RUNTIME, timeout=1)
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, FIXTURE_CONTEXT)):
            for method, record in ((client.record_intent, intent), (client.record_outcome, verification_outcome()),
                                   (client.bind_verifier, verifier)):
                metadata = method(record)
                self.assertEqual(record["record_id"], metadata["record_id"])
                self.assertEqual(canonical_json(record), canonical_json(client.read_record(record["record_id"])["record"]))
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=AssertionError("transport used")), \
                self.assertRaises(HistoryError):
            client.record_intent(verification_outcome())

    def test_allowed_operation_scope_and_thread_affinity_are_enforced_before_open(self):
        def limited(command, context):
            return HistoryAuthorization(PROJECT_A, RUNTIME, history_request_digest(command), "host",
                                        frozenset({"history.record_event"}))
        service = HistoryService(PROJECT_A, RUNTIME, self.factory, authorizer=limited)
        self.addCleanup(service.close)
        command = request("history.read_record", identity="read", payload={"record_id": "evt_1"})
        self.assertEqual("history_not_authorized", json.loads(service.handle(encode_history_request(command)))["reason_code"])
        replies = []
        worker = threading.Thread(target=lambda: replies.append(service.handle(encode_history_request(request()))))
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual("history_wrong_owner", json.loads(replies[0])["reason_code"])
        self.assertEqual([], self.opened)

    def test_malformed_or_open_fields_have_fixed_errors_without_echo(self):
        valid = json.loads(encode_history_request(request()))
        variants = [b"CANARY-private-input", b"\xff", b'{"x":1,"x":2}', b"{\"x\":NaN}", b"x" * 81_920]
        for field in ("producer", "role", "sql", "method", "path", "pid", "authorized"):
            variants.append(canonical_json({**valid, field: "CANARY-private-input"}))
        for raw in variants:
            with self.subTest(length=len(raw)), self.assertRaises(HistoryError) as caught:
                decode_history_request(raw)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
            result = self.service.handle(raw, FIXTURE_CONTEXT)
            self.assertFalse(json.loads(result)["ready"])
            self.assertNotIn(b"CANARY", result)
        self.assertEqual([], self.opened)

    def test_hook_context_cannot_use_lifecycle_or_read_operations(self):
        def hook(command, context):
            return HistoryAuthorization(PROJECT_A, RUNTIME, history_request_digest(command), "hook", HISTORY_OPERATIONS)
        service = HistoryService(PROJECT_A, RUNTIME, self.factory, authorizer=hook, clock_ms=lambda: 1000)
        self.addCleanup(service.close)
        command = request("history.record_outcome", payload={"record": verification_outcome()})
        self.assertEqual("history_not_authorized", json.loads(service.handle(encode_history_request(command)))["reason_code"])
        command = request("history.read_record", identity="read", payload={"record_id": "evt_1"})
        self.assertEqual("history_not_authorized", json.loads(service.handle(encode_history_request(command)))["reason_code"])
        self.assertEqual([], self.opened)
        command = request()
        value = decode_history_response(service.handle(encode_history_request(command)), command)
        self.assertEqual("hook", value["source"])

    def test_authorization_must_cover_exact_command_and_cannot_mutate_it(self):
        for mode in ("wrong-digest", "mutate"):
            def authorize(command, context):
                digest = history_request_digest(command)
                if mode == "mutate":
                    command.payload["record"]["observed_at_ms"] = 1001
                else:
                    digest = "0" * 64
                return HistoryAuthorization(PROJECT_A, RUNTIME, digest, "host", HISTORY_OPERATIONS)
            service = HistoryService(PROJECT_A, RUNTIME, self.factory, authorizer=authorize)
            self.addCleanup(service.close)
            self.assertEqual("history_not_authorized", json.loads(service.handle(encode_history_request(request())))["reason_code"])
        self.assertEqual([], self.opened)

    def test_terminal_replay_receipt_binding_and_wire_paging_are_bounded(self):
        for number in range(1, 41):
            record = json.loads(terminal(number))
            command = request("history.commit_terminal", identity=binding(number).claim_id,
                              payload={"record": record, "binding": binding(number).as_data()})
            receipt = self.call(command)
            self.assertEqual(receipt, self.call(command))
        seen, after = [], 0
        while True:
            command = request("history.pending_terminals", identity="pending", payload={"after_sequence": after, "limit": 100})
            raw = self.service.handle(encode_history_request(command), FIXTURE_CONTEXT)
            self.assertLessEqual(len(raw) + 1, MAX_HISTORY_REPLY_BYTES)
            page = decode_history_response(raw, command)
            if not page["deliveries"]:
                self.assertIsNone(page["next_after_sequence"])
                break
            seen.extend(value["delivery_sequence"] for value in page["deliveries"])
            after = page["next_after_sequence"]
        self.assertEqual(list(range(1, 41)), seen)
        payload = {"claim_id": "claim-1", "record_content_digest": hashlib.sha256(terminal()).hexdigest(), "receipt_digest": "c" * 64}
        command = request("history.acknowledge_terminal", identity="claim-1", payload=payload)
        self.assertEqual({"recorded": True}, self.call(command))
        self.assertEqual({"recorded": True}, self.call(command))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call(request("history.acknowledge_terminal", identity="claim-1", payload={**payload, "receipt_digest": "d" * 64}))

    def test_terminal_api_preserves_only_exact_trusted_diagnostic_metadata(self):
        record = json.loads(terminal())
        command = request("history.commit_terminal", identity="claim-1", payload={"record": record, "binding": binding().as_data()})
        raw = self.service.handle(encode_history_request(command), FIXTURE_CONTEXT)
        receipt = decode_history_response(raw, command)
        self.assertEqual(record, receipt["dispatch_result"])
        self.assertEqual("invalid_shape", receipt["dispatch_result"]["result_validation_reason_code"])
        self.assertTrue(receipt["dispatch_result"]["fenced"])
        forged = json.loads(raw)
        forged["result"]["dispatch_result"]["result_validation_reason_code"] = "CANARY-REJECTED-CONTENT"
        with self.assertRaisesRegex(HistoryError, "history_invalid_response") as caught:
            decode_history_response(canonical_json(forged), command)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))

    def test_owner_failure_preserves_fixed_reason_and_never_reopens_as_a_fallback(self):
        self.call(request("history.pending_terminals", identity="pending", payload={"after_sequence": 0, "limit": 1}))
        self.service._store._connection.execute("DELETE FROM history_identity")
        with self.assertRaisesRegex(HistoryError, "history_incompatible") as caught:
            self.call(request())
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual([True], self.opened)

    def test_response_shapes_and_scope_cannot_be_forged(self):
        command = request()
        good = json.loads(self.service.handle(encode_history_request(command), FIXTURE_CONTEXT))
        for changes in ({"ready": 1}, {"runtime_digest": "b" * 64}, {"project_id": PROJECT_B},
                        {"request_id": "other"}, {"extra": "CANARY"},
                        {"ready": False, "result": None, "reason_code": "CANARY"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, "history_invalid_response") as caught:
                decode_history_response(canonical_json({**good, **changes}), command)
            self.assertIsNone(caught.exception.__context__)
        bad = {**good, "result": {**good["result"], "content_digest": "f" * 64}}
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(canonical_json(bad), command)
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(b"x" * MAX_HISTORY_REPLY_BYTES, command)

    def test_client_validates_inputs_and_keeps_the_sent_request_immutable(self):
        endpoint = self.base / "unused.sock"
        for timeout in (True, 0, float("nan"), 61):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                HistoryClient(endpoint, PROJECT_A, RUNTIME, timeout=timeout)
        client = HistoryClient(endpoint, PROJECT_A, RUNTIME, timeout=1)
        for value in (None, [], "CANARY"):
            with self.assertRaises(HistoryError):
                client.record_event(value)
        record = event_record()

        def exchange(path, encoded, **options):
            reply = self.service.handle(encoded, FIXTURE_CONTEXT)
            record["observed_at_ms"] = 9999
            return reply
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=exchange) as call:
            metadata = client.record_event(record)
        self.assertEqual(1000, metadata["observed_at_ms"])
        call.assert_called_once()

    def test_client_import_and_unavailable_path_never_load_a_database_owner(self):
        program = """
import importlib.abc,json,sys
from pathlib import Path
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name=='sqlite3' or name in {'codex_agents.learning_store','codex_agents.project_history','codex_agents.history_service'}: raise AssertionError('persistence import')
sys.meta_path.insert(0,Block())
from codex_agents.history_api import HistoryClient
from codex_agents.history_contracts import HistoryError
try: HistoryClient(Path(sys.argv[1]),sys.argv[2],sys.argv[3],timeout=0.01).pending_terminals()
except HistoryError as exc: print(json.dumps({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules}))
else: raise SystemExit(1)
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
             str(self.base / "missing.sock"), PROJECT_A, RUNTIME], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"reason": "history_service_unavailable", "sqlite_loaded": False}, json.loads(completed.stdout))
        self.assertEqual([], list(self.directory.iterdir()))


SERVER_FIXTURE = r'''
import json,select,socket,sys,time
from pathlib import Path
from codex_agents.history_api import HISTORY_OPERATIONS,history_request_digest
from codex_agents.history_service import HistoryService,HistoryAuthorization
from codex_agents.project_history import ProjectHistory
directory,endpoint,project,runtime,mode=sys.argv[1:]
token=object()
def authorize(command,context):
 if context is not token: return None
 return HistoryAuthorization(project,runtime,history_request_digest(command),'host',HISTORY_OPERATIONS)
clock=[1000]
def now():
 clock[0]+=1
 return clock[0]
service=HistoryService(project,runtime,lambda:ProjectHistory(Path(directory),project),authorizer=authorize,clock_ms=now)
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
listener.bind(endpoint);listener.listen(8);listener.settimeout(0.1)
print('ready',flush=True)
count=0
try:
 while True:
  if select.select([sys.stdin],[],[],0)[0]:
   if sys.stdin.readline().strip()=='stop': break
  try: connection,_=listener.accept()
  except TimeoutError: continue
  with connection:
   deadline=time.monotonic()+1
   raw=bytearray()
   while b'\n' not in raw and len(raw)<=81920:
    connection.settimeout(max(0.001,deadline-time.monotonic()))
    chunk=connection.recv(min(8192,81921-len(raw)))
    if not chunk: break
    raw.extend(chunk)
    if time.monotonic()>=deadline: break
   payload=bytes(raw).split(b'\n',1)[0]
   reply=service.handle(payload,token)
   count+=1
   if mode=='drop' and count==1:
    print('dropped',flush=True)
    continue
   connection.sendall(reply+b'\n')
  if mode=='hold' and count==1:
   service._store._connection.execute('BEGIN IMMEDIATE')
   service._store._connection.execute('UPDATE history_identity SET next_source_sequence=next_source_sequence')
   print('holding',flush=True)
   stop=sys.stdin.readline().strip()=='stop'
   service._store._connection.execute('ROLLBACK')
   if stop: break
finally:
 listener.close();service.close();Path(endpoint).unlink(missing_ok=True)
'''


class HistoryProcessApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def start(self, name, project, *, mode="normal"):
        directory, endpoint = self.base / name, self.base / f"{name}.sock"
        directory.mkdir(mode=0o700, exist_ok=True)
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + SERVER_FIXTURE,
             str(directory), str(endpoint), project, RUNTIME, mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def stop():
            if process.poll() is None:
                try:
                    process.communicate("stop\n", timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.communicate(timeout=5)
            else:
                process.communicate(timeout=5)
        self.addCleanup(stop)
        self.assert_line(process, "ready")
        return HistoryClient(endpoint, project, RUNTIME, timeout=1), process, stop

    def assert_line(self, process, expected):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "fixture startup/control timeout")
        self.assertEqual(expected, process.stdout.readline().strip())

    def test_lost_reply_is_not_retried_and_explicit_retry_returns_first_metadata(self):
        client, process, _ = self.start("lost", PROJECT_A, mode="drop")
        record = event_record()
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            client.record_event(record)
        self.assert_line(process, "dropped")
        first = client.record_event(record)
        self.assertEqual(1, first["source_sequence"])
        self.assertEqual(1001, first["ingested_at_ms"])
        self.assertEqual(first, client.record_event(record))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            client.record_event({**record, "observed_at_ms": 1001})
        self.assertEqual(record, client.read_record(record["record_id"])["record"])

    def test_actual_b_api_commits_while_a_owner_holds_a_write_transaction(self):
        a, process, _ = self.start("a", PROJECT_A, mode="hold")
        b, _, _ = self.start("b", PROJECT_B)
        a.record_event(event_record("event-a"))
        self.assert_line(process, "holding")
        reply = b.record_event(event_record("event-b"))
        self.assertEqual(1, reply["source_sequence"])
        self.assertEqual("event-b", b.read_record("event-b")["metadata"]["record_id"])
        process.stdin.write("release\n")
        process.stdin.flush()
        self.assertEqual("event-a", a.read_record("event-a")["metadata"]["record_id"])

    def test_terminal_delivery_and_acknowledgement_replay_survive_process_restart(self):
        client, _, stop = self.start("terminal", PROJECT_A)
        delivery = client.commit_terminal(json.loads(terminal()), binding())
        self.assertEqual(delivery, client.commit_terminal(json.loads(terminal()), binding()))
        self.assertEqual([delivery], client.pending_terminals()["deliveries"])
        self.assertEqual(json.loads(terminal()), delivery["dispatch_result"])
        client.acknowledge_terminal("claim-1", delivery["record_content_digest"], "c" * 64)
        stop()
        client, _, _ = self.start("terminal", PROJECT_A)
        self.assertEqual([], client.pending_terminals()["deliveries"])
        self.assertEqual({"recorded": True}, client.acknowledge_terminal("claim-1", delivery["record_content_digest"], "c" * 64))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            client.acknowledge_terminal("claim-1", delivery["record_content_digest"], "d" * 64)


if __name__ == "__main__":
    unittest.main()
