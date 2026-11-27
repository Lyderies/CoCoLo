from __future__ import annotations
from tests.support import SHORT_TMP

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
import tests.test_bridge as bridge_fixtures
import tests.test_bridge_liveness as owner_fixtures
import tests.test_codex_activity_execution as codex_fixtures
from tests.test_liveness_clock import SECOND

from codex_agents import bridge, claude_activity
from codex_agents.claude_activity import ClaudeActivity, ClaudeActivityError
from codex_agents.owned_execution import OwnedExecutionKey, OwnedExecutionRegistry

MODEL = "claude-opus-5-5"


def assistant(*content, model=MODEL, **changes):
    return {"type": "assistant", "message": {"id": "msg_1", "model": model, "content": list(content)},
            "parent_tool_use_id": None, **changes}


def text(value="PRIVATE-TEXT"):
    return {"type": "text", "text": value}


def tool_use(identity="toolu_1", name="Read"):
    return {"type": "tool_use", "id": identity, "name": name, "input": {"file_path": "PRIVATE-PATH"}}


def tool_result(identity="toolu_1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": identity, "content": "PRIVATE-OUTPUT"}]},
        "parent_tool_use_id": None}


class ClaudeActivityTests(unittest.TestCase):
    def start(self):
        activity = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS)
        self.assertEqual("startup", activity.startup())
        self.assertIsNone(activity.startup())
        return activity

    def test_nonempty_requested_model_work_and_completed_tools_count(self):
        activity = self.start()
        self.assertEqual("activity", activity.observe(assistant(text())))
        self.assertEqual("activity", activity.observe(assistant({"type": "thinking", "thinking": "PRIVATE"})))
        self.assertIsNone(activity.observe(assistant(text(" \n"))))
        self.assertEqual("activity", activity.observe(assistant(tool_use())))
        self.assertIsNone(activity.observe(assistant(tool_use())))  # Repeated call ID.
        self.assertEqual("activity", activity.observe(tool_result()))
        self.assertIsNone(activity.observe(tool_result()))  # Already completed.
        self.assertIsNone(activity.observe(tool_result("toolu_unknown")))
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def test_replayed_text_and_completed_tool_calls_never_count_again(self):
        activity = self.start()
        self.assertEqual("activity", activity.observe(assistant(text())))
        self.assertIsNone(activity.observe(assistant(text())))  # The identical frame again.
        self.assertEqual("activity", activity.observe(assistant(text("PRIVATE-MORE"))))
        self.assertEqual("activity", activity.observe(assistant(tool_use())))
        self.assertEqual("activity", activity.observe(tool_result()))
        self.assertIsNone(activity.observe(assistant(tool_use())))  # A finished call replayed.
        self.assertIsNone(activity.observe(tool_result()))
        with self.assertRaisesRegex(ClaudeActivityError, "^claude_activity_invalid$"):
            activity.observe({"type": "assistant", "message": {"model": MODEL, "content": [text()]}})
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def stream(self, kind, **event):
        return {"type": "stream_event", "event": {"type": kind, **event}, "parent_tool_use_id": None}

    def delta(self, kind="text_delta", field="text", value="PRIVATE-PARTIAL", *, uuid=None, index=0):
        """One fragment frame; each has its own frame ID, as the client's do, unless one is replayed."""
        self.frames_made = getattr(self, "frames_made", 0) + 1
        return {**self.stream("content_block_delta", index=index, delta={"type": kind, field: value}),
                "uuid": uuid or f"00000000-0000-4000-8000-{self.frames_made:012d}"}

    def streaming(self, now):
        activity = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS, clock=lambda: now[0])
        activity.startup()
        activity.observe(self.stream("message_start", message={"id": "msg_1", "model": MODEL}))
        activity.observe(self.stream("content_block_start", index=0, content_block={"type": "text"}))
        return activity

    def test_partial_fragments_of_the_requested_model_count_at_most_once_per_interval(self):
        now = [100.0]
        activity = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS, clock=lambda: now[0])
        activity.startup()
        self.assertIsNone(activity.observe(self.delta()))  # No message has started.
        self.assertIsNone(activity.observe(self.stream("message_start", message={"id": "msg_1", "model": MODEL})))
        self.assertIsNone(activity.observe(self.stream("content_block_start", index=0, content_block={"type": "text"})))
        self.assertEqual("activity", activity.observe(self.delta()))
        self.assertIsNone(activity.observe(self.delta()))  # Coalesced: same interval.
        self.assertIsNone(activity.observe(self.delta(value=" \n")))
        now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
        self.assertIsNone(activity.observe(self.delta(value="   ")))  # Whitespace never counts.
        self.assertEqual("activity", activity.observe(self.delta("thinking_delta", "thinking")))
        now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
        first = self.delta("input_json_delta", "partial_json", '{"a"')
        self.assertEqual("activity", activity.observe(first))
        # The same frame replayed, interval after interval, is never new work.
        for _ in range(5):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            self.assertIsNone(activity.observe(first))
        second = self.delta("input_json_delta", "partial_json", ': 1}')
        self.assertEqual("activity", activity.observe(second))
        # Nor are earlier frames replayed in turn, or a replayed block start followed by them.
        for frame in (first, second, first, second):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            self.assertIsNone(activity.observe(frame))
        now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
        self.assertIsNone(activity.observe(self.stream("content_block_start", index=0, content_block={"type": "text"})))
        self.assertIsNone(activity.observe(first))
        # A fragment for a block that never started, or that has stopped, is not work.
        self.assertIsNone(activity.observe(self.delta(value="PRIVATE-OTHER", index=1)))
        self.assertIsNone(activity.observe(self.stream("content_block_stop", index=0)))
        self.assertIsNone(activity.observe(self.delta(value="PRIVATE-AFTER-STOP")))
        self.assertIsNone(activity.observe(self.stream("message_delta", delta={"stop_reason": "end_turn"})))
        self.assertIsNone(activity.observe(self.stream("message_stop")))
        now[0] += 10
        self.assertIsNone(activity.observe(self.delta()))  # After the message ended.
        # The whole stream of that message replayed is not new work either; a new message is.
        for message, expected in (("msg_1", None), ("msg_2", "activity")):
            now[0] += 10
            activity.observe(self.stream("message_start", message={"id": message, "model": MODEL}))
            activity.observe(self.stream("content_block_start", index=0, content_block={"type": "text"}))
            with self.subTest(message=message):
                self.assertEqual(expected, activity.observe(self.delta(value="PRIVATE-REPLAY")))
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def test_fragment_frames_of_an_earlier_message_replayed_inside_a_later_one_never_count(self):
        now = [100.0]
        activity = self.streaming(now)
        earlier = [self.delta(value=f"PRIVATE-{n}") for n in range(4)]
        ignored = self.delta(value="PRIVATE-OUTSIDE", index=3)  # Its block never started: it did not count.
        for frame in (*earlier, ignored):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            activity.observe(frame)
        activity.observe(self.stream("content_block_stop", index=0))
        activity.observe(self.stream("message_stop"))
        # A later message of the requested model is streaming, with its blocks open.
        activity.observe(self.stream("message_start", message={"id": "msg_2", "model": MODEL}))
        for index in (0, 3):
            activity.observe(self.stream("content_block_start", index=index, content_block={"type": "text"}))
        for frame in (*earlier, ignored, *reversed(earlier)):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            with self.subTest(frame=frame["uuid"]):
                self.assertIsNone(activity.observe(frame))
        now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
        self.assertEqual("activity", activity.observe(self.delta(value="PRIVATE-0")))  # The later message's own.
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def test_ordinary_output_that_repeats_itself_keeps_counting(self):
        now = [100.0]
        activity = self.streaming(now)
        # A long table or list: the same few fragments, over and over, each in its own frame.
        for turn in range(600):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            with self.subTest(turn=turn):
                self.assertEqual("activity", activity.observe(self.delta(value=("| ok ", "| - ")[turn % 2])))

    def test_a_fragment_frame_without_its_own_id_is_never_work(self):
        now = [100.0]
        activity = self.streaming(now)
        for identity in (None, "", 7, "PRIVATE/INVALID", "x" * 129):
            now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
            frame = self.delta()
            frame["uuid"] = identity
            with self.subTest(identity=identity):
                self.assertIsNone(activity.observe(frame))
        frame = self.delta()
        del frame["uuid"]
        self.assertIsNone(activity.observe(frame))
        self.assertEqual("activity", activity.observe(self.delta()))
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def test_fragment_frames_beyond_the_window_are_forgotten_never_a_fault(self):
        # O0c M6: the fragment memory is a recent window; the oldest frame is forgotten, the run goes on.
        now = [100.0]
        activity = self.streaming(now)
        with mock.patch.object(claude_activity, "MAX_STREAM_EVENTS", 3):
            for _ in range(3):
                now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
                activity.observe(self.delta())
            replayed = self.delta(uuid="00000000-0000-4000-8000-000000000003")
            self.assertIsNone(activity.observe(replayed))  # A recent frame is still recognised.
            for _ in range(50):
                now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
                self.assertEqual("activity", activity.observe(self.delta()))
            self.assertLessEqual(len(vars(activity)["_events"]), 3)

    def test_the_fragment_window_is_one_place_per_frame_and_bounded_below_the_stream_cap(self):
        smallest = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0}, "uuid": "a"}
        now = [100.0]
        activity = self.streaming(now)
        with mock.patch.object(claude_activity, "MAX_STREAM_EVENTS", 1):
            self.assertIsNone(activity.observe(smallest))
            self.assertIsNone(activity.observe({**smallest, "uuid": "b"}))
            self.assertEqual(1, len(vars(activity)["_events"]))

    def test_a_long_run_of_completed_work_never_reaches_the_cap(self):
        # O0c M6: only tool calls still in progress count toward the cap; completed work is forgotten.
        now = [100.0]
        activity = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS, clock=lambda: now[0])
        activity.startup()
        with mock.patch.object(claude_activity, "MAX_ACTIVITY_ITEMS", 8), \
                mock.patch.object(claude_activity, "MAX_STREAM_EVENTS", 8):
            for number in range(50):
                identity = f"toolu_{number}"
                self.assertEqual("activity", activity.observe(assistant(tool_use(identity))))
                self.assertEqual("activity", activity.observe(tool_result(identity)))
                self.assertEqual("activity", activity.observe(assistant(text(f"PRIVATE-{number}"))))
                message = f"msg_s{number}"
                activity.observe(self.stream("message_start", message={"id": message, "model": MODEL}))
                activity.observe(self.stream("content_block_start", index=0, content_block={"type": "text"}))
                now[0] += claude_activity.PARTIAL_ACTIVITY_SECONDS
                self.assertEqual("activity", activity.observe(self.delta()))
                activity.observe(self.stream("message_stop"))
            for name in ("_pending", "_done", "_work", "_streamed", "_events"):
                self.assertLessEqual(len(vars(activity)[name]), 8, name)
            # A late result for a forgotten call is an unknown call, as today; a replayed call is new, never a fault.
            self.assertIsNone(activity.observe(tool_result("toolu_0")))
            self.assertEqual("activity", activity.observe(assistant(tool_use("toolu_0"))))
            # The cap still bounds the calls in progress.
            for number in range(7):
                activity.observe(assistant(tool_use(f"open_{number}")))
            with self.assertRaisesRegex(ClaudeActivityError, "^monitor_unavailable$"):
                activity.observe(assistant(tool_use("open_7")))

    def test_partial_fragments_of_another_model_or_bad_shapes_never_count(self):
        now = [5.0]
        activity = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS, clock=lambda: now[0])
        activity.startup()
        activity.observe(self.stream("message_start", message={"id": "msg_1", "model": "claude-sonnet-5-5"}))
        self.assertIsNone(activity.observe(self.delta()))
        for frame in ({"type": "stream_event", "event": []}, self.stream("message_start", message={"model": MODEL}),
                      self.delta(value=[]), {**self.delta(), "parent_tool_use_id": "toolu_1"}):
            fresh = ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS, clock=lambda: 1.0)
            fresh.startup()
            fresh.observe(self.stream("message_start", message={"id": "msg_1", "model": MODEL}))
            fresh.observe(self.stream("content_block_start", index=0, content_block={"type": "text"}))
            with self.subTest(frame=frame), self.assertRaisesRegex(ClaudeActivityError, "^claude_activity_invalid$"):
                fresh.observe(frame)
        with self.assertRaises(ClaudeActivityError):
            ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS).observe(self.delta())  # Before startup.

    def test_metadata_billing_errors_and_other_models_do_not_refresh(self):
        activity = self.start()
        for frame in ({"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success"},
                      {"type": "rate_limit_event", "rate_limit_info": {}},
                      {"type": "stream_event", "event": {"type": "content_block_delta"}},
                      assistant(text(), model="claude-sonnet-5-5"),
                      assistant(text(), error="rate_limit"),
                      {"type": "user", "message": {"role": "user", "content": "PRIVATE-PROMPT"}}):
            with self.subTest(frame=frame["type"]):
                self.assertIsNone(activity.observe(frame))

    def test_bad_shapes_tools_and_subagent_frames_have_fixed_errors(self):
        for frame in ([], {"type": []}, {"type": "assistant", "message": []},
                      assistant(text([])), assistant(tool_use(identity="PRIVATE/INVALID")),
                      assistant(tool_use(name="Bash")), assistant(text(), parent_tool_use_id="toolu_1"),
                      {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": []}]}}):
            with self.subTest(frame=frame), self.assertRaisesRegex(ClaudeActivityError, "^claude_activity_invalid$"):
                self.start().observe(frame)
        with self.assertRaises(ClaudeActivityError):
            ClaudeActivity(MODEL, bridge.CLAUDE_TOOLS).observe(assistant(text()))

    def test_tools_in_progress_beyond_the_cap_are_a_monitoring_failure(self):
        activity = self.start()
        with mock.patch.object(claude_activity, "MAX_ACTIVITY_ITEMS", 1):
            activity.observe(assistant(tool_use()))
            with self.assertRaisesRegex(ClaudeActivityError, "^monitor_unavailable$"):
                activity.observe(assistant(tool_use("toolu_2")))


class ClaudeActivityExecutionTests(unittest.TestCase):
    """The fixed Claude reader on an owned execution with an inert stream-json fixture."""

    setUp = owner_fixtures.BridgeLivenessTests.setUp
    authorize = owner_fixtures.BridgeLivenessTests.authorize
    at = owner_fixtures.BridgeLivenessTests.at
    request = owner_fixtures.BridgeLivenessTests.request
    finish_fixture = owner_fixtures.BridgeLivenessTests.finish_fixture
    until = codex_fixtures.CodexActivityExecutionTests.until
    queued = codex_fixtures.CodexActivityExecutionTests.queued
    finished = codex_fixtures.CodexActivityExecutionTests.finished

    def execution(self, rows):
        temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("control", "export", "tmp"):
            (root / name).mkdir(mode=0o700)
        executable = root / "inert-claude"
        executable.write_text("#!" + sys.executable + "\n" + codex_fixtures.FIXTURE_BODY)
        executable.chmod(0o700)
        (root / "frames.json").write_text(json.dumps(rows))
        target = owner_fixtures.attempt()
        self.monitor.register(target, hard_deadline_ns=self.deadline)
        registry = OwnedExecutionRegistry()
        key = OwnedExecutionKey(target.project_id, "b" * 64, target.run_id, target.claim_id)
        ticket = registry.issue(key, deadline_ns=self.deadline, run_handle=target.run_handle)
        entry = registry._entry(ticket)
        outcomes, errors = [], []

        def run():
            try:
                outcomes.append(bridge.run_claude(
                    ledger=object(), project_id=target.project_id, run_handle=target.run_handle,
                    route={"model": MODEL, "effort": "high"},
                    assignment=bridge_fixtures.assignment(), role_prompt="Read the fixture only.",
                    export=root / "export", control=root / "control", environ={"PATH": os.defpath},
                    deadline_seconds=60, max_turns=4, executable=str(executable), _execution=ticket,
                    schema=bridge_fixtures.RESULT_SCHEMA,  # O10 VF-1 (requirement)
                ))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        supervisor = registry.handoff(ticket, worker)
        self.addCleanup(self.finish_fixture, worker, threading.Event(), entry, supervisor)
        self.owner.bind(target, supervisor)
        worker.start()
        return root, target, registry, ticket, entry, worker, supervisor, outcomes, errors

    def test_clean_init_is_startup_and_assistant_work_is_activity_until_silence(self):
        rows = [{"frame": bridge_fixtures.init_frame()}, {"pause": "continue"},
                {"frame": assistant(text())}]
        execution = self.execution(rows)
        root, target, registry, _, entry, _, supervisor, _, _ = execution
        self.until(lambda: self.monitor.status(target).health == "active")
        self.at(9)
        (root / "continue").touch()
        self.until(lambda: self.queued(registry, entry), tick=False)
        with registry._lock:
            self.assertEqual([("activity", self.origin + 9 * SECOND)], entry.activity.events)
        self.at(18)
        self.owner.run_once()
        self.assertEqual("stale", self.monitor.status(target).health)
        self.assertFalse(supervisor.snapshot()["stop_requested"])
        self.at(19)
        self.owner.run_once()
        self.finished(execution, "work_silence")

    def test_a_run_with_more_completed_calls_than_the_cap_is_not_aborted(self):
        # O0c M6: the cap bounds calls in progress, never the calls a long run completes.
        rows = [{"frame": bridge_fixtures.init_frame()}]
        for number in range(50):
            rows += [{"frame": assistant(tool_use(f"toolu_{number}"))}, {"frame": tool_result(f"toolu_{number}")}]
        with mock.patch.object(claude_activity, "MAX_ACTIVITY_ITEMS", 8):
            execution = self.execution([*rows, {"exit": True}])
            self.finished(execution, "missing_result")
        self.assertFalse(execution[4].activity.failed)

    def test_prohibited_tool_call_stops_before_activity(self):
        execution = self.execution([{"frame": bridge_fixtures.init_frame()},
                                    {"frame": assistant(tool_use(name="Bash"))}])
        self.finished(execution, "claude_tool_not_allowed")
        self.assertEqual(0, self.monitor._entry(execution[1]).clock._activity_events)

    def test_a_prohibited_tool_stops_as_soon_as_its_call_starts_streaming(self):
        execution = self.execution([
            {"frame": bridge_fixtures.init_frame()},
            {"frame": {"type": "stream_event", "event": {"type": "message_start",
                                                         "message": {"id": "msg_1", "model": MODEL}}}},
            {"frame": {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}}}}])
        self.finished(execution, "claude_tool_not_allowed")
        self.assertEqual(0, self.monitor._entry(execution[1]).clock._activity_events)

    def test_a_streamed_reroute_never_counts_and_marks_the_run(self):
        execution = self.execution([
            {"frame": bridge_fixtures.init_frame()},
            {"frame": {"type": "stream_event", "event": {"type": "message_start", "message": {
                "id": "msg_1", "model": "claude-sonnet-5-5"}}}},
            {"frame": {"type": "stream_event", "uuid": "00000000-0000-4000-8000-000000000001",
                       "event": {"type": "content_block_delta", "index": 0, "delta": {
                           "type": "text_delta", "text": "PRIVATE-PARTIAL"}}}},
            {"exit": True}])
        self.finished(execution, "missing_result")
        self.assertTrue(execution[7][0][1]["rerouted"])
        self.assertEqual(0, self.monitor._entry(execution[1]).clock._activity_events)

    def test_streamed_work_keeps_a_long_message_alive(self):
        rows = [{"frame": bridge_fixtures.init_frame()},
                {"frame": {"type": "stream_event", "event": {"type": "message_start",
                                                             "message": {"id": "msg_1", "model": MODEL}}}},
                {"frame": {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                             "content_block": {"type": "text", "text": ""}}}},
                {"pause": "continue"},
                {"frame": {"type": "stream_event", "uuid": "00000000-0000-4000-8000-000000000001",
                           "event": {"type": "content_block_delta", "index": 0, "delta": {
                               "type": "text_delta", "text": "PRIVATE-PARTIAL"}}}}]
        execution = self.execution(rows)
        root, target, registry, _, entry, _, _, _, _ = execution
        self.until(lambda: self.monitor.status(target).health == "active")
        self.at(9)
        (root / "continue").touch()
        self.until(lambda: self.queued(registry, entry), tick=False)
        with registry._lock:
            self.assertEqual([("activity", self.origin + 9 * SECOND)], entry.activity.events)
            self.assertNotIn("PRIVATE", repr(entry.activity.events))
        self.request(target)
        self.finished(execution, "cancelled")

    def test_rerouted_model_work_does_not_establish_activity(self):
        execution = self.execution([{"frame": bridge_fixtures.init_frame()},
                                    {"frame": assistant(text(), model="claude-sonnet-5-5")},
                                    {"exit": True}])
        self.finished(execution, "missing_result")
        self.assertTrue(execution[7][0][1]["rerouted"])
        self.assertEqual(0, self.monitor._entry(execution[1]).clock._activity_events)


if __name__ == "__main__":
    unittest.main()
