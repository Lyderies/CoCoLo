"""RSF-S1 primary-only Unix socket checks. Never run in the no-sockets worktree."""
import json
import unittest
from unittest import mock

from tests import test_learning_store as store_tests
from tests import test_rsf_s1_ingress as ingress_tests
from tests import test_account_ingress as account_sockets
from tests.support import SHORT_TMP

from codex_agents import collector_client
from codex_agents.collector_contracts import MAX_REQUEST_BYTES, CollectorUpgradeRequired
from codex_agents.learning_contracts import canonical_json
from codex_agents.account_api import ACCOUNT_OPERATIONS, ACCOUNT_READ_OPERATIONS
from codex_agents.learning_store import CollectorClient, CollectorServer
from codex_agents.service_transport import exchange_frame


class RuntimeIngressSocketTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName="runTest")
        with mock.patch.object(store_tests, "TEMP_ROOT", SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.server = CollectorServer(self.fixture.ledger, client_capabilities=self.fixture.clients)
        self.server.start()
        self.addCleanup(self.server.stop)

    def exchange(self, request):
        return json.loads(exchange_frame(self.fixture.state.socket, canonical_json(request),
            timeout=1.0, request_limit=MAX_REQUEST_BYTES, response_limit=16_384))

    def repair(self):
        return {"protocol": 1, "op": "issue_result_repair", "project_root": str(self.fixture.project_a),
                "assignment_digest": "a" * 64, "reason_code": "invalid_shape"}

    def assert_upgrade(self, reply):
        self.assertEqual({"ok": False, "code": "runtime_upgrade_required",
            "message": ingress_tests.UPGRADE_MESSAGE, "update_command": ingress_tests.UPDATE_COMMAND}, reply)

    def test_RSF_S1_1_old_wire_repair_changes_zero_rows_without_health(self):
        connection = self.fixture.ledger._connection
        self.fixture.ledger.issue_result_repair_receipt(
            self.fixture.project_a_id, "b" * 64, "invalid_shape", now_ms=1)
        # Disable independent sweeps so every observed change belongs to this request.
        with mock.patch.object(self.server, "_sweep_cycle"):
            with self.fixture.ledger._lock:
                before = list(connection.iterdump())
                changes = connection.total_changes
            reply = self.exchange(self.repair())
            with self.fixture.ledger._lock:
                self.assertEqual(changes, connection.total_changes)
                self.assertEqual(before, list(connection.iterdump()))
        self.assert_upgrade(reply)

    def test_RSF_S1_2_all_mutating_wire_operations_refuse_old_callers(self):
        for operation in sorted(ingress_tests.COLLECTOR_MUTATIONS):
            if operation == "shutdown_if_drained":
                continue  # UPG-5: accepted from another runtime (test_upgrade_handover, real process)
            for fields in ({}, {"runtime_digest": "0" * 64}):
                with self.subTest(operation=operation, fields=fields):
                    self.assert_upgrade(self.exchange({"protocol": 1, "op": operation, **fields}))
                    self.assertFalse(self.server._stop.is_set())

    def test_RSF_S1_1_current_client_writes_and_old_runtime_surfaces_update_command(self):
        client = CollectorClient(self.fixture.state.socket, timeout=1.0)
        with mock.patch.object(collector_client, "COLLECTOR_RUNTIME_DIGEST", "0" * 64):
            with self.assertRaises(CollectorUpgradeRequired) as caught:
                client.issue_result_repair(self.fixture.project_a, "a" * 64, "invalid_shape")
        self.assertEqual(ingress_tests.UPGRADE_MESSAGE, str(caught.exception))
        self.assertEqual("runtime_upgrade_required", caught.exception.code)
        receipt = client.issue_result_repair(self.fixture.project_a, "a" * 64, "invalid_shape")
        self.assertTrue(receipt.startswith("rpr_"))
        with self.fixture.ledger._lock:
            rows = self.fixture.ledger._connection.execute("SELECT receipt_id FROM repair_receipts").fetchall()
        self.assertEqual([receipt], [row[0] for row in rows])


@unittest.skipUnless(account_sockets.fixtures.SIGNATURE_WORKER, "fixed macOS worker guard")
class AccountRuntimeIngressSocketTests(unittest.TestCase):
    def setUp(self):
        self.wire = account_sockets.AccountIngressTests(methodName="runTest")
        self.wire.setUp()
        self.addCleanup(self.wire.doCleanups)
        self.addCleanup(self.wire.tearDown)
        self.wire.start()

    def test_RSF_S1_2_all_account_mutations_get_upgrade_reply_without_owner_handoff(self):
        ledger = self.wire.fixture.fixture.ledger
        service = self.wire.service
        for operation in sorted(ACCOUNT_OPERATIONS - ACCOUNT_READ_OPERATIONS):
            for fields in ({}, {"runtime_digest": "0" * 64}):
                with self.subTest(operation=operation, fields=fields):
                    request = self.wire.request(operation="account.billing_probe")
                    raw = canonical_json({"protocol_version": 1, "operation": operation,
                        "request_id": "rsf-wire", "project_id": request.project_id, "payload": {}, **fields})
                    client = self.wire.pair()
                    client.setblocking(False)
                    received = bytearray()

                    def read():
                        try:
                            part = client.recv(16_384)
                        except BlockingIOError:
                            return False
                        self.assertTrue(part)
                        received.extend(part)
                        return b"\n" in received

                    before = ledger._connection.total_changes
                    with mock.patch.object(service, "_authorizer", side_effect=AssertionError("old caller")):
                        client.sendall(raw + b"\n")
                        self.wire.wait(read, pump=False)
                    response = json.loads(bytes(received))
                    self.assertEqual("runtime_upgrade_required", response["reason_code"])
                    self.assertEqual(ingress_tests.UPGRADE_MESSAGE, response["message"])
                    self.assertEqual(ingress_tests.UPDATE_COMMAND, response["update_command"])
                    self.assertEqual(before, ledger._connection.total_changes)
                    self.assertEqual(0, self.wire.ingress.snapshot()["queued"])


if __name__ == "__main__":
    unittest.main()
