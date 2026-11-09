"""Actual fixed workers to project storage; fixture custody is not native proof."""
from __future__ import annotations

from contextlib import closing
from copy import copy
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_codex_worker as workers
from tests.test_project_results import KEY

from codex_agents import result_persistence
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_persistence import (
    MAX_PERSISTENCE_ENTRY_BYTES, ResultPersistenceHandle, ResultPersistenceLane,
)


class ResultPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workers.CodexWorkerTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = Path(self.fixture.temporary.name) / "history"
        self.directory.mkdir(mode=0o700)
        self.project = self.fixture.monitor.scope().project_id
        self.capacity = ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 120_000, 240_000)
        self.factory_threads = []
        self.factory = mock.Mock(side_effect=self.open_history)
        self.lane = self.new_lane()
        self.handles = {}
        self.deadline_ms = time.time_ns() // 1_000_000 + 60_000

    def open_history(self):
        self.factory_threads.append(threading.current_thread())
        return ProjectHistory(self.directory, self.project, result_key=KEY, result_capacity=self.capacity)

    def new_lane(self, **changes):
        # Explicit local reference fixture; normal lease execution requires the
        # account registration and original project writer consumption return.
        options = dict(max_entries=4, retained_bytes=4 * MAX_PERSISTENCE_ENTRY_BYTES,
                       allow_local_execution_reference=True)
        options.update(changes)
        lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, self.factory, **options)
        self.addCleanup(self.stop_lane, lane)
        return lane

    def stop_lane(self, lane):
        lane.close()
        lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive(), "fixture persistence owner did not stop")

    def wait(self, predicate):
        self.fixture.until(predicate)

    def prepared(self, target, handle):
        delivery = self.lane.prepare(target, handle, hard_deadline_at_ms=self.deadline_ms)
        self.handles[handle] = delivery
        self.wait(lambda: self.lane.snapshot(delivery)["state"] != "preparing")
        self.assertEqual("reserved", self.lane.snapshot(delivery)["state"])

    def bound(self, target, handle):
        self.lane.bind(self.handles[handle])

    def execution(self, number=1, **changes):
        return self.fixture.prepare(number, before_bind=self.prepared, after_bind=self.bound, **changes)

    def submit(self, prepared):
        self.fixture.returned(prepared)
        handle = self.handles[prepared[3]]
        self.lane.submit(handle)
        return handle

    def committed(self, handle):
        self.wait(lambda: self.lane.snapshot(handle)["state"] in {"committed", "failed", "unknown"})
        self.assertEqual("committed", self.lane.snapshot(handle)["state"], self.lane.snapshot(handle))
        return self.lane.delivery(handle)

    def test_original_prelaunch_assignment_and_actual_return_reach_one_writer(self):
        prepared = self.execution(submit=False)
        root, target, _, worker, _, assignment = prepared
        self.assertFalse((root / "launches").exists())
        self.assertEqual([self.lane._writer], self.factory_threads)
        self.assertNotEqual(threading.current_thread(), self.factory_threads[0])
        handle = self.handles[worker]
        self.lane.bind(handle)  # Exact prelaunch bind retry.
        self.fixture.pool.submit(worker)
        self.submit(prepared)
        delivery = self.committed(handle)
        self.assertEqual(target, delivery.binding)
        for _ in range(3):
            self.lane.submit(handle)
            self.lane.retry(handle)
            self.assertEqual(delivery, self.lane.delivery(handle))
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            self.assertEqual(canonical_json(workers.fixtures.result()), store.read_result(target))
            self.assertEqual((delivery,), store.pending_terminals())
            record = json.loads(store.record(delivery.record_id).content)
            self.assertEqual(target.claim_id, record["bridge_run_id"])
            self.assertFalse(record["fenced"])
            self.assertTrue(record["reviewed"])
            self.assertIsNone(record["result_validation_reason_code"])
            self.assertEqual(assignment["attempt"], target.attempt)
        self.assertEqual(["launch"], (root / "launches").read_text().splitlines())

    def test_slow_database_does_not_block_control_cancellation_or_second_result_intake(self):
        first = self.execution()
        second = self.execution(2, rows=[{"pause": "release"}])
        self.fixture.until(lambda: (second[0] / "launches").exists())
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.publish_result
        calls = []

        def blocked(store, *args):
            calls.append(threading.current_thread())
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture database gate timed out")
            return original(store, *args)

        with mock.patch.object(ProjectHistory, "publish_result", new=blocked):
            first_handle = self.submit(first)
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            reply = self.fixture.request(second[1])
            self.assertEqual("stop_requested", reply.result["cancellation_state"])
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual("committing", self.lane.snapshot(first_handle)["state"])
            self.fixture.returned(second)
            second_handle = self.submit(second)
            self.assertEqual("queued", self.lane.snapshot(second_handle)["state"])
            self.assertTrue(second[4].snapshot()["cleanup_confirmed"])
            self.fixture.at(61)
            self.fixture.owner.run_once()
            resume.set()
            self.committed(first_handle)
            self.committed(second_handle)
        self.assertEqual([self.lane._writer] * 2, calls)
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            first_record = json.loads(store.record(self.lane.delivery(first_handle).record_id).content)
            self.assertFalse(first_record["fenced"])
            self.assertEqual(canonical_json(workers.fixtures.result()), store.read_result(first[1]))
            body = json.loads(store.read_result(second[1]))
            self.assertEqual("cancelled", body["execution_state"])
            record = json.loads(store.record(self.lane.delivery(second_handle).record_id).content)
            self.assertTrue(record["fenced"])
            self.assertEqual("cancelled", record["terminal_state"])
        self.assertEqual("cleaned", second[4].snapshot()["state"])

    def test_explicit_lost_commit_reply_retry_keeps_first_facts_body_and_retention(self):
        prepared = self.execution()
        original = ProjectHistory.publish_result
        observations, deliveries = [], []

        def lose_first_reply(store, binding, assignment, body, captured):
            observations.append(captured.as_data())
            delivery = original(store, binding, assignment, body, captured)
            deliveries.append(delivery)
            if len(deliveries) == 1:
                raise OSError("PRIVATE-LOST-REPLY-CANARY")
            return delivery

        with mock.patch.object(ProjectHistory, "publish_result", new=lose_first_reply):
            handle = self.submit(prepared)
            self.wait(lambda: self.lane.snapshot(handle)["state"] == "failed")
            self.assertEqual("history_service_unavailable", self.lane.snapshot(handle)["reason_code"])
            with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                self.lane.delivery(handle)
            with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                self.lane.submit(handle)  # Only explicit persistence retry.
            time.sleep(0.01)
            self.lane.retry(handle)
            self.committed(handle)
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(deliveries[0], deliveries[1])
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            self.assertEqual((deliveries[0],), store.pending_terminals())
            row = store.result_reservation(prepared[1])
            self.assertEqual(self.capacity.retention_ms, row.retained_until_ms - row.prepared_at_ms)
            self.assertEqual(canonical_json(workers.fixtures.result()), store.read_result(prepared[1]))
        self.assertEqual(["launch"], (prepared[0] / "launches").read_text().splitlines())

    def test_full_sized_bytes_are_handed_off_without_owner_json_or_byte_copy(self):
        value = workers.fixtures.result()
        value.update(execution_state="completed", semantic_verdict="pass", admission_state="unverified",
                     retry={"attempt": 1, "retry_of_assignment_id": None}, usage={"input_tokens": 0})
        value["summary"] = ""
        value["summary"] = "x" * (MAX_RESULT_BODY_BYTES - len(canonical_json(value)))
        prepared = self.execution(value=value)
        self.fixture.returned(prepared)
        selected = self.fixture.owner.result(prepared[1], self.fixture.pool, prepared[3])
        self.assertEqual(MAX_RESULT_BODY_BYTES, len(selected.packet.body))
        original = result_persistence.parse_bounded_json
        owner_thread = threading.current_thread()

        def off_owner(*args, **kwargs):
            self.assertIsNot(owner_thread, threading.current_thread(), "JSON parsed on control owner")
            return original(*args, **kwargs)

        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        publish = ProjectHistory.publish_result

        def blocked(store, *args):
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture result gate timed out")
            return publish(store, *args)

        with mock.patch.object(result_persistence, "parse_bounded_json", new=off_owner), \
                mock.patch.object(ProjectHistory, "publish_result", new=blocked):
            handle = self.submit(prepared)
            self.assertTrue(entered.wait(5))
            captured = self.lane._jobs[handle].selected
            self.assertIsNot(selected, captured)
            self.assertIsNot(selected.packet, captured.packet)
            self.assertIs(selected.packet.body, captured.packet.body)
            # A consumer's mutation of its returned value cannot alter intake.
            object.__setattr__(selected.packet, "body", b"PRIVATE-REPLACEMENT-CANARY")
            resume.set()
            self.committed(handle)
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            self.assertEqual(canonical_json(value), store.read_result(prepared[1]))

    def test_validator_reason_survives_commit_without_rejected_answer_or_reasoning(self):
        value = {**workers.fixtures.result(), "summary": "PRIVATE-REJECTED-CANARY", "evidence": []}
        prepared = self.execution(value=value)
        handle = self.submit(prepared)
        delivery = self.committed(handle)
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            record = store.record(delivery.record_id).content
            self.assertEqual("missing_evidence", json.loads(record)["result_validation_reason_code"])
            self.assertTrue(json.loads(record)["fenced"])
            self.assertNotIn(b"PRIVATE-", record)
            self.assertNotIn(b"PRIVATE-", store.read_result(prepared[1]))
        self.assertNotIn("PRIVATE", repr(self.lane.snapshot(handle)))

    def test_forged_copied_foreign_and_wrong_thread_handles_have_no_write_authority(self):
        prepared = self.execution(submit=False)
        handle = self.handles[prepared[3]]
        for forged in (ResultPersistenceHandle(), copy(handle), object()):
            for call in (self.lane.bind, self.lane.submit, self.lane.retry, self.lane.delivery, self.lane.retire):
                with self.subTest(call=call, forged=type(forged)), self.assertRaises(HistoryError):
                    call(forged)
        foreign = self.new_lane()
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            foreign.submit(handle)
        errors = []

        def on_other_thread():
            for call in (self.lane.bind, self.lane.submit, self.lane.retry, self.lane.delivery, self.lane.close):
                try:
                    call() if call == self.lane.close else call(handle)
                except HistoryError as error:
                    errors.append(error.reason_code)

        thread = threading.Thread(target=on_other_thread)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["history_wrong_owner"] * 5, errors)
        self.assertFalse((prepared[0] / "launches").exists())

    def test_retirement_requires_original_release_not_returned_or_persisted_result(self):
        prepared = self.execution()
        handle = self.submit(prepared)
        self.committed(handle)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.lane.retire(handle)
        self.assertEqual("cleaned", prepared[4].snapshot()["state"])
        # Fixture-only acknowledgement; production requires account commit proof.
        self.fixture.registry.acknowledge_release(prepared[2])
        self.lane.retire(handle)
        self.fixture.owner.retire(prepared[1], self.fixture.pool, prepared[3])
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.lane.delivery(handle)

    def test_prepare_is_idempotent_but_changed_scope_deadline_or_late_source_is_refused(self):
        prepared = self.execution(submit=False)
        target, worker = prepared[1], prepared[3]
        handle = self.handles[worker]
        self.assertIs(handle, self.lane.prepare(target, worker, hard_deadline_at_ms=self.deadline_ms))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.lane.prepare(target, worker, hard_deadline_at_ms=self.deadline_ms + 1)
        # Same project, distinct registered run cannot replace this source.
        other = self.fixture.prepare(2, submit=False)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.lane.prepare(other[1], worker, hard_deadline_at_ms=self.deadline_ms)
        with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
            self.lane.prepare(other[1], other[3], hard_deadline_at_ms=self.deadline_ms)
        with self.assertRaises(Exception):
            self.lane.prepare(replace(target, primary_session_id="foreign"), worker,
                              hard_deadline_at_ms=self.deadline_ms)
        self.assertFalse((other[0] / "launches").exists())

    def test_explicit_budget_exhaustion_refuses_before_ticket_or_provider_launch(self):
        self.stop_lane(self.lane)
        self.lane = self.new_lane(max_entries=1, retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES)
        first = self.execution(submit=False)
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.execution(2, submit=False)
        self.assertEqual(1, len(self.fixture.registry._issued))
        self.assertFalse((first[0] / "launches").exists())
        self.assertEqual(2, len(self.fixture.pool._jobs))  # Unbound local input stays explicit.

    def test_invalid_prelaunch_assignment_never_opens_history_or_issues_execution(self):
        def reject(target, worker):
            handle = self.lane.prepare(target, worker, hard_deadline_at_ms=self.deadline_ms)
            self.wait(lambda: self.lane.snapshot(handle)["state"] == "failed")
            self.assertEqual("history_invalid_input", self.lane.snapshot(handle)["reason_code"])
            raise HistoryError("history_invalid_input")

        def invalid(data):
            data["assignment"]["permissions"]["network"] = True

        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.fixture.prepare(mutate=invalid, before_bind=reject)
        self.factory.assert_not_called()
        self.assertEqual(0, len(self.fixture.registry._issued))
        self.assertEqual([], list(self.directory.iterdir()))

    def test_wrong_project_factory_is_closed_and_cannot_reserve_or_launch(self):
        other = self.directory.parent / "wrong-project"
        other.mkdir(mode=0o700)
        project = "prj_" + "f" * 32
        opened = []

        def wrong_project():
            store = ProjectHistory(other, project, result_key=KEY, result_capacity=self.capacity)
            opened.append(store)
            return store

        self.factory.side_effect = wrong_project

        def reject(target, worker):
            handle = self.lane.prepare(target, worker, hard_deadline_at_ms=self.deadline_ms)
            self.wait(lambda: self.lane.snapshot(handle)["state"] == "failed")
            self.assertEqual("history_service_unavailable", self.lane.snapshot(handle)["reason_code"])
            raise HistoryError("history_service_unavailable")

        with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            self.fixture.prepare(before_bind=reject)
        self.assertEqual(1, len(opened))
        self.assertTrue(opened[0]._closed)
        self.assertIsNone(self.lane._store)
        self.assertEqual(0, len(self.fixture.registry._issued))
        with closing(ProjectHistory(other, project)):
            pass

    def test_original_prepared_scope_and_delivery_metadata_have_no_mutable_alias(self):
        def capture(target, worker):
            copy_target = replace(target)
            handle = self.lane.prepare(copy_target, worker, hard_deadline_at_ms=self.deadline_ms)
            self.handles[worker] = handle
            self.wait(lambda: self.lane.snapshot(handle)["state"] == "reserved")
            object.__setattr__(copy_target, "primary_session_id", "foreign-after-prepare")

        prepared = self.fixture.prepare(before_bind=capture, after_bind=self.bound)
        handle = self.submit(prepared)
        first = self.committed(handle)
        original = replace(first)
        object.__setattr__(first.binding, "primary_session_id", "foreign-after-delivery")
        second = self.lane.delivery(handle)
        self.assertEqual(prepared[1], second.binding)
        self.assertIsNot(first.binding, second.binding)
        self.assertEqual(original.result_digest, second.result_digest)

    def test_writer_interruption_marks_queued_custody_unknown_without_replacement(self):
        first, second = self.execution(), self.execution(2)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)

        def interrupt(*args):
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture interrupt gate timed out")
            raise KeyboardInterrupt("PRIVATE-WRITER-INTERRUPTION")

        with mock.patch.object(ProjectHistory, "publish_result", new=interrupt):
            first_handle = self.submit(first)
            self.assertTrue(entered.wait(5))
            second_handle = self.submit(second)
            resume.set()
            self.wait(lambda: self.lane._fault)
        for handle in (first_handle, second_handle):
            self.assertEqual({"state": "unknown", "reason_code": "history_service_unavailable",
                              "bound": True, "committed": False}, self.lane.snapshot(handle))
            with mock.patch.object(threading.Thread, "start", side_effect=AssertionError("replacement worker")), \
                    self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                self.lane.retry(handle)
        self.assertEqual("cleaned", first[4].snapshot()["state"])
        self.assertEqual("cleaned", second[4].snapshot()["state"])

    def test_unsafe_backend_reason_is_reduced_to_fixed_code_without_exception_content(self):
        prepared = self.execution()
        error = HistoryError("history_unavailable")
        error.reason_code = "PRIVATE-INJECTED-REASON-CANARY"
        with mock.patch.object(ProjectHistory, "publish_result", side_effect=error):
            handle = self.submit(prepared)
            self.wait(lambda: self.lane.snapshot(handle)["state"] == "failed")
        self.assertEqual("history_invalid_input", self.lane.snapshot(handle)["reason_code"])
        self.assertNotIn("PRIVATE", repr(self.lane.snapshot(handle)))

    def test_close_is_nonblocking_during_write_and_never_starts_or_joins_a_thread(self):
        prepared = self.execution()
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.publish_result

        def blocked(store, *args):
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture close gate timed out")
            return original(store, *args)

        with mock.patch.object(ProjectHistory, "publish_result", new=blocked):
            handle = self.submit(prepared)
            self.assertTrue(entered.wait(5))
            with mock.patch.object(threading.Thread, "start", side_effect=AssertionError("late start")), \
                    mock.patch.object(threading.Thread, "join", side_effect=AssertionError("owner joined")):
                self.lane.close()
                self.assertTrue(self.lane._writer.is_alive())
                self.assertEqual("committing", self.lane.snapshot(handle)["state"])
                with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                    self.lane.submit(handle)
            resume.set()
            self.committed(handle)


if __name__ == "__main__":
    unittest.main()
