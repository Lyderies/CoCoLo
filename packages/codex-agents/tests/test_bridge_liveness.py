"""Offline control/executor composition; fixture grants are not native proof."""
from __future__ import annotations
from tests.support import SHORT_TMP

import contextlib
from copy import copy
from dataclasses import asdict, replace
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.test_liveness_clock import FakeClock, SECOND
from tests.test_project_monitor import binding, limits, scope

from codex_agents import bridge, owned_execution, process_tree
from codex_agents.bridge_liveness import BridgeLivenessOwner
from codex_agents.control_api import (
    MAX_CONTROL_FRAME_BYTES, ControlError, ControlScope, control_request,
    decode_control_response, encode_control_request,
)
from codex_agents.control_loop import ControlLoop, ControlLoopLimits
from codex_agents.control_service import ControlAuthorization, ControlService
from codex_agents.owned_execution import (
    ExecutionCustodyError, ExecutionSupervisor, OwnedExecutionKey, OwnedExecutionRegistry,
)
from codex_agents.project_monitor import ProjectMonitor
from codex_agents.service_protocol import ServiceTarget, service_request_digest

CONTEXT = object()  # Fixture-only native frontend/authorization boundary.


def attempt(number=1):
    return replace(binding(number), run_id="run:" + f"{number:064x}",
                   claim_id="brg_" + f"{number:032x}")


class BridgeLivenessTests(unittest.TestCase):
    def setUp(self):
        self.origin = time.monotonic_ns()
        self.clock = FakeClock(self.origin)
        self.deadline = self.origin + 60 * SECOND
        self.monitor = ProjectMonitor(scope(), limits(), max_tracked_attempts=4, clock_ns=self.clock)
        self.scope = ControlScope(**asdict(scope()))
        self.service = ControlService(self.monitor, authorizer=self.authorize, clock_ns=self.clock)
        self.addCleanup(self.service.close)
        self.loop = ControlLoop(self.monitor, self.service, ControlLoopLimits(8, 4, 10 * SECOND, 1_000_000))
        self.addCleanup(self.loop.close)
        self.owner = BridgeLivenessOwner(self.loop)

    def authorize(self, request, context):
        if context is CONTEXT:
            return ControlAuthorization(self.scope, request.target, service_request_digest(request),
                                        frozenset({"project.status", "project.cancel"}))

    def at(self, seconds):
        self.clock.value = self.origin + seconds * SECOND

    def execution(self, number=1, *, where="callback", attach=True, duration=60):
        target = attempt(number)
        self.monitor.register(target, hard_deadline_ns=self.deadline)
        registry = OwnedExecutionRegistry()
        key = OwnedExecutionKey(target.project_id, f"{number:064x}", target.run_id, target.claim_id)
        ticket = registry.issue(key, deadline_ns=self.origin + duration * SECOND, run_handle=target.run_handle)
        entry = registry._entry(ticket)
        ready, resume = threading.Event(), threading.Event()
        outcomes, errors = [], []

        def blocked(_value):
            ready.set()
            if not resume.wait(10):
                raise RuntimeError("fixture release timed out")

        def run():
            try:
                outcomes.append(bridge.run_child(
                    [sys.executable, "-I", "-B", "-S", "-c",
                     "import time; print('{}', flush=True); time.sleep(30)"],
                    environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                    deadline_seconds=60, on_frame=blocked if where == "callback" else lambda frame: None,
                    _execution=ticket,
                ))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        supervisor = registry.handoff(ticket, worker)
        self.addCleanup(self.finish_fixture, worker, resume, entry, supervisor)
        if attach:
            self.owner.bind(target, supervisor)
        return target, registry, ticket, entry, worker, supervisor, ready, resume, outcomes, errors

    def finish_fixture(self, worker, resume, entry, supervisor):
        supervisor.request_stop()
        resume.set()
        if worker.ident is not None:
            worker.join(6)
            self.assertFalse(worker.is_alive(), "fixture executor did not stop")
        process = entry.process
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    def request(self, target, operation="project.cancel", *, context=CONTEXT):
        request = control_request(self.scope, operation,
                                  ServiceTarget(target.orchestration_handle, target.run_handle, target.attempt))
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        self.loop.attach(server, context)
        client.sendall(encode_control_request(request) + b"\n")
        client.setblocking(False)
        incoming = bytearray()
        for _ in range(64):
            self.owner.run_once()
            try:
                part = client.recv(MAX_CONTROL_FRAME_BYTES)
            except BlockingIOError:
                continue
            incoming.extend(part)
            if incoming.endswith(b"\n"):
                return decode_control_response(bytes(incoming[:-1]), request)
            if not part:
                self.fail("fixture connection closed without response")
        self.fail("bounded fixture control response did not finish")

    def assert_stopped_unreaped(self, entry, supervisor):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            observed = process_tree.process_record(entry.process.pid)
            if observed is not None and observed[3]:
                break
            time.sleep(0.01)
        else:
            self.fail("owned root did not stop while executor remained blocked")
        self.assertIsNone(entry.process.returncode)
        observed = supervisor.snapshot()
        self.assertTrue(observed["stop_requested"])
        self.assertTrue(observed["stop_signal_sent"])
        self.assertFalse(observed["root_waited"])
        self.assertFalse(observed["cleanup_confirmed"])

    def complete(self, execution, *, abort="cancelled"):
        target, registry, ticket, entry, worker, supervisor, ready, resume, outcomes, errors = execution
        resume.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([{"returncode": None, "abort": abort}], outcomes)
        self.owner.run_once()
        status = self.monitor.status(target)
        self.assertTrue(status.terminal_observed)
        self.assertTrue(status.cleanup_confirmed)
        self.assertEqual(entry.key, registry.key(ticket, cleaned=True))
        # Registry acknowledgement alone is not a durable account release.
        registry.acknowledge_release(ticket)
        self.assertTrue(supervisor.snapshot()["root_waited"])
        self.owner.run_once()
        return status

    def test_cancel_stops_retained_child_during_blocked_frame_without_claiming_cleanup(self):
        execution = self.execution()
        target, registry, ticket, entry, worker, supervisor, ready, _, _, _ = execution
        worker.start()
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        reply = self.request(target)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual("stop_requested", reply.result["cancellation_state"])
        self.assertFalse(reply.result["cleanup_confirmed"])
        self.assert_stopped_unreaped(entry, supervisor)
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)
        self.assertEqual("cancel_requested", self.complete(execution).stop_reason)

    def test_cancel_does_not_wait_for_a_blocked_process_snapshot(self):
        execution = self.execution(where="track")
        target, _, _, entry, worker, supervisor, ready, resume, _, _ = execution
        original = bridge._track

        def blocked(tree):
            ready.set()
            if not resume.wait(10):
                raise RuntimeError("fixture tracker timed out")
            return original(tree)

        with mock.patch.object(bridge, "_track", side_effect=blocked):
            worker.start()
            self.assertTrue(ready.wait(5))
            self.request(target)
            self.assert_stopped_unreaped(entry, supervisor)
            self.complete(execution)

    def test_startup_work_and_absolute_cutoffs_stop_without_executor_progress(self):
        for number, mode, seconds, reason in ((1, "startup", 18, "startup_silence"),
                                             (2, "work", 10, "work_silence"),
                                             (3, "deadline", 8, "absolute_deadline")):
            with self.subTest(mode=mode):
                # Separate local clock origin; prior finished attempts remain captured.
                self.clock.value = max(self.clock.value, time.monotonic_ns())
                self.origin = self.clock.value
                duration = seconds if mode == "deadline" else 60
                self.deadline = self.origin + duration * SECOND
                execution = self.execution(number, duration=duration)
                target, _, _, entry, worker, supervisor, ready, _, _, _ = execution
                worker.start()
                self.assertTrue(ready.wait(5))
                if mode != "startup":
                    self.monitor.startup(target)  # Trusted fixture fact, no native claim.
                self.at(seconds)
                self.owner.run_once()
                self.assertEqual(reason, self.monitor.status(target).stop_reason)
                self.assert_stopped_unreaped(entry, supervisor)
                abort = "timed_out" if mode == "deadline" else reason
                self.assertEqual(reason, self.complete(execution, abort=abort).stop_reason)

    def test_quiet_stale_and_recovery_do_not_extend_absolute_deadline(self):
        execution = self.execution()
        target, _, _, entry, worker, supervisor, ready, _, _, _ = execution
        worker.start()
        self.assertTrue(ready.wait(5))
        self.monitor.startup(target)
        for seconds, health in ((2, "quiet"), (5, "stale")):
            self.at(seconds)
            self.owner.run_once()
            self.assertEqual(health, self.monitor.status(target).health)
            self.assertFalse(supervisor.snapshot()["stop_requested"])
        self.at(6)
        self.assertEqual("active", self.monitor.activity(target).health)
        self.at(16)
        self.owner.run_once()
        self.assert_stopped_unreaped(entry, supervisor)
        self.assertEqual("work_silence", self.monitor.activity(target).stop_reason)
        self.assertEqual("work_silence", self.complete(execution, abort="work_silence").stop_reason)

    def test_cancel_is_exactly_scoped_between_two_actual_owned_children(self):
        first, second = self.execution(1), self.execution(2)
        for execution in (first, second):
            execution[4].start()
            self.assertTrue(execution[6].wait(5))
        self.request(first[0])
        self.assert_stopped_unreaped(first[3], first[5])
        self.assertFalse(second[5].snapshot()["stop_requested"])
        observed = process_tree.process_record(second[3].process.pid)
        self.assertIsNotNone(observed)
        self.assertFalse(observed[3])
        self.complete(first)
        self.request(second[0])
        self.complete(second)

    def test_closed_loop_and_failed_clock_still_apply_stops_and_observe_actual_cleanup(self):
        execution = self.execution()
        target, _, _, entry, worker, supervisor, ready, resume, outcomes, errors = execution
        worker.start()
        self.assertTrue(ready.wait(5))
        self.clock.value = ValueError("PRIVATE-CLOCK")
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.owner.run_once()
        self.assert_stopped_unreaped(entry, supervisor)
        self.assertEqual("monitor_unavailable", self.monitor.status(target).stop_reason)
        resume.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual("monitor_unavailable", outcomes[0]["abort"])
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.owner.run_once()
        self.assertTrue(self.monitor.status(target).cleanup_confirmed)

    def test_clock_interrupt_is_preserved_while_other_owned_children_are_stopped(self):
        executions = (self.execution(1), self.execution(2))
        for execution in executions:
            execution[4].start()
            self.assertTrue(execution[6].wait(5))
        original = KeyboardInterrupt("fixture interrupt")
        with mock.patch.object(self.monitor, "_clock", side_effect=original):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.owner.run_once()
        self.assertIs(original, caught.exception)
        for execution in executions:
            self.assert_stopped_unreaped(execution[3], execution[5])

    def test_stop_before_worker_start_never_launches_or_fabricates_cleanup(self):
        execution = self.execution()
        target, registry, ticket, entry, worker, supervisor, _, _, outcomes, errors = execution
        self.request(target)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([{"returncode": None, "abort": "cancelled"}], outcomes)
        self.assertIsNone(entry.process)
        self.assertFalse(supervisor.snapshot()["cleanup_confirmed"])
        with self.assertRaises(ExecutionCustodyError):
            registry.acknowledge_release(ticket)

    def test_handoff_revokes_issuer_execution_and_refuses_forged_or_foreign_supervisors(self):
        target, registry, ticket, _, worker, supervisor, _, _, _, _ = self.execution(attach=False)
        for operation in (ticket.enter, ticket.deadline_ns, ticket.stop_requested,
                          lambda: registry.handoff(ticket, threading.Thread())):
            with self.assertRaises(ExecutionCustodyError):
                operation()
        for forged in (ExecutionSupervisor(registry), copy(supervisor), ExecutionSupervisor(OwnedExecutionRegistry())):
            with self.assertRaises(ExecutionCustodyError):
                self.owner.bind(target, forged)
        self.owner.bind(target, supervisor)
        self.owner.bind(copy(target), supervisor)
        outcomes = []

        def foreign():
            for operation in (supervisor.snapshot, supervisor.request_stop, self.owner.run_once):
                try:
                    operation()
                except Exception:
                    outcomes.append("refused")
                else:
                    outcomes.append("allowed")

        other = threading.Thread(target=foreign)
        other.start()
        other.join(2)
        self.assertFalse(other.is_alive())
        self.assertEqual(["refused"] * 3, outcomes)
        self.assertFalse(supervisor.snapshot()["stop_requested"])
        self.assertIsNone(worker.ident)

    def test_wrong_target_deadline_and_missing_raw_handle_refuse_before_any_stop(self):
        target, registry, ticket, _, _, supervisor, _, _, _, _ = self.execution(attach=False, duration=59)
        with self.assertRaisesRegex(ControlError, "control_invalid_input"):
            self.owner.bind(target, supervisor)
        self.assertFalse(supervisor.snapshot()["stop_requested"])
        key = registry.key(ticket)
        other = OwnedExecutionRegistry()
        without = other.issue(key, deadline_ns=self.deadline)
        with self.assertRaises(ExecutionCustodyError):
            other.handoff(without, threading.Thread())
        for value in (True, "PRIVATE", "hrn_" + "a" * 49):
            with self.assertRaises(ExecutionCustodyError):
                OwnedExecutionRegistry().issue(key, deadline_ns=self.deadline, run_handle=value)

    def test_every_execution_target_label_matches_before_binding(self):
        target, _, _, _, _, supervisor, _, _, _, _ = self.execution(attach=False)
        for field, value in (("project_id", "prj_" + "b" * 32),
                             ("run_handle", "hrn_" + "b" * 48),
                             ("run_id", "run:" + "b" * 64), ("claim_id", "brg_" + "b" * 32)):
            with self.subTest(field=field):
                changed = replace(target, **{field: value})
                project_scope = replace(scope(), project_id=changed.project_id)
                monitor = ProjectMonitor(project_scope, limits(), max_tracked_attempts=4, clock_ns=self.clock)
                monitor.register(changed, hard_deadline_ns=self.deadline)
                service = ControlService(monitor, clock_ns=self.clock)
                self.addCleanup(service.close)
                loop = ControlLoop(monitor, service, ControlLoopLimits(4, 1, SECOND, 1_000_000))
                self.addCleanup(loop.close)
                with self.assertRaisesRegex(ControlError, "control_invalid_input"):
                    BridgeLivenessOwner(loop).bind(changed, supervisor)
                self.assertFalse(supervisor.snapshot()["stop_requested"])

    def test_unapproved_stop_reason_cannot_leak_content_or_change_the_first_stop(self):
        target, _, _, _, _, supervisor, _, _, _, _ = self.execution()
        with self.assertRaisesRegex(ExecutionCustodyError, "^execution_custody_unavailable$"):
            supervisor.request_stop("PRIVATE-REJECTED-CONTENT")
        self.assertFalse(supervisor.snapshot()["stop_requested"])
        self.request(target)
        supervisor.request_stop("work_silence")
        self.assertEqual("cancel_requested", supervisor.snapshot()["stop_reason"])

    def test_cancel_latched_before_factory_return_stops_actual_retained_child(self):
        execution = self.execution()
        target, _, _, entry, worker, supervisor, _, _, outcomes, errors = execution
        constructed, returned = threading.Event(), threading.Event()
        original = owned_execution._POPEN.__init__

        def delay(process, arguments, *args, **kwargs):
            original(process, arguments, *args, **kwargs)
            if arguments[:5] == [sys.executable, "-I", "-B", "-S", "-c"]:
                constructed.set()
                if not returned.wait(5):
                    raise RuntimeError("fixture factory return timed out")

        with mock.patch.object(owned_execution._POPEN, "__init__", delay):
            worker.start()
            self.assertTrue(constructed.wait(5))
            try:
                self.request(target)
                self.assertFalse(supervisor.snapshot()["process_returned"])
                self.assertFalse(supervisor.snapshot()["cleanup_confirmed"])
            finally:
                returned.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([{"returncode": None, "abort": "cancelled"}], outcomes)
        self.assertIsNotNone(entry.process.returncode)
        # Pre-bind cleanup cannot manufacture a fully identified completion.
        self.assertEqual("unknown", entry.state)
        self.assertFalse(supervisor.snapshot()["cleanup_confirmed"])

    def test_cleanup_revokes_group_signaling_before_wait_and_repeated_cancel(self):
        execution = self.execution()
        target, _, _, entry, worker, supervisor, ready, resume, _, _ = execution
        worker.start()
        self.assertTrue(ready.wait(5))
        original = bridge._stop_child
        cleaning, finish = threading.Event(), threading.Event()

        def blocked(process, tree):
            cleaning.set()
            if not finish.wait(5):
                raise RuntimeError("fixture cleanup timed out")
            original(process, tree)

        with mock.patch.object(bridge, "_stop_child", side_effect=blocked):
            self.request(target)
            resume.set()
            self.assertTrue(cleaning.wait(5))
            self.assertTrue(entry.cleanup_started)
            try:
                with mock.patch.object(owned_execution.os, "killpg", side_effect=AssertionError("late group signal")):
                    for _ in range(3):
                        self.owner.run_once()
                        self.assertFalse(supervisor.snapshot()["cleanup_confirmed"])
            finally:
                finish.set()
            self.complete(execution)

    def test_stop_signal_interrupt_cannot_skip_factory_or_handoff_abort_cleanup(self):
        for mode in ("spawn", "abort_handoff"):
            with self.subTest(mode=mode):
                target = attempt()
                registry = OwnedExecutionRegistry()
                key = OwnedExecutionKey(target.project_id, "b" * 64, target.run_id, target.claim_id)
                ticket = registry.issue(key, deadline_ns=self.deadline, run_handle=target.run_handle)
                entry = registry._entry(ticket)
                original, cleanup = KeyboardInterrupt("original factory fault"), SystemExit(4)
                errors = []

                def spawn():
                    return ticket.spawn([sys.executable, "-I", "-B", "-S", "-c", "import time; time.sleep(30)"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)

                def run():
                    try:
                        with mock.patch.object(registry, "_signal_owned_group", side_effect=cleanup):
                            if mode == "spawn":
                                with mock.patch.object(owned_execution, "ProcessTree", side_effect=original):
                                    spawn()
                            else:
                                spawn()
                                ticket.abort_handoff()
                    except BaseException as error:
                        errors.append(error)

                worker = threading.Thread(target=run)
                supervisor = registry.handoff(ticket, worker)
                self.addCleanup(self.finish_fixture, worker, threading.Event(), entry, supervisor)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual([original if mode == "spawn" else cleanup], errors)
                self.assertIsNotNone(entry.process.returncode)
                self.assertTrue(entry.cleanup_started)
                self.assertFalse(supervisor.snapshot()["cleanup_confirmed"])
