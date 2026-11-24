"""RSF-S1-1/2: real stores and framed handlers, without sockets or health preflight."""
from __future__ import annotations

import json
import selectors
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import test_learning_store as store_tests
from tests.learning_support import event_record

from codex_agents import collector_client
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_READ_OPERATIONS, ACCOUNT_PROTOCOL_VERSION,
    AccountError, AccountRequest,
    decode_account_response, encode_account_request,
)
from codex_agents.account_service import AccountOwnerViews, AccountService
from codex_agents.account_ingress import AccountIngress
from codex_agents.collector_contracts import COLLECTOR_PROTOCOL_VERSION, CollectorUpgradeRequired
from codex_agents.learning_contracts import canonical_json, external_id
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, CollectorClient, CollectorServer


UPDATE_COMMAND = "./setup.sh --update"
UPGRADE_MESSAGE = (
    "This client and the collector must use the same runtime. "
    "Update this project with: ./setup.sh --update"
)
COLLECTOR_MUTATIONS = {
    "bind", "ingest", "issue_result_repair", "register", "commit_owner_record",
    "learning_moved_if_empty", "shutdown", "shutdown_if_drained",
}


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)

    def repair_request(self):
        return {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "issue_result_repair",
                "project_root": str(self.fixture.project_a),
                "assignment_digest": "a" * 64, "reason_code": "invalid_shape"}

    def assert_upgrade(self, response):
        self.assertFalse(response["ok"])
        self.assertEqual("runtime_upgrade_required", response["code"])
        self.assertEqual(UPGRADE_MESSAGE, response["message"])
        self.assertEqual(UPDATE_COMMAND, response["update_command"])


class RepairIngressTests(StoreFixture):
    def test_RSF_S1_1_old_repair_bypasses_health_and_changes_zero_rows(self):
        # Expired receipt makes this catch deletion as well as insertion.
        self.ledger.issue_result_repair_receipt(
            self.fixture.project_a_id, "b" * 64, "invalid_shape", now_ms=1)
        connection = self.ledger._connection
        before_rows = list(connection.iterdump())
        before_changes = connection.total_changes
        response = self.server._dispatch(self.repair_request())
        self.assertEqual(before_changes, connection.total_changes)
        self.assertEqual(before_rows, list(connection.iterdump()))
        self.assert_upgrade(response)

    def test_RSF_S1_1_wrong_or_malformed_repair_identity_changes_zero_rows(self):
        for identity in (None, "", "0" * 64, 1, [], {}):
            with self.subTest(identity=identity):
                before = self.ledger._connection.total_changes
                response = self.server._dispatch({**self.repair_request(), "runtime_digest": identity})
                self.assertEqual(before, self.ledger._connection.total_changes)
                self.assert_upgrade(response)

    def test_RSF_S1_1_matching_repair_identity_writes_one_receipt(self):
        response = self.server._dispatch({**self.repair_request(), "runtime_digest": COLLECTOR_RUNTIME_DIGEST})
        self.assertTrue(response["ok"])
        rows = self.ledger._connection.execute(
            "SELECT receipt_id FROM repair_receipts WHERE project_id=?",
            (self.fixture.project_a_id,)).fetchall()
        self.assertEqual([response["receipt_id"]], [row[0] for row in rows])

    def test_RSF_S1_2_every_collector_mutation_refuses_before_touching_state(self):
        for operation in sorted(COLLECTOR_MUTATIONS):
            for fields in ({}, {"runtime_digest": "0" * 64}, {"runtime_digest": []}):
                with self.subTest(operation=operation, fields=fields):
                    # An incompatible caller is refused before parsing operation-specific inputs.
                    request = {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": operation, **fields}
                    before = self.ledger._connection.total_changes
                    self.server._bindings["keep"] = object()
                    response = self.server._dispatch(request)
                    self.assertEqual(before, self.ledger._connection.total_changes)
                    self.assertIn("keep", self.server._bindings)
                    self.assertFalse(self.server._stop.is_set())
                    self.assert_upgrade(response)

    def test_RSF_S1_1_incompatible_ingest_does_not_consume_a_valid_binding(self):
        bind = {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "bind",
                "project_root": str(self.fixture.project_a), "session_id": "session",
                "producer": "hook", "client_version": "codex-hooks-v1",
                "runtime_digest": COLLECTOR_RUNTIME_DIGEST}
        token = self.server._dispatch(bind)["binding_token"]
        request = {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "ingest",
                   "binding_token": token, "record": {}}
        self.assert_upgrade(self.server._dispatch(request))
        self.assertIn(token, self.server._bindings)


class CollectorClientIngressTests(StoreFixture):
    def test_RSF_S1_1_current_client_carries_identity_on_all_mutations_without_health(self):
        observed = []

        def exchange(path, raw, **kwargs):
            request = json.loads(raw)
            observed.append(request)
            self.assertEqual(COLLECTOR_RUNTIME_DIGEST, request.get("runtime_digest"))
            return canonical_json(self.server._dispatch(request))

        client = CollectorClient(self.fixture.state.socket)
        with mock.patch.object(collector_client, "exchange_frame", side_effect=exchange):
            token = client.bind(self.fixture.project_a, "session", "hook", "codex-hooks-v1")
            record = event_record("evt_rsf_ingress")
            record["run_id"] = external_id(self.fixture.state.digest_key(), "run",
                                           "codex-hooks-v1\x00session\x00root")
            self.assertTrue(client.ingest(token, record)["ok"])
            self.assertTrue(client.issue_result_repair(self.fixture.project_a, "a" * 64, "invalid_shape"))
            root = self.fixture.project_a.parent / "new-project"
            root.mkdir()
            self.assertTrue(client.register(root).startswith("prj_"))
            self.assertTrue(client.learning_moved_if_empty(root))
            with mock.patch.object(self.ledger, "commit_owner_record", return_value={"stored": True}):
                self.assertEqual({"stored": True}, client.commit_owner_record(self.fixture.project_a, {}, b""))
            client.shutdown(only_if_drained=True)
            client.shutdown()
        self.assertEqual(COLLECTOR_MUTATIONS, {request["op"] for request in observed})

    def test_RSF_S1_1_client_surfaces_the_plain_upgrade_message(self):
        response = {"ok": False, "code": "runtime_upgrade_required",
                    "message": UPGRADE_MESSAGE, "update_command": UPDATE_COMMAND}
        with mock.patch.object(collector_client, "exchange_frame", return_value=canonical_json(response)):
            with self.assertRaises(CollectorUpgradeRequired) as caught:
                CollectorClient(self.fixture.state.socket).issue_result_repair(
                    self.fixture.project_a, "a" * 64, "invalid_shape")
        self.assertEqual("runtime_upgrade_required", caught.exception.code)
        self.assertEqual(UPGRADE_MESSAGE, str(caught.exception))


class AccountIngressTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.authorize = mock.Mock(side_effect=AssertionError("runtime gate must precede authorizer"))
        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger),
                                      authorizer=self.authorize)
        self.addCleanup(self.service.close)

    def test_RSF_S1_2_every_account_mutation_refuses_missing_or_wrong_runtime_before_authorizer(self):
        mutations = ACCOUNT_OPERATIONS - ACCOUNT_READ_OPERATIONS
        self.assertEqual(16, len(mutations))  # batch 0.8.57b: + account.lost_run_closure (B4s)
        for operation in sorted(mutations):
            for fields in ({}, {"runtime_digest": "0" * 64}, {"runtime_digest": []}):
                with self.subTest(operation=operation, fields=fields):
                    raw = canonical_json({"protocol_version": ACCOUNT_PROTOCOL_VERSION,
                        "operation": operation, "request_id": "rsf-s1", "project_id": self.fixture.project_a_id,
                        "payload": {}, **fields})
                    before = self.ledger._connection.total_changes
                    response = json.loads(self.service.handle(raw))
                    self.assertEqual(before, self.ledger._connection.total_changes)
                    self.assertFalse(response["ready"])
                    self.assertEqual("runtime_upgrade_required", response["reason_code"])
                    self.assertEqual(UPGRADE_MESSAGE, response["message"])
                    self.assertEqual(UPDATE_COMMAND, response["update_command"])
        self.authorize.assert_not_called()

    def test_RSF_S1_1_account_client_surfaces_upgrade_with_exact_command(self):
        # A valid old-runtime probe envelope bypasses health and reaches the real handler.
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.billing_probe", "rsf-probe",
                                 self.fixture.project_a_id, "0" * 64, {})
        raw = self.service.handle(encode_account_request(request))
        response = json.loads(raw)
        self.assertEqual("runtime_upgrade_required", response["reason_code"])
        with self.assertRaises(AccountError) as caught:
            decode_account_response(raw, request)
        self.assertEqual("runtime_upgrade_required", caught.exception.reason_code)
        self.assertEqual(UPGRADE_MESSAGE, str(caught.exception))
        self.authorize.assert_not_called()

    def test_RSF_S1_1_matching_account_identity_reaches_existing_authorization(self):
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, "account.billing_probe", "rsf-current",
                                 self.fixture.project_a_id, COLLECTOR_RUNTIME_DIGEST, {})
        self.authorize.side_effect = None
        self.authorize.return_value = None
        response = json.loads(self.service.handle(encode_account_request(request)))
        self.assertEqual("account_not_authorized", response["reason_code"])
        self.authorize.assert_called_once()

    def test_RSF_S1_1_account_front_replies_to_missing_identity_before_decoder_or_owner(self):
        ingress = AccountIngress.__new__(AccountIngress)
        ingress._service = self.service
        for operation in sorted(ACCOUNT_OPERATIONS - ACCOUNT_READ_OPERATIONS):
            for fields in ({}, {"runtime_digest": "0" * 64}):
                with self.subTest(operation=operation, fields=fields):
                    raw = canonical_json({"protocol_version": ACCOUNT_PROTOCOL_VERSION,
                        "operation": operation, "request_id": "rsf-front",
                        "project_id": self.fixture.project_a_id, "payload": {}, **fields})
                    connection = mock.Mock()
                    connection.recv.return_value = raw + b"\n"
                    peer = SimpleNamespace(connection=connection, outgoing=None, handed=False,
                        incoming=bytearray(), context=None, deadline=time.monotonic_ns() + 1_000_000_000)
                    selector = mock.Mock()
                    before = self.ledger._connection.total_changes
                    with mock.patch.object(ingress, "_drop") as drop:
                        ingress._front_step(selector, "peer", peer, selectors.EVENT_READ)
                    self.assertEqual(before, self.ledger._connection.total_changes)
                    self.assertIsNotNone(peer.outgoing)
                    response = json.loads(peer.outgoing)
                    self.assertEqual("runtime_upgrade_required", response["reason_code"])
                    self.assertEqual(UPGRADE_MESSAGE, response["message"])
                    self.assertFalse(peer.handed)
                    drop.assert_not_called()
                    selector.modify.assert_called_once_with(connection, selectors.EVENT_WRITE, "peer")
        self.authorize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
