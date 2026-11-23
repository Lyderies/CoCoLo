"""Real account/project registration stores; synthetic enrollment, no model."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_storage as fixtures
from tests.project_schema_fixtures import legacy_cleanup_layout

from codex_agents import account_api
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, encode_account_request
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.execution_grant import ExecutionRegistrationInput, checked_execution_grant, verify_execution_grant
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger, LedgerError
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS
from codex_agents.service_transport import ServiceTransportError


class ProjectExecutionGrantTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimStorageTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.arm()
        self.receipt = self.fixture.call()
        self.binding = AttemptBinding(**{name: getattr(self.fixture.scope, name) for name in LEASE_SCOPE_FIELDS},
                                      claim_id=self.receipt["bridge_run_id"], claim_digest=self.receipt["receipt_digest"])
        self.fixture.store.bind_result_lease(self.fixture.scope, self.binding, self.fixture.assigned,
                                             hard_deadline_at_ms=self.receipt["deadline_at_ms"])
        self.evidence = self.fixture.store.attest_bound_result_lease(self.fixture.scope)
        self.expected, self.source = self.fixture.scope, "project"
        self.registration = ExecutionRegistrationInput(self.binding.run_handle, self.binding.claim_id,
                                                        self.binding.claim_digest, self.fixture.request.request_id,
                                                        self.fixture.base.launch.identity_digest)
        self.request = AccountRequest(1, "account.register_execution", "register", self.binding.project_id,
                                      COLLECTOR_RUNTIME_DIGEST, self.registration.payload())
        self.service = self.make_service()

    @property
    def ledger(self):
        return self.fixture.base.ledger

    def make_service(self):
        def authorize(request, context):
            if context is not self.fixture.base.token:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                        self.source, frozenset({"account.register_execution", "account.health"}),
                                        "a" * 64, self.expected, self.evidence)
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(service.close)
        return service

    def call(self, request=None):
        return self.fixture.base.call(request or self.request, service=self.service)

    def grant(self):
        return checked_execution_grant(self.call()["grant"])

    def consume(self, grant, **changes):
        options = dict(task=self.fixture.base.fixture.fixture.task(), route=self.fixture.base.fixture.route)
        options.update(changes)
        return self.fixture.store.consume_execution_grant(self.binding, grant.encode(), **options)

    def count(self, table):
        if table not in {"capacity_executions", "active_run_capacity"}:
            raise AssertionError("unexpected fixture table")
        return self.ledger._connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_account_registers_without_local_ticket_and_project_consumes_exactly_once(self):
        result = self.call()
        self.assertFalse(result["known"])
        grant = checked_execution_grant(result["grant"])
        self.assertEqual(grant, verify_execution_grant(self.fixture.key, self.binding, grant.encode()))
        row = self.ledger._connection.execute("SELECT clock_reference_ns,execution_deadline_ns FROM active_run_capacity").fetchone()
        self.assertEqual(tuple(row), (grant.clock_reference_ns, grant.deadline_ns))
        self.assertEqual(self.binding, grant.binding)
        self.assertEqual(1, self.count("capacity_executions"))
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))
        self.assertEqual({**result, "known": True}, self.call())
        self.assertEqual(grant, self.consume(grant))
        with self.assertRaisesRegex(HistoryError, "history_execution_consumed"):
            self.consume(grant)
        self.assertEqual(1, self.fixture.store._connection.execute("SELECT COUNT(*) FROM project_execution_consumptions").fetchone()[0])
        with self.assertRaises(LedgerError):
            self.ledger._new_owned_execution(self.binding.project_id, self.binding.run_handle, self.binding.claim_id)
        self.assertIsNone(self.ledger._connection.execute("SELECT released_at_ms FROM active_run_capacity").fetchone()[0])

    def test_client_makes_one_bounded_exchange_and_never_automatically_retries_lost_reply(self):
        client = AccountClient(Path("/private/tmp/fixture-account.sock"), self.binding.project_id, COLLECTOR_RUNTIME_DIGEST)

        def exchange(path, raw, **options):
            self.assertEqual(Path("/private/tmp/fixture-account.sock"), path)
            self.assertEqual(account_api.MAX_ACCOUNT_FRAME_BYTES, options["response_limit"])
            self.service.handle(raw, self.fixture.base.token)  # Actual registration commits; reply is lost.
            raise ServiceTransportError("transport_unavailable")

        with mock.patch.object(account_api, "exchange_frame", side_effect=exchange) as transport:
            with self.assertRaisesRegex(AccountError, "account_unavailable"):
                client.register_execution(**self.registration.payload(), request_id="client-registration")
            self.assertEqual(1, transport.call_count)
        self.assertEqual(1, self.count("capacity_executions"))
        with mock.patch.object(account_api, "exchange_frame", side_effect=lambda path, raw, **kw: self.service.handle(raw, self.fixture.base.token)) as transport:
            reply = client.register_execution(**self.registration.payload(), request_id="client-registration")
            self.assertTrue(reply["known"])
            self.assertEqual(1, transport.call_count)
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))

    def test_only_project_service_context_and_closed_registration_payload_are_accepted(self):
        for source in ("host", "owner", "hook", "app_server"):
            self.source = source
            with self.assertRaisesRegex(AccountError, "account_not_authorized"):
                self.call()
        self.source = "project"
        for extra in ({"pid": 1}, {"executable": "PRIVATE-ARBITRARY"}, {"grant": True}):
            raw = json.loads(encode_account_request(self.request))
            raw["payload"].update(extra)
            reply = json.loads(self.service.handle(canonical_json(raw), self.fixture.base.token))
            self.assertEqual("account_invalid_request", reply["reason_code"])
            self.assertNotIn("PRIVATE", json.dumps(reply))
        self.assertEqual(0, self.count("capacity_executions"))

    def test_bound_proof_original_request_and_original_actor_must_match(self):
        proof = self.evidence
        self.evidence = None
        with self.assertRaisesRegex(AccountError, "claim_storage_required"):
            self.call()
        self.evidence = self.fixture.evidence  # Earlier claim_pending proof cannot register execution.
        with self.assertRaisesRegex(AccountError, "claim_storage_invalid"):
            self.call()
        self.evidence = proof
        self.expected = replace(self.expected, supervisor_generation=2)
        with self.assertRaisesRegex(AccountError, "claim_storage_invalid"):
            self.call()
        self.expected = self.fixture.scope
        for field, value in (("claim_digest", "b" * 64), ("identity_digest", "b" * 64),
                             ("bridge_run_id", "brg_" + "b" * 32), ("claim_request_id", "other-request")):
            payload = replace(self.registration, **{field: value}).payload()
            request = replace(self.request, payload=payload)
            with self.subTest(field=field), self.assertRaises(AccountError):
                self.call(request)
        self.assertEqual(0, self.count("capacity_executions"))

    def test_account_transaction_rollback_preserves_unregistered_claim_and_binding(self):
        original = self.ledger._account_transaction
        before = self.ledger._connection.execute("SELECT native_assignment_id FROM host_run_bindings WHERE run_id=?",
                                                (self.binding.run_id,)).fetchone()[0]

        @contextmanager
        def fail_before_commit():
            with original():
                yield
                raise OSError("PRIVATE-REGISTRATION-FAILURE")

        with mock.patch.object(self.ledger, "_account_transaction", new=fail_before_commit), \
                self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.call()
        self.assertEqual(0, self.count("capacity_executions"))
        self.assertEqual(before, self.ledger._connection.execute("SELECT native_assignment_id FROM host_run_bindings WHERE run_id=?",
                                                               (self.binding.run_id,)).fetchone()[0])
        self.assertFalse(self.call()["known"])

    def test_lost_registration_reply_and_reopened_account_retrieve_original_grant(self):
        original = self.ledger._account_transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError("PRIVATE-REGISTRATION-LOST-REPLY")

        with mock.patch.object(self.ledger, "_account_transaction", new=lose_reply), \
                self.assertRaisesRegex(AccountError, "account_unavailable"):
            self.call()
        first = self.call()
        self.assertTrue(first["known"])
        old = self.ledger
        old.close()
        bridge = self.fixture.base.fixture.fixture
        self.fixture.base.ledger = LearningLedger(bridge.state, client_capabilities=bridge.clients)
        self.addCleanup(self.ledger.close)
        self.service = self.make_service()
        self.assertEqual(first, self.call())
        self.assertEqual(1, self.count("capacity_executions"))

    def test_reopened_account_cannot_mint_a_new_registration_or_extend_deadline(self):
        old = self.ledger
        old.close()
        bridge = self.fixture.base.fixture.fixture
        self.fixture.base.ledger = LearningLedger(bridge.state, client_capabilities=bridge.clients)
        self.addCleanup(self.ledger.close)
        self.service = self.make_service()
        with self.assertRaisesRegex(AccountError, "claim_execution_unavailable"):
            self.call()
        self.assertEqual(0, self.count("capacity_executions"))
        self.assertIsNone(self.ledger._connection.execute("SELECT released_at_ms FROM active_run_capacity").fetchone()[0])

    def test_exact_task_route_and_configuration_are_checked_before_local_consumption(self):
        grant = self.grant()
        task = self.fixture.base.fixture.fixture.task()
        task["objective"] += " PRIVATE-CHANGED"
        route = dict(self.fixture.base.fixture.route)
        route["model"] = "gpt-6-astra"
        for changes in ({"task": task}, {"route": route}):
            with self.assertRaisesRegex(HistoryError, "history_conflict"):
                self.consume(grant, **changes)
        self.assertEqual(0, self.fixture.store._connection.execute("SELECT COUNT(*) FROM project_execution_consumptions").fetchone()[0])
        self.consume(grant)

    def test_noncanonical_forged_and_oversized_grants_never_consume(self):
        grant = self.grant()
        raw = grant.encode()
        changed = json.loads(raw)
        changed["grant"]["registration_digest"] = "f" * 64
        extra = json.loads(raw)
        extra["grant"]["pid"] = 1
        invalid = (raw + b"\n", canonical_json(changed), canonical_json(extra),
                   b"PRIVATE-ARBITRARY", b"x" * 8193,
                   b'{"protocol_version":1,"protocol_version":1,' + raw[1:])
        options = dict(task=self.fixture.base.fixture.fixture.task(), route=self.fixture.base.fixture.route)
        for data in invalid:
            with self.subTest(length=len(data)), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.fixture.store.consume_execution_grant(self.binding, data, **options)
        self.assertEqual(0, self.fixture.store._connection.execute("SELECT COUNT(*) FROM project_execution_consumptions").fetchone()[0])
        self.consume(grant)

    def test_original_deadline_cannot_be_extended_by_delayed_consumption(self):
        grant = self.grant()
        self.fixture.base.stamp = grant.deadline_at_ms
        with self.assertRaisesRegex(HistoryError, "history_execution_not_ready"):
            self.consume(grant)
        self.assertEqual(0, self.fixture.store._connection.execute("SELECT COUNT(*) FROM project_execution_consumptions").fetchone()[0])
        self.assertIsNone(self.ledger._connection.execute("SELECT released_at_ms FROM active_run_capacity").fetchone()[0])

    def test_lost_project_consumption_reply_never_returns_another_issuable_consumption(self):
        grant = self.grant()
        original = self.fixture.store._transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError("PRIVATE-CONSUMPTION-LOST-REPLY")

        with mock.patch.object(self.fixture.store, "_transaction", new=lose_reply), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.consume(grant)
        self.fixture.store.close()
        self.fixture.store = self.fixture.open_history()
        with self.assertRaisesRegex(HistoryError, "history_execution_consumed"):
            self.consume(grant)
        self.assertEqual(1, self.fixture.store._connection.execute("SELECT COUNT(*) FROM project_execution_consumptions").fetchone()[0])

    def test_ordinary_schema3_open_refuses_and_explicit_upgrade_preserves_original_bindings(self):
        grant = self.grant()
        before_lease = replace(self.fixture.store.result_lease(self.fixture.scope), cleanup_reservation_digest=None)
        before_result = self.fixture.store.result_reservation(self.binding)
        legacy_cleanup_layout(self.fixture.store)
        self.fixture.store._connection.execute('DROP TABLE project_operational_qa')
        self.fixture.store._connection.execute('DROP TABLE project_qa_check_requests')
        self.fixture.store._connection.execute('DROP TABLE project_qa_check_receipts')
        self.fixture.store._connection.execute('DROP TABLE project_host_opens')
        self.fixture.store._connection.execute("DROP TABLE project_execution_consumptions")
        for table in ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations'):
            self.fixture.store._connection.execute('DROP TABLE ' + table)
        self.fixture.store._connection.execute("PRAGMA user_version=3")
        self.fixture.store.close()
        path = self.fixture.directory / "history.sqlite3"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.fixture.open_history()
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
        self.fixture.store = ProjectHistory(self.fixture.directory, self.fixture.project, result_key=self.fixture.key,
                                             result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
                                             result_clock_ms=lambda: self.fixture.base.stamp, allow_schema_upgrade=True,
                                             allow_legacy_orchestration_reference=True)
        self.assertEqual(19, self.fixture.store._connection.execute("PRAGMA user_version").fetchone()[0])  # (requirement) SB: schema 19
        self.assertEqual(before_lease, self.fixture.store.result_lease(self.fixture.scope))
        self.assertEqual(before_result, self.fixture.store.result_reservation(self.binding))
        self.consume(grant)


if __name__ == "__main__":
    unittest.main()
