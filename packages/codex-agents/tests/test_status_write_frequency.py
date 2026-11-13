"""How often a streaming child's activity is published and written; inert clients, no AI client.

Thousands of work frames go through the real readers and classifiers, the real owned
execution registry and the real project monitor. The bounds asserted are stated per
interval of the work clock or as a fixed cap for the run, never per frame.
"""
from __future__ import annotations

import hashlib
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
import tests.test_bridge as bridge_fixtures
import tests.test_bridge_liveness as owner_fixtures
import tests.test_claude_activity as claude_fixtures
import tests.test_codex_activity_execution as codex_fixtures

from codex_agents import bridge, claude_activity, codex_activity, event_shapes
from codex_agents.claude_activity import PARTIAL_ACTIVITY_SECONDS
from codex_agents.execution_activity import ActivityBuffer
from codex_agents.liveness_clock import MAX_RECEIVED_EVENTS, LivenessClock

MODEL = claude_fixtures.MODEL
BATCH = 200  # Fewer work frames than one poll of the monitor may receive.


def stream(kind, **event):
    return {"type": "stream_event", "event": {"type": kind, **event}, "parent_tool_use_id": None}


def fragment(number):
    """One streamed text fragment with its own frame ID, as the client sends them."""
    return {**stream("content_block_delta", index=0, delta={"type": "text_delta", "text": "PRIVATE-PARTIAL"}),
            "uuid": f"00000000-0000-4000-8000-{number:012d}"}


def opened_message(identity="msg_stream"):
    return [stream("message_start", message={"id": identity, "model": MODEL}),
            stream("content_block_start", index=0, content_block={"type": "text", "text": ""})]


def whole(number):
    """One whole assistant frame whose text no earlier frame carried."""
    return claude_fixtures.assistant(claude_fixtures.text(f"PRIVATE-WHOLE-{number}"))


class Counted:
    """Counts what the real objects accept; it changes nothing they do."""

    def __init__(self, test):
        self.published, self.delivered, self.observed = [], [], 0
        self._lock = threading.Lock()
        publish, received = ActivityBuffer.publish, LivenessClock.received
        counted = self

        def publishing(buffer, category):
            publish(buffer, category)
            with counted._lock:
                counted.published.append(category)  # Only what the registry's buffer accepted.

        def receiving(clock, events):
            with counted._lock:
                counted.delivered.append(sum(category in {"startup", "activity"} for category, _ in events))
            return received(clock, events)

        for owner, name, wrapper in ((ActivityBuffer, "publish", publishing), (LivenessClock, "received", receiving)):
            patcher = mock.patch.object(owner, name, wrapper)
            patcher.start()
            test.addCleanup(patcher.stop)

    def classifier(self, real, **options):
        """The real classifier, noting each frame it was shown so a test can wait for a burst."""
        counted = self

        class Shown(real):
            def observe(self, frame):
                try:
                    return super().observe(frame)
                finally:
                    with counted._lock:
                        counted.observed += 1

        return lambda *arguments: Shown(*arguments, **options)

    def activity(self):
        with self._lock:
            return self.published.count("activity")


class ActivityPublicationFrequencyTests(unittest.TestCase):
    """The fixed readers on an owned execution, bound to a project monitor with its control owner."""

    setUp = owner_fixtures.BridgeLivenessTests.setUp
    authorize = owner_fixtures.BridgeLivenessTests.authorize
    at = owner_fixtures.BridgeLivenessTests.at
    request = owner_fixtures.BridgeLivenessTests.request
    finish_fixture = owner_fixtures.BridgeLivenessTests.finish_fixture
    until = codex_fixtures.CodexActivityExecutionTests.until
    finished = codex_fixtures.CodexActivityExecutionTests.finished
    claude_execution = claude_fixtures.ClaudeActivityExecutionTests.execution
    codex_execution = codex_fixtures.CodexActivityExecutionTests.execution

    def counted(self):
        # Frame-shape diagnostics are the owner's opt-in; these runs have not opted in.
        self.addCleanup(event_shapes.configure, event_shapes._directory)
        event_shapes.configure(None)
        return Counted(self)

    def files(self, root, *, ignore=()):
        return {str(path.relative_to(root)): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in sorted(root.rglob("*")) if path.is_file() and path.name not in ignore}

    def release(self, root, name):
        # Hand the monitor everything already published before the next burst may start.
        self.owner.run_once()
        (root / name).touch()

    def shown(self, counted, frames, worker=None):
        self.until(lambda: counted.observed >= frames or (worker is not None and not worker.is_alive()))

    def assert_bounded_polls(self, counted, target):
        # Every poll handed the monitor at most its fixed batch, and nothing was dropped or repeated.
        self.assertLessEqual(max(counted.delivered), MAX_RECEIVED_EVENTS)
        self.assertEqual(len(counted.published), sum(counted.delivered))
        self.assertEqual(counted.activity(), self.monitor.status(target).liveness.activity_events)

    def test_thousands_of_claude_fragments_are_published_once_per_interval_of_the_work_clock_and_write_nothing(self):
        counted, work, burst = self.counted(), [100.0], 2000
        rows = [{"frame": bridge_fixtures.init_frame()}, *({"frame": frame} for frame in opened_message()),
                *({"frame": fragment(number)} for number in range(burst)), {"pause": "second"},
                *({"frame": fragment(number)} for number in range(burst, 2 * burst)), {"pause": "third"},
                *({"frame": fragment(number)} for number in range(2 * burst, 3 * burst)), {"pause": "end"}]
        classifier = counted.classifier(claude_activity.ClaudeActivity, clock=lambda: work[0])
        with mock.patch.object(bridge, "ClaudeActivity", classifier):
            execution = self.claude_execution(rows)
            root, target, _, _, _, _, _, _, errors = execution
            # The two frames that open the message, then every fragment of the first burst.
            self.shown(counted, 2 + burst)
            self.assertEqual(["startup", "activity"], counted.published)
            before = self.files(root)
            # A second burst inside the same interval adds nothing.
            work[0] = 100.0 + PARTIAL_ACTIVITY_SECONDS - 0.001
            self.release(root, "second")
            self.shown(counted, 2 + 2 * burst)
            self.assertEqual(["startup", "activity"], counted.published)
            # A third burst one whole interval after the first counted fragment adds one.
            work[0] = 100.0 + PARTIAL_ACTIVITY_SECONDS
            self.release(root, "third")
            self.shown(counted, 2 + 3 * burst)
            self.assertEqual(["startup", "activity", "activity"], counted.published)
            self.owner.run_once()
            # 6000 fragments over one interval of the work clock: one publication per interval begun.
            elapsed = work[0] - 100.0
            self.assertEqual(int(elapsed / PARTIAL_ACTIVITY_SECONDS) + 1, counted.activity())
            self.assert_bounded_polls(counted, target)
            self.assertEqual("active", self.monitor.status(target).health)
            # 4000 of those frames arrived after `before`: no file of the run was written or
            # changed for them, and its control folder holds only what the launch put there.
            self.assertEqual(before, self.files(root, ignore=("second", "third")))
            self.assertEqual({"settings.json", "role-prompt.md"}, {path.name for path in (root / "control").iterdir()})
            self.assertEqual([], errors)  # The ledger given to the reader is a bare object: any use fails the run.
            self.assertNotIn("PRIVATE", repr(counted.published) + repr(self.monitor.status(target)))
            self.request(target)
            self.finished(execution, "cancelled")

    def test_whole_claude_frames_are_published_up_to_the_fixed_run_cap_however_many_arrive(self):
        counted, cap, total = self.counted(), claude_activity.MAX_ACTIVITY_ITEMS, claude_activity.MAX_ACTIVITY_ITEMS + 3 * BATCH
        rows = [{"frame": bridge_fixtures.init_frame()}]
        for number in range(total):
            if number and number % BATCH == 0:
                rows.append({"pause": f"batch-{number // BATCH}"})
            rows.append({"frame": whole(number)})
        with mock.patch.object(bridge, "ClaudeActivity", counted.classifier(claude_activity.ClaudeActivity)):
            execution = self.claude_execution(rows)
            root, target, _, _, entry, worker, _, _, _ = execution
            for batch in range(1, total // BATCH):
                self.shown(counted, batch * BATCH, worker)
                if not worker.is_alive():
                    break
                self.release(root, f"batch-{batch}")
            # The frame after the cap is a monitoring failure for this run, not more activity.
            self.finished(execution, "monitor_unavailable")
        self.assertEqual(cap + 1, counted.observed)
        self.assertEqual(cap, counted.activity())
        self.assertEqual(["startup"], [category for category in counted.published if category != "activity"])
        self.assertLessEqual(max(counted.delivered), MAX_RECEIVED_EVENTS)
        self.assertLessEqual(sum(counted.delivered), 1 + cap)
        self.assertTrue(entry.activity.failed)
        self.assertEqual("monitor_unavailable", self.monitor.status(target).stop_reason)
        self.assertEqual({"settings.json", "role-prompt.md"}, {path.name for path in (root / "control").iterdir()})

    def test_codex_item_frames_are_published_once_per_stage_and_up_to_the_fixed_run_cap(self):
        counted, cap, repeats = self.counted(), codex_activity.MAX_ACTIVITY_ITEMS, 3000

        def item(kind, number):
            return {"frame": {"type": kind, "item": {"id": f"item_{number}", "type": "reasoning",
                                                     "text": "PRIVATE-REASONING-CANARY"}}}

        rows = [*codex_fixtures.PREFIX, item("item.started", 0), *(item("item.updated", 0) for _ in range(repeats)),
                item("item.completed", 0), {"pause": "items"}]
        total = cap + 3 * BATCH
        for number in range(1, total):
            if number % BATCH == 0:
                rows.append({"pause": f"batch-{number // BATCH}"})
            rows.append(item("item.completed", number))
        with mock.patch.object(bridge, "CodexActivity", counted.classifier(codex_activity.CodexActivity)):
            execution = self.codex_execution(rows)
            root, target, _, _, entry, worker, _, _, _ = execution
            # thread.started, turn.started, then one item started, updated 3000 times and completed.
            self.shown(counted, 2 + repeats + 2)
            self.assertEqual(["startup", "activity", "activity", "activity"], counted.published)
            self.release(root, "items")
            for batch in range(1, total // BATCH):
                self.shown(counted, 2 + repeats + 2 + batch * BATCH - 1, worker)
                if not worker.is_alive():
                    break
                self.release(root, f"batch-{batch}")
            self.finished(execution, "monitor_unavailable")
        # The first item counted at each of its three stages; every other item once; none after the cap.
        self.assertEqual(3 + cap - 1, counted.activity())
        self.assertLessEqual(counted.activity(), 3 * cap)
        self.assertEqual(2 + repeats + 2 + cap, counted.observed)
        self.assertLessEqual(max(counted.delivered), MAX_RECEIVED_EVENTS)
        self.assertLessEqual(sum(counted.delivered), 1 + 3 + cap - 1)
        self.assertTrue(entry.activity.failed)
        self.assertEqual("monitor_unavailable", self.monitor.status(target).stop_reason)
        self.assertEqual([], [path.name for path in (root / "tmp").iterdir()])


if __name__ == "__main__":
    unittest.main()
