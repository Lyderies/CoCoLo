"""Capacity recovery over the actual owner/service, using fake providers only."""
from __future__ import annotations
from tests.support import SHORT_TMP

from copy import deepcopy
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tests.test_account_api as wire_fixtures
import tests.test_owned_execution as capacity_fixtures
from tests.support import ORCHESTRATION_ROOT

from codex_agents import account_api
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_request, decode_account_response, encode_account_request,
)
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger
from codex_agents.owned_execution import OwnedExecutionRegistry
from codex_agents.service_transport import ServiceTransportError

UNKNOWN = {"state": "unknown", "receipt_digest": None, "released_at_ms": None}
HELD = {**UNKNOWN, "state": "held"}
HANDLE = "hrn_" + "0" * 48
BRIDGE = "brg_" + "1" * 32


class CapacityStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capacity_fixtures.CapacityCompletionTests("test_known_release_survives_restart_and_clock_rollback")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.token = object()
        self.views, self.service = self.make_service()

    @property
    def ledger(self):
        return self.fixture.ledger

    @property
    def project(self):
        return self.fixture.fixture.project_id

    def make_service(self, *, source="host", scope="a" * 64, operations=ACCOUNT_OPERATIONS):
        views = AccountOwnerViews(self.ledger)

        def authorize(request, context):
            # Test-only context; this is not native caller enrollment.
            if context is not self.token:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST,
                                        account_request_digest(request), source, operations, scope)

        service = AccountService(COLLECTOR_RUNTIME_DIGEST, views, authorizer=authorize)
        self.addCleanup(service.close)
        return views, service

    def request(self, handle=HANDLE, bridge=BRIDGE, *, project=None, identity="capacity-read"):
        return AccountRequest(1, "account.capacity_status", identity, project or self.project,
                              COLLECTOR_RUNTIME_DIGEST, {"run_handle": handle, "bridge_run_id": bridge})

    def call(self, request, *, service=None, context=None):
        raw = (service or self.service).handle(encode_account_request(request),
                                             self.token if context is None else context)
        return decode_account_response(raw, request)

    def test_unknown_and_expired_held_are_distinct_without_launch_release_or_writes(self):
        self.assertEqual(UNKNOWN, self.call(self.request()))
        handle, intent = self.fixture.reserve_claim()
        request = self.request(handle, intent["bridge_run_id"])
        before = self.ledger._connection.total_changes
        with mock.patch.object(self.ledger, "_clock_ms", return_value=2**63 - 1), \
                mock.patch.object(self.ledger, "_new_owned_execution", side_effect=AssertionError("launch")), \
                mock.patch.object(self.ledger, "_release_active_capacity", side_effect=AssertionError("release")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("extra owner")):
            self.assertEqual(HELD, self.call(request))
            self.assertEqual(UNKNOWN, self.call(self.request(handle, BRIDGE)))
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(1, self.fixture.held())

    def test_same_read_identity_observes_held_then_actual_completion_without_cached_state(self):
        original = self.ledger._new_owned_execution
        observations = []

        def registered(project, handle, bridge):
            ticket = original(project, handle, bridge)
            observations.append(self.call(self.request(handle, bridge)))
            return ticket

        with mock.patch.object(self.ledger, "_new_owned_execution", side_effect=registered):
            handle, output = self.fixture.run_one()
        self.assertEqual([HELD], observations)
        status = self.call(self.request(handle, output["bridge_report"]["bridge_run_id"]))
        self.assertEqual("released", status["state"])
        self.assertEqual(0, self.fixture.held())
        self.assertEqual(64, len(status["receipt_digest"]))

    def test_unknown_cleanup_stays_held_after_terminal_result_and_owner_restart(self):
        with mock.patch.object(OwnedExecutionRegistry, "finish", return_value=False):
            handle, output = self.fixture.run_one()
        request = self.request(handle, output["bridge_report"]["bridge_run_id"])
        self.assertEqual(HELD, self.call(request))
        self.fixture.reopen()
        self.views, self.service = self.make_service()
        self.assertEqual(HELD, self.call(request))
        self.assertEqual(1, self.fixture.held())

    def test_actual_receipt_survives_restart_and_wall_rollback_without_new_execution(self):
        handle, output = self.fixture.run_one()
        request = self.request(handle, output["bridge_report"]["bridge_run_id"])
        expected = self.call(request)
        self.fixture.reopen()
        self.views, self.service = self.make_service()
        before = self.ledger._connection.total_changes
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1), \
                mock.patch.object(self.ledger, "_new_owned_execution", side_effect=AssertionError("relaunch")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("extra owner")):
            self.assertEqual(expected, self.call(request))
        self.assertEqual(before, self.ledger._connection.total_changes)

    def test_cross_project_and_bridge_mismatch_do_not_disclose_a_released_receipt(self):
        handle, output = self.fixture.run_one()
        bridge = output["bridge_report"]["bridge_run_id"]
        other = Path(self.fixture.fixture.temporary.name) / "other-project"
        other.mkdir()
        project = self.ledger.register_project(other, now_ms=1)
        self.assertEqual(UNKNOWN, self.call(self.request(handle, bridge, project=project)))
        self.assertEqual(UNKNOWN, self.call(self.request(handle, BRIDGE)))

    def test_forged_or_partial_release_provenance_is_an_error_and_is_never_repaired(self):
        handle, output = self.fixture.run_one()
        request = self.request(handle, output["bridge_report"]["bridge_run_id"])
        self.ledger._connection.execute("UPDATE capacity_completions SET receipt_digest=?", ("f" * 64,))
        self.ledger._connection.execute("UPDATE active_run_capacity SET release_digest=?", ("f" * 64,))
        before = self.ledger._connection.total_changes
        with self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.call(request)
        self.assertEqual(before, self.ledger._connection.total_changes)

    def test_missing_completion_registration_or_changed_terminal_binding_cannot_report_release(self):
        statements = (
            "DELETE FROM capacity_completions WHERE bridge_run_id=?",
            "DELETE FROM capacity_executions WHERE bridge_run_id=?",
            "UPDATE capacity_completions SET terminal_content_digest='" + "0" * 64 + "' WHERE bridge_run_id=?",
        )
        for sql in statements:
            with self.subTest(sql=sql):
                handle, output = self.fixture.run_one()
                bridge = output["bridge_report"]["bridge_run_id"]
                self.ledger._connection.execute(sql, (bridge,))
                before = self.ledger._connection.total_changes
                with self.assertRaisesRegex(AccountError, "account_unavailable"):
                    self.call(self.request(handle, bridge))
                self.assertEqual(before, self.ledger._connection.total_changes)

    def test_default_role_scope_and_operation_denial_precede_owner_access(self):
        default = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views)
        self.addCleanup(default.close)
        variants = [default]
        variants += [self.make_service(source=source)[1] for source in ("owner", "hook", "app_server")]
        variants += [self.make_service(scope=None)[1],
                     self.make_service(operations=frozenset({"account.health"}))[1]]
        with mock.patch.object(AccountOwnerViews, "capacity_status", side_effect=AssertionError("backend")):
            for service in variants:
                with self.subTest(source=service), self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(self.request(), service=service)

    def test_mutation_of_authorized_payload_is_refused_with_original_binding(self):
        request = self.request()

        def mutate(command, context):
            digest = account_request_digest(command)
            command.payload["bridge_run_id"] = "brg_" + "2" * 32
            return AccountAuthorization(command.project_id, COLLECTOR_RUNTIME_DIGEST, digest,
                                        "host", ACCOUNT_OPERATIONS, "a" * 64)

        service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views, authorizer=mutate)
        self.addCleanup(service.close)
        with mock.patch.object(self.views, "capacity_status", side_effect=AssertionError("backend")), \
                self.assertRaisesRegex(AccountError, "account_not_authorized"):
            self.call(request, service=service)

    def test_wrong_runtime_and_authorization_project_are_refused_before_owner_access(self):
        original = self.request()
        changed = AccountRequest(1, original.operation, original.request_id, self.project, "f" * 64, original.payload)
        variants = [(self.service, changed)]
        for project, runtime in (("prj_" + "e" * 32, COLLECTOR_RUNTIME_DIGEST), (self.project, "f" * 64)):
            authorization = AccountAuthorization(project, runtime, account_request_digest(original),
                                                "host", ACCOUNT_OPERATIONS, "a" * 64)
            service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views,
                                     authorizer=lambda request, context, grant=authorization: grant)
            self.addCleanup(service.close)
            variants.append((service, original))
        with mock.patch.object(self.views, "capacity_status", side_effect=AssertionError("backend")):
            for service, request in variants:
                with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(request, service=service)

    def test_inactive_project_is_refused_before_capacity_metadata_access(self):
        self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,))
        with mock.patch.object(self.ledger, "_capacity_status", side_effect=AssertionError("inactive capacity read")), \
                self.assertRaisesRegex(AccountError, "account_project_not_found"):
            self.call(self.request())

    def test_pending_owner_transaction_is_not_exposed_or_committed_by_status(self):
        self.ledger._connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(AccountError, "account_unavailable"):
                self.call(self.request())
            self.assertTrue(self.ledger._connection.in_transaction)
            health = AccountRequest(1, "account.health", "health", self.project, COLLECTOR_RUNTIME_DIGEST, {})
            self.assertEqual({"transport_alive": True, "database_checked": False}, self.call(health))
            self.assertTrue(self.ledger._connection.in_transaction)
        finally:
            self.ledger._connection.rollback()

    def test_uncommitted_project_deletion_is_not_disclosed_through_status_errors(self):
        self.ledger._connection.execute("BEGIN IMMEDIATE")
        try:
            self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,))
            with mock.patch.object(self.views, "_project_locked", wraps=self.views._project_locked) as project_read:
                with self.assertRaisesRegex(AccountError, "account_unavailable"):
                    self.call(self.request())
                project_read.assert_not_called()
            self.assertTrue(self.ledger._connection.in_transaction)
        finally:
            self.ledger._connection.rollback()
        self.assertEqual(UNKNOWN, self.call(self.request()))

    def test_owner_thread_rejection_does_not_make_health_depend_on_sql(self):
        result = []

        def other_thread():
            try:
                self.call(self.request())
            except AccountError as error:
                result.append(error.reason_code)
            request = AccountRequest(1, "account.health", "health", self.project, COLLECTOR_RUNTIME_DIGEST, {})
            result.append(self.call(request))

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["account_wrong_owner", {"transport_alive": True, "database_checked": False}], result)

    def test_closed_payload_and_fixed_failures_never_echo_untrusted_fields(self):
        data = json.loads(encode_account_request(self.request()))
        for payload in ({}, {**data["payload"], "cleanup_confirmed": True},
                        {**data["payload"], "pid": 12345}, {**data["payload"], "run_handle": "CANARY-private"},
                        {**data["payload"], "bridge_run_id": True}):
            raw = canonical_json({**data, "payload": payload})
            with self.assertRaisesRegex(AccountError, "account_invalid_request"):
                decode_account_request(raw)
            self.assertNotIn(b"CANARY", self.service.handle(raw, self.token))

    def test_closed_response_types_and_request_digest_reject_substituted_receipts(self):
        request = self.request()
        raw = self.service.handle(encode_account_request(request), self.token)
        data = json.loads(raw)
        for value in ({**UNKNOWN, "state": "stopped"}, {**HELD, "receipt_digest": "a" * 64},
                      {"state": "released", "receipt_digest": "a" * 64, "released_at_ms": True},
                      {"state": "released", "receipt_digest": "a" * 64, "released_at_ms": -1},
                      {**UNKNOWN, "cleanup_confirmed": True}):
            with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                decode_account_response(canonical_json({**data, "result": value}), request)
        changed = deepcopy(request.payload)
        changed["bridge_run_id"] = "brg_" + "2" * 32
        substitute = AccountRequest(1, request.operation, request.request_id, request.project_id,
                                    request.runtime_digest, changed)
        with self.assertRaisesRegex(AccountError, "account_invalid_response"):
            decode_account_response(raw, substitute)

    def test_client_unavailable_has_one_exchange_no_retry_or_exception_content(self):
        client = AccountClient(Path("/private/tmp/capacity-status-unavailable.sock"), self.project, COLLECTOR_RUNTIME_DIGEST)
        with mock.patch.object(account_api, "exchange_frame", side_effect=ServiceTransportError("CANARY")) as exchange:
            with self.assertRaisesRegex(AccountError, "account_unavailable") as caught:
                client.capacity_status(HANDLE, BRIDGE, request_id="read-1")
        self.assertEqual(1, exchange.call_count)
        self.assertIsNone(caught.exception.__context__)

    def test_real_socket_lost_reply_then_owner_restart_returns_same_keyed_receipt(self):
        handle, output = self.fixture.run_one()
        bridge = output["bridge_report"]["bridge_run_id"]
        expected = self.call(self.request(handle, bridge))
        for number, mode in enumerate(("drop", "normal")):
            with self.subTest(mode=mode):
                endpoint, process, stop = self.start_server(mode)
                client = AccountClient(endpoint, self.project, COLLECTOR_RUNTIME_DIGEST, timeout=.5)
                if number == 0:
                    with self.assertRaisesRegex(AccountError, "account_invalid_response"):
                        client.capacity_status(handle, bridge, request_id="read-1")
                    self.server_line(process, "seen-1")
                self.assertEqual(expected, client.capacity_status(handle, bridge, request_id="read-1"))
                self.server_line(process, "seen-2" if number == 0 else "seen-1")
                stop()
        self.assertEqual(1, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])

    def start_server(self, mode):
        directory = tempfile.TemporaryDirectory(prefix="capacity-status-", dir=SHORT_TMP)
        self.addCleanup(directory.cleanup)
        endpoint = Path(directory.name) / "view.sock"
        source = wire_fixtures.ACCOUNT_SERVER_FIXTURE.replace(
            "'owner',ACCOUNT_OPERATIONS)", "'host',ACCOUNT_OPERATIONS,'a'*64)",
        )
        self.assertNotEqual(source, wire_fixtures.ACCOUNT_SERVER_FIXTURE)
        self.ledger.close()
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + source,
             str(self.fixture.fixture.state.root), str(endpoint), COLLECTOR_RUNTIME_DIGEST, self.project, mode],
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
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                self.fixture.fixture.ledger = LearningLedger(self.fixture.fixture.state,
                                                           client_capabilities=self.fixture.fixture.clients)
                self.fixture.addCleanup(self.ledger.close)

        self.addCleanup(stop)
        self.server_line(process, "ready")
        return endpoint, process, stop

    def server_line(self, process, expected):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "fixture response unavailable")
        self.assertEqual(expected, process.stdout.readline().strip())

    def test_client_import_chain_and_unavailable_read_cannot_open_sqlite(self):
        source = '''import importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'sqlite3','codex_agents.learning_store'}: raise RuntimeError('database client import')
sys.meta_path.insert(0,Block())
from pathlib import Path
from codex_agents.account_api import AccountClient,AccountError
try:
 AccountClient(Path('/private/tmp/capacity-status-no-service.sock'),'prj_'+'0'*32,'a'*64).capacity_status('hrn_'+'0'*48,'brg_'+'1'*32)
except AccountError as error:
 print(error.reason_code)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c",
                                 f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + source],
                                capture_output=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"account_unavailable\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
