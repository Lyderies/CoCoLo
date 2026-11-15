"""Pre-claim project writer stages with actual fixed inert provider workers."""
from dataclasses import replace
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_persistence as fixtures

from codex_agents import result_persistence
from codex_agents.claim_storage import MAX_STORAGE_EVIDENCE_BYTES
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultLeaseBinding


class ResultLeaseLaneTests(unittest.TestCase):
    def setUp(self):
        self.composition = fixtures.ResultPersistenceTests(methodName="runTest")
        self.composition.setUp()
        self.addCleanup(self.composition.doCleanups)
        self.worker, self.lane = self.composition.fixture, self.composition.lane
        self.handles, self.scopes = {}, {}

    def before(self, target, worker_handle, *, waiting=None, opening=None):
        scope = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                   claim_request_id="original-claim", claim_request_digest="a" * 64)
        handle = self.lane.prepare_lease(scope, worker_handle)
        self.handles[worker_handle], self.scopes[worker_handle] = handle, scope
        self.worker.until(lambda: self.lane.snapshot(handle)["state"] != "preparing")
        self.assertEqual("reserved", self.lane.snapshot(handle)["state"])
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.lane.storage_evidence(handle)
        self.assertIsNone(self.lane.lease(handle).claim_id)
        self.assertEqual(0, len(self.worker.registry._issued))
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.lane.bind(handle)
        self.lane.arm(handle)
        self.worker.until(lambda: self.lane.snapshot(handle)["state"] != "arming")
        self.assertEqual("claim_pending", self.lane.snapshot(handle)["state"])
        evidence = self.lane.storage_evidence(handle)
        self.assertIs(evidence, self.lane.storage_evidence(handle))
        self.assertLessEqual(len(evidence), MAX_STORAGE_EVIDENCE_BYTES)
        self.assertNotIn(b"PRIVATE", evidence)
        self.assertEqual(0, len(self.worker.registry._issued))
        if opening is not None:
            opening(target, worker_handle, handle)
        self.lane.bind_claim(handle, target, hard_deadline_at_ms=self.composition.deadline_ms)
        if waiting is not None:
            waiting(target, worker_handle, handle)
        self.worker.until(lambda: self.lane.snapshot(handle)["state"] != "binding_claim")
        self.assertEqual("reserved", self.lane.snapshot(handle)["state"], self.lane.snapshot(handle))
        self.assertEqual("bound", self.lane.lease(handle).state)
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.lane.storage_evidence(handle)

    def after(self, target, worker_handle):
        self.lane.bind(self.handles[worker_handle])
        self.worker.owner.run_once()

    def test_preclaim_lease_arm_claim_bind_then_actual_worker_result_persists(self):
        prepared = self.worker.prepare(before_bind=self.before, after_bind=self.after)
        handle = self.handles[prepared[3]]
        self.worker.returned(prepared)
        lease = self.lane.lease(handle)
        self.assertEqual("bound", lease.state)
        self.lane.bind_claim(handle, prepared[1], hard_deadline_at_ms=self.composition.deadline_ms)
        self.lane.submit(handle)
        delivery = self.composition.committed(handle)
        self.assertEqual(prepared[1], delivery.binding)
        self.assertEqual(["launch"], (prepared[0] / "launches").read_text().splitlines())
        copied = self.lane.lease(handle)
        object.__setattr__(copied.binding, "claim_request_id", "mutated")
        self.assertEqual("original-claim", self.lane.lease(handle).binding.claim_request_id)
        self.assertEqual(lease, self.lane.lease(handle))

    def test_delayed_claim_bind_keeps_control_responsive_and_prevents_launch_after_cancel(self):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = ProjectHistory.bind_result_lease

        def blocked(store, *args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("fixture claim-bind gate expired")
            return original(store, *args, **kwargs)

        def during(target, worker_handle, handle):
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            reply = self.worker.request(target)
            self.assertEqual("stop_requested", reply.result["cancellation_state"])
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(0, len(self.worker.registry._issued))
            self.assertEqual("prepared", self.worker.pool.snapshot(worker_handle)["state"])
            self.assertEqual("binding_claim", self.lane.snapshot(handle)["state"])
            resume.set()

        def before(target, worker_handle):
            self.before(target, worker_handle, waiting=during)
            self.assertTrue(self.worker.monitor.status(target).stop_requested)
            raise HistoryError("history_result_not_ready")  # Frontend stops before execution registration.

        with mock.patch.object(ProjectHistory, "bind_result_lease", new=blocked):
            with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                self.worker.prepare(before_bind=before, after_bind=self.after)
        self.assertEqual(0, len(self.worker.registry._issued))
        self.assertTrue(all(not (root / "launches").exists() for root in self.worker.workspaces))
        handle = next(iter(self.handles.values()))
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.lane.submit(handle)
        self.composition.stop_lane(self.lane)
        store = self.composition.open_history()
        try:
            self.assertEqual((), store.pending_terminals())  # No fabricated provider return/cleanup.
        finally:
            store.close()

    def test_lost_durable_claim_bind_reply_retries_exactly_without_refresh_or_early_execution(self):
        original = ProjectHistory.bind_result_lease
        observed = []

        def lose_reply(store, *args, **kwargs):
            result = original(store, *args, **kwargs)
            observed.append(result)
            if len(observed) == 1:
                raise OSError("PRIVATE-LOST-CLAIM-BIND-REPLY")
            return result

        def after_failed(target, worker_handle, handle):
            self.worker.until(lambda: self.lane.snapshot(handle)["state"] == "failed")
            self.assertEqual(0, len(self.worker.registry._issued))
            with self.assertRaisesRegex(HistoryError, "history_conflict"):
                self.lane.bind_claim(handle, target, hard_deadline_at_ms=self.composition.deadline_ms + 1)
            self.lane.bind_claim(handle, target, hard_deadline_at_ms=self.composition.deadline_ms)

        def before(target, worker_handle):
            self.before(target, worker_handle, waiting=after_failed)

        with mock.patch.object(ProjectHistory, "bind_result_lease", new=lose_reply):
            prepared = self.worker.prepare(before_bind=before, after_bind=self.after, submit=False)
        self.assertEqual(observed[0], observed[1])
        self.assertFalse((prepared[0] / "launches").exists())
        handle = self.handles[prepared[3]]
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.lane.bind_claim(handle, prepared[1], hard_deadline_at_ms=self.composition.deadline_ms + 1)

    def test_prepare_lease_needs_no_monitor_claim_and_refuses_wrong_owner_scope(self):
        original = result_persistence.parse_bounded_json
        owner = threading.current_thread()

        def off_owner(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread(), "owner parsed persistent bytes")
            return original(*args, **kwargs)

        def before(target, worker_handle):
            scope = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                       claim_request_id="no-monitor-claim", claim_request_digest="b" * 64)
            with self.assertRaisesRegex(HistoryError, "history_not_authorized"):
                self.lane.prepare_lease(replace(scope, primary_session_id="foreign"), worker_handle)
            # The pre-claim identity has no claim ID and is checked against only
            # the owning supervisor's scope, not a placeholder monitor entry.
            self.worker.monitor._attempts.clear()  # Fixture removes its helper's pre-registered claim.
            with mock.patch.object(threading.Thread, "start", side_effect=AssertionError("late start")), \
                    mock.patch.object(threading.Thread, "join", side_effect=AssertionError("owner join")), \
                    mock.patch.object(result_persistence, "parse_bounded_json", new=off_owner):
                handle = self.lane.prepare_lease(scope, worker_handle)
                self.worker.until(lambda: self.lane.snapshot(handle)["state"] == "reserved")
                self.lane.arm(handle)
                self.worker.until(lambda: self.lane.snapshot(handle)["state"] == "claim_pending")
            self.assertEqual({}, self.worker.monitor._attempts)
            self.assertEqual(0, len(self.worker.registry._issued))
            raise HistoryError("history_result_not_ready")  # Stop helper before ticket issuance.

        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.worker.prepare(before_bind=before)

    def test_known_pending_lease_on_new_local_handle_never_looks_like_fresh_admission(self):
        scopes = []

        def stop_after_arm(target, worker_handle):
            scope = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                       claim_request_id="pending", claim_request_digest="a" * 64)
            handle = self.lane.prepare_lease(scope, worker_handle)
            self.worker.until(lambda: self.lane.snapshot(handle)["state"] == "reserved")
            self.lane.arm(handle)
            self.worker.until(lambda: self.lane.snapshot(handle)["state"] == "claim_pending")
            scopes.append(scope)
            raise HistoryError("history_result_not_ready")

        with self.assertRaises(HistoryError):
            self.worker.prepare(before_bind=stop_after_arm)

        def known(target, worker_handle):
            handle = self.lane.prepare_lease(scopes[0], worker_handle)
            self.worker.until(lambda: self.lane.snapshot(handle)["state"] != "preparing")
            self.assertEqual("lease_claim_pending", self.lane.snapshot(handle)["state"])
            with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
                self.lane.arm(handle)
            raise HistoryError("history_result_not_ready")

        with self.assertRaises(HistoryError):
            self.worker.prepare(before_bind=known)
        self.assertEqual(0, len(self.worker.registry._issued))


if __name__ == "__main__":
    unittest.main()
