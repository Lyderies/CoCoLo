"""E1b-3/4/7: pure in-memory owner pump; events and controlled monotonic time."""
from __future__ import annotations

from copy import copy
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements

from codex_agents.ledger_owner_queue import (
    DEFAULT_LEDGER_QUEUE_LIMITS,
    LedgerOwnerQueue,
    LedgerQueueBusy,
    LedgerQueueLimits,
)

A, B, C = ("prj_" + letter * 32 for letter in "abc")
MS = 1_000_000


class Clock:
    now = 1_000_000_000

    def __call__(self):
        return self.now


class LedgerOwnerQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(6, 3, 3, 800 * MS), clock_ns=self.clock)

    def offer(self, project=A, operation=lambda: "ok", **options):
        values = dict(caller="account", busy_reason="account_busy",
                      deadline_ns=self.clock.now + 800 * MS)
        values.update(options)
        return self.queue.offer(project, operation, **values)

    def busy(self, operation, reason="account_busy"):
        with self.assertRaises(LedgerQueueBusy) as raised:
            operation()
        self.assertEqual(raised.exception.reason_code, reason)

    def gate(self, frontend):
        """Deterministic handoff while execute_next still owns the running job.

        Unit TF-12 forbids blocking thread waits. The callback invokes the
        frontend continuation between entered and release events instead.
        """
        entered, release = threading.Event(), threading.Event()

        def operation():
            entered.set()
            frontend(entered, release)
            self.assertTrue(release.wait(0), "callback release event missing")
            return "committed"

        return entered, release, operation

    @requirements("E1b-3", "E1b-7")
    def test_running_abandonment_keeps_global_and_project_custody(self):
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(2, 2, 1, 800 * MS), clock_ns=self.clock)
        pending = []

        def frontend(entered, release):
            self.assertTrue(entered.wait(1))
            self.queue.abandon(first)
            self.assertFalse(first.handled.is_set())
            self.busy(lambda: self.offer(A))
            pending.append(self.offer(B))
            self.busy(lambda: self.offer(C))
            self.assertEqual(self.queue.snapshot()["retained"], 2)
            self.assertEqual(self.queue.snapshot()["projects"], 2)
            release.set()

        _, _, operation = self.gate(frontend)
        first = self.offer(operation=operation)
        self.assertTrue(self.queue.execute_next())
        self.assertTrue(first.handled.wait(1))
        self.assertEqual(self.queue.snapshot()["retained"], 1)
        second = pending[0]
        self.assertFalse(second.handled.is_set())

    @requirements("E1b-3", "E1b-7")
    def test_abandoned_queue_entry_is_handled_without_execution(self):
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(1, 1, 1, 800 * MS), clock_ns=self.clock)
        operation = mock.Mock()
        handle = self.offer(operation=operation)
        self.queue.abandon(handle)
        self.busy(lambda: self.offer(B))
        self.assertEqual(self.queue.snapshot()["retained"], 1)
        self.assertFalse(handle.handled.is_set())
        self.assertTrue(self.queue.execute_next())
        self.assertTrue(handle.handled.wait(1))
        operation.assert_not_called()
        self.assertEqual(self.queue.snapshot()["retained"], 0)
        self.assertFalse(self.queue.execute_next())

    @requirements("E1b-3")
    def test_project_backlog_yields_turns_and_preserves_fifo(self):
        order = []
        handles = [self.offer(project, lambda value=value: order.append(value))
                   for project, value in ((A, "a1"), (A, "a2"), (A, "a3"), (B, "b1"), (B, "b2"))]
        self.busy(lambda: self.offer(A))
        for _ in handles:
            self.assertTrue(self.queue.execute_next())
        self.assertEqual(order, ["a1", "b1", "a2", "b2", "a3"])
        self.assertTrue(all(handle.handled.is_set() for handle in handles))
        self.assertEqual(self.queue.snapshot()["retained"], 0)
        self.assertFalse(self.queue.execute_next())

    @requirements("E1b-3")
    def test_running_project_keeps_project_count_slot(self):
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(4, 1, 4, 800 * MS), clock_ns=self.clock)
        def frontend(entered, release):
            self.assertTrue(entered.wait(1))
            self.busy(lambda: self.offer(B))
            self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"project_limit": 1}})
            release.set()

        _, _, operation = self.gate(frontend)
        handle = self.offer(operation=operation)
        self.assertTrue(self.queue.execute_next())
        self.assertTrue(handle.handled.wait(1))
        self.offer(B)

    @requirements("E1b-3")
    def test_overflow_is_named_counted_and_snapshot_is_detached(self):
        self.queue = LedgerOwnerQueue(LedgerQueueLimits(2, 2, 1, 800 * MS), clock_ns=self.clock)
        first = self.offer()
        self.busy(lambda: self.offer(A))
        self.offer(B)
        self.busy(lambda: self.offer(C, caller="open", busy_reason="open_account_busy"), "open_account_busy")
        self.busy(lambda: self.offer(C, caller="collector", busy_reason="operation_rejected"), "operation_rejected")
        expected = {"account": {"project_full": 1}, "open": {"queue_full": 1},
                    "collector": {"queue_full": 1}}
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot["refusals"], expected)
        self.assertEqual(set(snapshot), {"retained", "projects", "maximum", "refusals"})
        snapshot["refusals"]["account"]["project_full"] = 999
        self.assertEqual(self.queue.snapshot()["refusals"], expected)
        self.queue.execute_next()
        self.assertEqual(self.queue.wait(first), "ok")
        self.offer(C)

    @requirements("E1b-3")
    def test_limits_and_handle_owner_validation(self):
        self.assertEqual(DEFAULT_LEDGER_QUEUE_LIMITS, LedgerQueueLimits(16, 8, 2, 800 * MS))
        self.assertEqual(LedgerOwnerQueue().snapshot()["maximum"], 16)
        for values in ((0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 0, 1), (1, 1, 1, 0),
                       (True, 1, 1, 1), (1, 1, 1, True), (4097, 1, 1, 1),
                       (1, 257, 1, 1), (1, 1, 4097, 1), (1, 1, 1, 800 * MS + 1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LedgerQueueLimits(*values)
        with self.assertRaises(ValueError):
            LedgerOwnerQueue({})
        for project, operation, options in (("bad", lambda: None, {}), (A, None, {}),
                (A, lambda: None, {"deadline_ns": True}), (A, lambda: None, {"caller": ""}),
                (A, lambda: None, {"busy_reason": "invented_busy"})):
            with self.subTest(project=project, options=options), self.assertRaises(ValueError):
                self.offer(project, operation, **options)
        handle = self.offer()
        for foreign in (object(), copy(handle), LedgerOwnerQueue()):
            with self.assertRaises(ValueError):
                self.queue.wait(foreign)
        other = LedgerOwnerQueue(clock_ns=self.clock)
        with self.assertRaises(ValueError):
            other.abandon(handle)
        self.queue.execute_next()
        self.assertEqual(self.queue.wait(handle), "ok")

        with mock.patch("codex_agents.ledger_owner_queue.threading.current_thread", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "owner"):
                self.queue.execute_next()

    @requirements("E1b-4")
    def test_wait_uses_remaining_deadline_and_counts_once(self):
        for requested, effective in ((100 * MS, 100 * MS), (5000 * MS, 800 * MS)):
            with self.subTest(requested=requested):
                queue = LedgerOwnerQueue(clock_ns=self.clock)
                start = self.clock.now
                handle = queue.offer(A, lambda: None, caller="account", busy_reason="account_busy",
                                     deadline_ns=start + requested)
                self.clock.now = start + 25 * MS

                def expires(timeout):
                    self.assertAlmostEqual(timeout, (effective - 25 * MS) / 1_000_000_000)
                    self.clock.now = start + effective
                    return False

                with mock.patch.object(handle._ready, "wait", side_effect=expires) as wait:
                    self.busy(lambda: queue.wait(handle))
                    wait.assert_called_once()
                self.busy(lambda: queue.wait(handle))
                self.assertEqual(queue.snapshot()["refusals"], {"account": {"deadline": 1}})
                self.assertEqual(queue.snapshot()["retained"], 1)
                self.assertFalse(handle.handled.is_set())
                queue.execute_next()
                self.assertTrue(handle.handled.is_set())

    @requirements("E1b-4", "E1b-7")
    def test_expired_queued_work_is_skipped_and_acknowledged(self):
        operation = mock.Mock()
        deadline = self.clock.now + 100 * MS
        handle = self.offer(operation=operation, deadline_ns=deadline)
        self.clock.now = deadline
        self.queue.execute_next()
        self.assertTrue(handle.handled.wait(1))
        self.busy(lambda: self.queue.wait(handle))
        operation.assert_not_called()
        self.assertEqual(self.queue.snapshot()["retained"], 0)
        self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"deadline": 1}})
        self.busy(lambda: self.offer(deadline_ns=deadline))
        self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"deadline": 2}})

    @requirements("E1b-3", "E1b-4", "E1b-7")
    def test_running_timeout_keeps_custody_until_whole_completion(self):
        effects = []
        def frontend(entered, release):
            self.assertTrue(entered.wait(1))
            self.clock.now += 800 * MS
            self.busy(lambda: self.queue.wait(handle))
            self.assertEqual(effects, ["begin"])
            self.assertEqual(self.queue.snapshot()["retained"], 1)
            self.assertFalse(handle.handled.is_set())
            release.set()

        _, _, gate = self.gate(frontend)

        def whole():
            effects.append("begin")
            gate()
            effects.append("commit")
            return "success"

        handle = self.offer(operation=whole)
        self.assertTrue(self.queue.execute_next())
        self.assertTrue(handle.handled.wait(1))
        self.assertEqual(effects, ["begin", "commit"])
        self.assertEqual(self.queue.snapshot()["retained"], 0)
        self.busy(lambda: self.queue.wait(handle))
        self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"deadline": 1}})

    @requirements("E1b-4", "E1b-7")
    def test_late_execution_commits_whole_and_answers_busy(self):
        effects = []
        deadline = self.clock.now + 100 * MS

        def commit():
            effects.append("begin")
            self.clock.now = deadline
            effects.append("commit")
            return "success"

        handle = self.offer(operation=commit, deadline_ns=deadline)
        self.assertTrue(self.queue.execute_next())
        self.assertTrue(handle.handled.wait(1))
        self.assertEqual(effects, ["begin", "commit"])
        self.assertEqual(self.queue.snapshot()["retained"], 0)
        self.busy(lambda: self.queue.wait(handle))
        self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"deadline": 1}})

    @requirements("E1b-4")
    def test_reply_collected_after_deadline_answers_busy(self):
        deadline = self.clock.now + 100 * MS
        handle = self.offer(deadline_ns=deadline)
        self.queue.execute_next()
        self.assertTrue(handle.handled.is_set())
        self.clock.now = deadline
        self.busy(lambda: self.queue.wait(handle))
        self.assertEqual(self.queue.snapshot()["refusals"], {"account": {"deadline": 1}})
