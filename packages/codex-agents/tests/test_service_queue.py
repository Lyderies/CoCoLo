from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import threading
import unittest

import tests.support  # noqa: F401

from codex_agents.service_queue import (
    MAX_PAYLOAD_BYTES,
    ProjectFairQueue,
    QueueLimits,
    QueueSnapshot,
    ServiceQueueError,
)

A = "prj_" + "a" * 32
B = "prj_" + "b" * 32
C = "prj_" + "c" * 32


class ProjectFairQueueTests(unittest.TestCase):
    def test_project_backlog_cannot_take_another_projects_quota_or_turn(self) -> None:
        queue = ProjectFairQueue(QueueLimits(2, 3, 8))
        for payload in (b"a1", b"a2", b"a3"):
            queue.offer(A, payload)
        with self.assertRaisesRegex(ServiceQueueError, "project_queue_full"):
            queue.offer(A, b"excess")
        queue.offer(B, b"b1")
        queue.offer(B, b"b2")
        self.assertEqual(QueueSnapshot(2, 5, 10), queue.snapshot())
        actual = [queue.take_nowait() for _ in range(5)]
        self.assertEqual([(A, b"a1"), (B, b"b1"), (A, b"a2"), (B, b"b2"), (A, b"a3")],
                         [(item.project_id, item.payload) for item in actual])
        self.assertEqual(QueueSnapshot(0, 0, 0), queue.snapshot())
        self.assertIsNone(queue.take_nowait())

    def test_project_slot_reclaimed_only_when_its_queue_is_empty(self) -> None:
        queue = ProjectFairQueue(QueueLimits(2, 2, 8))
        queue.offer(A, b"a1")
        queue.offer(A, b"a2")
        queue.offer(B, b"b1")
        queue.take_nowait()
        with self.assertRaisesRegex(ServiceQueueError, "queue_project_limit"):
            queue.offer(C, b"c1")
        self.assertEqual(B, queue.take_nowait().project_id)
        queue.offer(C, b"c1")
        self.assertEqual(A, queue.take_nowait().project_id)
        self.assertEqual(C, queue.take_nowait().project_id)

    def test_invalid_limits_and_aggregate_memory_bounds(self) -> None:
        for values in ((True, 1, 1), (0, 1, 1), (1, 0, 1), (1, 1, 0),
                       (257, 1, 1), (256, 17, 1), (256, 16, 65536),
                       (1, 1, 65537), (1, 1, float("inf"))):
            with self.subTest(values=values), self.assertRaisesRegex(ServiceQueueError, "invalid_queue_limits"):
                QueueLimits(*values)
        limits = QueueLimits(256, 4, 65536)
        self.assertEqual(MAX_PAYLOAD_BYTES, limits.max_projects * limits.max_per_project * limits.max_item_bytes)
        with self.assertRaisesRegex(ServiceQueueError, "invalid_queue_limits"):
            ProjectFairQueue({"max_projects": 2})

    def test_invalid_items_are_rejected_without_retaining_rejected_values(self) -> None:
        queue = ProjectFairQueue(QueueLimits(2, 2, 4))
        for project, payload in (("secret-invalid-project", b"ok"), (None, b"ok"),
                                 (A, bytearray(b"x")), (A, "secret-text"), (A, b""), (A, b"12345")):
            with self.subTest(project=project, size=len(payload)), self.assertRaises(ServiceQueueError) as caught:
                queue.offer(project, payload)
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(QueueSnapshot(0, 0, 0), queue.snapshot())
        queue.offer(A, b"1234")
        self.assertEqual(4, queue.snapshot().payload_bytes)

    def test_separate_reserved_lane_remains_usable_when_routine_lane_is_full(self) -> None:
        routine = ProjectFairQueue(QueueLimits(1, 1, 8))
        control = ProjectFairQueue(QueueLimits(1, 1, 8))
        routine.offer(A, b"activity")
        with self.assertRaises(ServiceQueueError):
            routine.offer(A, b"more")
        control.offer(A, b"cancel")
        self.assertEqual(b"cancel", control.take_nowait().payload)
        self.assertEqual(1, routine.snapshot().pending)

    def test_simultaneous_offers_preserve_quota_and_deliver_every_accepted_item_once(self) -> None:
        queue = ProjectFairQueue(QueueLimits(2, 8, 8))
        barrier = threading.Barrier(21)
        accepted, refused = [], []
        result_lock = threading.Lock()

        def produce(index):
            payload = str(index).encode()
            barrier.wait(timeout=5)
            try:
                queue.offer(A, payload)
            except ServiceQueueError as error:
                with result_lock:
                    refused.append(error.reason_code)
            else:
                with result_lock:
                    accepted.append(payload)

        workers = [threading.Thread(target=produce, args=(index,)) for index in range(20)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(8, len(accepted))
        self.assertEqual(["project_queue_full"] * 12, refused)
        queue.offer(B, b"b1")
        first = queue.take_nowait()
        self.assertEqual(A, first.project_id)
        self.assertEqual(B, queue.take_nowait().project_id)
        delivered = [first.payload, *(queue.take_nowait().payload for _ in range(7))]
        self.assertCountEqual(accepted, delivered)
        self.assertEqual(8, len(set(delivered)))
        self.assertIsNone(queue.take_nowait())
        self.assertEqual(QueueSnapshot(0, 0, 0), queue.snapshot())

    def test_concurrent_consumers_deliver_each_pending_item_once(self) -> None:
        queue = ProjectFairQueue(QueueLimits(2, 8, 8))
        expected = [(project, str(index).encode()) for project in (A, B) for index in range(8)]
        for project, payload in expected:
            queue.offer(project, payload)
        delivered = []
        result_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def consume():
            barrier.wait(timeout=5)
            while (item := queue.take_nowait()) is not None:
                with result_lock:
                    delivered.append((item.project_id, item.payload))

        workers = [threading.Thread(target=consume) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(expected, delivered)
        self.assertEqual(QueueSnapshot(0, 0, 0), queue.snapshot())


if __name__ == "__main__":
    unittest.main()
