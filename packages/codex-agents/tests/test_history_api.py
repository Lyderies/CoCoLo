from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from codex_agents.learning_contracts import canonical_json, validate_learning_record
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity
from codex_agents.service_transport import MAX_FRAME_BYTES
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
            self.assertEqual(command.project_id, reply["project_id"])
            self.assertEqual(command.runtime_digest, reply["runtime_digest"])
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

    def test_borrowed_project_writer_is_reconsulted_and_never_closed_by_service(self):
        store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(store.close)
        borrowed = mock.Mock(return_value=store)
        service = HistoryService(PROJECT_A, RUNTIME, borrowed, authorizer=fixture_authorizer,
                                 clock_ms=lambda: 1000, close_history=False)
        self.addCleanup(service.close)
        command = request()
        first = decode_history_response(service.handle(encode_history_request(command), FIXTURE_CONTEXT), command)
        self.assertEqual(first["source_sequence"], 1)
        before = borrowed.call_count
        read = request("history.read_record", identity="read", payload={"record_id": event_record()["record_id"]})
        decode_history_response(service.handle(encode_history_request(read), FIXTURE_CONTEXT), read)
        self.assertGreater(borrowed.call_count, before)
        service.close()
        self.assertEqual(store.record(event_record()["record_id"]).content, canonical_json(event_record()))

    def test_borrowed_foreign_project_is_refused_without_closing_its_owner(self):
        store = ProjectHistory(self.directory, PROJECT_B)
        self.addCleanup(store.close)
        service = HistoryService(PROJECT_A, RUNTIME, lambda: store, authorizer=fixture_authorizer,
                                 clock_ms=lambda: 1000, close_history=False)
        self.addCleanup(service.close)
        command = request()
        reply = json.loads(service.handle(encode_history_request(command), FIXTURE_CONTEXT))
        self.assertFalse(reply["ready"])
        service.close()
        self.assertEqual(store.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000).source_sequence, 1)

    def test_borrowed_store_substitution_refuses_and_preserves_both_owners(self):
        first = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(first.close)
        other_directory = self.base / "other-history"
        other_directory.mkdir(mode=0o700)
        other = ProjectHistory(other_directory, PROJECT_A)
        self.addCleanup(other.close)
        current = [first]
        service = HistoryService(PROJECT_A, RUNTIME, lambda: current[0], authorizer=fixture_authorizer,
                                 clock_ms=lambda: 1000, close_history=False)
        self.addCleanup(service.close)
        command = request()
        decode_history_response(service.handle(encode_history_request(command), FIXTURE_CONTEXT), command)
        current[0] = other
        self.assertFalse(json.loads(service.handle(encode_history_request(command), FIXTURE_CONTEXT))["ready"])
        service.close()
        self.assertEqual(first.record(event_record()["record_id"]).source_sequence, 1)
        self.assertEqual(other.append_record(canonical_json(event_record()), source="host", ingested_at_ms=1000).source_sequence, 1)

    def test_borrowed_owner_revocation_is_observed_before_reuse(self):
        store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(store.close)
        live = [True]
        def original_owner():
            if not live[0]:
                raise HistoryError("history_service_unavailable")
            return store
        service = HistoryService(PROJECT_A, RUNTIME, original_owner, authorizer=fixture_authorizer,
                                 clock_ms=lambda: 1000, close_history=False)
        self.addCleanup(service.close)
        command = request()
        decode_history_response(service.handle(encode_history_request(command), FIXTURE_CONTEXT), command)
        live[0] = False
        reply = json.loads(service.handle(encode_history_request(command), FIXTURE_CONTEXT))
        self.assertFalse(reply["ready"])
        self.assertEqual(reply["reason_code"], "history_service_unavailable")
        self.assertEqual(store.record(event_record()["record_id"]).source_sequence, 1)

    def test_borrowed_service_withholds_committed_reply_if_encoder_closes_it(self):
        store = ProjectHistory(self.directory, PROJECT_A)
        self.addCleanup(store.close)
        service = HistoryService(PROJECT_A, RUNTIME, lambda: store, authorizer=fixture_authorizer,
                                 clock_ms=lambda: 1000, close_history=False)
        self.addCleanup(service.close)
        original = service._reply
        def encode_then_close(*args, **kwargs):
            reply = original(*args, **kwargs)
            if kwargs.get("result") is not None:
                service.close()
            return reply
        command = request()
        with mock.patch.object(service, "_reply", side_effect=encode_then_close):
            reply = json.loads(service.handle(encode_history_request(command), FIXTURE_CONTEXT))
        self.assertFalse(reply["ready"])
        self.assertEqual(store.record(event_record()["record_id"]).source_sequence, 1)

    def test_named_record_operations_round_trip_their_exact_records(self):
        intent = {
            "schema_version": 1, "record_type": "dispatch_intent", "record_id": "intent-api",
            "run_id": "run-api", "bridge_run_id": "brg_" + "a" * 32, "provider": "codex",
            "model": "gpt-6-astra", "effort": "high", "observed_at_ms": 1000, "deadline_at_ms": 2000,
        }
        verifier = {"schema_version": 1, "record_type": "verification_binding", "record_id": "verify-api",
                    "run_id": "run-verifier", "worker_outcome_digest": "b" * 64, "observed_at_ms": 1000}
        client = HistoryClient(self.base / "unused.sock", PROJECT_A, RUNTIME, timeout=1)
        unverified = verification_outcome()
        unverified.update(status='unverified', accepted=False, admission_state='unverified')
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, FIXTURE_CONTEXT)):
            for method, record in ((client.record_intent, intent), (client.record_outcome, unverified),
                                   (client.bind_verifier, verifier)):
                metadata = method(record)
                self.assertEqual(record["record_id"], metadata["record_id"])
                self.assertEqual(canonical_json(record), canonical_json(client.read_record(record["record_id"])["record"]))
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=AssertionError("transport used")), \
                self.assertRaises(HistoryError):
            client.record_intent(verification_outcome())

    def test_host_record_outcome_cannot_bypass_original_result_and_qa_commit(self):
        record = verification_outcome('generic-accepted')
        command = request('history.record_outcome', identity=record['record_id'], payload={'record': record})
        with self.assertRaisesRegex(HistoryError, '^history_invalid_input$'):
            self.call(command)
        self.assertEqual(0, self.service._store._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        unverified = {**record, 'record_id': 'generic-unverified', 'status': 'unverified',
                      'accepted': False, 'admission_state': 'unverified'}
        command = request('history.record_outcome', identity=unverified['record_id'], payload={'record': unverified})
        first = self.call(command)
        self.assertEqual(first, self.call(command))
        self.assertEqual(1, self.service._store._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])

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

    def test_recycled_numeric_thread_identity_does_not_adopt_a_handler(self):
        owner_ident = threading.get_ident()
        authorize = mock.Mock(wraps=fixture_authorizer)
        factory = mock.Mock(side_effect=AssertionError("foreign owner opened persistence"))
        service = HistoryService(PROJECT_A, RUNTIME, factory, authorizer=authorize)
        self.addCleanup(service.close)
        replies = []

        def foreign_thread():
            replies.append(json.loads(service.handle(encode_history_request(request()), FIXTURE_CONTEXT)))
            try:
                service.close()
            except HistoryError as exc:
                replies.append(exc.reason_code)
            else:
                replies.append("closed")

        # Model OS reuse of a numeric thread ID while retaining the actual new
        # Thread object. Rebinding this module's alias does not patch threading's
        # own registry or SQLite's independent thread check.
        replacement = SimpleNamespace(get_ident=lambda: owner_ident, current_thread=threading.current_thread)
        with mock.patch("codex_agents.history_service.threading", replacement):
            worker = threading.Thread(target=foreign_thread)
            worker.start()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual("history_wrong_owner", replies[0]["reason_code"])
        self.assertEqual("history_wrong_owner", replies[1])
        authorize.assert_not_called()
        factory.assert_not_called()

    @unittest.skipUnless(hasattr(os, "fork"), "process-affinity check requires fork")
    def test_forked_handler_cannot_authorize_open_or_close_history(self):
        for already_open in (False, True):
            with self.subTest(already_open=already_open):
                if already_open:
                    self.call(request())
                authorize = mock.Mock(wraps=fixture_authorizer)
                self.service._authorizer = authorize
                child_command = request(payload={"record": event_record("evt-foreign-process")})
                reader, writer = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(reader)
                    try:
                        before = len(self.opened)
                        reply = json.loads(self.service.handle(encode_history_request(child_command), FIXTURE_CONTEXT))
                        try:
                            self.service.close()
                        except HistoryError as exc:
                            close_reason = exc.reason_code
                        else:
                            close_reason = "closed"
                        facts = {"handle_reason": reply["reason_code"], "close_reason": close_reason,
                                 "authorize_calls": authorize.call_count, "factory_calls": len(self.opened)-before}
                        os.write(writer, canonical_json(facts))
                    finally:
                        os.close(writer)
                        os._exit(0)
                os.close(writer)
                try:
                    ready = bool(select.select([reader], [], [], 5)[0])
                    if not ready:
                        os.kill(child, signal.SIGKILL)
                    raw = os.read(reader, 4096) if ready else b""
                finally:
                    os.close(reader)
                    _, status = os.waitpid(child, 0)
                self.assertTrue(ready, "inherited handler did not refuse promptly")
                self.assertEqual(0, os.waitstatus_to_exitcode(status))
                self.assertEqual({"handle_reason": "history_wrong_owner", "close_reason": "history_wrong_owner",
                                  "authorize_calls": 0, "factory_calls": 0}, json.loads(raw))
                self.call(request())
                with self.assertRaisesRegex(HistoryError, "history_not_found"):
                    self.call(request("history.read_record", identity="check-foreign",
                                      payload={"record_id": "evt-foreign-process"}))

    def test_malformed_or_open_fields_have_fixed_errors_without_echo(self):
        valid = json.loads(encode_history_request(request()))
        variants = [b"CANARY-private-input", b"\xff", b'{"x":1,"x":2}', b"{\"x\":NaN}", b"x" * 81_920]
        variants.extend(canonical_json({**valid, "operation": value}) for value in ([], {}, None, True))
        for field in ("producer", "role", "sql", "method", "path", "pid", "authorized"):
            variants.append(canonical_json({**valid, field: "CANARY-private-input"}))
        for raw in variants:
            with self.subTest(length=len(raw)), self.assertRaises(HistoryError) as caught:
                decode_history_request(raw)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("CANARY", str(caught.exception))
            result = self.service.handle(raw, FIXTURE_CONTEXT)
            self.assertFalse(json.loads(result)["ready"])
            self.assertEqual(caught.exception.reason_code, json.loads(result)["reason_code"])
            self.assertNotIn(b"CANARY", result)
            self.assertIsNone(json.loads(result)["project_id"])
            self.assertIsNone(json.loads(result)["runtime_digest"])
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
        record["client_diagnostics"] = {
            "exit_code": 1, "result_frame_seen": True, "result_subtype": "error_during_execution", "error_code": "server_error",
        }
        command = request("history.commit_terminal", identity="claim-1", payload={"record": record, "binding": binding().as_data()})
        raw = self.service.handle(encode_history_request(command), FIXTURE_CONTEXT)
        receipt = decode_history_response(raw, command)
        self.assertEqual(record, receipt["dispatch_result"])
        self.assertEqual("invalid_shape", receipt["dispatch_result"]["result_validation_reason_code"])
        self.assertTrue(receipt["dispatch_result"]["fenced"])
        self.assertEqual(record["client_diagnostics"], receipt["dispatch_result"]["client_diagnostics"])
        forged = json.loads(raw)
        forged["result"]["dispatch_result"]["result_validation_reason_code"] = "CANARY-REJECTED-CONTENT"
        with self.assertRaisesRegex(HistoryError, "history_invalid_response") as caught:
            decode_history_response(canonical_json(forged), command)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))

    def test_ack_reply_cannot_be_substituted_for_changed_receipt_parameters(self):
        commit = request("history.commit_terminal", identity="claim-1",
                         payload={"record": json.loads(terminal()), "binding": binding().as_data()})
        delivery = self.call(commit)
        payload = {"claim_id": "claim-1", "record_content_digest": delivery["record_content_digest"], "receipt_digest": "c" * 64}
        command = request("history.acknowledge_terminal", identity="claim-1", payload=payload)
        reply = self.service.handle(encode_history_request(command), FIXTURE_CONTEXT)
        self.assertEqual({"recorded": True}, decode_history_response(reply, command))
        for changes in ({"receipt_digest": "d" * 64}, {"record_content_digest": "f" * 64}):
            changed = request("history.acknowledge_terminal", identity="claim-1", payload={**payload, **changes})
            with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(reply, changed)

    def test_cross_runtime_commit_is_refused_but_historical_pending_binding_is_retained(self):
        record = json.loads(terminal())
        foreign = {**binding().as_data(), "runtime_digest": "b" * 64}
        valid = request("history.commit_terminal", identity="claim-1", payload={"record": record, "binding": binding().as_data()})
        raw = json.loads(encode_history_request(valid))
        raw["payload"]["binding"] = foreign
        reply = json.loads(self.service.handle(canonical_json(raw), FIXTURE_CONTEXT))
        self.assertFalse(reply["ready"])
        self.assertEqual("history_invalid_request", reply["reason_code"])
        self.assertEqual([], self.opened)
        from codex_agents.history_contracts import AttemptBinding
        store = ProjectHistory(self.directory, PROJECT_A)
        store.commit_terminal(terminal(), AttemptBinding(**foreign), ingested_at_ms=1000)
        store.close()
        page = self.call(request("history.pending_terminals", identity="pending", payload={"after_sequence": 0, "limit": 1}))
        self.assertEqual("b" * 64, page["deliveries"][0]["binding"]["runtime_digest"])
        payload = {"claim_id": "claim-1", "record_content_digest": page["deliveries"][0]["record_content_digest"],
                   "receipt_digest": "c" * 64}
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.call(request("history.acknowledge_terminal", identity="claim-1", payload=payload))
        unchanged = self.call(request("history.pending_terminals", identity="pending-again", payload={"after_sequence": 0, "limit": 1}))
        self.assertEqual(page, unchanged)

    def test_client_selects_the_explicit_record_read_response_ceiling(self):
        client = HistoryClient(self.base / "unused.sock", PROJECT_A, RUNTIME, timeout=1)
        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=lambda path, raw, **opts: self.service.handle(raw, FIXTURE_CONTEXT)) as wire:
            client.record_event(event_record())
            client.read_record("evt_1")
        self.assertEqual([MAX_HISTORY_REPLY_BYTES, MAX_FRAME_BYTES], [call.kwargs["response_limit"] for call in wire.call_args_list])

    def test_read_and_pending_require_history_types_and_exact_integer_versions(self):
        self.call(request())
        read = request("history.read_record", identity="read", payload={"record_id": "evt_1"})
        original = json.loads(self.service.handle(encode_history_request(read), FIXTURE_CONTEXT))
        for version in (True, 1.0):
            response = json.loads(canonical_json(original))
            record = response["result"]["record"]
            record["schema_version"] = version
            # Preserve the shared legacy reader's exact historical semantics.
            self.assertEqual(canonical_json(record), canonical_json(validate_learning_record(record)))
            response["result"]["metadata"]["content_digest"] = hashlib.sha256(canonical_json(record)).hexdigest()
            with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(canonical_json(response), read)
        response = json.loads(canonical_json(original))
        record = {"schema_version": 1, "record_type": "provider_headroom", "record_id": "evt_1", "provider": "codex",
                  "account_fingerprint": "a" * 64, "windows": [], "status": None, "run_id": None,
                  "phase": None, "observed_at_ms": 1000}
        validate_learning_record(record)
        response["result"]["record"] = record
        response["result"]["metadata"]["content_digest"] = hashlib.sha256(canonical_json(record)).hexdigest()
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(canonical_json(response), read)

        commit = request("history.commit_terminal", identity="claim-1",
                         payload={"record": json.loads(terminal()), "binding": binding().as_data()})
        self.call(commit)
        pending = request("history.pending_terminals", identity="pending", payload={"after_sequence": 0, "limit": 1})
        original = json.loads(self.service.handle(encode_history_request(pending), FIXTURE_CONTEXT))
        for version in (True, 1.0):
            response = json.loads(canonical_json(original))
            data = response["result"]["deliveries"][0]
            record = data["dispatch_result"]
            record.pop("client_diagnostics")
            record.pop("result_validation_reason_code")
            record["schema_version"] = version
            validate_learning_record(record)
            data["record_content_digest"] = hashlib.sha256(canonical_json(record)).hexdigest()
            with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(canonical_json(response), pending)

    def test_post_commit_reply_fault_keeps_durable_truth_and_exact_retry(self):
        command = request()
        original_reply = self.service._reply

        def reply_fault(identity, **arguments):
            if arguments.get("reason") is None:
                raise RuntimeError("CANARY-response-fault")
            return original_reply(identity, **arguments)
        with mock.patch.object(self.service, "_reply", side_effect=reply_fault):
            raw = self.service.handle(encode_history_request(command), FIXTURE_CONTEXT)
        with self.assertRaisesRegex(HistoryError, "history_service_unavailable") as caught:
            decode_history_response(raw, command)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))
        entry = self.service._store.record("evt_1")
        self.assertEqual(canonical_json(event_record()), entry.content)
        retry = self.call(command)
        self.assertEqual(1, retry["source_sequence"])
        self.assertEqual(entry.ingested_at_ms, retry["ingested_at_ms"])

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

    def test_learning_cycle_is_polled_by_its_request_and_its_report_is_paged(self):
        directory = self.base / "keyed"
        directory.mkdir(mode=0o700)
        self.service = HistoryService(
            PROJECT_A, RUNTIME, lambda: ProjectHistory(
                directory, PROJECT_A, result_key=b"K" * 32,
                result_capacity=ResultCapacity(1, 1_048_576, 120_000, 120_000)),
            authorizer=fixture_authorizer, clock_ms=lambda: 1000)
        self.addCleanup(self.service.close)
        cycle = request("history.learning_cycle", payload={"max_age_ms": 1000}, identity="cycle-1")
        view = self.call(cycle)
        self.assertIsNone(view["reason_code"])
        for _ in range(2000):
            if view["state"] != "pending":
                break
            time.sleep(0.002)
            view = self.call(cycle)
        self.assertEqual("ready", view["state"])
        self.assertEqual("compact-learning-cycle-v1", view["summary"]["format"])
        self.assertEqual(0, view["summary"]["mined_case_count"])
        page = request("history.learning_report", payload={"offset": 0}, identity="cycle-1")
        part = self.call(page)
        self.assertEqual(view["report_bytes"], part["report_bytes"])
        self.assertEqual([], json.loads(part["chunk"])["mined_case_ids"])
        # A cycle keeps the window it started with, and only a finished one has a report.
        changed = request("history.learning_cycle", payload={"max_age_ms": 1001}, identity="cycle-1")
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.call(changed)
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.call(request("history.learning_report", payload={"offset": 0}, identity="cycle-2"))
        for operation, payload in (("history.learning_cycle", {}), ("history.learning_cycle", {"max_age_ms": 0}),
                                   ("history.learning_cycle", {"max_age_ms": 1, "as_of_ms": 1}),
                                   ("history.learning_report", {"offset": -1}),
                                   ("history.learning_report", {"offset": 0, "bytes": 9})):
            with self.subTest(operation=operation, payload=payload), \
                    self.assertRaisesRegex(HistoryError, "history_invalid_request"):
                encode_history_request(request(operation, payload=payload, identity="cycle-3"))
        good = json.loads(self.service.handle(encode_history_request(cycle), FIXTURE_CONTEXT))
        for changes in ({"state": "done"}, {"reason_code": "learning_busy"}, {"summary": None},
                        {"report_bytes": None}, {"extra": 1}):
            forged = {**good, "result": {**good["result"], **changes}}
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(canonical_json(forged), cycle)
        chunk = json.loads(self.service.handle(encode_history_request(page), FIXTURE_CONTEXT))
        for changes in ({"next_offset": 0}, {"chunk": 5}, {"report_bytes": 1}):
            forged = {**chunk, "result": {**chunk["result"], **changes}}
            with self.subTest(changes=changes), self.assertRaisesRegex(HistoryError, "history_invalid_response"):
                decode_history_response(canonical_json(forged), page)

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
   if mode=='formatted-read' and json.loads(payload)['operation']=='history.read_record':
    # Legal JSON whitespace exercises a read frame above the ordinary ceiling
    # while keeping its record schema and canonical content unchanged.
    reply=b' '*20000+reply
    print('formatted-read',flush=True)
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

    def test_explicit_record_read_frame_can_exceed_the_ordinary_reply_limit(self):
        client, process, _ = self.start("large-read", PROJECT_A, mode="formatted-read")
        record = event_record("large-record")
        content = canonical_json(record)
        receipt = client.record_event(record)
        returned = client.read_record(record["record_id"])
        self.assert_line(process, "formatted-read")
        self.assertEqual(content, canonical_json(returned["record"]))
        self.assertEqual(receipt, returned["metadata"])

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
