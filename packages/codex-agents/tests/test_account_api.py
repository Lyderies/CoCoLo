from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import generate_test_owner_key

from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, MAX_ACCOUNT_FRAME_BYTES,
    AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_request, decode_account_response, encode_account_request,
)
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime

RUNTIME = "a" * 64
TOKEN = object()


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = LearningState(self.base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(lambda: self.ledger.close())
        self.roots = [self.base / name for name in ("project-a", "project-b")]
        for root in self.roots:
            root.mkdir()
        self.projects = [self.ledger.register_project(root, now_ms=i + 1) for i, root in enumerate(self.roots)]
        self.views = AccountOwnerViews(self.ledger)

        def authorize(command, context):
            # Fixture-only server context, not JSON or native enrollment proof.
            if context is not TOKEN or command.project_id not in self.projects:
                return None
            return AccountAuthorization(command.project_id, RUNTIME, account_request_digest(command),
                                        "host", ACCOUNT_OPERATIONS)

        self.authorize = authorize
        self.service = AccountService(RUNTIME, self.views, authorizer=authorize)
        self.addCleanup(self.service.close)

    def request(self, operation="account.health", *, project=None, runtime=RUNTIME, identity="read-1"):
        return AccountRequest(ACCOUNT_PROTOCOL_VERSION, operation, identity, project or self.projects[0], runtime, {})

    def call(self, request, *, service=None, context=TOKEN):
        return decode_account_response((service or self.service).handle(encode_account_request(request), context), request)

    def test_default_denial_wrong_context_runtime_and_project_do_no_backend_work(self):
        default = AccountService(RUNTIME, self.views)
        self.addCleanup(default.close)
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            with mock.patch.object(self.views, "project", side_effect=AssertionError("backend read")), \
                    mock.patch.object(self.views, "owner_key", side_effect=AssertionError("backend read")):
                variants = [(default, self.request(op), TOKEN) for op in ACCOUNT_OPERATIONS]
                variants += [
                    (self.service, self.request(), {"role": "host", "authorized": True}),
                    (self.service, self.request(runtime="b" * 64), TOKEN),
                    (self.service, self.request(project="prj_" + "c" * 32), TOKEN),
                ]
                for service, request, context in variants:
                    with self.assertRaisesRegex(AccountError, "account_not_authorized") as caught:
                        self.call(request, service=service, context=context)
                    self.assertIsNone(caught.exception.__context__)
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual([], statements)

    def test_views_use_one_existing_owner_and_change_no_keys_or_rows(self):
        key = self.state.digest_key()
        changes = self.ledger._connection.total_changes
        history = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")]
        with mock.patch.object(self.state, "recorded_owner_key", side_effect=AssertionError("second DB connection")):
            a = self.call(self.request("account.project"))
            b = self.call(self.request("account.project", project=self.projects[1]))
            owner = self.call(self.request("account.owner_key"))
            self.assertEqual(owner, self.call(self.request("account.owner_key", project=self.projects[1])))
            self.assertEqual({"transport_alive": True, "database_checked": False}, self.call(self.request()))
        self.assertEqual({"project_id": self.projects[0], "status": "active", "created_at_ms": 1}, a)
        self.assertEqual({"project_id": self.projects[1], "status": "active", "created_at_ms": 2}, b)
        self.assertEqual(("ok", 1, 0, None), (owner["state"], owner["sequence"], owner["replacements"], owner["last_replaced_at_ms"]))
        self.assertEqual(changes, self.ledger._connection.total_changes)
        self.assertEqual(history, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")])
        self.assertEqual(key, self.state.digest_key())
        self.assertNotIn("root", canonical_json(a).decode())
        self.service.close()
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.call(self.request())
        self.assertEqual(self.projects[0], self.ledger.project_for_root(self.roots[0]))

    def test_malformed_frames_and_open_fields_are_fixed_and_never_echoed(self):
        data = json.loads(encode_account_request(self.request()))
        variants = [b"CANARY-private", b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * MAX_ACCOUNT_FRAME_BYTES]
        for field in ("role", "producer", "path", "sql", "method", "pid", "no_api_keys"):
            variants.append(canonical_json({**data, field: "CANARY-private"}))
            variants.append(canonical_json({**data, "payload": {field: "CANARY-private"}}))
        for version in (True, 1.0, 2):
            variants.append(canonical_json({**data, "protocol_version": version}))
        for raw in variants:
            with self.subTest(size=len(raw)), self.assertRaises(AccountError) as caught:
                decode_account_request(raw)
            self.assertIsNone(caught.exception.__context__)
            response = self.service.handle(raw, TOKEN)
            self.assertNotIn(b"CANARY", response)
            reply = json.loads(response)
            self.assertFalse(reply["ready"])
            self.assertIsNone(reply["project_id"])
            self.assertIsNone(reply["runtime_digest"])
            self.assertIsNone(reply["request_digest"])

    def test_authorization_is_project_scoped_and_hooks_get_health_only(self):
        def wrong_project(command, context):
            return AccountAuthorization(self.projects[1], RUNTIME, account_request_digest(command), "host", ACCOUNT_OPERATIONS)
        wrong = AccountService(RUNTIME, self.views, authorizer=wrong_project)
        self.addCleanup(wrong.close)
        with self.assertRaisesRegex(AccountError, "account_not_authorized"):
            self.call(self.request(), service=wrong)
        for source in ("hook", "app_server"):
            def authorize(command, context):
                return AccountAuthorization(command.project_id, RUNTIME, account_request_digest(command), source, ACCOUNT_OPERATIONS)
            service = AccountService(RUNTIME, self.views, authorizer=authorize)
            self.addCleanup(service.close)
            self.assertFalse(self.call(self.request(), service=service)["database_checked"])
            for operation in ("account.project", "account.owner_key"):
                with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(self.request(operation), service=service)

    def test_callback_mutation_is_refused_with_original_bound_labels(self):
        def mutate(command, context):
            digest = account_request_digest(command)
            command.payload["sql"] = "CANARY-private"
            return AccountAuthorization(command.project_id, RUNTIME, digest, "host", ACCOUNT_OPERATIONS)
        service = AccountService(RUNTIME, self.views, authorizer=mutate)
        self.addCleanup(service.close)
        with mock.patch.object(self.views, "project", side_effect=AssertionError("backend read")), \
                self.assertRaisesRegex(AccountError, "account_not_authorized"):
            self.call(self.request("account.project"), service=service)

    def test_response_binding_closed_types_and_byte_limits(self):
        request = self.request("account.project")
        response = json.loads(self.service.handle(encode_account_request(request), TOKEN))
        for changed in (self.request("account.owner_key"), self.request("account.project", project=self.projects[1]),
                        self.request("account.project", identity="another-read")):
            with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                decode_account_response(canonical_json(response), changed)
        for field, value in (("protocol_version", True), ("ready", 1), ("request_digest", "0" * 64)):
            with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                decode_account_response(canonical_json({**response, field: value}), request)
        for value in (True, 1.0, -1):
            bad = {**response, "result": {**response["result"], "created_at_ms": value}}
            with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                decode_account_response(canonical_json(bad), request)
        with self.assertRaisesRegex(AccountError, "account_invalid_response"):
            decode_account_response(b" " * MAX_ACCOUNT_FRAME_BYTES + canonical_json(response), request)

    def test_bad_backend_values_and_faults_are_fixed_without_canaries(self):
        request = self.request("account.project")
        for effect in (RuntimeError("CANARY-SQL-path"), None):
            with mock.patch.object(self.views, "project", side_effect=effect, return_value={"CANARY-private": True}):
                raw = self.service.handle(encode_account_request(request), TOKEN)
            self.assertNotIn(b"CANARY", raw)
            with self.assertRaises(AccountError) as caught:
                decode_account_response(raw, request)
            self.assertIn(caught.exception.reason_code, {"account_unavailable", "account_invalid_response"})

    def test_owner_file_mismatch_stays_mismatch_and_no_implicit_enrollment(self):
        request = self.request("account.owner_key")
        first = self.call(request)
        line = self.state.allowed_signers_path.read_bytes()
        try:
            self.state.allowed_signers_path.write_bytes(b"")
            second = self.call(request)
            self.assertEqual("mismatch", second["state"])
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(first["sequence"], second["sequence"])
        finally:
            self.state.allowed_signers_path.write_bytes(line)
        self.assertEqual(first, self.call(request))

    def test_wrong_sql_thread_is_refused_but_health_survives_a_blocked_owner(self):
        replies = []
        request = self.request("account.project")
        worker = threading.Thread(target=lambda: replies.append(self.service.handle(encode_account_request(request), TOKEN)))
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        with self.assertRaisesRegex(AccountError, "account_wrong_owner"):
            decode_account_response(replies[0], request)
        entered, release = threading.Event(), threading.Event()
        health, failures = [], []
        original = self.views.project
        def blocked(project):
            with self.ledger._lock:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("health waited for DB writer")
                return original(project)
        def check_health():
            try:
                if not entered.wait(2):
                    raise AssertionError("owner did not enter")
                health.append(self.call(self.request()))
            except BaseException as exc:
                failures.append(exc)
            finally:
                release.set()
        worker = threading.Thread(target=check_health, daemon=True)
        worker.start()
        try:
            with mock.patch.object(self.views, "project", side_effect=blocked):
                self.assertEqual(self.projects[0], self.call(request)["project_id"])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)
        self.assertEqual([{"transport_alive": True, "database_checked": False}], health)

    def test_forked_handler_has_no_backend_ownership(self):
        if not hasattr(os, "fork"):
            self.skipTest("requires POSIX fork")
        request = self.request()
        reader, writer = os.pipe()
        try:
            # The child must reject its inherited owner BEFORE taking any lock.
            with self.service._front_lock:
                child = os.fork()
                if child == 0:
                    os.close(reader)
                    os.write(writer, self.service.handle(encode_account_request(request), TOKEN))
                    os.close(writer)
                    os._exit(0)
                os.close(writer)
                ready = bool(select.select([reader], [], [], 2)[0])
                if ready:
                    raw = os.read(reader, MAX_ACCOUNT_FRAME_BYTES)
                else:
                    os.kill(child, signal.SIGKILL)
        finally:
            os.close(reader)
            _, status = os.waitpid(child, 0)
        self.assertTrue(ready, "forked handler waited on its inherited lock")
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        with self.assertRaisesRegex(AccountError, "account_wrong_owner"):
            decode_account_response(raw, request)

    def test_client_uses_one_exchange_and_no_retry_on_unavailable(self):
        client = AccountClient(self.base / "unused.sock", self.projects[0], RUNTIME, timeout=1)
        with mock.patch("codex_agents.account_api.exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, TOKEN)) as wire:
            self.assertEqual(self.projects[0], client.project()["project_id"])
            self.assertEqual("ok", client.owner_key()["state"])
        self.assertEqual(2, wire.call_count)
        self.assertTrue(all(call.kwargs["response_limit"] == MAX_ACCOUNT_FRAME_BYTES for call in wire.call_args_list))
        with self.assertRaisesRegex(AccountError, "account_unavailable") as caught:
            client.health()
        self.assertIsNone(caught.exception.__context__)

    def test_client_import_chain_has_no_sqlite_even_when_service_unavailable(self):
        script = r'''
import importlib.abc,json,sys
from pathlib import Path
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='sqlite3' or fullname.startswith('sqlite3.') or fullname in {'codex_agents.learning_store','codex_agents.account_service'}:
   raise AssertionError('persistence import')
sys.meta_path.insert(0,Block())
from codex_agents.account_api import AccountClient,AccountError
try:
 AccountClient(Path(sys.argv[1]),sys.argv[2],sys.argv[3],timeout=.1).project()
except AccountError as exc:
 print(json.dumps({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules}))
'''
        completed = subprocess.run([sys.executable, "-I", "-B", "-S", "-c",
                                    f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + script,
                                    str(self.base / "missing.sock"), self.projects[0], RUNTIME],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        self.assertEqual({"reason": "account_unavailable", "sqlite_loaded": False}, json.loads(completed.stdout))

    def _start_view_server(self, name, *, mode="normal"):
        endpoint = self.base / (name + ".sock")
        self.ledger.close()
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + ACCOUNT_SERVER_FIXTURE,
             str(self.state.root), str(endpoint), RUNTIME, ",".join(self.projects), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stopped = False

        def stop():
            nonlocal stopped
            if stopped:
                return
            stopped = True
            try:
                if process.poll() is None:
                    try:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                self.ledger = LearningLedger(self.state)
        self.addCleanup(stop)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "fixture did not start")
        line = process.stdout.readline().strip()
        self.assertEqual("ready", line, process.stderr.read() if process.poll() is not None else line)
        return endpoint, process, stop

    def _server_line(self, process, expected):
        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
        self.assertEqual(expected, process.stdout.readline().strip())

    def test_real_socket_reads_two_projects_one_owner_and_survive_restart(self):
        key = self.state.digest_key()
        expected = self.call(self.request("account.owner_key"))
        endpoint, process, stop = self._start_view_server("views")
        a = AccountClient(endpoint, self.projects[0], RUNTIME)
        b = AccountClient(endpoint, self.projects[1], RUNTIME)
        self.assertEqual(self.projects[0], a.project()["project_id"])
        self._server_line(process, "seen-1")
        self.assertEqual(self.projects[1], b.project()["project_id"])
        self._server_line(process, "seen-2")
        self.assertEqual(expected, b.owner_key())
        self._server_line(process, "seen-3")
        stop()
        self.assertEqual(key, self.state.digest_key())
        endpoint, process, stop = self._start_view_server("views")
        self.assertEqual(expected, AccountClient(endpoint, self.projects[0], RUNTIME).owner_key())
        self._server_line(process, "seen-1")
        stop()
        self.assertEqual(key, self.state.digest_key())

    def test_dropped_or_partial_read_reply_is_not_automatically_retried(self):
        for mode in ("drop", "partial"):
            endpoint, process, stop = self._start_view_server(mode, mode=mode)
            client = AccountClient(endpoint, self.projects[0], RUNTIME, timeout=.5)
            try:
                # EOF is passed to the closed codec; empty/truncated JSON is invalid.
                with self.assertRaisesRegex(AccountError, "account_invalid_response") as caught:
                    client.project(request_id="same-read")
                self.assertIsNone(caught.exception.__context__)
                self._server_line(process, "seen-1")
                self.assertEqual(self.projects[0], client.project(request_id="same-read")["project_id"])
                self._server_line(process, "seen-2")
            finally:
                stop()


ACCOUNT_SERVER_FIXTURE = r'''
import select,socket,sys,time
from pathlib import Path
from codex_agents.account_api import ACCOUNT_OPERATIONS,account_request_digest
from codex_agents.account_service import AccountAuthorization,AccountOwnerViews,AccountService
from codex_agents.learning_store import LearningLedger,LearningState
state,endpoint,runtime,project_list,mode=sys.argv[1:]
projects=frozenset(project_list.split(','))
token=object()
def authorize(command,context):
 # Disposable harness grants this test-owner context the two known project views.
 # This is deliberately NOT native caller or transport identity proof.
 if context is not token or command.project_id not in projects: return None
 return AccountAuthorization(command.project_id,runtime,account_request_digest(command),'owner',ACCOUNT_OPERATIONS)
ledger=LearningLedger(LearningState(Path(state)))
service=AccountService(runtime,AccountOwnerViews(ledger),authorizer=authorize)
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
listener.bind(endpoint);listener.listen(4);listener.settimeout(.1)
print('ready',flush=True)
count=0
try:
 while True:
  if select.select([sys.stdin],[],[],0)[0] and sys.stdin.readline().strip()=='stop': break
  try: connection,_=listener.accept()
  except TimeoutError: continue
  with connection:
   raw=bytearray();deadline=time.monotonic()+1
   while b'\n' not in raw and len(raw)<=16384:
    connection.settimeout(max(.001,deadline-time.monotonic()))
    chunk=connection.recv(min(8192,16385-len(raw)))
    if not chunk: break
    raw.extend(chunk)
    if time.monotonic()>=deadline: break
   reply=service.handle(bytes(raw).split(b'\n',1)[0],token)
   count+=1;print('seen-'+str(count),flush=True)
   if mode=='drop' and count==1: continue
   if mode=='partial' and count==1:
    connection.sendall(reply[:len(reply)//2])
    continue
   connection.sendall(reply+b'\n')
finally:
 listener.close();service.close();ledger.close();Path(endpoint).unlink(missing_ok=True)
'''
