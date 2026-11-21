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
        # Keep success-path storage and observation timestamps in one elapsed
        # fixture domain. macOS monotonic time can pause during host suspend
        # while realtime advances beyond the real retention window. Dedicated
        # storage tests independently exercise production expiry and rollback.
        self._elapsed_ns = time.monotonic_ns
        self._wall_origin_ns = time.time_ns()
        self._elapsed_origin_ns = self._elapsed_ns()
        clock_patch = mock.patch.object(result_persistence, "_TIME_NS", self.fixture_time_ns)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.directory = Path(self.fixture.temporary.name) / "history"
        self.directory.mkdir(mode=0o700)
        self.project = self.fixture.monitor.scope().project_id
        self.capacity = ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 120_000, 240_000)
        self.factory_threads = []
        self.factory = mock.Mock(side_effect=self.open_history)
        self.lane = self.new_lane()
        self.handles = {}
        self.deadline_ms = self.fixture_time_ns() // 1_000_000 + 60_000

    def fixture_time_ns(self):
        return self._wall_origin_ns + self._elapsed_ns() - self._elapsed_origin_ns

    def open_history(self):
        self.factory_threads.append(threading.current_thread())
        return ProjectHistory(self.directory, self.project, result_key=KEY, result_capacity=self.capacity,
                              allow_legacy_orchestration_reference=True,
                              result_clock_ms=lambda: self.fixture_time_ns() // 1_000_000)

    def new_lane(self, **changes):
        # Explicit local reference fixture; normal lease execution requires the
        # account registration and original project writer consumption return.
        options = dict(max_entries=4, retained_bytes=4 * MAX_PERSISTENCE_ENTRY_BYTES,
                       allow_local_execution_reference=True)
        options.update(changes)
        lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, self.factory, **options)
        self.addCleanup(self.stop_lane, lane)
        return lane

    def test_R6_7_reporting_writes_have_their_own_room_and_never_take_the_writers(self):
        release = threading.Event()

        def slow_history():
            release.wait(10)
            return self.open_history()

        lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, mock.Mock(side_effect=slow_history),
                                     max_entries=1, retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=True)
        try:
            for _ in range(result_persistence.REPORT_SLOTS):
                lane.report_failed_before_open('hrn_' + 'a' * 48, 1, 'run_failed')
            with self.assertRaisesRegex(HistoryError, 'history_writer_busy'):  # waits; fails nothing
                lane.report_failed_before_open('hrn_' + 'a' * 48, 1, 'run_failed')
            # Room is left for the lane's one job and its close marker.
            self.assertGreaterEqual(lane._incoming.maxsize - lane._incoming.qsize(), 2)
            lane.close()
        finally:
            release.set()
        lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive())

    def test_R6_14_earlier_requests_have_their_own_room_too_and_the_close_marker_always_fits(self):
        release = threading.Event()

        def slow_history():
            release.wait(10)
            return self.open_history()

        lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, mock.Mock(side_effect=slow_history),
                                     max_entries=1, retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=True)
        try:
            for _ in range(result_persistence.EARLIER_SLOTS):
                lane.expire_results()
            with self.assertRaisesRegex(HistoryError, 'history_writer_busy'):
                lane.expire_results()
            for _ in range(result_persistence.REPORT_SLOTS):
                lane.report_failed_before_open('hrn_' + 'a' * 48, 1, 'run_failed')
            # The lane's one job and its close marker still fit.
            self.assertGreaterEqual(lane._incoming.maxsize - lane._incoming.qsize(), 2)
            lane.close()
        finally:
            release.set()
        lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive())

    def test_R6_19_a_stopped_writer_fails_every_queued_request_and_a_closed_lane_says_so_before_busy(self):
        # The fault is in place before anything is queued, and holds until every request is (review 040-s1-r6-4).
        queued = threading.Event()

        def fault(request):
            queued.wait(10)
            raise RuntimeError('fixture writer fault')

        lane = ResultPersistenceLane(self.fixture.owner, self.fixture.pool, self.factory,
                                     max_entries=1, retained_bytes=MAX_PERSISTENCE_ENTRY_BYTES,
                                     allow_local_execution_reference=True)
        lane._earlier = fault
        requests = [lane.report_failed_before_open('hrn_' + 'a' * 48, 1, 'run_failed')
                    for _ in range(result_persistence.REPORT_SLOTS)]
        requests += [lane.expire_results() for _ in range(result_persistence.EARLIER_SLOTS)]
        queued.set()
        lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive())
        self.assertEqual({('failed', 'history_service_unavailable')}, {(r.state, r.reason) for r in requests})
        with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):  # before the full quota
            lane.report_failed_before_open('hrn_' + 'a' * 48, 1, 'run_failed')

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

    def test_fixture_result_clock_is_independent_of_unrelated_realtime_jump(self):
        prepared = self.execution()
        self.fixture.returned(prepared)
        actual_time_ns = time.time_ns
        # A host suspend or wall-clock change must not unpredictably turn this
        # success-path fixture into a retention-expiry test. Production clocks
        # and their explicit expiry/rollback tests remain separate.
        with mock.patch.object(time, "time_ns", new=lambda: actual_time_ns() + 3_600_000_000_000):
            delivery = self.committed(self.submit(prepared))
        self.assertEqual(prepared[1], delivery.binding)

    def test_actual_client_usage_reaches_writer_without_model_usage_or_content(self):
        value = workers.fixtures.result()
        value["usage"] = {"input_tokens": 9999}
        counts = {"input_tokens": 17, "cached_input_tokens": 5, "output_tokens": 9, "reasoning_output_tokens": 3}
        prepared = self.execution(rows=[{"result": workers.fixtures.null_filled(value)},
                                        {"frame": {"type": "turn.completed", "usage": counts}}])
        handle = self.submit(prepared)
        delivery = self.committed(handle)
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            terminal = json.loads(store.record(delivery.record_id).content)
            entry = store._client_usage_entry(prepared[1], terminal, prepared[-1]["model"])
            record = json.loads(entry.content)
            self.assertEqual("provider_usage", record["record_type"])
            self.assertEqual(counts, {name: record[name] for name in counts})
            self.assertEqual(9999, json.loads(store.read_result(prepared[1]))["usage"]["input_tokens"])
            self.assertNotIn(b"PRIVATE", entry.content)

    def test_actual_missing_client_usage_records_unavailable_alongside_terminal(self):
        prepared = self.execution(rows=[{"result": workers.fixtures.null_filled(workers.fixtures.result())}])
        delivery = self.committed(self.submit(prepared))
        self.stop_lane(self.lane)
        with closing(self.open_history()) as store:
            terminal = json.loads(store.record(delivery.record_id).content)
            entry = store._client_usage_entry(prepared[1], terminal, prepared[-1]["model"])
            record = json.loads(entry.content)
            self.assertEqual(("observation_unavailable", "usage_unavailable"),
                             (record["record_type"], record["reason_code"]))
            self.assertEqual((delivery,), store.pending_terminals())

    def test_slow_database_does_not_block_control_cancellation_or_second_result_intake(self):
        first = self.execution()
        second = self.execution(2, rows=[{"pause": "release"}])
        self.fixture.until(lambda: (second[0] / "launches").exists())
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.publish_result
        calls = []

        def blocked(store, *args, **kwargs):
            calls.append(threading.current_thread())
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture database gate timed out")
            return original(store, *args, **kwargs)

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

        def lose_first_reply(store, binding, assignment, body, captured, **kwargs):
            observations.append(captured.as_data())
            delivery = original(store, binding, assignment, body, captured, **kwargs)
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

        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture result gate timed out")
            return publish(store, *args, **kwargs)

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
            store = ProjectHistory(other, project, result_key=KEY, result_capacity=self.capacity,
                                   allow_legacy_orchestration_reference=True)
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

        def interrupt(*args, **kwargs):
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

        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise RuntimeError("fixture close gate timed out")
            return original(store, *args, **kwargs)

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

    def test_O10_D13_the_lane_says_when_its_writer_faulted_and_never_after_a_normal_close(self):
        """O10 D13: the service stops itself on a writer fault, so the lane says so (and only then)."""
        class WriterFault(BaseException):
            pass

        def fault(history, *args, **kwargs):
            raise WriterFault("the writer stopped")
        self.assertFalse(self.lane.faulted())
        self.stop_lane(self.lane)
        self.assertFalse(self.lane.faulted(), "a closed lane is not a faulted one")
        lane = self.new_lane()
        with mock.patch.object(ProjectHistory, "expire_results", fault):
            lane.expire_results()
            lane._writer.join(6)
        self.assertFalse(lane._writer.is_alive())
        self.assertTrue(lane.faulted())

    def test_O10_D13_an_invalid_reason_code_in_an_earlier_request_never_faults_the_writer(self):
        """O10 D13 (second path): the writer's handler rebuilds `HistoryError(error.reason_code)`; an
        invalid code becomes `history_invalid_input` there, it does not raise, so the lane goes on."""
        error = HistoryError("history_unavailable")
        error.reason_code = "PRIVATE-INJECTED-REASON-CANARY"
        with mock.patch.object(ProjectHistory, "expire_results", side_effect=error):
            request = self.lane.expire_results()
            self.wait(lambda: request.state != "queued")
        self.assertEqual(("failed", "history_invalid_input"), (request.state, request.reason))
        self.assertTrue(self.lane._writer.is_alive())
        self.assertFalse(self.lane._fault)
        self.answered(self.lane.expire_results())

    def answered(self, request):
        self.wait(lambda: request.state != "queued")
        self.assertEqual("done", request.state, request.reason)
        return request.result

    def test_O10_D1_the_writer_pass_releases_an_earlier_services_unarmed_lease_past_its_time(self):
        """O10 D1: `expire_result_leases` had no production caller, so the unarmed lease of a service that
        stopped was never released. The service's own periodic writer pass now releases it."""
        from tests.learning_support import assignment
        from tests.test_project_history import binding
        from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultLeaseBinding
        target = binding(1, project=self.project)
        lease = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                   claim_request_id="claim-request-1", claim_request_digest="1" * 64)
        earlier = self.open_history()  # the earlier service, which stopped before arming its lease
        try:
            until = earlier.reserve_result_lease(lease, assignment()).reserved_until_ms
        finally:
            earlier.close()
        self._wall_origin_ns += (until + 1) * 1_000_000 - self.fixture_time_ns()  # past its time
        self.answered(self.lane.expire_results())
        self.stop_lane(self.lane)
        store = self.open_history()
        try:
            self.assertEqual("expired", store.result_lease(lease).state)
        finally:
            store.close()

    def test_O10_D2_a_restarted_service_settles_the_registrations_its_previous_run_left(self):
        """O10 D2: the instance compared was the project's durable supervisor id, the same after a restart,
        so rows a stopped service left were never marked. A restarted service (same project, same
        durable scope) now marks them `service_stopped`, and leaves its own alone."""
        self.answered(self.lane.report_registered_run("orc_" + "a" * 48, "hrn_" + "1" * 48, "run-1", 1))
        self.stop_lane(self.lane)
        successor = self.new_lane()
        self.answered(successor.report_registered_run("orc_" + "a" * 48, "hrn_" + "2" * 48, "run-2", 1))
        self.answered(successor.expire_results())
        self.stop_lane(successor)
        store = self.open_history()
        try:
            rows = dict(store._connection.execute("SELECT run_id,failed_reason FROM project_registered_runs"))
        finally:
            store.close()
        self.assertEqual({"run-1": "service_stopped", "run-2": None}, rows)


if __name__ == "__main__":
    unittest.main()
