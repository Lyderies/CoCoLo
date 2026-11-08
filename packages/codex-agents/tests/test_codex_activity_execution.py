"""Actual fixed reader/worker with an inert exec-protocol fixture, no AI client."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
import tests.test_bridge as bridge_fixtures
import tests.test_bridge_liveness as owner_fixtures
from tests.test_liveness_clock import SECOND

from codex_agents import bridge
from codex_agents.control_api import ControlError
from codex_agents.liveness_clock import MAX_RECEIVED_EVENTS
from codex_agents.owned_execution import ExecutionSupervisor, OwnedExecutionKey, OwnedExecutionRegistry

PREFIX = [{"frame": {"type": "thread.started", "thread_id": "thread_1"}},
          {"frame": {"type": "turn.started"}}]


def reasoning(number=1):
    return {"frame": {"type": "item.completed", "item": {
        "id": f"item_{number}", "type": "reasoning", "text": "PRIVATE-REASONING-CANARY",
    }}}


class CodexActivityExecutionTests(unittest.TestCase):
    setUp = owner_fixtures.BridgeLivenessTests.setUp
    authorize = owner_fixtures.BridgeLivenessTests.authorize
    at = owner_fixtures.BridgeLivenessTests.at
    request = owner_fixtures.BridgeLivenessTests.request
    finish_fixture = owner_fixtures.BridgeLivenessTests.finish_fixture
    assert_stopped_unreaped = owner_fixtures.BridgeLivenessTests.assert_stopped_unreaped

    def execution(self, rows):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("control", "export", "tmp"):
            (root / name).mkdir(mode=0o700)
        executable = root / "inert-exec"
        executable.write_text("#!" + sys.executable + "\n" + '''
import json, sys, time
from pathlib import Path
root = Path(__file__).parent
for row in json.loads((root / "frames.json").read_text()):
    if "pause" in row:
        end = time.monotonic() + 15
        while not (root / row["pause"]).exists():
            if time.monotonic() >= end:
                sys.exit(13)
            time.sleep(0.005)
    elif "exit" in row:
        sys.exit(0)
    else:
        print(json.dumps(row["frame"]), flush=True)
time.sleep(30)
''')
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
                outcomes.append(bridge.run_codex(
                    ledger=object(), project_id=target.project_id,
                    route={"model": "gpt-6-astra", "effort": "high"},
                    assignment=bridge_fixtures.assignment(), role_prompt="Read the fixture only.",
                    export=root / "export", control=root / "control", environ={"PATH": os.defpath},
                    schema=b'{"type":"object","properties":{}}', deadline_seconds=60,
                    executable=str(executable), _execution=ticket,
                ))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        supervisor = registry.handoff(ticket, worker)
        self.addCleanup(self.finish_fixture, worker, threading.Event(), entry, supervisor)
        self.owner.bind(target, supervisor)
        worker.start()
        return root, target, registry, ticket, entry, worker, supervisor, outcomes, errors

    def until(self, predicate, *, tick=True):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if tick:
                self.owner.run_once()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("bounded fixture observation did not arrive")

    def queued(self, registry, entry):
        with registry._lock:
            return bool(entry.activity.events)

    def finished(self, execution, reason):
        _, target, _, _, entry, worker, _, outcomes, errors = execution
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(outcomes))
        self.assertIsNone(outcomes[0][0])
        self.assertEqual(reason, outcomes[0][1]["outcome"])
        self.assertNotIn("PRIVATE", repr(outcomes))
        self.assertIsNotNone(entry.process.returncode)
        self.owner.run_once()
        self.assertTrue(self.monitor.status(target).cleanup_confirmed)

    def test_fixed_codex_reader_keeps_original_activity_receipt_through_delayed_consumption(self):
        event = reasoning()
        event["frame"]["timestamp"] = (1 << 63) - 1  # Untrusted provider time is ignored.
        execution = self.execution([*PREFIX, {"pause": "continue"}, event])
        root, target, registry, _, entry, _, supervisor, _, _ = execution
        self.until(lambda: self.monitor.status(target).health == "active")
        self.at(9)
        (root / "continue").touch()
        self.until(lambda: self.queued(registry, entry), tick=False)
        with registry._lock:
            self.assertEqual([("activity", self.origin + 9 * SECOND)], entry.activity.events)
            self.assertNotIn("PRIVATE", repr(entry.activity.events))
        self.at(18)
        self.owner.run_once()
        status = self.monitor.status(target)
        self.assertEqual(("stale", 9000, 1),
                         (status.health, status.liveness.silence_age_ms, status.liveness.activity_events))
        self.assertFalse(supervisor.snapshot()["stop_requested"])
        self.at(19)
        self.owner.run_once()
        self.finished(execution, "work_silence")
        self.assertEqual("work_silence", self.monitor.status(target).stop_reason)

    def test_event_published_after_snapshot_is_ordered_after_its_watermark(self):
        execution = self.execution([*PREFIX, {"pause": "continue"}, reasoning()])
        root, target, registry, _, entry, _, supervisor, _, _ = execution
        self.until(lambda: self.monitor.status(target).health == "active")
        self.at(9)
        original = ExecutionSupervisor.receive_activity
        interleaved = False

        def interleave(source):
            nonlocal interleaved
            batch = original(source)
            if source is supervisor and not interleaved:
                interleaved = True
                self.clock.value = self.origin + 9 * SECOND + SECOND // 2
                (root / "continue").touch()
                self.until(lambda: self.queued(registry, entry), tick=False)
                self.at(11)
            return batch

        with mock.patch.object(ExecutionSupervisor, "receive_activity", interleave):
            self.owner.run_once()
        status = self.monitor.status(target)
        self.assertTrue(interleaved)
        self.assertEqual(("active", 1500, None),
                         (status.health, status.liveness.silence_age_ms, status.stop_reason))
        self.request(target)
        self.finished(execution, "cancelled")

    def test_prohibited_item_fence_runs_before_activity_classification(self):
        bad = {"frame": {"type": "item.completed", "item": {
            "id": "item_1", "type": "mcp_tool_call", "text": "PRIVATE-CONTENT",
        }}}
        execution = self.execution([*PREFIX, bad])
        self.finished(execution, "codex_tool_not_allowed:mcp_tool_call")
        self.assertEqual(0, self.monitor._entry(execution[1]).clock._activity_events)

    def test_rerouted_and_usage_events_do_not_establish_activity(self):
        rows = [PREFIX[0], {"frame": {"type": "turn.started", "model": "gpt-6-luna"}},
                reasoning(), {"frame": {"type": "turn.completed", "usage": {}}}, {"exit": True}]
        execution = self.execution(rows)
        self.finished(execution, "missing_result")
        self.assertTrue(execution[7][0][1]["rerouted"])
        self.assertIsNone(self.monitor._entry(execution[1]).clock._last_activity)

    def test_activity_queue_overflow_fences_and_stops_monitoring_instead_of_renewing_work(self):
        execution = self.execution([*PREFIX, *(reasoning(i) for i in range(MAX_RECEIVED_EVENTS + 1))])
        _, target, _, _, entry, worker, _, outcomes, errors = execution
        worker.join(6)  # Deliberately do not consume this bounded overload.
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual("monitor_unavailable", outcomes[0][1]["outcome"])
        self.assertTrue(entry.activity.failed)
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.owner.run_once()
        self.assertEqual("monitor_unavailable", self.monitor.status(target).stop_reason)
        self.assertTrue(self.monitor.status(target).cleanup_confirmed)
