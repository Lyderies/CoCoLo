"""Actual account/project stores and client counts; synthetic native authority."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_run_provenance as fixtures
from tests.learning_support import event_record
from tests.test_bridge import result

from codex_agents.history_api import HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_result_contracts import HostResultContext, HostResultReference
from codex_agents.learning_adapters import empty_usage
from codex_agents.learning_contracts import canonical_json
from codex_agents.result_contracts import CodexUsageObservation, ResultObservation

COUNTS = {"input_tokens": 17, "cached_input_tokens": 5, "output_tokens": 9, "reasoning_output_tokens": 3}


class ProjectResultUsageTests(unittest.TestCase):
    def setUp(self):
        original = fixtures.RunProvenanceTests(methodName='runTest')
        original.setUp()
        self.addCleanup(original.doCleanups)
        original.managed_project()
        self.base = original.base
        self.fixture = self.base.fixture
        self.base.consume(original.grant, provenance=original.proof())
        self.binding = self.base.binding
        source = self.fixture.base.fixture
        self.inputs = HostResultContext(self.binding, self.fixture.assigned, source.fixture.task(), source.route,
                                       source.fixture.clients["bridge-v1"])
        self.body = result()
        self.body["summary"] = "PRIVATE-BODY-CANARY"
        self.body["usage"] = {"input_tokens": 9999, "output_tokens": 8888}
        self.observation = ResultObservation(self.binding.claim_id, self.fixture.base.stamp, False, True, None)

    @property
    def store(self):
        return self.fixture.store

    def usage(self, counts=COUNTS):
        return CodexUsageObservation(self.fixture.assigned["model"], None if counts is None else dict(counts))

    def publish(self, counts=COUNTS):
        return self.store.publish_result(self.binding, self.fixture.assigned, canonical_json(self.body), self.observation,
                                         client_usage=self.usage(counts))

    def admit(self, delivery):
        reference = HostResultReference(self.binding, delivery.record_id, delivery.record_content_digest, delivery.result_digest)
        _, inputs = self.inputs.snapshot()
        inputs.pop("binding")
        return self.store.admit_host_result(reference, **inputs, before_commit=lambda: None)

    def count(self, kind):
        return self.store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type=?", (kind,)).fetchone()[0]

    def assert_held(self):
        self.assertIsNone(self.base.ledger._connection.execute("SELECT released_at_ms FROM active_run_capacity").fetchone()[0])

    def test_actual_client_counts_override_model_usage_and_have_exact_durable_provenance(self):
        delivery = self.publish()
        admitted = self.admit(delivery)
        receipt = admitted["admission"]
        self.assertEqual(2, receipt["schema_version"])
        entry = self.store.record(receipt["usage_record_id"])
        record = json.loads(entry.content)
        self.assertEqual("provider", entry.source)
        self.assertEqual("provider_usage", record["record_type"])
        self.assertEqual(COUNTS, {name: record[name] for name in COUNTS})
        self.assertEqual(entry.content_digest, receipt["usage_content_digest"])
        outcome = json.loads(self.store.record(receipt["outcome_record_id"]).content)
        self.assertEqual({**empty_usage(), "input_tokens": 17, "cached_tokens": 5, "output_tokens": 9, "reasoning_tokens": 3},
                         outcome["usage"])
        self.assertEqual("client_observed", outcome["usage_sources"]["input_tokens"]["source"])
        self.assertFalse(outcome["accepted"])
        self.assertNotIn(b"PRIVATE", canonical_json(record))
        self.assertNotIn(b"PRIVATE", canonical_json(admitted))
        self.assertEqual(0, self.base.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='provider_usage'").fetchone()[0])
        self.assert_held()

    def test_unavailable_client_measurement_is_unknown_and_never_model_reported_or_zero(self):
        delivery = self.publish(None)
        receipt = self.admit(delivery)["admission"]
        record = json.loads(self.store.record(receipt["usage_record_id"]).content)
        self.assertEqual(("observation_unavailable", "usage_unavailable"), (record["record_type"], record["reason_code"]))
        outcome = json.loads(self.store.record(receipt["outcome_record_id"]).content)
        self.assertEqual(empty_usage(), outcome["usage"])
        self.assertTrue(all(value["source"] == "unavailable" for value in outcome["usage_sources"].values()))
        self.assertEqual(0, self.count("provider_usage"))

    def test_inconsistent_subcounts_are_retained_but_not_used_as_false_workflow_totals(self):
        counts = {**COUNTS, "cached_input_tokens": 20, "reasoning_output_tokens": 12}
        delivery = self.publish(counts)
        receipt = self.admit(delivery)["admission"]
        recorded = json.loads(self.store.record(receipt["usage_record_id"]).content)
        self.assertEqual(20, recorded["cached_input_tokens"])
        outcome = json.loads(self.store.record(receipt["outcome_record_id"]).content)
        self.assertIsNone(outcome["usage"]["cached_tokens"])
        self.assertIsNone(outcome["usage"]["reasoning_tokens"])
        self.assertEqual(17, outcome["usage"]["input_tokens"])

    def test_failed_terminal_commit_rolls_back_usage_outbox_and_terminal_together(self):
        original = self.store._append_locked

        def fail_usage(record, *args):
            if record["record_type"] == "provider_usage":
                raise OSError("PRIVATE-USAGE-COMMIT")
            return original(record, *args)

        with mock.patch.object(self.store, "_append_locked", side_effect=fail_usage), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.publish()
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual(0, self.count("dispatch_result"))
        self.assertEqual(0, self.count("provider_usage"))
        self.assertEqual("reserved", self.store.result_reservation(self.binding).state)
        self.publish()
        self.assertEqual(1, self.count("provider_usage"))
        self.assert_held()

    def test_prepared_usage_cannot_change_drop_or_gain_values_after_artifact_failure(self):
        with mock.patch.object(self.store, "_write_result_file", side_effect=OSError("PRIVATE-ARTIFACT")), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.publish()
        for counts in (None, {**COUNTS, "input_tokens": 18}):
            with self.assertRaisesRegex(HistoryError, "history_conflict"):
                self.publish(counts)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.publish_result(self.binding, self.fixture.assigned, canonical_json(self.body), self.observation)
        self.publish()

    def test_lost_terminal_commit_reply_reopens_with_original_usage_and_first_receipt(self):
        original = self.store._transaction
        calls = []

        @contextmanager
        def lose_second():
            with original():
                yield
            calls.append(True)
            if len(calls) == 2:
                raise OSError("PRIVATE-LOST-TERMINAL-REPLY")

        with mock.patch.object(self.store, "_transaction", new=lose_second), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.publish()
        self.assertEqual(1, self.count("dispatch_result"))
        self.assertEqual(1, self.count("provider_usage"))
        self.store.close()
        self.fixture.store = self.fixture.open_history()
        delivery = self.publish()
        first = self.admit(delivery)
        self.assertEqual(first, self.admit(delivery))
        self.assertEqual(1, self.count("provider_usage"))

    def test_known_outcome_receipt_survives_body_retirement_but_missing_usage_cannot_be_repaired(self):
        delivery = self.publish()
        first = self.admit(delivery)
        self.store.retire_result(self.binding)
        self.store.close()
        self.fixture.store = self.fixture.open_history()
        with mock.patch.object(self.store, "read_result", side_effect=AssertionError("body read")):
            self.assertEqual(first, self.admit(delivery))
        self.store._connection.execute("DELETE FROM records WHERE record_id=?", (first["admission"]["usage_record_id"],))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.admit(delivery)
        self.assertEqual(0, self.count("provider_usage"))

    def test_missing_or_changed_usage_before_first_admission_cannot_downgrade_to_unknown(self):
        delivery = self.publish()
        identity = self.store._client_usage_id(self.binding)
        original = self.store.record(identity)
        self.store._connection.execute("DELETE FROM records WHERE record_id=?", (identity,))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.admit(delivery)
        self.assertEqual(0, self.count("outcome"))
        # Direct SQL is an explicit corruption fixture, never a normal repair.
        changed = json.loads(original.content)
        changed["input_tokens"] = 18
        self.store._connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (original.project_id, original.record_id, "provider_usage", original.source, original.source_sequence,
             original.observed_at_ms, original.ingested_at_ms, canonical_json(changed),
             hashlib.sha256(canonical_json(changed)).hexdigest(), int(original.late)),
        )
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.admit(delivery)
        self.assertEqual(0, self.count("outcome"))

    def test_companion_namespace_collision_rolls_back_terminal_and_retains_prepared_identity(self):
        collision = {**event_record(), "record_id": self.store._client_usage_id(self.binding)}
        original = self.store.append_record(canonical_json(collision), source="host", ingested_at_ms=self.fixture.base.stamp)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish()
        self.assertEqual(original, self.store.record(original.record_id))
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual(0, self.count("dispatch_result"))
        self.assertEqual("reserved", self.store.result_reservation(self.binding).state)
        self.assert_held()

    def test_invalid_counts_foreign_model_and_generic_append_cannot_supply_measurements(self):
        for counts in ({**COUNTS, "input_tokens": True}, {**COUNTS, "input_tokens": -1},
                       {**COUNTS, "input_tokens": 1 << 63}, {**COUNTS, "arbitrary": 1}):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.publish(counts)
        foreign = replace(self.usage(), requested_model="gpt-6-sol" if self.usage().requested_model != "gpt-6-sol" else "gpt-6-astra")
        with self.assertRaises(HistoryError):
            self.store.publish_result(self.binding, self.fixture.assigned, canonical_json(self.body), self.observation,
                                       client_usage=foreign)
        receipt = self.admit(self.publish())["admission"]
        entry = self.store.record(receipt["usage_record_id"])
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.store.append_record(entry.content, source="provider", ingested_at_ms=self.fixture.base.stamp)

    def test_existing_v1_receipt_and_prepared_digest_stay_unchanged_without_client_measurements(self):
        delivery = self.store.publish_result(self.binding, self.fixture.assigned, canonical_json(self.body), self.observation)
        first = self.admit(delivery)
        self.assertEqual(1, first["admission"]["schema_version"])
        self.assertNotIn("usage_record_id", first["admission"])
        self.assertEqual(first, self.admit(delivery))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish()

    def test_history_api_preserves_usage_reference_and_allows_only_reading_the_safe_companion(self):
        delivery = self.publish()
        reference = HostResultReference(self.binding, delivery.record_id, delivery.record_content_digest, delivery.result_digest)
        token = object()

        def authorize(request, context):
            if context is token:
                return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), "host",
                                            frozenset({"history.admit_host_result", "history.read_record"}), self.inputs)

        service = HistoryService(self.binding.project_id, self.binding.runtime_digest, lambda: self.store, authorizer=authorize)
        self.addCleanup(service.close)
        request = HistoryRequest(1, "history.admit_host_result", self.binding.claim_id, self.binding.project_id,
                                 self.binding.runtime_digest, reference.as_data())
        raw = service.handle(encode_history_request(request), token)
        reply = decode_history_response(raw, request)
        self.assertEqual(2, reply["admission"]["schema_version"])
        read = HistoryRequest(1, "history.read_record", "read", self.binding.project_id, self.binding.runtime_digest,
                              {"record_id": reply["admission"]["usage_record_id"]})
        companion = decode_history_response(service.handle(encode_history_request(read), token), read)["record"]
        self.assertEqual(17, companion["input_tokens"])
        changed = json.loads(raw)
        changed["result"]["admission"]["usage_content_digest"] = "f" * 64
        with self.assertRaisesRegex(HistoryError, "history_invalid_response"):
            decode_history_response(canonical_json(changed), request)
