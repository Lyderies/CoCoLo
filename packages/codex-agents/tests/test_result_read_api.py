from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests.learning_support import assignment, result
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_project_history import PROJECT_A, PROJECT_B, binding, terminal
from tests.test_project_results import KEY

from codex_agents.bridge import _fenced_result
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_read_api import (
    MAX_RESULT_READ_FRAME_BYTES, RESULT_CHUNK_BYTES, ResultReadClient,
    decode_result_read_request, decode_result_read_response, encode_result_read_request,
    result_read_request, result_read_request_digest,
)
from codex_agents.result_read_service import ResultReadAuthorization, ResultReadService
from codex_agents.service_transport import ServiceTransportError

RUNTIME = binding().runtime_digest
CONTEXT = object()  # A fixture-only server object, never native caller proof.

SERVER_FIXTURE = r'''
import json,select,socket,sys,time
from pathlib import Path
from codex_agents.history_contracts import AttemptBinding
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES,ResultCapacity
from codex_agents.result_read_api import result_read_request_digest
from codex_agents.result_read_service import ResultReadAuthorization,ResultReadService
directory,endpoint,binding_json,mode=sys.argv[1:]
binding=AttemptBinding(**json.loads(binding_json))
token=object()
def authorize(command,context):
 if context is not token or command.binding!=binding: return None
 return ResultReadAuthorization(binding,command.result_digest,result_read_request_digest(command))
def open_store():
 return ProjectHistory(Path(directory),binding.project_id,result_key=b'r'*32,
  result_capacity=ResultCapacity(8,3*MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1000)
service=ResultReadService(binding.project_id,binding.runtime_digest,open_store,authorizer=authorize)
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
listener.bind(endpoint);listener.listen(4);listener.settimeout(0.1)
print('ready',flush=True)
count=0
try:
 while True:
  if select.select([sys.stdin],[],[],0)[0] and sys.stdin.readline().strip()=='stop': break
  try: connection,_=listener.accept()
  except TimeoutError: continue
  with connection:
   deadline=time.monotonic()+1
   raw=bytearray()
   while b'\n' not in raw and len(raw)<16384:
    connection.settimeout(max(0.001,deadline-time.monotonic()))
    chunk=connection.recv(min(8192,16384-len(raw)))
    if not chunk: break
    raw.extend(chunk)
    if time.monotonic()>=deadline: break
   reply=service.handle(bytes(raw).split(b'\n',1)[0],token)
   count+=1
   if mode=='drop' and count==1:
    print('dropped',flush=True)
    continue
   connection.sendall(reply+b'\n')
finally:
 listener.close();service.close();Path(endpoint).unlink(missing_ok=True)
'''


def fixture_authorizer(command, context):
    if context is not CONTEXT or command.binding != binding():
        return None
    return ResultReadAuthorization(command.binding, command.result_digest, result_read_request_digest(command))


class ResultReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / "history"
        self.directory.mkdir(mode=0o700)
        self.now = 1000
        self.capacity = ResultCapacity(8, 3 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.opened = []
        self.prepared = 0
        self.service = self.new_service()
        self.client = ResultReadClient(self.base / "read.sock", timeout=10)
        self.body, self.digest = self.prepare()

    def open_store(self):
        return ProjectHistory(self.directory, PROJECT_A, result_key=KEY, result_capacity=self.capacity,
                              result_clock_ms=lambda: self.now)

    def factory(self):
        self.opened.append(True)
        return self.open_store()

    def new_service(self, **changes):
        options = {"authorizer": fixture_authorizer}
        options.update(changes)
        service = ResultReadService(PROJECT_A, RUNTIME, self.factory, **options)
        self.addCleanup(service.close)
        return service

    def prepare(self, *, size=None, kind="legacy"):
        if self.prepared:
            self.service.close()
            self.directory = self.base / f"prepared-{self.prepared}"
            self.directory.mkdir(mode=0o700)
            self.service = self.new_service()
        self.prepared += 1
        value = result()
        value["summary"] = "PRIVATE-READ-RESULT-CANARY"
        if kind == "negative":
            value.update(execution_state="completed", semantic_verdict="not-ready", admission_state="unverified",
                         retry={"attempt": 1, "retry_of_assignment_id": None})
            value["evidence"][0]["outcome"] = "fail"
        elif kind == "fenced":
            value = _fenced_result(assignment(), "timed_out", "timed_out")
        if size is not None:
            value["summary"] = ""
            value["summary"] = "x" * (size - len(canonical_json(value)))
        body = canonical_json(value)
        record = json.loads(terminal())
        record.update(result_digest=keyed_digest(KEY, body), fenced=kind == "fenced", reviewed=True,
                      terminal_state="timed_out" if kind == "fenced" else None,
                      result_validation_reason_code=None)
        with closing(self.open_store()) as store:
            store.reserve_result(binding(), assignment(), hard_deadline_at_ms=2000)
            store.commit_result(canonical_json(record), binding(), assignment(), body)
        return body, record["result_digest"]

    def call(self, command=None, *, context=CONTEXT, service=None):
        command = command or result_read_request(binding(), self.digest)
        raw = (service or self.service).handle(encode_result_read_request(command), context)
        return decode_result_read_response(raw, command)

    def wire(self, path, raw, **options):
        self.assertEqual(MAX_RESULT_READ_FRAME_BYTES, options["request_limit"])
        self.assertEqual(MAX_RESULT_READ_FRAME_BYTES, options["response_limit"])
        self.assertLessEqual(len(raw) + 1, MAX_RESULT_READ_FRAME_BYTES)
        reply = self.service.handle(raw, CONTEXT)
        self.assertLessEqual(len(reply) + 1, MAX_RESULT_READ_FRAME_BYTES)
        return reply

    def test_exact_small_body_and_terminal_metadata_are_read_without_write_authority(self):
        chunk = self.call()
        self.assertEqual(self.body, chunk.data)
        self.assertEqual((len(self.body), self.digest, "dispatch-1", 121_000),
                         (chunk.body_size, chunk.result_digest, chunk.terminal_record_id, chunk.expires_at_ms))
        self.assertEqual(1, len(self.opened))
        self.assertEqual(self.body, self.call().data)
        self.assertFalse(hasattr(self.client, "commit_result"))

    def test_full_one_mib_is_exactly_128_bounded_exchanges(self):
        self.body, self.digest = self.prepare(size=MAX_RESULT_BODY_BYTES)
        with mock.patch("codex_agents.result_read_api.exchange_frame", side_effect=self.wire) as exchange:
            actual = self.client.read_result(binding(), self.digest)
        self.assertEqual(self.body, actual)
        self.assertEqual(128, exchange.call_count)
        self.assertEqual(list(range(0, MAX_RESULT_BODY_BYTES, RESULT_CHUNK_BYTES)),
                         [decode_result_read_request(call.args[1]).offset for call in exchange.call_args_list])
        self.assertEqual(1, len(self.opened))

    def test_negative_and_host_fenced_bodies_remain_unchanged(self):
        # Separate durable stores avoid reusing the same result business identity.
        for kind in ("negative", "fenced"):
            with self.subTest(kind=kind):
                self.body, self.digest = self.prepare(kind=kind)
                with mock.patch("codex_agents.result_read_api.exchange_frame", side_effect=self.wire):
                    self.assertEqual(self.body, self.client.read_result(binding(), self.digest))

    def test_default_denial_forged_role_and_wrong_project_never_open_history(self):
        default = self.new_service(authorizer=None)
        for service, context, command in (
            (default, CONTEXT, result_read_request(binding(), self.digest)),
            (self.service, {"role": "primary", "authorized": True}, result_read_request(binding(), self.digest)),
            (self.service, CONTEXT, result_read_request(replace(binding(), project_id=PROJECT_B), self.digest)),
            (self.service, CONTEXT, result_read_request(replace(binding(), runtime_digest="b" * 64), self.digest)),
        ):
            with self.subTest(context=context), self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.call(command, context=context, service=service)
        self.assertEqual([], self.opened)

    def test_every_attempt_binding_field_is_checked_before_open(self):
        for field, value in vars(binding()).items():
            changed = (value + 1 if type(value) is int else PROJECT_B if field == "project_id" else
                       "b" * 64 if field.endswith("digest") else "orc_" + "b" * 48 if field == "orchestration_handle"
                       else "hrn_" + "b" * 48 if field == "run_handle" else "different")
            command = result_read_request(replace(binding(), **{field: changed}), self.digest)
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.call(command)
        self.assertEqual([], self.opened)

    def test_stale_or_mutated_authorizer_output_is_denied_before_open(self):
        for field, change in (("request_digest", "b" * 64), ("result_digest", "b" * 64),
                              ("binding", replace(binding(), attempt=2))):
            def authorize(command, context):
                return replace(fixture_authorizer(command, context), **{field: change})
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.call(service=self.new_service(authorizer=authorize))
        self.assertEqual([], self.opened)

    def test_authorizer_cannot_mutate_request_then_obtain_a_changed_target(self):
        def mutate(command, context):
            authorization = fixture_authorizer(command, context)
            object.__setattr__(command, "binding", replace(binding(), attempt=2))
            return authorization
        service = self.new_service(authorizer=mutate)
        command = result_read_request(binding(), self.digest)
        raw = service.handle(encode_result_read_request(command), CONTEXT)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            decode_result_read_response(raw, command)
        self.assertNotIn(b"PRIVATE-READ", raw)
        self.assertEqual([], self.opened)

    def test_revocation_after_body_io_returns_only_a_fixed_denial(self):
        count = 0
        def revoke(command, context):
            nonlocal count
            count += 1
            return fixture_authorizer(command, context) if count == 1 else None
        command = result_read_request(binding(), self.digest)
        raw = self.new_service(authorizer=revoke).handle(encode_result_read_request(command), CONTEXT)
        self.assertEqual(2, count)
        self.assertIsNone(json.loads(raw)["result"])
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            decode_result_read_response(raw, command)

    def test_expiry_during_final_authorization_never_returns_cached_bytes(self):
        count = 0
        def expire(command, context):
            nonlocal count
            count += 1
            if count == 2:
                self.now = 121_000
            return fixture_authorizer(command, context)
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.call(service=self.new_service(authorizer=expire))
        self.assertEqual([], list(self.directory.glob(".result-*")))

    def test_expiry_during_body_read_never_returns_cached_bytes(self):
        original = ProjectHistory.read_result
        def expire(store, target):
            body = original(store, target)
            self.now = 121_000
            return body
        with mock.patch.object(ProjectHistory, "read_result", expire), \
                self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.call()

    def test_missing_corrupt_expired_and_wrong_digest_do_not_return_a_body(self):
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call(result_read_request(binding(), "b" * 64))
        artifact = next(self.directory.glob(".result-*.json"))
        artifact.write_bytes(b"PRIVATE-CORRUPT-CANARY")
        command = result_read_request(binding(), self.digest)
        raw = self.service.handle(encode_result_read_request(command), CONTEXT)
        self.assertIsNone(json.loads(raw)["result"])
        self.assertNotIn(b"PRIVATE-CORRUPT", raw)
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            decode_result_read_response(raw, command)

    def test_unprepared_and_past_end_offsets_fail_without_a_partial_success(self):
        with self.assertRaisesRegex(HistoryError, "history_invalid_request"):
            self.call(result_read_request(binding(), self.digest, RESULT_CHUNK_BYTES))
        self.service.close()
        self.directory = self.base / "reserved"
        self.directory.mkdir(mode=0o700)
        with closing(self.open_store()) as store:
            store.reserve_result(binding(), assignment(), hard_deadline_at_ms=2000)
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.call(service=self.new_service())

    def test_request_schema_rejects_producer_methods_paths_and_bad_offsets(self):
        valid = json.loads(encode_result_read_request(result_read_request(binding(), self.digest)))
        for field, value in (("operation", "result.publish"), ("protocol_version", True),
                              ("offset", True), ("offset", 1), ("offset", -1),
                              ("offset", MAX_RESULT_BODY_BYTES), ("request_id", "changed"),
                              ("path", "/private/arbitrary"), ("producer", "host")):
            data = {**valid, field: value}
            with self.subTest(field=field, value=value), self.assertRaisesRegex(HistoryError, "history_invalid_request"):
                decode_result_read_request(canonical_json(data))
        with self.assertRaisesRegex(HistoryError, "history_invalid_request"):
            decode_result_read_request(b'{"protocol_version":1,"protocol_version":1}')
        self.assertEqual([], self.opened)

    def test_malformed_oversized_and_private_errors_have_only_fixed_codes(self):
        for raw in (b"PRIVATE-INVALID-CANARY", b"\xff", b"[" * 1000,
                    b"x" * MAX_RESULT_READ_FRAME_BYTES):
            reply = json.loads(self.service.handle(raw, CONTEXT))
            self.assertEqual("history_invalid_request", reply["reason_code"])
            self.assertIsNone(reply["binding"])
            self.assertIsNone(reply["result"])
            with self.assertRaises(HistoryError) as caught:
                decode_result_read_request(raw)
            self.assertIsNone(caught.exception.__context__)
        self.assertEqual([], self.opened)

    def test_response_codec_rejects_every_scope_or_chunk_mismatch(self):
        command = result_read_request(binding(), self.digest)
        valid = json.loads(self.service.handle(encode_result_read_request(command), CONTEXT))
        mutations = []
        for field, value in (("request_id", "other"), ("request_digest", "b" * 64),
                              ("protocol_version", True), ("ready", 1), ("extra", True)):
            mutations.append({**valid, field: value})
        for field, value in (("attempt", True), ("supervisor_generation", True), ("claim_id", "other")):
            mutations.append({**valid, "binding": {**valid["binding"], field: value}})
        for field, value in (("offset", True), ("offset", RESULT_CHUNK_BYTES), ("body_size", True),
                              ("body_size", MAX_RESULT_BODY_BYTES + 1), ("result_digest", "b" * 64),
                              ("terminal_record_id", ""), ("expires_at_ms", True),
                              ("data_base64", "!!!!"), ("data_base64", ""), ("extra", True)):
            mutations.append({**valid, "result": {**valid["result"], field: value}})
        for value in mutations:
            with self.subTest(value=value), self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_result_read_response(canonical_json(value), command)

    def test_whole_read_rejects_changed_terminal_size_or_retention_between_chunks(self):
        self.body, self.digest = self.prepare(size=20_000)
        for field, value in (("terminal_record_id", "changed"), ("expires_at_ms", 122_000), ("body_size", 21_000)):
            def change(path, raw, **options):
                reply = json.loads(self.wire(path, raw, **options))
                if decode_result_read_request(raw).offset:
                    reply["result"][field] = value
                return canonical_json(reply)
            with self.subTest(field=field), mock.patch("codex_agents.result_read_api.exchange_frame", side_effect=change) as wire:
                with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                    self.client.read_result(binding(), self.digest)
                self.assertEqual(2, wire.call_count)

    def test_retirement_between_chunks_discards_the_partial_whole_read(self):
        self.body, self.digest = self.prepare(size=20_000)
        def retire(path, raw, **options):
            command = decode_result_read_request(raw)
            if command.offset:
                self.service._store.retire_result(binding())
            return self.wire(path, raw, **options)
        with mock.patch("codex_agents.result_read_api.exchange_frame", side_effect=retire) as wire, \
                self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.client.read_result(binding(), self.digest)
        self.assertEqual(2, wire.call_count)

    def test_all_chunks_share_one_deadline_and_no_late_body_is_returned(self):
        self.body, self.digest = self.prepare(size=20_000)
        clock = [0.0]
        timeouts = []
        def delayed(path, raw, **options):
            timeouts.append(options["timeout"])
            reply = self.wire(path, raw, **options)
            clock[0] += 0.4
            return reply
        client = ResultReadClient(self.base / "read.sock", timeout=1)
        with mock.patch("codex_agents.result_read_api.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                mock.patch("codex_agents.result_read_api.exchange_frame", side_effect=delayed), \
                self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            client.read_result(binding(), self.digest)
        self.assertEqual(3, len(timeouts))
        self.assertEqual([1, 0.6], timeouts[:2])
        self.assertAlmostEqual(0.2, timeouts[2])

    def test_lost_response_is_not_retried_or_translated_into_a_write(self):
        with mock.patch("codex_agents.result_read_api.exchange_frame",
                        side_effect=ServiceTransportError("deadline_exceeded")) as wire, \
                self.assertRaisesRegex(HistoryError, "history_service_unavailable") as caught:
            self.client.read_result(binding(), self.digest)
        self.assertEqual(1, wire.call_count)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual([], self.opened)

    def test_wrong_thread_including_reused_numeric_identity_cannot_open_or_close(self):
        replies = []
        def foreign():
            replies.append(json.loads(self.service.handle(encode_result_read_request(
                result_read_request(binding(), self.digest)), CONTEXT)))
            try:
                self.service.close()
            except HistoryError as exc:
                replies.append(exc.reason_code)
        owner_ident = threading.get_ident()
        alias = SimpleNamespace(get_ident=lambda: owner_ident, current_thread=threading.current_thread)
        with mock.patch("codex_agents.result_read_service.threading", alias):
            worker = threading.Thread(target=foreign)
            worker.start()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual("history_wrong_owner", replies[0]["reason_code"])
        self.assertEqual("history_wrong_owner", replies[1])
        self.assertEqual([], self.opened)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_forked_handler_cannot_read_or_close_parent_store(self):
        self.call()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                reply = json.loads(self.service.handle(encode_result_read_request(
                    result_read_request(binding(), self.digest)), CONTEXT))
                try:
                    self.service.close()
                    closed = True
                except HistoryError as exc:
                    closed = exc.reason_code
                os.write(write_fd, canonical_json({"reason": reply["reason_code"], "closed": closed}))
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            if not select.select([read_fd], [], [], 5)[0]:
                os.kill(pid, signal.SIGKILL)
                self.fail("owned fork fixture did not reply")
            data = json.loads(os.read(read_fd, 4096))
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        self.assertEqual({"reason": "history_wrong_owner", "closed": "history_wrong_owner"}, data)
        self.assertEqual(self.body, self.call().data)

    def test_maximal_closed_envelope_still_fits_16_kib(self):
        values = binding().as_data()
        for field in ("primary_session_id", "supervisor_instance_id", "run_id", "claim_id"):
            values[field] = "z" * 160
        values["supervisor_generation"] = (1 << 63) - 1
        values["attempt"] = (1 << 31) - 1
        command = result_read_request(AttemptBinding(**values), "f" * 64, MAX_RESULT_BODY_BYTES - RESULT_CHUNK_BYTES)
        reply = canonical_json({
            "protocol_version": 1, "request_id": command.request_id,
            "request_digest": result_read_request_digest(command), "binding": command.binding.as_data(),
            "ready": True, "reason_code": None, "result": {
                "result_digest": command.result_digest, "body_size": MAX_RESULT_BODY_BYTES,
                "terminal_record_id": "z" * 160, "expires_at_ms": (1 << 63) - 1,
                "offset": command.offset, "data_base64": base64.b64encode(b"\xff" * RESULT_CHUNK_BYTES).decode("ascii"),
            },
        })
        self.assertLessEqual(len(encode_result_read_request(command)) + 1, MAX_RESULT_READ_FRAME_BYTES)
        self.assertLessEqual(len(reply) + 1, MAX_RESULT_READ_FRAME_BYTES)
        self.assertEqual(RESULT_CHUNK_BYTES, len(decode_result_read_response(reply, command).data))

    def test_client_import_and_unavailable_endpoint_never_load_sqlite_or_an_owner(self):
        script = r'''
import importlib.abc,json,sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name in {'sqlite3','_sqlite3','codex_agents.learning_store','codex_agents.project_history','codex_agents.result_read_service'}:
   raise AssertionError('persistence import')
sys.meta_path.insert(0,Guard())
from codex_agents.history_contracts import AttemptBinding,HistoryError
from codex_agents.result_read_api import ResultReadClient
data=json.loads(sys.argv[2])
try: ResultReadClient(Path(sys.argv[1]),timeout=0.1).read_result(AttemptBinding(**data),'a'*64)
except HistoryError as exc: print(json.dumps({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules}))
'''
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(self.base / "missing.sock"),
                                    json.dumps(binding().as_data())], cwd=ORCHESTRATION_ROOT,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"reason": "history_service_unavailable", "sqlite_loaded": False}, json.loads(completed.stdout))

    def test_actual_socket_lost_reply_and_owner_restart_keep_exact_retained_body(self):
        self.body, self.digest = self.prepare(size=20_000)
        endpoint = self.base / "read.sock"

        def start(mode):
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-S", "-c",
                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + SERVER_FIXTURE,
                 str(self.directory), str(endpoint), json.dumps(binding().as_data()), mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            def stop():
                if process.poll() is None:
                    try:
                        output, error = process.communicate("stop\n", timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        output, error = process.communicate(timeout=5)
                else:
                    output, error = process.communicate(timeout=5)
                return output, error
            self.addCleanup(stop)
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "owned fixture startup timeout")
            self.assertEqual("ready", process.stdout.readline().strip())
            return process, stop

        process, stop = start("drop")
        client = ResultReadClient(endpoint, timeout=2)
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            client.read_result(binding(), self.digest)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual("dropped", process.stdout.readline().strip())
        self.assertEqual(self.body, client.read_result(binding(), self.digest))
        _, error = stop()
        self.assertEqual(0, process.returncode, error)
        self.assertFalse(endpoint.exists())
        process, stop = start("normal")
        self.assertEqual(self.body, client.read_result(binding(), self.digest))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            client.read_result(binding(), "b" * 64)
        _, error = stop()
        self.assertEqual(0, process.returncode, error)
        with closing(self.open_store()) as store:
            reservation = store.result_reservation(binding())
            self.assertEqual((1000, 121_000), (reservation.prepared_at_ms, reservation.expires_at_ms))
            self.assertEqual(1, len(store.pending_terminals()))


if __name__ == "__main__":
    unittest.main()
