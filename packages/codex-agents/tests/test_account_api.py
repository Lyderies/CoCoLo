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
from tests.test_owner_key_contracts import public_value

from codex_agents.account_api import (
    ACCOUNT_READ_OPERATIONS as ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, MAX_ACCOUNT_FRAME_BYTES,
    AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_request, decode_account_response, encode_account_request,
)
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.owner_key_contracts import checked_public_key
from codex_agents.service_transport import ServiceTransportError

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
        payload = ({"run_handle": "hrn_" + "0" * 48, "bridge_run_id": "brg_" + "1" * 32}
                   if operation == "account.capacity_status" else {})
        return AccountRequest(ACCOUNT_PROTOCOL_VERSION, operation, identity, project or self.projects[0], runtime, payload)

    def call(self, request, *, service=None, context=TOKEN):
        return decode_account_response((service or self.service).handle(encode_account_request(request), context), request)

    def owner_service(self, *, source="owner", operations=ACCOUNT_OPERATIONS):
        def authorize(request, context):
            if self.authorize(request, context) is None:
                return None
            return AccountAuthorization(request.project_id, RUNTIME, account_request_digest(request), source, operations)
        service = AccountService(RUNTIME, self.views, authorizer=authorize)
        self.addCleanup(service.close)
        return service

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
        with mock.patch.object(self.state, "recorded_owner_key", side_effect=AssertionError("second DB connection")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("second DB connection")):
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

    def test_inactive_project_cannot_read_registration_or_owner_metadata(self):
        self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.projects[0],))
        with mock.patch.object(self.state, "_owner_signer_bytes", side_effect=AssertionError("inactive project read key")):
            for operation in ("account.project", "account.owner_key"):
                with self.assertRaisesRegex(AccountError, "account_project_not_found"):
                    self.call(self.request(operation))
        self.assertFalse(self.call(self.request())["database_checked"])

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
            self.assertIsNone(reply["request_id"])
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

    def test_adapter_digest_runtime_and_operation_limits_are_checked_before_backend(self):
        request = self.request("account.project")
        for changed in ({"runtime_digest": "b" * 64}, {"request_digest": "c" * 64},
                        {"allowed_operations": frozenset({"account.health"})}):
            def authorize(command, context):
                values = {"project_id": command.project_id, "runtime_digest": RUNTIME,
                          "request_digest": account_request_digest(command), "source": "host",
                          "allowed_operations": ACCOUNT_OPERATIONS}
                return AccountAuthorization(**{**values, **changed})
            service = AccountService(RUNTIME, self.views, authorizer=authorize)
            self.addCleanup(service.close)
            with mock.patch.object(self.views, "project", side_effect=AssertionError("backend read")), \
                    self.assertRaisesRegex(AccountError, "account_not_authorized"):
                self.call(request, service=service)

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
        for field, value in (("protocol_version", True), ("ready", 1),
                             ("runtime_digest", "b" * 64), ("request_digest", "0" * 64)):
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

    def test_empty_history_distinguishes_missing_empty_and_damaged_signer_files(self):
        # Separate disposable unconfigured account, not a per-project production owner.
        state = LearningState(self.base / "unconfigured")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(state)
        self.addCleanup(ledger.close)
        project = ledger.register_project(self.roots[0], now_ms=1)
        views = AccountOwnerViews(ledger)
        changes = ledger._connection.total_changes
        expected = {"state": "not_configured", "fingerprint": None, "sequence": 0,
                    "replacements": 0, "last_replaced_at_ms": None}
        path = state.allowed_signers_path
        self.assertEqual(expected, views.owner_key(project))
        path.unlink()
        self.assertEqual(expected, views.owner_key(project))
        for content in (b"CANARY-malformed", b"x" * 4097):
            path.write_bytes(content)
            path.chmod(0o600)
            self.assertEqual({**expected, "state": "mismatch"}, views.owner_key(project))
            path.unlink()
        path.write_bytes(b"")
        path.chmod(0o644)
        self.assertEqual({**expected, "state": "mismatch"}, views.owner_key(project))
        path.unlink()
        path.symlink_to(self.base / "missing-signer")
        self.assertEqual({**expected, "state": "mismatch"}, views.owner_key(project))
        path.unlink()
        path.write_bytes(self.state.allowed_signers_path.read_bytes())
        path.chmod(0o600)
        self.assertEqual({**expected, "state": "mismatch"}, views.owner_key(project))
        self.assertEqual(changes, ledger._connection.total_changes)
        self.assertEqual(0, ledger._connection.execute("SELECT COUNT(*) FROM owner_key_history").fetchone()[0])

    def test_signer_fifo_cannot_stall_the_shared_owner(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("requires POSIX FIFOs")
        for mode in ("fifo", "fifo-eof", "swap-at-open"):
            with self.subTest(mode=mode):
                try:
                    completed = subprocess.run(
                        [sys.executable, "-I", "-B", "-S", "-c",
                         f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + FIFO_OWNER_FIXTURE,
                         str(self.base / mode), str(ORCHESTRATION_ROOT), mode],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, check=False,
                    )
                except subprocess.TimeoutExpired:
                    self.fail("owner view blocked on a signer FIFO")
                self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
                data = json.loads(completed.stdout)
                self.assertEqual("mismatch", data["owner"]["state"])
                self.assertEqual(0, data["owner"]["sequence"])
                self.assertEqual("active", data["project"]["status"])
                self.assertEqual(0, data["history_count"])
                self.assertNotIn(b"CANARY", completed.stdout)

    def test_owner_replacement_is_a_fresh_public_view_through_the_same_connection(self):
        key = self.state.digest_key()
        request = self.request("account.owner_key")
        first = self.call(request)
        directory = self.base / "replacement"
        directory.mkdir()
        _, public = generate_test_owner_key(directory)
        self.ledger.replace_owner_key(public, now_ms=42)
        changes = self.ledger._connection.total_changes
        with mock.patch.object(self.state, "recorded_owner_key", side_effect=AssertionError("second DB connection")):
            second = self.call(request)
        self.assertEqual("ok", second["state"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["sequence"] + 1, second["sequence"])
        self.assertEqual((1, 42), (second["replacements"], second["last_replaced_at_ms"]))
        self.assertEqual(changes, self.ledger._connection.total_changes)
        self.assertEqual(key, self.state.digest_key())

    def test_wrong_sql_thread_is_refused_but_health_survives_a_blocked_owner(self):
        replies = []
        for operation in ("account.project", "account.owner_key"):
            request = self.request(operation)
            worker = threading.Thread(target=lambda: replies.append(self.service.handle(encode_account_request(request), TOKEN)))
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            with self.assertRaisesRegex(AccountError, "account_wrong_owner"):
                decode_account_response(replies[-1], request)
        entered, release = threading.Event(), threading.Event()
        request = self.request("account.project")
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

    def test_reconstructing_views_in_another_thread_cannot_adopt_the_ledger(self):
        for reconstruct in (False, True):
            with self.subTest(reconstruct=reconstruct):
                results = []
                def read():
                    try:
                        if reconstruct:
                            AccountOwnerViews(self.ledger).project(self.projects[0])
                        else:
                            self.views.owner_key(self.projects[0])
                    except AccountError as exc:
                        results.append(exc.reason_code)
                    except BaseException:
                        results.append("unexpected")
                    else:
                        results.append("accepted")
                worker = threading.Thread(target=read, daemon=True)
                with self.ledger._lock:
                    worker.start()
                    worker.join(1)
                    refused_before_lock = not worker.is_alive()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(refused_before_lock, "view reached a foreign owner lock")
                self.assertEqual(["account_wrong_owner"], results)

    def test_thread_equality_cannot_adopt_the_account_connection(self):
        class EqualThread(threading.Thread):
            __hash__ = object.__hash__

            def __eq__(self, other):
                return True

        for reconstruct in (False, True):
            with self.subTest(reconstruct=reconstruct):
                results = []

                def read():
                    try:
                        views = AccountOwnerViews(self.ledger) if reconstruct else self.views
                        views.project(self.projects[0])
                    except AccountError as exc:
                        results.append(exc.reason_code)
                    except AssertionError:
                        results.append("foreign_resource_access")
                    else:
                        results.append("accepted")

                with mock.patch.object(
                    self.state, "harden_database_files", side_effect=AssertionError("foreign resource access"),
                ) as harden:
                    worker = EqualThread(target=read, daemon=True)
                    worker.start()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(["account_wrong_owner"], results)
                harden.assert_not_called()

    def test_reconstructing_views_after_fork_cannot_adopt_the_ledger(self):
        if not hasattr(os, "fork"):
            self.skipTest("requires POSIX fork")
        for reconstruct in (False, True):
            with self.subTest(reconstruct=reconstruct):
                self._fork_owner_read(reconstruct=reconstruct)

    def _fork_owner_read(self, *, reconstruct):
        reader, writer = os.pipe()
        child = None
        raw = None
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.ledger._lock:
                held.set()
                release.wait(10)
        # An RLock held by the forking thread is reentrant in its child. Another
        # thread must hold it to expose any lock acquisition before the guard.
        locker = threading.Thread(target=hold, daemon=True)
        locker.start()
        try:
            self.assertTrue(held.wait(3))
            child = os.fork()
            if child == 0:
                os.close(reader)
                try:
                    if reconstruct:
                        AccountOwnerViews(self.ledger).project(self.projects[0])
                    else:
                        self.views.owner_key(self.projects[0])
                except AccountError as exc:
                    message = exc.reason_code
                except BaseException:
                    message = "unexpected"
                else:
                    message = "accepted"
                os.write(writer, message.encode("ascii"))
                os.close(writer)
                os._exit(0)
            os.close(writer)
            writer = None
            ready = bool(select.select([reader], [], [], 2)[0])
            if ready:
                raw = os.read(reader, MAX_ACCOUNT_FRAME_BYTES)
            else:
                os.kill(child, signal.SIGKILL)
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)
            if child is not None:
                _, status = os.waitpid(child, 0)
            release.set()
            locker.join(5)
        self.assertFalse(locker.is_alive())
        self.assertTrue(ready, "reconstructed view waited on an inherited owner lock")
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        self.assertEqual(b"account_wrong_owner", raw)

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
        with mock.patch("codex_agents.account_api.exchange_frame", side_effect=ServiceTransportError("transport_unavailable")) as wire:
            with self.assertRaisesRegex(AccountError, "account_unavailable") as caught:
                client.health()
        wire.assert_called_once()
        self.assertIsNone(caught.exception.__context__)
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
client=AccountClient(Path(sys.argv[1]),sys.argv[2],sys.argv[3],timeout=.1)
observed=[]
for read in (client.project,client.owner_public_key):
 try: read()
 except AccountError as exc:
  observed.append({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules})
print(json.dumps(observed))
'''
        completed = subprocess.run([sys.executable, "-I", "-B", "-S", "-c",
                                    f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + script,
                                    str(self.base / "missing.sock"), self.projects[0], RUNTIME],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        self.assertEqual([{"reason": "account_unavailable", "sqlite_loaded": False}] * 2, json.loads(completed.stdout))

    def test_public_owner_view_requires_owner_source_and_operation_before_backend(self):
        request = self.request("account.owner_public_key")
        with mock.patch.object(self.views, "owner_public_key", side_effect=AssertionError("unauthorized key read")):
            for source in ("host", "hook", "app_server"):
                with self.subTest(source=source), self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(request, service=self.owner_service(source=source))
            with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                self.call(request, service=self.owner_service(operations=frozenset({"account.owner_key"})))
            with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                self.call(request, service=self.owner_service(), context={"source": "owner", "authorized": True})

    def test_public_owner_grant_cannot_be_reused_for_changed_scope_or_request(self):
        original = self.request("account.owner_public_key")
        authorization = AccountAuthorization(self.projects[0], RUNTIME, account_request_digest(original),
                                             "owner", frozenset({"account.owner_public_key"}))
        service = AccountService(RUNTIME, self.views, authorizer=lambda request, context: authorization)
        self.addCleanup(service.close)
        with mock.patch.object(self.views, "owner_public_key", side_effect=AssertionError("wrong bound read")):
            for changed in (self.request("account.owner_public_key", project=self.projects[1]),
                            self.request("account.owner_public_key", identity="another-read"),
                            self.request("account.owner_public_key", runtime="b" * 64)):
                with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(changed, service=service)

    def test_inactive_project_public_key_read_refuses_before_signer_access(self):
        self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.projects[0],))
        with mock.patch.object(self.state, "_owner_signer_bytes", side_effect=AssertionError("inactive project read key")):
            with self.assertRaisesRegex(AccountError, "account_project_not_found"):
                self.call(self.request("account.owner_public_key"), service=self.owner_service())

    def test_public_owner_view_returns_exact_key_and_generation_without_writes(self):
        line = self.state.allowed_signers_path.read_bytes()
        history = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")]
        key = self.state.digest_key()
        changes = self.ledger._connection.total_changes
        service = self.owner_service()
        with mock.patch("sqlite3.connect", side_effect=AssertionError("second connection")), mock.patch.object(
            self.state, "recorded_owner_key", side_effect=AssertionError("second connection")
        ):
            first = self.call(self.request("account.owner_public_key"), service=service)
            second = self.call(self.request("account.owner_public_key", project=self.projects[1]), service=service)
            legacy = self.call(self.request("account.owner_key"))
        self.assertEqual(first, second)
        self.assertEqual(line, ("codex-agents-owner " + first["public_key"] + "\n").encode("ascii"))
        self.assertEqual(legacy, {name: value for name, value in first.items() if name != "public_key"})
        self.assertEqual(("ok", 1, 0, None), tuple(first[name] for name in
                         ("state", "sequence", "replacements", "last_replaced_at_ms")))
        self.assertEqual(history, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")])
        self.assertEqual(changes, self.ledger._connection.total_changes)
        self.assertEqual(key, self.state.digest_key())
        self.assertEqual(line, self.state.allowed_signers_path.read_bytes())

    def test_public_owner_view_rechecks_replacement_with_the_same_read_identity(self):
        service = self.owner_service()
        request = self.request("account.owner_public_key")
        first = self.call(request, service=service)
        directory = self.base / "new-public-owner"
        directory.mkdir()
        _, public = generate_test_owner_key(directory)
        self.ledger.replace_owner_key(public, now_ms=42)
        second = self.call(request, service=service)
        self.assertEqual(first["sequence"] + 1, second["sequence"])
        self.assertNotEqual(first["public_key"], second["public_key"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual((1, 42), (second["replacements"], second["last_replaced_at_ms"]))
        self.assertEqual(self.call(self.request("account.owner_key"))["fingerprint"], second["fingerprint"])

    def test_public_owner_view_never_returns_a_key_for_mismatched_signer_state(self):
        service = self.owner_service()
        line = self.state.allowed_signers_path.read_bytes()
        changes = self.ledger._connection.total_changes
        try:
            for raw in (b"", b"CANARY-INVALID-SIGNER"):
                self.state.allowed_signers_path.write_bytes(raw)
                view = self.call(self.request("account.owner_public_key"), service=service)
                self.assertEqual("mismatch", view["state"])
                self.assertIsNone(view["public_key"])
                self.assertEqual(1, view["sequence"])
                self.assertEqual(raw, self.state.allowed_signers_path.read_bytes())
        finally:
            self.state.allowed_signers_path.write_bytes(line)
        self.assertEqual(changes, self.ledger._connection.total_changes)

    def test_public_owner_view_missing_or_different_valid_signer_keeps_recorded_generation(self):
        service = self.owner_service()
        request = self.request("account.owner_public_key")
        before = self.call(request, service=service)
        path = self.state.allowed_signers_path
        line = path.read_bytes()
        history = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_key_history")]
        changes = self.ledger._connection.total_changes
        digest_key = self.state.digest_key()
        directory = self.base / "other-valid-signer"
        directory.mkdir()
        _, public = generate_test_owner_key(directory)
        other_line = self.state._owner_signer_content(public)
        self.assertNotEqual(line, other_line)
        expected = {**before, "state": "mismatch", "public_key": None}
        try:
            for content in (None, other_line):
                with self.subTest(signer="missing" if content is None else "different-valid"):
                    path.unlink(missing_ok=True)
                    if content is not None:
                        path.write_bytes(content)
                        path.chmod(0o600)
                    self.assertEqual(expected, self.call(request, service=service))
                    if content is None:
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(content, path.read_bytes())
                    self.assertEqual(history, [tuple(row) for row in self.ledger._connection.execute(
                        "SELECT * FROM owner_key_history")])
                    self.assertEqual(changes, self.ledger._connection.total_changes)
                    self.assertEqual(digest_key, self.state.digest_key())
        finally:
            path.write_bytes(line)
            path.chmod(0o600)
        self.assertEqual(before, self.call(request, service=service))

    def test_unconfigured_public_owner_view_has_no_key_or_implicit_enrollment(self):
        state = LearningState(self.base / "unconfigured-owner")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(state)
        self.addCleanup(ledger.close)
        root = self.base / "unconfigured-project"
        root.mkdir()
        project = ledger.register_project(root, now_ms=1)
        views = AccountOwnerViews(ledger)

        def authorize(request, context):
            # Synthetic owner context, not a native caller/endpoint proof.
            if context is TOKEN and request.project_id == project:
                return AccountAuthorization(project, RUNTIME, account_request_digest(request),
                                            "owner", ACCOUNT_OPERATIONS)
            return None

        service = AccountService(RUNTIME, views, authorizer=authorize)
        self.addCleanup(service.close)
        request = self.request("account.owner_public_key", project=project)
        expected = {"state": "not_configured", "fingerprint": None, "sequence": 0,
                    "replacements": 0, "last_replaced_at_ms": None, "public_key": None}
        changes = ledger._connection.total_changes
        self.assertEqual(expected, self.call(request, service=service))
        state.allowed_signers_path.unlink()
        self.assertEqual(expected, self.call(request, service=service))
        self.assertFalse(state.allowed_signers_path.exists())
        state.allowed_signers_path.write_bytes(b"CANARY-INVALID-SIGNER")
        state.allowed_signers_path.chmod(0o600)
        self.assertEqual({**expected, "state": "mismatch"}, self.call(request, service=service))
        self.assertEqual(b"CANARY-INVALID-SIGNER", state.allowed_signers_path.read_bytes())
        self.assertEqual(0, ledger._connection.execute("SELECT COUNT(*) FROM owner_key_history").fetchone()[0])
        self.assertEqual(changes, ledger._connection.total_changes)

    def test_new_strict_read_does_not_rewrite_or_change_legacy_owner_encodings(self):
        padded = public_value("ssh-rsa", b"\x00\x01\x00\x01", b"\x00\x01" + b"\x55" * 127)
        line = ("codex-agents-owner " + padded + "\n").encode("ascii")
        self.state.allowed_signers_path.write_bytes(line)
        self.ledger._connection.execute(
            "INSERT INTO owner_key_history(sequence,signer_line,reason,recorded_at_ms) VALUES(2,?,'replaced',42)",
            (line,),
        )
        changes = self.ledger._connection.total_changes
        prior = self.call(self.request("account.owner_key"))
        self.assertEqual("ok", prior["state"])
        with self.assertRaisesRegex(AccountError, "account_incompatible"):
            self.call(self.request("account.owner_public_key"), service=self.owner_service())
        self.assertEqual(prior, self.call(self.request("account.owner_key")))
        self.assertEqual(line, self.state.allowed_signers_path.read_bytes())
        self.assertEqual(line, self.ledger._connection.execute("SELECT signer_line FROM owner_key_history WHERE sequence=2").fetchone()[0])
        self.assertEqual(changes, self.ledger._connection.total_changes)

    def test_public_key_reply_binds_fingerprint_state_and_closed_shape(self):
        request = self.request("account.owner_public_key")
        service = self.owner_service()
        valid = json.loads(service.handle(encode_account_request(request), TOKEN))
        cases = [
            {**valid["result"], "public_key": value}
            for value in (None, True, {}, [], "", valid["result"]["public_key"] + " comment")
        ]
        cases += [
            {**valid["result"], "fingerprint": "SHA256:" + "A" * 43},
            {**valid["result"], "state": "mismatch"},
            {**valid["result"], "extra": True},
        ]
        missing = dict(valid["result"])
        del missing["public_key"]
        cases.append(missing)
        for result in cases:
            with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                decode_account_response(canonical_json({**valid, "result": result}), request)
        legacy = self.request("account.owner_key")
        changed = {**valid, "request_digest": account_request_digest(legacy)}
        with self.assertRaisesRegex(AccountError, "account_invalid_response"):
            decode_account_response(canonical_json(changed), legacy)

    def test_largest_public_key_reply_fits_the_existing_frame(self):
        request = self.request("account.owner_public_key", identity="r" * 160)
        value = public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + b"\x55" * 3028)
        _, fingerprint = checked_public_key(value)
        maximum = (1 << 63) - 1
        result = {"state": "ok", "public_key": value, "fingerprint": fingerprint,
                  "sequence": maximum, "replacements": maximum, "last_replaced_at_ms": maximum}
        labels = {"request_id": request.request_id, "project_id": request.project_id,
                  "runtime_digest": request.runtime_digest, "request_digest": account_request_digest(request)}
        raw = AccountService._reply(labels, result=result)
        self.assertLessEqual(len(raw) + 1, MAX_ACCOUNT_FRAME_BYTES)
        self.assertEqual(result, decode_account_response(raw, request))

    def test_real_socket_public_owner_reads_use_explicit_retry_and_survive_restart(self):
        expected = self.views.owner_public_key(self.projects[0])
        endpoint, process, stop = self._start_view_server("public-owner", mode="drop")
        client = AccountClient(endpoint, self.projects[0], RUNTIME, timeout=.5)
        with self.assertRaisesRegex(AccountError, "account_invalid_response"):
            client.owner_public_key(request_id="public-read")
        self._server_line(process, "seen-1")
        self.assertEqual(expected, client.owner_public_key(request_id="public-read"))
        self._server_line(process, "seen-2")
        stop()
        endpoint, process, stop = self._start_view_server("public-owner")
        self.assertEqual(expected, AccountClient(endpoint, self.projects[1], RUNTIME).owner_public_key(request_id="public-read"))
        self._server_line(process, "seen-1")
        stop()

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
        self.assertEqual({"transport_alive": True, "database_checked": False}, a.health())
        self._server_line(process, "seen-4")
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


FIFO_OWNER_FIXTURE = r'''
import json,os,sys,threading
from pathlib import Path
from unittest import mock
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_store import LearningLedger,LearningState,authorize_collector_runtime
base,source,mode=sys.argv[1:]
base=Path(base);base.mkdir(mode=0o700)
state=LearningState(base/'state')
authorize_collector_runtime(state,Path(source)/'cli.py')
ledger=LearningLedger(state)
try:
 root=base/'project';root.mkdir()
 project=ledger.register_project(root,now_ms=1)
 views=AccountOwnerViews(ledger)
 path=state.allowed_signers_path
 path.unlink()
 if mode in {'fifo','fifo-eof'}: os.mkfifo(path,0o600)
 else:
  path.write_bytes(b'CANARY-malformed');path.chmod(0o600)
 original_open=os.open
 stop=threading.Event();helper=None
 def close_writer():
  while not stop.wait(.01):
   try: descriptor=original_open(path,os.O_WRONLY|os.O_NONBLOCK)
   except OSError: continue
   os.close(descriptor)
   return
 if mode=='fifo-eof':
  helper=threading.Thread(target=close_writer,daemon=True);helper.start()
 def swap(candidate,flags,*args,**kwargs):
  if mode=='swap-at-open' and Path(candidate)==path:
   path.unlink();os.mkfifo(path,0o600)
  return original_open(candidate,flags,*args,**kwargs)
 try:
  with mock.patch('os.open',side_effect=swap):
   owner=views.owner_key(project)
 finally:
  stop.set()
  if helper is not None: helper.join(1)
 print(json.dumps({'owner':owner,'project':views.project(project),
                   'history_count':ledger._connection.execute('SELECT COUNT(*) FROM owner_key_history').fetchone()[0]}))
finally:
 ledger.close()
'''


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
