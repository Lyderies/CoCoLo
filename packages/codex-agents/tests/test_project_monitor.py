from __future__ import annotations

import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_liveness_clock import FakeClock, SECOND

from codex_agents.history_contracts import AttemptBinding
from codex_agents.liveness_clock import MAX_CLOCK_NS, LivenessClockError, LivenessLimits
from codex_agents.project_monitor import MAX_TRACKED_ATTEMPTS, ProjectMonitor, ProjectMonitorError, SupervisorScope


def binding(number=1, *, project="a"):
    return AttemptBinding("prj_" + project * 32, "session-1", "a" * 64, "supervisor-1", 1,
                          "orc_" + "a" * 48, "hrn_" + f"{number:048x}", f"run-{number}",
                          1, f"claim-{number}", f"{number:064x}")


def scope(*, project="a"):
    return SupervisorScope("prj_" + project * 32, "session-1", "a" * 64, "supervisor-1", 1)


def limits():
    return LivenessLimits(18 * SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND)


class InterruptClock(FakeClock):
    def __call__(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return super().__call__()


class ProjectMonitorTests(unittest.TestCase):
    def make_monitor(self, *, capacity=4, project="a", clock=None):
        source = FakeClock() if clock is None else clock
        return source, ProjectMonitor(scope(project=project), limits(),
                                      max_tracked_attempts=capacity, clock_ns=source)

    def test_duplicate_registration_cannot_rearm_startup_or_work_or_deadline(self):
        for started in (False, True):
            with self.subTest(started=started):
                source, owner = self.make_monitor()
                owner.register(binding(), hard_deadline_ns=100 * SECOND)
                if started:
                    owner.startup(binding())
                source.at(9)
                status = owner.register(binding(), hard_deadline_ns=100 * SECOND)
                self.assertEqual(91_000, status.liveness.hard_deadline_remaining_ms)
                if started:
                    self.assertEqual(9000, status.liveness.silence_age_ms)
                else:
                    self.assertEqual(9000, status.liveness.startup_remaining_ms)
                source.at(10 if started else 18)
                stopped = owner.status(binding())
                self.assertEqual("work_silence" if started else "startup_silence", stopped.stop_reason)

    def test_every_owner_scope_field_refuses_before_clock_sampling(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        for field, value in (("project_id", "prj_" + "b" * 32), ("primary_session_id", "other"),
                             ("runtime_digest", "b" * 64), ("supervisor_instance_id", "other"),
                             ("supervisor_generation", 2)):
            for operation in (owner.status, owner.startup, owner.activity, owner.cancel,
                              owner.observe_terminal, owner.observe_cleanup,
                              lambda target: owner.register(target, hard_deadline_ns=100 * SECOND)):
                with self.subTest(field=field, operation=operation.__name__):
                    before = source.calls
                    with self.assertRaisesRegex(ProjectMonitorError, "monitor_wrong_scope"):
                        operation(replace(binding(), **{field: value}))
                    self.assertEqual(before, source.calls)

    def test_exact_attempt_binding_and_registration_parameters_cannot_change(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        for field, value in (("orchestration_handle", "orc_" + "b" * 48), ("run_id", "other"),
                             ("claim_id", "other"), ("claim_digest", "b" * 64)):
            with self.subTest(field=field):
                changed = replace(binding(), **{field: value})
                before = source.calls
                with self.assertRaisesRegex(ProjectMonitorError, "monitor_binding_conflict"):
                    owner.status(changed)
                with self.assertRaisesRegex(ProjectMonitorError, "monitor_binding_conflict"):
                    owner.register(changed, hard_deadline_ns=100 * SECOND)
                self.assertEqual(before, source.calls)
        for deadline, requested in ((101 * SECOND, None), (99 * SECOND, None),
                                    (100 * SECOND, replace(limits(), startup_ns=17 * SECOND)),
                                    (100 * SECOND, replace(limits(), quiet_ns=SECOND)),
                                    (100 * SECOND, replace(limits(), stale_ns=4 * SECOND)),
                                    (100 * SECOND, replace(limits(), silence_ns=9 * SECOND))):
            with self.subTest(deadline=deadline, requested=requested):
                with self.assertRaisesRegex(ProjectMonitorError, "monitor_binding_conflict"):
                    owner.register(binding(), hard_deadline_ns=deadline, limits=requested)
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_attempt_not_found"):
            owner.cancel(replace(binding(), attempt=2))

    def test_registration_captures_scope_binding_and_limit_values(self):
        source = FakeClock()
        policy = limits()
        identity = scope()
        target = binding()
        owner = ProjectMonitor(identity, policy, max_tracked_attempts=2, clock_ns=source)
        narrower = replace(policy, startup_ns=12 * SECOND)
        status = owner.register(target, hard_deadline_ns=100 * SECOND, limits=narrower)
        object.__setattr__(identity, "project_id", "prj_" + "b" * 32)
        object.__setattr__(target, "claim_id", "mutated")
        object.__setattr__(status.binding, "run_handle", "hrn_" + "b" * 48)
        object.__setattr__(policy, "startup_ns", 1000 * SECOND)
        object.__setattr__(narrower, "startup_ns", 1000 * SECOND)
        source.at(12)
        self.assertEqual("startup_silence", owner.status(binding()).stop_reason)
        self.assertEqual(binding(), owner.status(binding()).binding)
        second = owner.register(binding(2), hard_deadline_ns=100 * SECOND)
        self.assertEqual(18_000, second.liveness.startup_remaining_ms)

    def test_capacity_retains_stopped_entries_and_existing_control(self):
        source, owner = self.make_monitor(capacity=1)
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        owner.cancel(binding())
        owner.observe_terminal(binding())
        owner.observe_cleanup(binding())
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_capacity_exceeded"):
            owner.register(binding(2), hard_deadline_ns=100 * SECOND)
        status = owner.register(binding(), hard_deadline_ns=100 * SECOND)
        self.assertEqual((True, True, "cancel_requested"),
                         (status.terminal_observed, status.cleanup_confirmed, status.stop_reason))
        self.assertEqual(1, len(owner.tick()))
        self.assertEqual((), owner.pending_stops())

    def test_projects_and_attempts_progress_independently(self):
        source, a = self.make_monitor()
        _, b = self.make_monitor(project="b", clock=source)
        for owner, target in ((a, binding()), (a, binding(2)), (b, binding(project="b"))):
            owner.register(target, hard_deadline_ns=100 * SECOND)
            owner.startup(target)
        source.at(8)
        a.activity(binding())
        a.cancel(binding(2))
        self.assertEqual(("active", "stopping"), tuple(status.health for status in a.tick()))
        self.assertEqual("stale", b.status(binding(project="b")).health)
        source.at(10)
        self.assertEqual("work_silence", b.pending_stops()[0].stop_reason)
        self.assertEqual("cancel_requested", a.pending_stops()[0].stop_reason)

    def test_stop_terminal_and_cleanup_observations_are_independent_and_sticky(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        owner.startup(binding())
        source.at(9)
        requested = owner.cancel(binding())
        self.assertEqual((True, False, False),
                         (requested.stop_requested, requested.terminal_observed, requested.cleanup_confirmed))
        self.assertEqual((requested,), owner.pending_stops())
        self.assertEqual((requested,), owner.pending_stops())
        source.at(1000)
        terminal = owner.observe_terminal(binding())
        self.assertEqual(("cancel_requested", True, False, False),
                         (terminal.stop_reason, terminal.terminal_observed,
                          terminal.cleanup_confirmed, terminal.liveness.terminal))
        cleaned = owner.observe_cleanup(binding())
        self.assertEqual((True, True, "cancel_requested"),
                         (cleaned.terminal_observed, cleaned.cleanup_confirmed, cleaned.stop_reason))
        self.assertEqual((), owner.pending_stops())
        self.assertEqual(cleaned, owner.activity(binding()))

    def test_timer_wins_exact_boundary_and_completion_before_boundary_wins_cancel(self):
        for terminal_first in (False, True):
            source, owner = self.make_monitor()
            owner.register(binding(), hard_deadline_ns=10 * SECOND)
            owner.startup(binding())
            source.at(9 if terminal_first else 10)
            if terminal_first:
                owner.observe_terminal(binding())
            status = owner.cancel(binding())
            self.assertEqual(None if terminal_first else "absolute_deadline", status.stop_reason)
            self.assertEqual(terminal_first, status.terminal_observed)
            self.assertEqual(terminal_first, status.liveness.terminal)

    def test_clock_failure_requests_cleanup_and_does_not_skip_other_attempts(self):
        class OnceBad(FakeClock):
            def __call__(self):
                if self.value == -1:
                    self.value = 5 * SECOND
                    raise OSError("CANARY-private-clock-exception")
                return super().__call__()

        source, owner = self.make_monitor(clock=OnceBad())
        for target in (binding(), binding(2)):
            owner.register(target, hard_deadline_ns=100 * SECOND)
            owner.startup(target)
        source.value = -1
        first, second = owner.tick()
        self.assertEqual(("unavailable", None, "monitor_unavailable"),
                         (first.health, first.liveness, first.stop_reason))
        self.assertEqual(("unavailable", "monitor_unavailable"), (second.health, second.stop_reason))
        self.assertEqual("unavailable", owner.cancel(binding()).health)
        terminal = owner.observe_terminal(binding())
        self.assertEqual((True, False, "monitor_unavailable"),
                         (terminal.terminal_observed, terminal.cleanup_confirmed, terminal.stop_reason))
        self.assertEqual(2, len(owner.pending_stops()))
        owner.observe_cleanup(binding())
        self.assertEqual((binding(2),), tuple(status.binding for status in owner.pending_stops()))
        owner.observe_cleanup(binding(2))
        self.assertEqual((), owner.pending_stops())
        self.assertNotIn("CANARY", str(asdict(first)))

    def test_invalid_work_does_not_mark_startup_or_terminal_or_cleanup(self):
        _, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        with self.assertRaisesRegex(LivenessClockError, "liveness_invalid_transition"):
            owner.activity(binding())
        status = owner.status(binding())
        self.assertEqual(("starting", False, False),
                         (status.health, status.terminal_observed, status.cleanup_confirmed))

    def test_strict_bounds_and_tampered_values_are_fixed_errors_without_clock_use(self):
        source = FakeClock()
        for maximum in (True, 0, MAX_TRACKED_ATTEMPTS + 1, 1.0, None):
            with self.subTest(maximum=maximum), self.assertRaisesRegex(ProjectMonitorError, "monitor_invalid_input"):
                ProjectMonitor(scope(), limits(), max_tracked_attempts=maximum, clock_ns=source)
        _, owner = self.make_monitor(clock=source)
        for deadline in (True, -1, 1.0, 1 << 63):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(ProjectMonitorError, "monitor_invalid_input"):
                owner.register(binding(), hard_deadline_ns=deadline)
        malformed = object.__new__(AttemptBinding)
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_invalid_input") as caught:
            owner.status(malformed)
        self.assertIsNone(caught.exception.__context__)
        for policy in (object(), object.__new__(LivenessLimits), replace(limits(), silence_ns=11 * SECOND)):
            with self.subTest(policy_type=type(policy)), self.assertRaisesRegex(
                ProjectMonitorError, "monitor_invalid_input",
            ) as caught:
                owner.register(binding(), hard_deadline_ns=100 * SECOND, limits=policy)
            self.assertIsNone(caught.exception.__context__)
        self.assertEqual(0, source.calls)

    def test_elapsed_original_deadline_stops_at_registration(self):
        _, owner = self.make_monitor()
        status = owner.register(binding(), hard_deadline_ns=0)
        self.assertEqual(("stopping", "absolute_deadline", False, False),
                         (status.health, status.stop_reason, status.terminal_observed, status.cleanup_confirmed))

    def test_foreign_equal_thread_and_changed_pid_refuse_before_validation_or_sampling(self):
        for phase in ("cancel", "observe_terminal", "observe_cleanup"):
            with self.subTest(phase=phase):
                self.check_foreign_owner(phase)

    def check_foreign_owner(self, phase):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        getattr(owner, phase)(binding())
        calls = source.calls
        operations = (lambda: owner.register(object(), hard_deadline_ns=True), lambda: owner.status(object()),
                      lambda: owner.startup(object()), lambda: owner.activity(object()),
                      lambda: owner.cancel(object()), lambda: owner.observe_terminal(object()),
                      lambda: owner.observe_cleanup(object()), owner.tick, owner.pending_stops, owner.pending_cleanup)
        results = []

        def attempt():
            for operation in operations:
                try:
                    operation()
                except ProjectMonitorError as exc:
                    results.append(exc.reason_code)

        class EqualThread(threading.Thread):
            def __eq__(self, other):
                return True

            __hash__ = object.__hash__

        thread = EqualThread(target=attempt)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        with mock.patch("codex_agents.project_monitor.os.getpid", return_value=os.getpid() + 1):
            attempt()
        self.assertEqual(["monitor_wrong_owner"] * (2 * len(operations)), results)
        self.assertEqual(calls, source.calls)

    def regressed_monitor(self):
        source = FakeClock(50 * SECOND)
        owner = ProjectMonitor(scope(), LivenessLimits(180 * SECOND, 120 * SECOND, 300 * SECOND, 600 * SECOND),
                               max_tracked_attempts=3, clock_ns=source)
        owner.register(binding(2), hard_deadline_ns=1000 * SECOND)
        owner.startup(binding(2))
        source.at(100)
        owner.register(binding(), hard_deadline_ns=1000 * SECOND)
        owner.startup(binding())
        source.at(70)
        owner.tick()
        return source, owner

    def test_shared_regression_invalidates_all_undecided_attempts_and_future_registration(self):
        source, owner = self.regressed_monitor()
        self.assertEqual(("unavailable", "unavailable"), tuple(status.health for status in owner.tick()))
        before = source.calls
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_unavailable"):
            owner.register(binding(3), hard_deadline_ns=1000 * SECOND)
        self.assertEqual(before, source.calls)

    def test_every_invalid_shared_sample_latches_all_attempts_without_resampling(self):
        for value in (True, -1, 1.0, None, MAX_CLOCK_NS + 1, OSError("CANARY-private-clock")):
            with self.subTest(value_type=type(value)):
                source, owner = self.make_monitor()
                for target in (binding(), binding(2)):
                    owner.register(target, hard_deadline_ns=100 * SECOND)
                    owner.startup(target)
                before = source.calls
                source.value = value
                statuses = owner.tick()
                self.assertEqual(("unavailable", "unavailable"), tuple(s.health for s in statuses))
                self.assertTrue(all(s.liveness is None and s.stop_reason == "monitor_unavailable" for s in statuses))
                self.assertNotIn("CANARY", str(asdict(statuses[0])))
                self.assertEqual(before + 1, source.calls)
                source.at(1)
                owner.tick()
                self.assertEqual(before + 1, source.calls)

    def test_registration_clock_failure_aborts_tracking_and_cannot_restart_health(self):
        source, owner = self.make_monitor()
        source.value = OSError("CANARY-private-clock")
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_unavailable") as caught:
            owner.register(binding(), hard_deadline_ns=100 * SECOND)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual((), owner.tick())
        source.at(1)
        with self.assertRaisesRegex(ProjectMonitorError, "monitor_unavailable"):
            owner.register(binding(), hard_deadline_ns=100 * SECOND)
        self.assertEqual(1, source.calls)

    def test_reentrant_shared_clock_cannot_clear_an_inner_failure(self):
        class ReentrantClock(FakeClock):
            callback = None

            def __call__(self):
                value = super().__call__()
                callback, self.callback = self.callback, None
                if callback is not None:
                    callback()
                return value

        source, owner = self.make_monitor(clock=ReentrantClock())
        for target in (binding(), binding(2)):
            owner.register(target, hard_deadline_ns=100 * SECOND)
        source.callback = lambda: owner.status(binding(2))
        self.assertEqual("unavailable", owner.status(binding()).health)
        self.assertEqual(("unavailable", "unavailable"), tuple(s.health for s in owner.tick()))

    def test_nonlocal_interrupt_preserves_each_valid_terminal_or_cleanup_observation(self):
        for operation, field in (("observe_terminal", "terminal_observed"), ("observe_cleanup", "cleanup_confirmed")):
            for exception in (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(operation=operation, exception=exception.__name__):
                    source, owner = self.make_monitor(clock=InterruptClock())
                    owner.register(binding(), hard_deadline_ns=100 * SECOND)
                    source.value = exception()
                    with self.assertRaises(exception):
                        getattr(owner, operation)(binding())
                    source.at(1)
                    status = owner.status(binding())
                    self.assertTrue(getattr(status, field))
                    self.assertIsNone(status.liveness)
                    self.assertEqual("monitor_unavailable", status.stop_reason)
                    self.assertEqual("cleaned" if operation == "observe_cleanup" else "unavailable", status.health)

    def test_nonlocal_tick_interrupt_propagates_with_monitor_wide_failure_latched(self):
        source, owner = self.make_monitor(clock=InterruptClock())
        for target in (binding(), binding(2)):
            owner.register(target, hard_deadline_ns=100 * SECOND)
            owner.startup(target)
        source.value = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            owner.tick()
        source.at(1)
        self.assertEqual(("unavailable", "unavailable"), tuple(s.health for s in owner.tick()))
        self.assertEqual(2, len(owner.pending_cleanup()))

    def test_frozen_stop_and_terminal_decisions_survive_shared_failure_and_output_mutation(self):
        source, owner = self.make_monitor()
        for target in (binding(), binding(2), binding(3)):
            owner.register(target, hard_deadline_ns=100 * SECOND)
        cancelled = owner.cancel(binding())
        owner.observe_terminal(binding(2))
        object.__setattr__(cancelled.liveness, "health", "active")
        object.__setattr__(cancelled.liveness, "stop_reason", "mutated")
        source.value = OSError("CANARY-private-clock")
        statuses = owner.tick()
        self.assertEqual(("stopping", "terminal", "unavailable"), tuple(s.health for s in statuses))
        self.assertEqual(("cancel_requested", None, "monitor_unavailable"), tuple(s.stop_reason for s in statuses))
        self.assertEqual(3, len(owner.pending_cleanup()))

    def test_cleanup_before_startup_is_final_without_fabricating_terminal(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        cleaned = owner.observe_cleanup(binding())
        self.assertEqual(("cleaned", None, None, False, True),
                         (cleaned.health, cleaned.liveness, cleaned.stop_reason,
                          cleaned.terminal_observed, cleaned.cleanup_confirmed))
        calls = source.calls
        for operation in (owner.startup, owner.activity, owner.status, owner.cancel, owner.observe_cleanup):
            source.value = KeyboardInterrupt()
            self.assertEqual(cleaned, operation(binding()))
        self.assertEqual(calls, source.calls)
        source.at(1)
        terminal = owner.observe_terminal(binding())
        self.assertEqual(("cleaned", None, True, True),
                         (terminal.health, terminal.stop_reason, terminal.terminal_observed, terminal.cleanup_confirmed))
        self.assertEqual(calls + 1, source.calls)
        source.value = KeyboardInterrupt()
        self.assertEqual(terminal, owner.observe_terminal(binding()))
        self.assertEqual(calls + 1, source.calls)
        self.assertEqual((), owner.pending_cleanup())

    def test_early_cleanup_does_not_suppress_late_authoritative_terminal_deadline(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=10 * SECOND)
        owner.startup(binding())
        source.at(1)
        owner.observe_cleanup(binding())
        source.at(10)
        terminal = owner.observe_terminal(binding())
        self.assertEqual(("cleaned", "absolute_deadline", True, True, None),
                         (terminal.health, terminal.stop_reason, terminal.terminal_observed,
                          terminal.cleanup_confirmed, terminal.liveness))
        self.assertEqual((), owner.pending_stops())

    def test_first_cleanup_classifies_elapsed_cutoff_and_never_resamples_it(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=10 * SECOND)
        owner.startup(binding())
        source.at(10)
        cleaned = owner.observe_cleanup(binding())
        self.assertEqual(("cleaned", "absolute_deadline", None, False),
                         (cleaned.health, cleaned.stop_reason, cleaned.liveness, cleaned.terminal_observed))
        calls = source.calls
        source.value = KeyboardInterrupt()
        self.assertEqual(cleaned, owner.register(binding(), hard_deadline_ns=10 * SECOND))
        self.assertEqual(cleaned, owner.observe_cleanup(binding()))
        self.assertEqual(calls, source.calls)

    def test_normal_terminal_has_pending_cleanup_without_a_stop_request(self):
        _, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        status = owner.observe_terminal(binding())
        self.assertEqual((True, False, False),
                         (status.terminal_observed, status.stop_requested, status.cleanup_confirmed))
        self.assertEqual((), owner.pending_stops())
        self.assertEqual((status,), owner.pending_cleanup())
        owner.observe_cleanup(binding())
        self.assertEqual((), owner.pending_cleanup())

    @unittest.skipUnless(hasattr(os, "fork"), "requires inherited process fixture")
    def test_actual_fork_refuses_inherited_monitor_before_sampling(self):
        source, owner = self.make_monitor()
        owner.register(binding(), hard_deadline_ns=100 * SECOND)
        calls = source.calls
        pid = os.fork()
        if pid == 0:
            try:
                owner.status(object())
            except ProjectMonitorError as exc:
                os._exit(0 if exc.reason_code == "monitor_wrong_owner" and source.calls == calls else 2)
            os._exit(3)
        _, result = os.waitpid(pid, 0)
        self.assertEqual(0, os.waitstatus_to_exitcode(result))

    def test_fresh_import_opens_no_persistence_owner_or_sqlite_dependency(self):
        code = """import sys
sys.path.insert(0, sys.argv[1])
from codex_agents.project_monitor import ProjectMonitor
assert not any(name in sys.modules for name in ('sqlite3', 'codex_agents.learning_store',
    'codex_agents.project_history', 'codex_agents.learning_adapters', 'codex_agents.bridge'))
print('metadata-only import')
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ORCHESTRATION_ROOT)],
                                capture_output=True, timeout=5)
        self.assertEqual((0, b"metadata-only import\n"), (result.returncode, result.stdout), result.stderr)

    def test_local_control_and_timers_continue_during_real_project_write_lock(self):
        # Disposable library fixture, not a production service/admission proof.
        worker = """import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('CREATE TABLE fixture(value INTEGER)')
db.commit()
db.execute('BEGIN EXCLUSIVE')
print('LOCKED', flush=True)
sys.stdin.buffer.read(1)
db.rollback()
db.close()
"""
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            database = Path(directory) / "fixture.sqlite"
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", worker, str(database)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                self.assertEqual(b"LOCKED\n", process.stdout.readline())
                connection = sqlite3.connect(database, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("BEGIN IMMEDIATE")
                finally:
                    connection.close()
                source, owner = self.make_monitor()
                owner.register(binding(), hard_deadline_ns=100 * SECOND)
                owner.register(binding(2), hard_deadline_ns=100 * SECOND)
                owner.startup(binding())
                source.at(9)
                self.assertEqual("cancel_requested", owner.cancel(binding()).stop_reason)
                source.at(18)
                self.assertEqual(("cancel_requested", "startup_silence"),
                                 tuple(status.stop_reason for status in owner.pending_stops()))
                self.assertIsNone(process.poll())
            finally:
                try:
                    process.communicate(input=b"x", timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)
            self.assertEqual(0, process.returncode)


if __name__ == "__main__":
    unittest.main()
