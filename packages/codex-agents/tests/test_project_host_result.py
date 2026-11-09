"""Actual project/account stores, synthetic enrollment; no provider execution."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_project_execution_grant as fixtures
from tests.test_bridge import result

from codex_agents import history_api
from codex_agents.bridge import _fenced_result
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_result_contracts import HostResultContext, HostResultReference
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import build_host_result_records
from codex_agents.learning_adapters import empty_usage
from codex_agents.result_contracts import ResultObservation
from codex_agents.service_transport import ServiceTransportError
from tests.learning_support import event_record


class ProjectHostResultTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectExecutionGrantTests(methodName="runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.grant = self.base.grant()
        self.base.consume(self.grant)
        self.binding = self.base.binding
        source = self.fixture.base.fixture
        self.inputs = HostResultContext(self.binding, self.fixture.assigned, source.fixture.task(), source.route,
                                       source.fixture.clients["bridge-v1"])
        self.body = result()
        self.body["summary"] = "PRIVATE-RESULT-BODY-CANARY"
        self.delivery = self.fixture.store.publish_result(
            self.binding, self.fixture.assigned, canonical_json(self.body),
            ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None),
        )
        self.reference = HostResultReference(self.binding, self.delivery.record_id,
                                             self.delivery.record_content_digest, self.delivery.result_digest)
        self.context = object()
        self.authorizations = 0
        self.revoke = False
        self.source = "host"
        self.service = self.make_service()
        self.client = HistoryClient(Path("/private/tmp/fixture-host-result.sock"), self.binding.project_id,
                                     self.binding.runtime_digest, timeout=1)

    def authorize(self, request, context):
        if context is not self.context:
            return None
        self.authorizations += 1
        if self.revoke and self.authorizations > 1:
            return None
        return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), self.source,
                                    frozenset({"history.admit_host_result", "history.read_record"}), self.inputs)

    def make_service(self, authorizer=None):
        service = HistoryService(self.binding.project_id, self.binding.runtime_digest, lambda: self.fixture.store,
                                  authorizer=authorizer or self.authorize)
        self.addCleanup(service.close)
        return service

    def request(self, reference=None):
        reference = reference or self.reference
        return HistoryRequest(1, "history.admit_host_result", reference.binding.claim_id,
                              reference.binding.project_id, reference.binding.runtime_digest, reference.as_data())

    def call(self, reference=None, service=None, context=None):
        command = self.request(reference)
        return decode_history_response((service or self.service).handle(encode_history_request(command),
                                        self.context if context is None else context), command)

    def records(self, record_type="host_result_admission"):
        return self.fixture.store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type=?",
                                                       (record_type,)).fetchone()[0]

    def assert_held(self):
        self.assertIsNone(self.base.ledger._connection.execute("SELECT released_at_ms FROM active_run_capacity").fetchone()[0])
        self.assertEqual(0, len(self.base.ledger._capacity_executions._pending))

    def test_client_service_derives_exact_existing_unverified_records_without_account_outcomes(self):
        with mock.patch.object(history_api, "exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, self.context)):
            admitted = self.client.admit_host_result(self.reference)
            receipt = admitted["admission"]
            outcome = self.client.read_record(receipt["outcome_record_id"])["record"]
            event = self.client.read_record(receipt["event_record_id"])["record"]
        expected_event, expected_outcome, _ = build_host_result_records(
            assignment=self.inputs.assignment, task=self.inputs.task, result=self.body,
            observation={"run_id": self.binding.run_id, "client_version": "bridge-v1",
                         "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]},
            host_evidence={"run_id": self.binding.run_id, "coverage": "lifecycle_partial", "usage": empty_usage(),
                           "usage_source": "host_observed", "observed_at_ms": self.fixture.base.stamp, "no_progress_events": 0},
            execution_configuration=self.inputs.route["execution_configuration"], digest_key=self.fixture.key,
            client_capability=self.inputs.client_capability,
        )
        self.assertEqual((expected_event, expected_outcome), (event, outcome))
        self.assertEqual(content_digest(outcome), receipt["host_outcome_digest"])
        self.assertEqual("unverified", outcome["admission_state"])
        self.assertFalse(outcome["accepted"])
        self.assertFalse(outcome["deterministic_pass"])
        self.assertFalse(outcome["security_pass"])
        self.assertNotIn(b"PRIVATE-RESULT-BODY-CANARY", canonical_json(admitted))
        self.assertEqual(0, self.base.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='outcome'").fetchone()[0])
        self.assert_held()

    def test_known_receipt_survives_body_retirement_reopen_and_clock_rollback(self):
        first = self.call()
        self.fixture.store.retire_result(self.binding)
        self.service.close()
        self.fixture.store = self.fixture.open_history()
        self.service = self.make_service()
        self.fixture.base.stamp -= 1
        with mock.patch.object(self.fixture.store, "read_result", side_effect=AssertionError("body read")), \
                mock.patch.object(self.fixture.store, "_result_now", side_effect=AssertionError("clock refreshed")):
            self.assertEqual(first, self.call())
        self.assertEqual(1, self.records())
        self.assert_held()

    def test_missing_body_before_first_admission_cannot_reconstruct_an_outcome(self):
        self.fixture.store.retire_result(self.binding)
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.call()
        self.assertEqual(0, self.records())
        self.assertEqual(0, self.records("outcome"))
        self.assert_held()

    def test_original_inputs_and_capability_are_bound_to_consumed_registration(self):
        original = self.inputs
        changes = (("assignment", "summary", "PRIVATE-CHANGED"), ("task", "family", "planning"),
                   ("route", "provider", "claude"), ("client_capability", "version", "PRIVATE-CHANGED"))
        for name, field, value in changes:
            changed = deepcopy(getattr(original, name))
            changed[field] = value
            self.inputs = replace(original, **{name: changed})
            with self.subTest(field=name), self.assertRaises(HistoryError) as caught:
                self.call()
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(0, self.records())
        self.inputs = original
        self.call()

    def test_independently_expected_attempt_scope_is_required_before_open(self):
        original = self.inputs
        for field, value in (("primary_session_id", "foreign-primary"), ("supervisor_instance_id", "foreign-owner"),
                             ("supervisor_generation", 2), ("attempt", 2)):
            self.inputs = replace(original, binding=replace(self.binding, **{field: value}))
            with self.subTest(field=field), self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.call()
            self.assertIsNone(self.service._store)
        self.assertEqual(0, self.records())

    def test_changed_terminal_scope_and_missing_original_consumption_are_refused(self):
        for reference in (replace(self.reference, terminal_record_id="other"),
                          replace(self.reference, terminal_content_digest="f" * 64),
                          replace(self.reference, result_digest="f" * 64),
                          replace(self.reference, binding=replace(self.binding, supervisor_generation=2))):
            with self.assertRaises(HistoryError):
                self.call(reference)
        self.fixture.store._connection.execute("DELETE FROM project_execution_consumptions")
        with self.assertRaisesRegex(HistoryError, "history_execution_not_ready"):
            self.call()
        self.assertEqual(0, self.records())
        self.assert_held()

    def test_default_denial_and_non_host_sources_cannot_admit(self):
        opened = []
        default = HistoryService(self.binding.project_id, self.binding.runtime_digest, lambda: opened.append(True))
        self.addCleanup(default.close)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.call(service=default)
        self.assertEqual([], opened)
        for source in ("hook", "app_server"):
            self.source = source
            with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.call()
        self.source = "host"
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.call(context={"role": "host", "authorized": True})
        self.assertEqual(0, self.records())

    def test_revocation_and_input_mutation_after_body_io_prevent_commit(self):
        self.revoke = True
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.call()
        self.assertEqual(0, self.records("outcome"))
        self.revoke, self.authorizations = False, 0
        original = self.fixture.store.read_result

        def mutate(binding):
            body = original(binding)
            self.inputs.task["description"] = "PRIVATE-MUTATION"
            return body

        with mock.patch.object(self.fixture.store, "read_result", side_effect=mutate), \
                self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.call()
        self.assertEqual(0, self.records("outcome"))

    def test_atomic_rollback_and_lost_commit_reply_reconcile_without_duplicate(self):
        original = self.fixture.store._transaction

        @contextmanager
        def rollback():
            with original():
                yield
                raise OSError("PRIVATE-ROLLBACK")

        with mock.patch.object(self.fixture.store, "_transaction", new=rollback), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.call()
        self.assertEqual(0, self.records("outcome"))
        self.assertEqual(0, self.records("event"))

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError("PRIVATE-LOST-COMMIT-REPLY")

        with mock.patch.object(self.fixture.store, "_transaction", new=lose_reply), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.call()
        self.assertEqual(1, self.records())
        first = self.call()
        self.assertEqual(first, self.call())
        self.assertEqual(1, self.records("outcome"))
        self.assert_held()

    def test_closed_request_has_no_result_repair_or_configuration_override(self):
        raw = json.loads(encode_history_request(self.request()))
        for name in ("result", "observation", "repair_receipt", "task", "route", "accepted", "pid"):
            changed = deepcopy(raw)
            changed["payload"][name] = "PRIVATE-OVERRIDE"
            reply = json.loads(self.service.handle(canonical_json(changed), self.context))
            self.assertEqual("history_invalid_request", reply["reason_code"])
            self.assertNotIn("PRIVATE", str(reply))
        self.assertEqual(0, self.records())

    def test_lost_transport_reply_never_auto_retries_and_explicit_retry_is_exact(self):
        def lose(path, raw, **opts):
            self.service.handle(raw, self.context)
            raise ServiceTransportError("transport_unavailable")

        with mock.patch.object(history_api, "exchange_frame", side_effect=lose) as exchange, \
                self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            self.client.admit_host_result(self.reference)
        self.assertEqual(1, exchange.call_count)
        self.assertEqual(1, self.records())
        with mock.patch.object(history_api, "exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, self.context)):
            first = self.client.admit_host_result(self.reference)
            self.assertEqual(first, self.client.admit_host_result(self.reference))

    def test_fenced_invalid_return_keeps_exact_reason_and_cannot_become_accepted(self):
        second = fixtures.ProjectExecutionGrantTests(methodName="runTest")
        second.setUp()
        self.addCleanup(second.doCleanups)
        second.consume(second.grant())
        fixture = second.fixture
        body = _fenced_result(fixture.assigned, "failed", "invalid_result")
        delivery = fixture.store.publish_result(
            second.binding, fixture.assigned, canonical_json(body),
            ResultObservation(second.binding.claim_id, fixture.base.stamp, True, False, "failed", "assignment_mismatch"),
        )
        reference = HostResultReference(second.binding, delivery.record_id, delivery.record_content_digest, delivery.result_digest)
        source = fixture.base.fixture
        admitted = fixture.store.admit_host_result(reference, assignment=fixture.assigned, task=source.fixture.task(),
                                                    route=source.route, client_capability=source.fixture.clients["bridge-v1"],
                                                    before_commit=lambda: None)
        terminal = admitted["admission"]["dispatch_result"]
        outcome = json.loads(fixture.store.record(admitted["admission"]["outcome_record_id"]).content)
        self.assertEqual("assignment_mismatch", terminal["result_validation_reason_code"])
        self.assertTrue(terminal["fenced"])
        self.assertFalse(terminal["reviewed"])
        self.assertEqual("failed", outcome["execution_state"])
        self.assertEqual("rejected", outcome["admission_state"])
        self.assertFalse(outcome["accepted"])

    def test_expiry_during_authority_recheck_prevents_fresh_admission(self):
        original = self.authorize

        def expire(request, context):
            authorization = original(request, context)
            if self.authorizations == 2:
                self.fixture.base.stamp = self.fixture.store.result_reservation(self.binding).expires_at_ms
            return authorization

        service = self.make_service(expire)
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.call(service=service)
        self.assertEqual(0, self.records("outcome"))

    def test_corrupted_receipt_link_is_not_repaired(self):
        first = self.call()
        identity = first["admission"]["record_id"]
        entry = self.fixture.store.record(identity)
        value = json.loads(entry.content)
        value["host_outcome_digest"] = "f" * 64
        changed = canonical_json(value)
        self.fixture.store._connection.execute("UPDATE records SET content=?,content_digest=? WHERE record_id=?",
                                                (changed, content_digest(value), identity))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call()
        self.assertEqual(changed, self.fixture.store.record(identity).content)
        self.assertEqual(1, self.records())

    def test_receipt_namespace_collision_cannot_be_overwritten(self):
        scope = keyed_digest(self.fixture.key, b"project-host-result-v1\0" + canonical_json(self.binding.as_data()))
        collision = {**event_record(), "record_id": "host_result_admission:" + scope}
        entry = self.fixture.store.append_record(canonical_json(collision), source="host",
                                                 ingested_at_ms=self.fixture.base.stamp)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call()
        self.assertEqual(entry, self.fixture.store.record(entry.record_id))
        self.assertEqual(0, self.records("outcome"))

    def test_response_links_and_terminal_diagnostics_cannot_be_changed(self):
        request = self.request()
        reply = json.loads(self.service.handle(encode_history_request(request), self.context))
        for label in ("admission_commit", "event_commit", "outcome_commit"):
            changed = deepcopy(reply)
            changed["result"][label]["content_digest"] = "f" * 64
            with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(canonical_json(changed), request)
        changed = deepcopy(reply)
        changed["result"]["admission"]["dispatch_result"]["summary"] = "PRIVATE-REJECTED-CONTENT"
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(canonical_json(changed), request)

    def test_admission_record_cannot_be_inserted_through_generic_append(self):
        admitted = self.call()
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.fixture.store.append_record(canonical_json(admitted["admission"]), source="host",
                                             ingested_at_ms=self.fixture.base.stamp)
        with self.assertRaisesRegex(HistoryError, "history_invalid_request"):
            HistoryRequest(1, "history.record_outcome", admitted["admission"]["record_id"],
                           self.binding.project_id, self.binding.runtime_digest, {"record": admitted["admission"]})

    def test_client_import_chain_cannot_load_sqlite(self):
        program = """
import sys
sys.path.insert(0, sys.argv[1])
class NoSql:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('sqlite3', '_sqlite3'):
            raise AssertionError('client imported SQLite')
sys.meta_path.insert(0, NoSql())
from codex_agents.history_api import HistoryClient
from codex_agents.host_result_contracts import HostResultReference
"""
        completed = subprocess.run([sys.executable, "-B", "-c", program, str(ORCHESTRATION_ROOT)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_actual_socket_client_reconciles_project_owned_outcome_on_writer_thread(self):
        # Explicit synthetic frontend, not native caller/enrollment evidence.
        self.fixture.store.close()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        endpoint = Path(temporary.name) / "history.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(endpoint))
        listener.listen(2)
        listener.settimeout(3)
        errors = []

        def owner():
            service = HistoryService(self.binding.project_id, self.binding.runtime_digest, self.fixture.open_history,
                                     authorizer=self.authorize)
            try:
                for _ in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        raw = bytearray()
                        while not raw.endswith(b"\n") and len(raw) <= 16_384:
                            part = connection.recv(4096)
                            if not part:
                                raise RuntimeError("fixture_incomplete_request")
                            raw.extend(part)
                        connection.sendall(service.handle(bytes(raw[:-1]), self.context) + b"\n")
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                service.close()

        writer = threading.Thread(target=owner)
        writer.start()
        try:
            client = HistoryClient(endpoint, self.binding.project_id, self.binding.runtime_digest, timeout=2)
            first = client.admit_host_result(self.reference)
            self.assertEqual(first, client.admit_host_result(self.reference))
        finally:
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual([], errors)
        self.fixture.store = self.fixture.open_history()
        self.assertEqual(first["admission_commit"]["content_digest"],
                         self.fixture.store.record(first["admission"]["record_id"]).content_digest)
        self.assertEqual(1, self.records("outcome"))
        self.assert_held()
