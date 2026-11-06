from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.learning_support import assignment, result
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_project_history import PROJECT_A, PROJECT_B, binding
from tests.test_project_results import KEY

from codex_agents.bridge import _fenced_result
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity, ResultObservation


def observation(**changes):
    values = dict(bridge_run_id="brg_" + "a" * 32, observed_at_ms=1000,
                  fenced=False, reviewed=True, terminal_state=None)
    values.update(changes)
    return ResultObservation(**values)


class ResultPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "history"
        self.directory.mkdir(mode=0o700)
        self.now = 1000
        self.capacity = ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.store = self.open()
        self.addCleanup(lambda: self.store.close())
        self.body = canonical_json({**result(), "summary": "PRIVATE-PUBLICATION-CANARY"})

    def open(self, **changes):
        options = dict(result_key=KEY, result_capacity=self.capacity, result_clock_ms=lambda: self.now)
        options.update(changes)
        return ProjectHistory(self.directory, PROJECT_A, **options)

    def reserve(self):
        return self.store.reserve_result(binding(), assignment(), hard_deadline_at_ms=2000)

    def publish(self, *, body=None, captured=None, assigned=None, target=None):
        return self.store.publish_result(target or binding(), assigned or assignment(),
                                         self.body if body is None else body,
                                         observation() if captured is None else captured)

    def record(self, delivery):
        return json.loads(self.store.record(delivery.record_id).content)

    def test_owner_constructs_the_existing_record_shape_id_and_private_digest(self):
        self.reserve()
        delivery = self.publish()
        record = self.record(delivery)
        self.assertEqual(3, record["schema_version"])
        self.assertEqual(keyed_digest(KEY, self.body), delivery.result_digest)
        self.assertEqual(delivery.result_digest, record["result_digest"])
        self.assertEqual(binding().run_id, record["run_id"])
        self.assertEqual(observation().as_data(), {name: record[name] for name in observation().as_data()})
        material = {name: value for name, value in record.items() if name != "record_id"}
        self.assertEqual("dispatch_result:" + content_digest(material), record["record_id"])
        self.assertEqual(self.body, self.store.read_result(binding()))
        self.assertEqual((delivery,), self.store.pending_terminals())
        rows = self.store._connection.execute("SELECT content FROM records").fetchall()
        self.assertNotIn(b"PRIVATE-PUBLICATION", b"".join(row[0] for row in rows))

    def test_retry_and_restart_preserve_capture_ingestion_identity_and_retention(self):
        self.reserve()
        first = self.publish()
        entry = self.store.record(first.record_id)
        original = self.store.result_reservation(binding())
        self.now = 1500
        self.assertEqual(first, self.publish())
        self.store.close()
        self.now = 1800
        self.store = self.open(result_capacity=replace(self.capacity, retention_ms=240_000))
        self.assertEqual(first, self.publish())
        self.assertEqual(entry, self.store.record(first.record_id))
        self.assertEqual(original, self.store.result_reservation(binding()))
        self.assertEqual((1000, 121_000), (original.prepared_at_ms, original.retained_until_ms))

    def test_changed_valid_body_or_captured_facts_cannot_replace_committed_result(self):
        self.reserve()
        first = self.publish()
        self.now = 1500
        diagnostics = {"exit_code": 1, "result_frame_seen": False, "result_subtype": None, "error_code": None}
        for captured in (observation(bridge_run_id="brg_" + "b" * 32), observation(observed_at_ms=1500),
                         observation(reviewed=False), observation(fenced=True, terminal_state="completed"),
                         observation(client_diagnostics=diagnostics)):
            with self.subTest(captured=captured), self.assertRaisesRegex(HistoryError, "history_conflict"):
                self.publish(captured=captured)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish(body=canonical_json({**result(), "summary": "CHANGED-VALID-RESULT"}))
        self.assertEqual((first,), self.store.pending_terminals())
        self.assertEqual(self.body, self.store.read_result(binding()))

    def test_original_assignment_and_every_reserved_binding_field_are_required(self):
        self.reserve()
        changed = assignment()
        changed["objective"] += " changed"
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish(assigned=changed)
        for field, value in vars(binding()).items():
            replacement = (value + 1 if type(value) is int else PROJECT_B if field == "project_id" else
                           "b" * 64 if field.endswith("digest") else "orc_" + "b" * 48 if field == "orchestration_handle"
                           else "hrn_" + "b" * 48 if field == "run_handle" else "different")
            with self.subTest(field=field), self.assertRaises(HistoryError):
                self.publish(target=replace(binding(), **{field: replacement}))
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual([], list(self.directory.glob(".result-*")))

    def test_capture_time_must_be_between_original_reservation_and_trusted_now(self):
        self.reserve()
        for timestamp in (0, 999, 1001, (1 << 63) - 1):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.publish(captured=observation(observed_at_ms=timestamp))
        value = self.store.result_reservation(binding())
        self.assertIsNone(value.prepared_at_ms)
        self.assertEqual([], list(self.directory.glob(".result-*")))
        self.now = 1500
        delivery = self.publish(captured=observation(observed_at_ms=1200))
        self.assertEqual(1200, self.record(delivery)["observed_at_ms"])
        self.assertEqual(1500, self.store.record(delivery.record_id).ingested_at_ms)

    def test_no_implicit_reservation_or_key_creation(self):
        with self.assertRaisesRegex(HistoryError, "history_result_not_reserved"):
            self.publish()
        self.assertEqual([], list(self.directory.glob(".result-*")))
        self.store.close()
        self.store = self.open(result_key=None, result_capacity=None)
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.publish()
        self.assertEqual((), self.store.pending_terminals())

    def test_negative_and_host_fenced_outputs_are_published_exactly(self):
        self.reserve()
        negative = result()
        negative.update(execution_state="completed", semantic_verdict="not-ready", admission_state="unverified",
                        retry={"attempt": 1, "retry_of_assignment_id": None})
        negative["evidence"][0]["outcome"] = "fail"
        body = canonical_json(negative)
        delivery = self.publish(body=body)
        self.assertFalse(self.record(delivery)["fenced"])
        self.assertEqual(body, self.store.read_result(binding()))

        self.store.close()
        self.directory = self.directory.parent / "fenced"
        self.directory.mkdir(mode=0o700)
        self.store = self.open()
        self.reserve()
        body = canonical_json(_fenced_result(assignment(), "failed", "invalid_result"))
        captured = observation(fenced=True, terminal_state="failed", result_validation_reason_code="missing_evidence",
                               client_diagnostics={"exit_code": 1, "result_frame_seen": False,
                                                   "result_subtype": None, "error_code": None})
        delivery = self.publish(body=body, captured=captured)
        record = self.record(delivery)
        self.assertTrue(record["fenced"])
        self.assertEqual("failed", record["terminal_state"])
        self.assertEqual("missing_evidence", record["result_validation_reason_code"])
        self.assertEqual(captured.client_diagnostics, record["client_diagnostics"])
        self.assertEqual(body, self.store.read_result(binding()))
        with self.assertRaises(HistoryError):
            self.publish(body=body)  # A delivery caller cannot drop the actual fence.

    def test_invalid_noncanonical_or_wrong_assignment_body_never_reaches_file_io(self):
        self.reserve()
        invalid = (b"PRIVATE-REJECTED-CANARY", self.body + b"\n",
                   canonical_json({**result(), "assignment_id": "other"}),
                   canonical_json({**result(), "summary": "x" * MAX_RESULT_BODY_BYTES}))
        with mock.patch.object(self.store, "_write_result_file", side_effect=AssertionError("file I/O")):
            for body in invalid:
                with self.subTest(size=len(body)), self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
                    self.publish(body=body)
                self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(self.store.result_reservation(binding()).prepared_at_ms)
        self.assertEqual((), self.store.pending_terminals())

    def test_full_one_mib_needs_no_caller_supplied_key_or_record(self):
        self.reserve()
        value = result()
        value["summary"] = ""
        value["summary"] = "x" * (MAX_RESULT_BODY_BYTES - len(canonical_json(value)))
        body = canonical_json(value)
        self.assertEqual(MAX_RESULT_BODY_BYTES, len(body))
        delivery = self.publish(body=body)
        self.assertEqual(keyed_digest(KEY, body), delivery.result_digest)
        self.assertEqual(body, self.store.read_result(binding()))

    def test_observation_is_closed_strict_and_content_free_on_failure(self):
        for field, values in (
            ("observed_at_ms", (True, 1.5, "1000", -1, 1 << 100)),
            ("fenced", (1, None, "false")), ("reviewed", (1, None, "true")),
            ("bridge_run_id", ("PRIVATE-INVALID-CANARY", None, 1)),
            ("terminal_state", ("PRIVATE-INVALID-CANARY", [], 1)),
            ("result_validation_reason_code", ("PRIVATE-INVALID-CANARY", [], 1)),
            ("client_diagnostics", ({"PRIVATE-INVALID-CANARY": True}, [], "raw", 1)),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
                    observation(**{field: value})
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn("PRIVATE", str(caught.exception))
        for captured in ({"role": "executor", **observation().as_data()}, object()):
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.publish(captured=captured)

    def test_mutated_observation_is_rechecked_and_nested_values_are_snapshotted(self):
        self.reserve()
        invalid = observation()
        object.__setattr__(invalid, "observed_at_ms", True)
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.publish(captured=invalid)
        diagnostics = {"exit_code": 1, "result_frame_seen": False, "result_subtype": None, "error_code": None}
        captured = observation(client_diagnostics=diagnostics)
        snapshot = captured.as_data()
        snapshot["client_diagnostics"]["exit_code"] = 2
        self.assertEqual(1, diagnostics["exit_code"])
        assigned = assignment()
        original_write = self.store._write_result_file
        def mutate_then_write(*args):
            diagnostics["exit_code"] = 3
            assigned["objective"] += " changed after capture"
            return original_write(*args)
        with mock.patch.object(self.store, "_write_result_file", side_effect=mutate_then_write):
            delivery = self.publish(captured=captured, assigned=assigned)
        self.assertEqual(1, self.record(delivery)["client_diagnostics"]["exit_code"])
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish(captured=captured)

    def test_failed_metadata_commit_keeps_exact_owner_generated_retry_identity(self):
        self.reserve()
        with mock.patch.object(self.store, "_commit_terminal_locked", side_effect=OSError("PRIVATE-FAILURE")), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.publish()
        before = self.store.result_reservation(binding())
        self.assertIsNone(before.terminal_record_id)
        self.assertEqual((1000, 121_000), (before.prepared_at_ms, before.retained_until_ms))
        self.assertEqual((), self.store.pending_terminals())
        self.store.close()
        self.now = 1800
        self.store = self.open()
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.publish(captured=observation(observed_at_ms=1800))
        first = self.publish()
        self.assertEqual(1, first.delivery_sequence)
        self.assertEqual(1000, self.record(first)["observed_at_ms"])
        self.assertEqual(121_000, self.store.result_reservation(binding()).retained_until_ms)

    def test_wrong_thread_guard_precedes_clock_validation_and_file_work(self):
        clock = mock.Mock(side_effect=AssertionError("clock called"))
        self.store._result_clock = clock
        results = []
        def foreign():
            try:
                self.store.publish_result(None, None, None, None)
            except HistoryError as exc:
                results.append(exc.reason_code)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["history_wrong_owner"], results)
        clock.assert_not_called()

    def test_real_process_death_before_file_after_file_and_after_commit_is_recoverable(self):
        script = r'''
import base64,json,os,sys
from pathlib import Path
from codex_agents.history_contracts import AttemptBinding
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES,ResultCapacity,ResultObservation
data=json.loads(sys.stdin.read());phase=sys.argv[2]
store=ProjectHistory(Path(sys.argv[1]),data['binding']['project_id'],result_key=b'r'*32,
 result_capacity=ResultCapacity(8,2*MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1000)
original_write=store._write_result_file
def write(*args):
 if phase=='before_file': os._exit(91)
 original_write(*args)
 if phase=='after_file': os._exit(92)
store._write_result_file=write
original_commit=store.commit_result
def commit(*args):
 delivery=original_commit(*args)
 if phase=='after_commit': os._exit(93)
 return delivery
store.commit_result=commit
store.publish_result(AttemptBinding(**data['binding']),data['assignment'],base64.b64decode(data['body']),
 ResultObservation(**data['observation']))
raise AssertionError('crash point not reached')
'''
        for number, phase in enumerate(("before_file", "after_file", "after_commit"), 91):
            with self.subTest(phase=phase):
                self.store.close()
                self.directory = self.directory.parent / phase
                self.directory.mkdir(mode=0o700)
                self.now = 1000
                self.store = self.open()
                self.reserve()
                self.store.close()
                data = {"binding": binding().as_data(), "assignment": assignment(),
                        "body": base64.b64encode(self.body).decode("ascii"), "observation": observation().as_data()}
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", "-S", "-c",
                     f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + script,
                     str(self.directory), phase], input=json.dumps(data), text=True,
                    capture_output=True, timeout=10,
                )
                self.assertEqual(number, completed.returncode, completed.stderr)
                self.now = 1800
                self.store = self.open()
                before = self.store.result_reservation(binding())
                self.assertEqual((1000, 121_000), (before.prepared_at_ms, before.retained_until_ms))
                if phase != "after_commit":
                    with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                        self.store.read_result(binding())
                else:
                    self.assertEqual(self.body, self.store.read_result(binding()))
                with self.assertRaisesRegex(HistoryError, "history_conflict"):
                    self.publish(captured=observation(observed_at_ms=1800))
                delivery = self.publish()
                self.assertEqual(1, delivery.delivery_sequence)
                self.assertEqual(self.body, self.store.read_result(binding()))
                self.assertEqual(1000, self.record(delivery)["observed_at_ms"])
                self.assertEqual((delivery,), self.store.pending_terminals())


if __name__ == "__main__":
    unittest.main()
