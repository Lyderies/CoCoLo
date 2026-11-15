"""Original project writer plus API intake; enrollment/providers are fixtures."""
from contextlib import closing
from copy import copy
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_persistence as fixtures
from tests.learning_support import event_record

from codex_agents.history_api import (
    HISTORY_OPERATIONS, HistoryClient, HistoryRequest, decode_history_response,
    encode_history_request, history_request_digest,
)
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.project_history import ProjectHistory
from codex_agents.project_request_queue import (
    ProjectRequestHandle, ProjectRequestLimits, ProjectRequestQueue, REQUEST_SLOT_BYTES,
)
from codex_agents.result_persistence import MAX_PERSISTENCE_ENTRY_BYTES
from codex_agents.service_transport import MAX_FRAME_BYTES

SECOND = 1_000_000_000
CONTEXT = object()  # Explicit fake enrollment; never a native caller proof.


class ProjectRequestLaneTests(unittest.TestCase):
    def setUp(self):
        self.composition = fixtures.ResultPersistenceTests(methodName="runTest")
        self.composition.setUp()
        self.addCleanup(self.composition.doCleanups)
        self.worker = self.composition.fixture
        self.composition.stop_lane(self.composition.lane)
        self.allowed = True
        self.project = self.worker.monitor.scope().project_id
        self.runtime = self.worker.monitor.scope().runtime_digest
        self.limits = ProjectRequestLimits(2, 10 * SECOND)
        self.lane = self.composition.new_lane(max_entries=2, request_limits=self.limits,
            retained_bytes=2 * MAX_PERSISTENCE_ENTRY_BYTES + self.limits.retained_bytes,
            history_authorizer=self.authorize)
        self.composition.lane = self.lane

    def authorize(self, command, context):
        if self.allowed and context is CONTEXT:
            return HistoryAuthorization(self.project, self.runtime, history_request_digest(command),
                                        "host", HISTORY_OPERATIONS)

    def command(self, identifier="api-event"):
        record = event_record(identifier)
        return HistoryRequest(1, "history.record_event", record["record_id"], self.project, self.runtime,
                              {"record": record})

    def queue(self, command, **changes):
        options = dict(transport_context=CONTEXT, deadline_ns=time.monotonic_ns() + 8 * SECOND)
        options.update(changes)
        return self.lane.queue_request(encode_history_request(command), **options)

    def response(self, handle):
        result = []
        def ready():
            reply = self.lane.take_request(handle)
            if reply is not None:
                result.append(reply)
                return True
            return False
        self.worker.until(ready)
        return result[0]

    def test_closed_api_requests_and_actual_worker_results_share_one_original_writer(self):
        command = self.command()
        first = decode_history_response(self.response(self.queue(command)), command)
        prepared = self.composition.execution()
        delivery = self.composition.committed(self.composition.submit(prepared))
        self.assertEqual(first["source_sequence"], 1)
        self.assertEqual(self.composition.factory.call_count, 1)
        self.assertEqual(self.composition.factory_threads, [self.lane._writer])
        self.assertEqual(self.lane.request_snapshot()["retained"], 0)
        self.composition.stop_lane(self.lane)
        with closing(self.composition.open_history()) as store:
            self.assertEqual(store.record(command.request_id).source, "host")
            self.assertEqual(store.pending_terminals(), (delivery,))

    def test_real_history_client_reaches_existing_writer_from_separate_frontend(self):
        endpoint = Path(self.worker.temporary.name) / "history-api.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        listener.listen(1)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        errors = []
        def frontend():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    with connection.makefile("rb") as incoming:
                        raw = incoming.readline(MAX_FRAME_BYTES + 1)
                    if not raw.endswith(b"\n") or len(raw) > MAX_FRAME_BYTES:
                        raise AssertionError("fixture frame invalid")
                    handle = self.lane.queue_request(raw[:-1], CONTEXT,
                        deadline_ns=time.monotonic_ns() + 4 * SECOND)
                    end = time.monotonic() + 4
                    while time.monotonic() < end:
                        reply = self.lane.take_request(handle)
                        if reply is not None:
                            connection.sendall(reply + b"\n")
                            return
                        time.sleep(.001)
                    self.lane.abandon_request(handle)
                    raise AssertionError("fixture request timeout")
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=frontend)
        thread.start()
        try:
            client = HistoryClient(endpoint, self.project, self.runtime, timeout=5)
            self.assertEqual(client.record_event(event_record("socket-event"))["source_sequence"], 1)
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.composition.factory_threads, [self.lane._writer])

    def test_full_api_queue_preserves_result_capacity_and_live_cancellation(self):
        completed = self.composition.execution()
        prepared = self.composition.execution(2, rows=[{"pause": "release"}])
        self.worker.until(lambda: (prepared[0] / "launches").exists())
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.append_record
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise AssertionError("fixture API gate expired")
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, "append_record", new=blocked):
            first = self.queue(self.command("blocked-api"))
            self.assertTrue(entered.wait(5))
            second = self.queue(self.command("queued-api"))
            with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                self.queue(self.command("overload-api"))
            start = time.monotonic()
            reply = self.worker.request(prepared[1])
            self.assertEqual(reply.result["cancellation_state"], "stop_requested")
            self.assertLess(time.monotonic() - start, 2)
            results = [self.composition.submit(value) for value in (completed, prepared)]
            self.assertTrue(all(self.lane.snapshot(value)["state"] == "queued" for value in results))
            self.assertEqual(self.lane.request_snapshot()["retained"], 2)
            # Both real result entries and the ordinary queue are occupied.
            # Closing must still enqueue its reserved marker without waiting.
            start = time.monotonic()
            self.lane.close()
            self.assertLess(time.monotonic() - start, 1)
            resume.set()
            for handle in (first, second):
                with self.assertRaises(HistoryError):
                    self.lane.take_request(handle)
            for result in results:
                self.composition.committed(result)
            self.lane._writer.join(6)
            self.assertFalse(self.lane._writer.is_alive())

    def test_abandoned_and_expired_requests_do_not_recycle_executing_quota(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.append_record
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise AssertionError("fixture API gate expired")
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, "append_record", new=blocked):
            first = self.queue(self.command("abandoned-commit"))
            self.assertTrue(entered.wait(5))
            second = self.queue(self.command("expired-before-write"), deadline_ns=time.monotonic_ns() + 10_000_000)
            self.lane.abandon_request(first)
            self.assertEqual(self.lane.request_snapshot()["retained"], 2)
            with self.assertRaises(HistoryError):
                self.queue(self.command("cannot-recycle"))
            time.sleep(.02)
            with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                self.lane.take_request(second)
            self.assertEqual(self.lane.request_snapshot()["retained"], 2)
            resume.set()
            self.worker.until(lambda: self.lane.request_snapshot()["retained"] == 0)
        self.composition.stop_lane(self.lane)
        with closing(self.composition.open_history()) as store:
            self.assertEqual(store.record("abandoned-commit").source_sequence, 1)
            with self.assertRaises(HistoryError):
                store.record("expired-before-write")

    def test_expiry_after_commit_withholds_reply_and_exact_retry_recovers_original(self):
        clock = [time.monotonic_ns()]
        deadline = clock[0] + 1000
        committed = threading.Event()
        original = ProjectHistory.append_record
        def commit_then_expire(store, *args, **kwargs):
            entry = original(store, *args, **kwargs)
            clock[0] = deadline + 1
            committed.set()
            return entry
        command = self.command("committed-lost-reply")
        with (
            mock.patch("codex_agents.project_request_queue.time.monotonic_ns", side_effect=lambda: clock[0]),
            mock.patch.object(ProjectHistory, "append_record", new=commit_then_expire),
        ):
            handle = self.queue(command, deadline_ns=deadline)
            self.assertTrue(committed.wait(5))
            with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                self.lane.take_request(handle)
            self.worker.until(lambda: self.lane.request_snapshot()["retained"] == 0)
        recovered = decode_history_response(self.response(self.queue(command)), command)
        self.assertEqual(recovered["source_sequence"], 1)
        self.assertEqual(self.composition.factory.call_count, 1)

    def test_queued_request_rechecks_revoked_context_before_database_work(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.append_record
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise AssertionError("fixture API gate expired")
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, "append_record", new=blocked):
            first = self.queue(self.command("original-authorized"))
            self.assertTrue(entered.wait(5))
            second = self.queue(self.command("revoked-before-write"))
            self.allowed = False
            resume.set()
            self.response(first)
            self.assertEqual(json.loads(self.response(second))["reason_code"], "history_not_authorized")
        self.composition.stop_lane(self.lane)
        with closing(self.composition.open_history()) as store:
            with self.assertRaises(HistoryError):
                store.record("revoked-before-write")

    def test_shutdown_slot_is_reserved_and_borrowed_service_does_not_close_busy_writer(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.append_record
        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(6):
                raise AssertionError("fixture API gate expired")
            return original(store, *args, **kwargs)
        with mock.patch.object(ProjectHistory, "append_record", new=blocked):
            first = self.queue(self.command("shutdown-api"))
            self.assertTrue(entered.wait(5))
            self.queue(self.command("not-started-api"))
            start = time.monotonic()
            self.lane.close()
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(self.lane._writer.is_alive())
            with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
                self.lane.take_request(first)
            resume.set()
            self.lane._writer.join(6)
        self.assertFalse(self.lane._writer.is_alive())
        self.assertEqual(self.lane.request_snapshot()["retained"], 0)
        with closing(self.composition.open_history()) as store:
            self.assertEqual(store.record("shutdown-api").source_sequence, 1)
            with self.assertRaises(HistoryError):
                store.record("not-started-api")

    def test_default_denial_and_wrong_context_do_not_open_history(self):
        command = self.command()
        raw = self.response(self.queue(command, transport_context=object()))
        self.assertEqual(json.loads(raw)["reason_code"], "history_not_authorized")
        self.assertEqual(self.composition.factory.call_count, 0)

    def test_disabled_requests_and_insufficient_memory_are_refused(self):
        with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
            self.composition.new_lane(request_limits=self.limits)
        default = self.composition.new_lane()
        with self.assertRaisesRegex(HistoryError, "history_service_unavailable"):
            default.queue_request(encode_history_request(self.command()), CONTEXT,
                                  deadline_ns=time.monotonic_ns() + SECOND)


class ProjectRequestQueueTests(unittest.TestCase):
    def test_input_and_retention_bounds_and_plain_opaque_context(self):
        for count, wait in ((True, SECOND), (0, SECOND), (65, SECOND), (1, True), (1, 0)):
            with self.assertRaises(HistoryError):
                ProjectRequestLimits(count, wait)
        limits = ProjectRequestLimits(1, SECOND)
        self.assertEqual(limits.retained_bytes, REQUEST_SLOT_BYTES)
        queue = ProjectRequestQueue(limits)
        for raw, context, deadline in (
            (bytearray(b"{}"), None, time.monotonic_ns() + SECOND),
            (b"{}\n", None, time.monotonic_ns() + SECOND),
            (b"x" * MAX_FRAME_BYTES, None, time.monotonic_ns() + SECOND),
            (b"{}", {"authorized": True}, time.monotonic_ns() + SECOND),
            (b"{}", None, True), (b"{}", None, 0),
        ):
            with self.assertRaises(HistoryError):
                queue.prepare(raw, context, deadline_ns=deadline)
        handle = queue.prepare(b"{}", object(), deadline_ns=time.monotonic_ns() + SECOND)
        with self.assertRaises(HistoryError):
            queue.prepare(b"{}", None, deadline_ns=time.monotonic_ns() + SECOND)
        for invalid in (copy(handle), ProjectRequestHandle(), object()):
            with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                queue.take(invalid)
        self.assertIsNone(queue.take(handle))
        queue.reject_unqueued(handle)
        self.assertEqual(queue.snapshot()["retained"], 0)

    def test_expiry_and_default_denial_do_not_open_store_or_switch_service(self):
        queue = ProjectRequestQueue(ProjectRequestLimits(2, SECOND))
        factory = mock.Mock(side_effect=AssertionError("unauthorized SQL factory"))
        service = HistoryService("prj_" + "a" * 32, "b" * 64, factory)
        self.addCleanup(service.close)
        handle = queue.prepare(b"{}", None, deadline_ns=time.monotonic_ns() + SECOND)
        queue.execute(handle, service)
        self.assertFalse(json.loads(queue.take(handle))["ready"])
        factory.assert_not_called()
        other = HistoryService("prj_" + "a" * 32, "b" * 64, factory)
        self.addCleanup(other.close)
        handle = queue.prepare(b"{}", None, deadline_ns=time.monotonic_ns() + SECOND)
        with self.assertRaisesRegex(HistoryError, "history_wrong_owner"):
            queue.execute(handle, other)
        queue.execute(handle, service)
        queue.take(handle)
        queue.close()
        queue.finish_writer(service)
