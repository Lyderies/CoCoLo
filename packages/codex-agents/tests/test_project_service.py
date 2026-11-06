from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.learning_support import assignment, event_record, result
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_project_history import PROJECT_A, PROJECT_B, binding
from tests.test_result_publication import observation
from tests.test_project_results import KEY

from codex_agents.history_api import (
    HISTORY_OPERATIONS, HistoryClient, HistoryRequest, decode_history_response,
    encode_history_request, history_request_digest,
)
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_read_api import (
    ResultReadClient, decode_result_read_response, encode_result_read_request,
    result_read_request, result_read_request_digest,
)
from codex_agents.result_read_service import ResultReadAuthorization, ResultReadService

HISTORY_CONTEXT = object()
READ_CONTEXT = object()  # Distinct fake native contexts, never decoded from JSON.


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / "history"
        self.directory.mkdir(mode=0o700)
        self.capacity = ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.body = canonical_json(result())
        with closing(self.open()) as store:
            store.reserve_result(binding(), assignment(), hard_deadline_at_ms=2000)
            self.delivery = store.publish_result(binding(), assignment(), self.body, observation())
        self.factory = mock.Mock(side_effect=self.open)
        self.service = self.new_service()

    def open(self):
        return ProjectHistory(self.directory, PROJECT_A, result_key=KEY,
                              result_capacity=self.capacity, result_clock_ms=lambda: 1000)

    def history_authorizer(self, request, context):
        if context is not HISTORY_CONTEXT:
            return None
        return HistoryAuthorization(PROJECT_A, binding().runtime_digest, history_request_digest(request),
                                    "host", HISTORY_OPERATIONS)

    def read_authorizer(self, request, context):
        if context is not READ_CONTEXT or request.binding != binding():
            return None
        return ResultReadAuthorization(binding(), request.result_digest, result_read_request_digest(request))

    def new_service(self, **changes):
        options = dict(authorizer=self.history_authorizer, result_authorizer=self.read_authorizer,
                       clock_ms=lambda: 1000)
        options.update(changes)
        service = HistoryService(PROJECT_A, binding().runtime_digest, self.factory, **options)
        self.addCleanup(service.close)
        return service

    def history_request(self, operation="history.read_record", payload=None):
        payload = {"record_id": self.delivery.record_id} if payload is None else payload
        identity = payload["record"]["record_id"] if "record" in payload else "read"
        return HistoryRequest(1, operation, identity, PROJECT_A, binding().runtime_digest, payload)

    def history_call(self, request=None, *, context=HISTORY_CONTEXT, service=None):
        request = request or self.history_request()
        return decode_history_response((service or self.service).handle(encode_history_request(request), context), request)

    def read_call(self, *, context=READ_CONTEXT, service=None):
        request = result_read_request(binding(), self.delivery.result_digest)
        return decode_result_read_response((service or self.service).handle(encode_result_read_request(request), context), request)

    def test_metadata_then_result_and_new_metadata_use_one_owner_connection(self):
        self.assertEqual(self.delivery.result_digest, self.history_call()["record"]["result_digest"])
        self.assertEqual(self.body, self.read_call().data)
        event = event_record("composed-event")
        receipt = self.history_call(self.history_request("history.record_event", {"record": event}))
        self.assertEqual(2, receipt["source_sequence"])
        self.assertEqual(self.body, self.read_call().data)
        self.assertIs(self.service._store, self.service._reader._store)
        self.factory.assert_called_once_with()

    def test_result_then_metadata_and_repeated_reads_use_one_owner_connection(self):
        self.assertEqual(self.body, self.read_call().data)
        self.assertEqual(self.delivery.record_id, self.history_call()["record"]["record_id"])
        self.assertEqual(self.body, self.read_call().data)
        self.factory.assert_called_once_with()

    def test_read_and_history_contexts_cannot_exchange_authority(self):
        for context in (HISTORY_CONTEXT, {"role": "primary", "authorized": True}):
            with self.subTest(context=context), self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.read_call(context=context)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.history_call(context=READ_CONTEXT)
        self.factory.assert_not_called()
        self.read_call()
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.history_call(self.history_request("history.record_event", {"record": event_record("forged-event")}),
                              context=READ_CONTEXT)
        self.factory.assert_called_once_with()
        self.assertEqual(1, len(self.service._store.pending_terminals()))

    def test_each_authorizer_defaults_to_denial_independently_before_open(self):
        for service, operation in (
            (self.new_service(authorizer=None), self.history_call),
            (self.new_service(result_authorizer=None), self.read_call),
        ):
            with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                operation(service=service)
        self.factory.assert_not_called()
        only_read = self.new_service(authorizer=None)
        self.assertEqual(self.body, self.read_call(service=only_read).data)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.history_call(service=only_read)
        self.factory.assert_called_once_with()

    def test_close_releases_common_owner_once_and_refuses_before_routing(self):
        self.read_call()
        self.history_call()
        store = self.service._store
        with mock.patch.object(store, "close", wraps=store.close) as close:
            self.service.close()
            self.service.close()
            close.assert_called_once_with()
        with closing(self.open()) as reopened:
            self.assertEqual(self.body, reopened.read_result(binding()))
        request = self.history_request()
        reply = self.service.handle(encode_history_request(request), HISTORY_CONTEXT)
        self.assertEqual("history_wrong_owner", json.loads(reply)["reason_code"])
        self.assertIsNone(json.loads(reply)["project_id"])
        # Refusal before parsing deliberately carries no validated request labels.
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(reply, request)

    def test_borrowed_reader_close_cannot_close_the_project_owner(self):
        with closing(self.open()) as owner:
            reader = ResultReadService(PROJECT_A, binding().runtime_digest, lambda: owner,
                                       authorizer=self.read_authorizer, close_history=False)
            self.addCleanup(reader.close)
            request = result_read_request(binding(), self.delivery.result_digest)
            self.assertEqual(self.body, decode_result_read_response(
                reader.handle(encode_result_read_request(request), READ_CONTEXT), request).data)
            with mock.patch.object(owner, "close", wraps=owner.close) as close:
                reader.close()
                close.assert_not_called()
                self.assertEqual(self.body, owner.read_result(binding()))

    def test_lifetime_policy_is_trusted_constructor_state_not_json(self):
        for value in (1, 0, None, "false"):
            with self.subTest(value=value), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                ResultReadService(PROJECT_A, binding().runtime_digest, self.factory, close_history=value)
        request = result_read_request(binding(), self.delivery.result_digest)
        value = json.loads(encode_result_read_request(request))
        value["close_history"] = False
        reply = json.loads(self.service.handle(canonical_json(value), READ_CONTEXT))
        self.assertFalse(reply["ready"])
        self.assertEqual("history_invalid_request", reply["reason_code"])
        self.factory.assert_not_called()

    def test_common_factory_fault_has_no_other_owner_or_fallback(self):
        self.factory.side_effect = OSError("PRIVATE-FACTORY-CANARY")
        for operation in (self.history_call, self.read_call):
            with self.assertRaisesRegex(HistoryError, "history_service_unavailable") as caught:
                operation()
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(2, self.factory.call_count)  # One call for each explicit request.
        self.assertIsNone(self.service._store)
        self.assertIsNone(self.service._reader._store)

    def test_misbound_factory_store_is_closed_once_without_body_disclosure(self):
        directory = self.base / "other-project"
        directory.mkdir(mode=0o700)
        other = ProjectHistory(directory, PROJECT_B)
        self.addCleanup(other.close)
        self.factory.side_effect = None
        self.factory.return_value = other
        with mock.patch.object(other, "close", wraps=other.close) as close, \
                self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            self.read_call()
        close.assert_called_once_with()
        self.assertIsNone(self.service._store)
        with closing(ProjectHistory(directory, PROJECT_B)):
            pass

    def test_owner_thread_guard_precedes_routing_or_backend_work(self):
        request = result_read_request(binding(), self.delivery.result_digest)
        replies = []
        def foreign():
            replies.append(json.loads(self.service.handle(encode_result_read_request(request), READ_CONTEXT)))
            try:
                self.service.close()
            except HistoryError as exc:
                replies.append(exc.reason_code)
        with mock.patch.object(self.service, "_result_read", side_effect=AssertionError("foreign routing")) as route:
            worker = threading.Thread(target=foreign)
            worker.start()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual("history_wrong_owner", replies[0]["reason_code"])
        self.assertEqual("history_wrong_owner", replies[1])
        route.assert_not_called()
        self.factory.assert_not_called()

    def test_actual_shared_socket_lost_read_reply_and_restart_keep_metadata_and_body(self):
        script = r'''
import json,select,socket,sys,time
from pathlib import Path
from codex_agents.history_api import HISTORY_OPERATIONS,history_request_digest
from codex_agents.history_contracts import AttemptBinding
from codex_agents.history_service import HistoryService,HistoryAuthorization
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES,ResultCapacity
from codex_agents.result_read_api import result_read_request_digest
from codex_agents.result_read_service import ResultReadAuthorization
directory,endpoint,encoded_binding,expected_digest,mode=sys.argv[1:]
binding=AttemptBinding(**json.loads(encoded_binding));context=object();opens=0
def factory():
 global opens
 opens+=1
 return ProjectHistory(Path(directory),binding.project_id,result_key=b'r'*32,
  result_capacity=ResultCapacity(8,2*MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1000)
def authorize_history(request,token):
 if token is not context:return None
 return HistoryAuthorization(binding.project_id,binding.runtime_digest,history_request_digest(request),'host',HISTORY_OPERATIONS)
def authorize_read(request,token):
 if token is not context or request.binding!=binding or request.result_digest!=expected_digest:return None
 return ResultReadAuthorization(binding,expected_digest,result_read_request_digest(request))
service=HistoryService(binding.project_id,binding.runtime_digest,factory,authorizer=authorize_history,
 result_authorizer=authorize_read,clock_ms=lambda:1000)
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
listener.bind(endpoint);listener.listen(8);listener.settimeout(0.1);dropped=False
print('ready',flush=True)
try:
 while True:
  if select.select([sys.stdin],[],[],0)[0] and sys.stdin.readline().strip()=='stop':break
  try:connection,_=listener.accept()
  except TimeoutError:continue
  with connection:
   deadline=time.monotonic()+1;raw=bytearray()
   while b'\n' not in raw and len(raw)<81920:
    connection.settimeout(max(0.001,deadline-time.monotonic()))
    chunk=connection.recv(min(8192,81920-len(raw)))
    if not chunk:break
    raw.extend(chunk)
    if time.monotonic()>=deadline:break
   payload=bytes(raw).split(b'\n',1)[0];reply=service.handle(payload,context)
   if mode=='drop' and not dropped and json.loads(payload)['operation']=='result.read_chunk':
    dropped=True;print('dropped',flush=True);continue
   connection.sendall(reply+b'\n')
finally:
 listener.close();service.close();Path(endpoint).unlink(missing_ok=True)
 print(json.dumps({'factory_opens':opens}),flush=True)
'''
        endpoint = self.base / "project.sock"
        def start(mode):
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-S", "-c",
                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + script,
                 str(self.directory), str(endpoint), json.dumps(binding().as_data()), self.delivery.result_digest, mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            def stop():
                if process.poll() is None:
                    try:
                        return process.communicate("stop\n", timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                return process.communicate(timeout=5)
            self.addCleanup(stop)
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "owned fixture startup timeout")
            self.assertEqual("ready", process.stdout.readline().strip())
            return process, stop

        metadata = HistoryClient(endpoint, PROJECT_A, binding().runtime_digest, timeout=2)
        reader = ResultReadClient(endpoint, timeout=2)
        process, stop = start("drop")
        first = metadata.read_record(self.delivery.record_id)
        self.assertEqual(self.delivery.result_digest, first["record"]["result_digest"])
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            reader.read_result(binding(), self.delivery.result_digest)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual("dropped", process.stdout.readline().strip())
        self.assertEqual(first, metadata.read_record(self.delivery.record_id))
        self.assertEqual(self.body, reader.read_result(binding(), self.delivery.result_digest))
        receipt = metadata.record_event(event_record("socket-composed-event"))
        self.assertEqual(2, receipt["source_sequence"])
        output, error = stop()
        self.assertEqual(0, process.returncode, error)
        self.assertEqual({"factory_opens": 1}, json.loads(output))
        process, stop = start("normal")
        self.assertEqual(self.body, reader.read_result(binding(), self.delivery.result_digest))
        self.assertEqual(first, metadata.read_record(self.delivery.record_id))
        self.assertEqual(receipt, metadata.read_record("socket-composed-event")["metadata"])
        output, error = stop()
        self.assertEqual(0, process.returncode, error)
        self.assertEqual({"factory_opens": 1}, json.loads(output))


if __name__ == "__main__":
    unittest.main()
