"""C3: one stop action for every reason, and "gone" asked of the OS (PLAN-0.4.0-supervisor-slices.md, C3).

Requirements (RS-2 and RS-6 for the child process, RS-C-5, RS-C-7, RS-C-12, gaps C1, C8, C10), container
mode only (behind C2's switch; the process mode is unchanged):
- C3-1 One stop action: every reason a run is stopped for (the liveness clock's startup and work silence,
  absolute deadline, cancel and monitor unavailable; the bridge's own deadline and end; the service closing;
  the detached watchdog) stops the run's container through `container_runtime.stop` (SIGTERM, the grace,
  SIGKILL to every process in it, removal), and the journal records which reason stopped it first.
  Stops of one container never overlap: a stop already under way is joined, a container proven gone is
  never stopped again.
- C3-2 Gone, never claimed falsely: `gone` only when the OS says so; anything else (still running, a
  populated cgroup, podman unreachable) is `stop_failed`: the run is not confirmed clean, so its slot stays
  held and nothing is released.
- C3-3 A failed stop is tried again with back-off (STOP_RETRY_SECONDS doubling to STOP_RETRY_MAX_SECONDS)
  while the service runs; once the OS says gone the run is confirmed clean and its normal release goes on.
- C3-4 Status: the project's run containers counted by state, each `stop_failed` with its reason, detail
  and attempts, a plain summary, and an owner alert (the status's attention list) naming the container
  and how to end it by hand.
- C3-5 A live service settles its own ended runs: a failed run with an admitted claim whose container is
  gone is settled with the account (`settle_lost_run`), its entry `settled` and its preparation given
  back, without waiting for the service to stop; never a run on its normal path, never one whose
  container is not gone. At the service's own stop, its containers not yet gone are stopped once more
  (`service_close`) and those gone settled; another instance's entries are left to recovery (C4).
- C3-6 The detached watchdog, retargeted: in container mode it stops the container through
  `container_runtime.stop` when the service that launched it has died, and leaves it to the live
  service otherwise.

The real-podman tests run only with podman, its machine and python:3.12-slim present (never pulled);
they create containers named `cocolo-test-*` with a per-test project label and remove exactly those.
"""
from __future__ import annotations

from concurrent.futures import Future
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from tests.test_container_launch_journal import (BRIDGE, CONTAINER_PROJECT, HANDLE, OTHER_BRIDGE, PROJECT,
                                                 TimeoutPodman, claiming_run, deadline_ms, driver, private_folder,
                                                 write_intent)
from tests.test_container_runtime import FakeClock, FakeHostFiles, REAL_IMAGE, REAL_PODMAN, REAL_SKIP_REASON

from codex_agents import bridge, container_launch, launch_journal, process_tree
from codex_agents import container_runtime as cr
from codex_agents.codex_worker import CodexWorkerLimits, CodexWorkerPool
from codex_agents.launch_journal import JournalError, LaunchJournal
from codex_agents.liveness_clock import LIVENESS_STOP_REASONS
from codex_agents.owned_execution import ExecutionCustodyError, OwnedExecutionKey, OwnedExecutionRegistry

# The bridge's abort for each stop the liveness clock (or the cancel) asks for.
ABORTS = {'cancel_requested': 'cancelled', 'absolute_deadline': 'timed_out', 'startup_silence': 'startup_silence',
          'work_silence': 'work_silence', 'monitor_unavailable': 'monitor_unavailable'}
# Stands in for the attached container output: runs while the fake container runs.
WAIT_WHILE_RUNNING = 'import os, sys, time\nwhile os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n'


class RunningPodman(TimeoutPodman):
    """The fake podman; each running container is a flag file its attach stand-in waits on."""

    def __init__(self, host, folder):
        super().__init__(host)
        self.folder = folder

    def flag(self, name):
        return os.path.join(self.folder, name + '.running')

    def _run(self, container):
        super()._run(container)
        Path(self.flag(container.name)).touch()

    def _end(self, container, code):
        super()._end(container, code)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.flag(container.name))


def stop_launcher(test, *, grace_seconds=1):
    """A launcher over the running fake podman; its back-off clock is the test's fake clock."""
    root = private_folder(test)
    test.workspace = os.path.join(root, 'workspace')
    os.mkdir(test.workspace)
    test.seccomp = os.path.join(root, 'seccomp.json')
    Path(test.seccomp).write_text('{}')
    test.host = FakeHostFiles()
    test.podman = RunningPodman(test.host, root)
    test.clock = FakeClock()
    runtime = cr.ContainerRuntime(CONTAINER_PROJECT, runner=test.podman, host_files=test.host,
                                  monotonic=test.clock.monotonic, sleep=test.clock.sleep)
    test.journal = LaunchJournal(Path(root) / 'launches')
    test.addCleanup(test.journal.release)
    config = container_launch.ContainerLaunchConfig(image='python:3.12-slim', seccomp_profile=test.seccomp,
                                                    grace_seconds=grace_seconds)

    def attach(started):
        return [sys.executable, '-I', '-B', '-c', WAIT_WHILE_RUNNING, test.podman.flag(started.name)]

    launcher = container_launch.ContainerLauncher(runtime, test.journal, config, attach=attach,
                                                  clock=test.clock.monotonic)
    test.addCleanup(launcher.close)
    return launcher


def wait_until(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class BridgeRun:
    """One bridge run of the registry's ticket on its own worker thread; the test thread supervises it."""

    def __init__(self, test, registry, ticket, *, command=('python3', '-c', 'pass'), deadline=30.0):
        self.test, self.registry, self.ticket = test, registry, ticket
        self.command, self.deadline, self.outcome = list(command), deadline, {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.supervisor = registry.handoff(ticket, self.thread)

    def _run(self):
        self.outcome.update(bridge.run_child(self.command, environment={'PATH': os.defpath},
                                             cwd=Path(self.test.workspace), deadline_seconds=self.deadline,
                                             on_frame=lambda frame: None, _execution=self.ticket))

    def start(self):
        self.thread.start()
        self.test.assertTrue(wait_until(lambda: self.supervisor.snapshot()['state'] == 'active'),
                             'the run did not start: %r' % (self.outcome,))
        return self

    def join(self, seconds=30.0):
        self.thread.join(seconds)
        self.test.assertFalse(self.thread.is_alive(), 'the bridge run did not end')
        return self.outcome


class ContainerStopCase(unittest.TestCase):
    """A registry whose runs are launched in containers over the running fake podman."""

    def setUp(self):
        self.launcher = stop_launcher(self)
        self.token = write_intent(self.launcher)
        self.journal.admitted(self.token, BRIDGE, deadline_ms())
        self.registry = OwnedExecutionRegistry()
        self.registry.launch_journal = self.journal
        self.registry.container_launch = self.launcher
        self.ticket = self.registry.issue(OwnedExecutionKey(PROJECT, 'b' * 64, 'run:' + 'c' * 64, BRIDGE),
                                          deadline_ns=time.monotonic_ns() + 60_000_000_000, run_handle=HANDLE)

    def entry(self):
        return self.journal.entry(self.token)

    def name(self):
        return self.entry()['container_name']

    def stops_of(self, name):
        return [call for call in self.podman.commands('stop') if call[-1] == name]

    def bridge_run(self, **options):
        return BridgeRun(self, self.registry, self.ticket, **options)

    def confirmed(self, run):
        return run.supervisor.snapshot()['cleanup_confirmed']


# --------------------------------------------------------------------------------------------
# C3-1 One stop action, for every reason
# --------------------------------------------------------------------------------------------

class EveryReasonTests(ContainerStopCase):
    def test_every_liveness_reason_stops_the_container_through_the_one_stop_action(self):
        self.assertEqual(set(ABORTS), set(LIVENESS_STOP_REASONS))
        self.assertTrue(set(LIVENESS_STOP_REASONS) <= launch_journal.CONTAINER_STOP_REASONS)
        for reason in sorted(LIVENESS_STOP_REASONS):
            with self.subTest(reason=reason):
                self.setUp()
                run = self.bridge_run().start()
                name = self.name()
                self.assertEqual('started', self.entry()['state'])
                self.assertEqual([], self.stops_of(name))
                run.supervisor.request_stop(reason)
                outcome = run.join()
                self.assertEqual(ABORTS[reason], outcome['abort'])
                # SIGTERM, the grace (the launcher's), then the kill and removal: podman's stop, then rm.
                self.assertEqual([('stop', '--time=1', name)], self.stops_of(name))
                self.assertIn(('rm', '--force', '--time=0', name), self.podman.commands('rm'))
                self.assertNotIn(name, self.podman.containers)
                entry = self.entry()
                self.assertEqual(('gone', reason, 1), (entry['state'], entry['stop_reason'], entry['stop_attempts']))
                # Proven gone, so the run is confirmed clean and may be released.
                self.assertTrue(self.confirmed(run))
                self.assertEqual(BRIDGE, self.registry.key(self.ticket, cleaned=True).bridge_run_id)

    def test_a_repeated_request_never_stops_the_container_twice(self):
        run = self.bridge_run().start()
        name = self.name()
        for _ in range(5):  # the liveness loop asks again on every synchronization
            run.supervisor.request_stop('work_silence')
        run.join()
        self.assertEqual(1, len(self.stops_of(name)))
        self.assertEqual(('gone', 'work_silence', 1),
                         (self.entry()['state'], self.entry()['stop_reason'], self.entry()['stop_attempts']))

    def test_the_bridges_own_deadline_is_the_absolute_deadline(self):
        run = self.bridge_run(deadline=0.5).start()
        name = self.name()
        self.assertEqual('timed_out', run.join()['abort'])
        self.assertEqual(1, len(self.stops_of(name)))
        self.assertEqual(('gone', 'absolute_deadline'), (self.entry()['state'], self.entry()['stop_reason']))
        self.assertTrue(self.confirmed(run))

    def test_a_run_that_ends_by_itself_is_stopped_the_same_way(self):
        run = self.bridge_run().start()
        name = self.name()
        os.unlink(self.podman.flag(name))  # its output ends (the fake container itself still "runs")
        self.assertIsNone(run.join()['abort'])
        self.assertEqual([('stop', '--time=1', name)], self.stops_of(name))
        self.assertEqual(('gone', 'run_ended'), (self.entry()['state'], self.entry()['stop_reason']))
        self.assertTrue(self.confirmed(run))

    def test_the_service_closing_stops_its_runs_as_service_close(self):
        run = self.bridge_run().start()
        self.registry.closing()
        run.supervisor.request_stop()  # what the worker pool's close asks of every run
        self.assertEqual('cancelled', run.join()['abort'])
        self.assertEqual(('gone', 'service_close'), (self.entry()['state'], self.entry()['stop_reason']))
        self.assertTrue(self.confirmed(run))

    def test_closing_the_worker_pool_marks_its_registry_closing(self):
        registry = OwnedExecutionRegistry()
        pool = CodexWorkerPool(registry, CodexWorkerLimits(1, 16 * 1_048_576))
        self.assertFalse(registry.is_closing)
        pool.close()
        self.assertTrue(registry.is_closing)

    def test_a_stop_asked_while_the_container_is_starting_stops_it_once_it_exists(self):
        original = self.launcher.start
        created, asked = threading.Event(), threading.Event()

        def start(*args, **kwargs):
            started = original(*args, **kwargs)
            created.set()
            asked.wait(10)  # the stop arrives before the registry has the container
            return started

        with mock.patch.object(self.launcher, 'start', side_effect=start):
            run = BridgeRun(self, self.registry, self.ticket)
            run.thread.start()
            self.assertTrue(created.wait(10))
            run.supervisor.request_stop('cancel_requested')
            asked.set()
            outcome = run.join()
        self.assertEqual('cancelled', outcome['abort'])
        self.assertEqual(('gone', 'cancel_requested'), (self.entry()['state'], self.entry()['stop_reason']))
        self.assertNotIn(self.name(), self.podman.containers)
        # Its local output reader was stopped before it was ever tracked, so (as in the process mode)
        # the execution is not confirmed clean here; the gone container lets the driver settle it.
        self.assertFalse(self.confirmed(run))

    def test_a_stop_under_way_is_joined_not_repeated(self):
        started = container_launch.StartedContainer(self.token, self.journal.entry(self.token)['container_name'],
                                                    '', '')
        gate, entered = threading.Event(), threading.Event()

        def slow_stop(*args, **kwargs):
            entered.set()
            gate.wait(10)
            return cr.StopResult('gone', 'the container no longer exists')

        with mock.patch.object(self.launcher.runtime, 'stop', side_effect=slow_stop) as stop:
            future = self.launcher.request_stop(started, 'startup_silence')
            self.assertTrue(entered.wait(10))
            joined = []
            joiner = threading.Thread(target=lambda: joined.append(self.launcher.stop(started, 'run_ended')))
            joiner.start()
            time.sleep(0.1)
            self.assertEqual([], joined)  # waits for the stop under way
            gate.set()
            joiner.join(10)
            self.assertTrue(future.result(10))
        self.assertEqual([True], joined)
        self.assertEqual(1, stop.call_count)
        self.assertEqual(('gone', 'startup_silence', 1),
                         (self.entry()['state'], self.entry()['stop_reason'], self.entry()['stop_attempts']))
        # Proven gone: never stopped again.
        self.assertTrue(self.launcher.stop(started, 'service_close', now=True))
        self.assertEqual([], self.podman.commands('stop'))

    def test_a_refused_launch_records_its_stop_reason(self):
        self.podman.reported_timeout = 0
        with self.assertRaises(container_launch.ContainerLaunchRefused):
            self.launcher.start(BRIDGE, ('python3', '-c', 'pass'), workspace=self.workspace, run_tag='0' * 32,
                                deadline_ns=time.monotonic_ns() + 30_000_000_000)
        self.assertEqual(('gone', 'launch_refused'), (self.entry()['state'], self.entry()['stop_reason']))

    def test_the_journal_takes_only_known_reasons_and_keeps_the_first(self):
        self.journal.started(self.token, 'a' * 64, '/machine.slice/libpod-x.scope')
        with self.assertRaises(JournalError):
            self.journal.stopping(self.token, 'because')
        self.journal.stopping(self.token, 'work_silence')
        self.journal.stopped(self.token, False, 'not gone')
        self.journal.stopping(self.token, 'service_close')
        entry = self.entry()
        self.assertEqual(('stopping', 'work_silence', 2), (entry['state'], entry['stop_reason'], entry['stop_attempts']))
        # A stop interrupted part way (its service died) may be tried again.
        self.journal.stopping(self.token, 'service_close')
        self.assertEqual(3, self.entry()['stop_attempts'])


# --------------------------------------------------------------------------------------------
# C3-2 stop_failed holds the slot; C3-3 retried with back-off; C3-4 status and the owner alert
# --------------------------------------------------------------------------------------------

class StopFailedTests(ContainerStopCase):
    def failed_run(self, reason='work_silence'):
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        run = self.bridge_run().start()
        run.supervisor.request_stop(reason)
        self.assertEqual(ABORTS[reason], run.join()['abort'])
        return run

    def test_a_container_still_running_after_the_kill_is_stop_failed_and_keeps_its_slot(self):
        run = self.failed_run()
        entry = self.entry()
        self.assertEqual(('stop_failed', 'work_silence', 1), (entry['state'], entry['stop_reason'],
                                                              entry['stop_attempts']))
        self.assertIn('hold its slot', entry['stop_detail'])
        self.assertIn(entry['container_name'], self.podman.containers)
        # Not confirmed clean: no release can start for it.
        self.assertFalse(self.confirmed(run))
        with self.assertRaises(ExecutionCustodyError):
            self.registry.key(self.ticket, cleaned=True)
        with self.assertRaises(ExecutionCustodyError):
            self.registry.acknowledge_release(self.ticket)

    def test_podman_unreachable_is_stop_failed_never_gone(self):
        run = self.bridge_run().start()
        self.podman.unreachable = True
        run.supervisor.request_stop('monitor_unavailable')
        run.join()
        entry = self.entry()
        self.assertEqual('stop_failed', entry['state'])
        self.assertIn('not reachable', entry['stop_detail'])
        self.assertFalse(self.confirmed(run))

    def test_a_failed_stop_is_retried_with_back_off_until_the_os_says_gone(self):
        run = self.failed_run()
        name = self.name()
        stops = lambda: len(self.stops_of(name))  # noqa: E731
        self.assertEqual(1, stops())
        self.assertEqual(0, self.launcher.retry_stops())  # not due yet
        self.assertEqual(1, stops())
        delays = []
        for attempt in range(2, 6):
            delay = container_launch.stop_retry_delay(attempt - 1)
            delays.append(delay)
            self.clock.now += delay - 0.5
            self.assertEqual(0, self.launcher.retry_stops())
            self.clock.now += 0.5
            self.assertEqual(1, self.launcher.retry_stops())
            self.assertEqual(attempt, stops())
            self.assertEqual(('stop_failed', attempt), (self.entry()['state'], self.entry()['stop_attempts']))
            self.assertFalse(self.confirmed(run))
        self.assertEqual([container_launch.STOP_RETRY_SECONDS * 2 ** n for n in range(4)], delays)
        self.assertEqual(container_launch.STOP_RETRY_MAX_SECONDS, container_launch.stop_retry_delay(40))
        # The container goes: the next retry proves it, and the run is confirmed clean by itself.
        self.podman.stop_does_nothing = self.podman.rm_fails = False
        self.clock.now += container_launch.stop_retry_delay(5)
        self.assertEqual(1, self.launcher.retry_stops())
        self.assertEqual(('gone', 'work_silence', 6), (self.entry()['state'], self.entry()['stop_reason'],
                                                      self.entry()['stop_attempts']))
        self.assertNotIn(name, self.podman.containers)
        self.assertTrue(self.confirmed(run))
        self.assertEqual(BRIDGE, self.registry.key(self.ticket, cleaned=True).bridge_run_id)
        # Nothing is left to retry.
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        self.assertEqual(0, self.launcher.retry_stops())

    def test_status_counts_the_containers_by_state_and_names_each_stop_failed(self):
        self.failed_run('startup_silence')
        name = self.name()
        other = write_intent(self.launcher)  # a second run, not yet started
        holds = launch_journal.container_holds(self.journal.directory)
        self.assertEqual({'intent': 1, 'started': 0, 'stopping': 0, 'gone': 0, 'stop_failed': 1}, holds['counts'])
        [failed] = holds['stop_failed']
        self.assertEqual((name, 'startup_silence', 1), (failed['container'], failed['reason'], failed['attempts']))
        self.assertIn('hold its slot', failed['detail'])
        self.assertGreaterEqual(failed['age_ms'], 0)
        self.assertIn('1 could not be stopped', holds['summary'])
        [line] = holds['attention']
        self.assertIn(name, line)
        self.assertIn('could not be stopped', line)
        self.assertIn('slot stays held', line)
        self.assertIn('podman rm --force ' + name, line)
        self.assertNotIn('Traceback', line)
        # A settled entry is no longer counted.
        self.journal.remove(other)
        self.assertEqual(0, launch_journal.container_holds(self.journal.directory)['counts']['intent'])

    def test_status_is_empty_without_a_journal(self):
        holds = launch_journal.container_holds(Path(self.journal.directory) / 'missing')
        self.assertEqual({'intent': 0, 'started': 0, 'stopping': 0, 'gone': 0, 'stop_failed': 0}, holds['counts'])
        self.assertEqual(([], []), (holds['stop_failed'], holds['attention']))

    def test_learning_status_shows_the_holds_and_alerts_the_owner(self):
        from codex_agents import client_runtime
        from codex_agents.learning_store import LearningState, authorize_collector_runtime
        self.failed_run('absolute_deadline')
        name = self.name()
        base = Path(private_folder(self))
        state = LearningState(base / 'state')
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / 'cli.py')
        project = base / 'project'
        (project / '.git').mkdir(parents=True)
        project_id = 'prj_' + 'a' * 32
        launches = Path(state.root) / 'projects' / project_id / 'launches'
        launches.parent.mkdir(parents=True, mode=0o700)
        os.mkdir(launches, 0o700)
        for path in Path(self.journal.directory).glob('lnc_*.json'):
            target = launches / path.name
            target.write_bytes(path.read_bytes())
            os.chmod(target, 0o600)
        owner = mock.Mock()
        owner.owner_key.return_value = {'state': 'present', 'fingerprint': 'f', 'replacements': 0,
                                        'last_replaced_at_ms': None}
        with mock.patch.object(client_runtime, '_account_target', return_value=(project_id, 'e')), \
                mock.patch.object(client_runtime, 'enroll_clients'), \
                mock.patch.object(client_runtime, 'AccountClient', return_value=owner), \
                mock.patch.object(client_runtime.CollectorClient, 'health',
                                  side_effect=client_runtime.CollectorError('x')):
            report = client_runtime.status_client(state.root, project)
        self.assertEqual(1, report['container_holds']['counts']['stop_failed'])
        self.assertEqual('absolute_deadline', report['container_holds']['stop_failed'][0]['reason'])
        alert = [line for line in report['account_health']['attention'] if name in line]
        self.assertEqual(1, len(alert), report['account_health'])
        self.assertIn('could not be stopped', alert[0])


# --------------------------------------------------------------------------------------------
# C3-5 A live service settles its own ended runs
# --------------------------------------------------------------------------------------------

class Immediately:
    """Stands in for the driver's helper thread: runs each call at once."""

    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as error:  # noqa: BLE001 - the future carries it, as a real executor's does
            future.set_exception(error)
        return future

    def shutdown(self, **_):
        pass


class OwnEndedRunTests(unittest.TestCase):
    def setUp(self):
        self.driver = driver(self)
        self.driver._sweeper = Immediately()
        self.driver._settle_entry = mock.Mock(return_value={'state': 'settled'})
        self.driver._runs = {}

    def admitted_run(self, *, state='failed', container='gone', bridge_run_id=BRIDGE, reason='startup_silence'):
        """A run of this service whose claim the account admitted, with its container entry in that state."""
        run = claiming_run()
        self.driver._advance(run)  # its intent, then the claim
        run.key = (HANDLE[:-1] + str(len(self.driver._runs)), 1)
        token = run.launch_token
        self.journal.admitted(token, bridge_run_id, deadline_ms())
        if container != 'intent':
            self.journal.started(token, 'a' * 64, '/machine.slice/libpod-x.scope')
            if container in ('gone', 'stop_failed'):
                self.journal.stopping(token, reason)
                self.journal.stopped(token, container == 'gone', 'the container no longer exists'
                                     if container == 'gone' else 'not gone 10 s after the kill; hold its slot')
        run.receipt = {'state': 'claimed', 'bridge_run_id': bridge_run_id, 'deadline_at_ms': deadline_ms()}
        run.state, run.reason, run.prepared = state, reason, 'preparation-' + bridge_run_id
        self.driver._runs[run.key] = run
        return run

    def test_a_failed_runs_gone_container_is_settled_without_a_restart(self):
        run = self.admitted_run()
        self.driver._settle_own_ended()
        self.driver._settle_own_ended()
        self.driver._settle_entry.assert_called_once()
        [settled] = self.driver._settle_entry.call_args.args
        self.assertEqual((run.launch_token, BRIDGE), (settled['token'], settled['bridge_run_id']))
        entry = self.journal.entry(run.launch_token)
        self.assertEqual(('settled', 'settled'), (entry['state'], entry['outcome']))
        # Its preparation is given back: the account no longer holds the run.
        self.driver._preparation.discard.assert_called_once_with('preparation-' + BRIDGE)
        self.assertIsNone(run.prepared)
        self.assertIn('preparation-' + BRIDGE, self.driver._giving_back)
        # Settled once.
        for _ in range(3):
            self.driver._settle_own_ended()
        self.driver._settle_entry.assert_called_once()

    def test_a_run_whose_container_is_not_gone_is_never_settled(self):
        for container in ('stop_failed', 'started', 'intent'):
            with self.subTest(container=container):
                self.setUp()
                run = self.admitted_run(container=container)
                for _ in range(3):
                    self.driver._settle_own_ended()
                self.driver._settle_entry.assert_not_called()
                self.assertEqual(container, self.journal.entry(run.launch_token)['state'])
                self.driver._preparation.discard.assert_not_called()
                self.assertIsNotNone(run.prepared)

    def test_a_run_on_its_normal_path_is_never_settled_as_lost(self):
        for state in ('acknowledging', 'cleanup', 'releasing', 'terminal'):
            with self.subTest(state=state):
                self.setUp()
                run = self.admitted_run(state=state)
                self.driver._settle_own_ended()
                self.driver._settle_entry.assert_not_called()
                self.assertEqual('gone', self.journal.entry(run.launch_token)['state'])

    def test_a_process_mode_run_is_left_as_today(self):
        run = self.admitted_run()
        run.launch_token = None
        self.driver._settle_own_ended()
        self.driver._settle_entry.assert_not_called()

    def test_a_refused_settlement_is_asked_again_later(self):
        run = self.admitted_run()
        self.driver._settle_entry.side_effect = RuntimeError('account unreachable')
        self.driver._settle_own_ended()  # asked
        self.driver._settle_own_ended()  # refused: asked again later
        self.assertEqual(1, self.driver._settle_entry.call_count)
        self.assertEqual('gone', self.journal.entry(run.launch_token)['state'])
        self.driver._settle_entry.side_effect = None
        clock = [100.0]
        self.driver._clock = lambda: clock[0]
        self.driver._settle_own_ended()
        self.assertEqual(1, self.driver._settle_entry.call_count)  # not yet
        clock[0] += container_launch.STOP_RETRY_MAX_SECONDS
        self.driver._settle_own_ended()
        self.driver._settle_own_ended()
        self.assertEqual(2, self.driver._settle_entry.call_count)
        self.assertEqual('settled', self.journal.entry(run.launch_token)['state'])

    def test_the_live_sweep_retries_failed_stops(self):
        self.driver._containers = mock.Mock()
        self.driver._journal = mock.Mock()
        self.driver._journal.entries.return_value = []
        self.driver._journal.instances.return_value = []
        self.driver._settle_lost(False)
        self.driver._containers.retry_stops.assert_called_once_with()


class StopAtServiceExitTests(unittest.TestCase):
    def setUp(self):
        self.driver = driver(self)
        self.launcher = stop_launcher(self)
        self.driver._journal, self.driver._containers = self.journal, self.launcher
        self.driver._settle_entry = mock.Mock(return_value={'state': 'settled'})
        self.driver._remove_workspace = mock.Mock()

    def started(self, bridge_run_id):
        token = write_intent(self.launcher)
        self.journal.admitted(token, bridge_run_id, deadline_ms())
        self.launcher.start(bridge_run_id, ('python3', '-c', 'pass'), workspace=self.workspace, run_tag='0' * 32,
                            deadline_ns=time.monotonic_ns() + 30_000_000_000)
        return token

    def test_at_its_own_stop_the_service_stops_what_is_left_and_settles_what_is_gone(self):
        gone = self.started(BRIDGE)
        self.journal.stopping(gone, 'run_ended')
        self.journal.stopped(gone, True, 'the container no longer exists')
        held = self.started(OTHER_BRIDGE)
        held_name = self.journal.entry(held)['container_name']
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.assertFalse(self.launcher.stop(container_launch.StartedContainer(
            held, held_name, self.journal.entry(held)['container_id'], self.journal.entry(held)['cgroup_path']),
            'work_silence'))
        self.assertEqual('stop_failed', self.journal.entry(held)['state'])
        self.podman.stop_does_nothing = self.podman.rm_fails = False
        lost = self.driver._settle_lost(True)
        # The one not gone is stopped once more, at once, keeping its first reason, then settled.
        self.assertNotIn(held_name, self.podman.containers)
        for token in (gone, held):
            self.assertEqual('settled', self.journal.entry(token)['state'])
        self.assertEqual('work_silence', self.journal.entry(held)['stop_reason'])
        self.assertEqual(2, self.driver._settle_entry.call_count)
        self.assertEqual('released', lost[HANDLE][1])

    def test_a_container_that_will_not_go_at_the_services_stop_stays_held(self):
        held = self.started(BRIDGE)
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        lost = self.driver._settle_lost(True)
        entry = self.journal.entry(held)
        self.assertEqual(('stop_failed', 'service_close'), (entry['state'], entry['stop_reason']))
        self.driver._settle_entry.assert_not_called()
        self.assertEqual({HANDLE: ('codex-host-v1', 'held')}, lost)

    def test_another_instances_containers_are_left_to_recovery(self):
        token = self.started(BRIDGE)
        later = LaunchJournal(self.journal.directory)
        self.addCleanup(later.release)
        self.journal.release()
        self.driver._journal = later
        self.driver._settle_lost(True)
        self.assertEqual('started', later.entry(token)['state'])
        self.assertEqual([], self.podman.commands('stop'))
        self.driver._settle_entry.assert_not_called()


# --------------------------------------------------------------------------------------------
# C3-6 The detached watchdog, retargeted to the container
# --------------------------------------------------------------------------------------------

class FakeContainerWatch:
    def __init__(self):
        self.parent = True
        self.stops = 0

    def parent_alive(self):
        return self.parent

    def stop(self):
        self.stops += 1
        return cr.StopResult('gone', 'the container no longer exists')


class WatchdogTests(unittest.TestCase):
    def child(self, seconds='30'):
        process = subprocess.Popen(['/bin/sleep', seconds], start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        tree = process_tree.ProcessTree(process.pid, None)
        self.assertTrue(wait_until(lambda: tree.identify(process_tree.process_table()), 5))
        return process, tree.root_birth

    def watch(self, process, birth, container, deadline=60.0):
        thread = threading.Thread(target=process_tree.watch, daemon=True, kwargs=dict(
            root=process.pid, birth=birth, deadline=time.monotonic() + deadline, announce=lambda: None,
            container=container))
        thread.start()
        return thread

    def test_with_its_service_gone_the_watchdog_stops_the_container(self):
        process, birth = self.child()
        container = FakeContainerWatch()
        thread = self.watch(process, birth, container)
        time.sleep(0.6)
        self.assertEqual(0, container.stops)  # the service lives: it stops its own runs
        container.parent = False  # the service died
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, container.stops)
        self.assertIsNotNone(process.wait(5))  # the local attach is killed too

    def test_with_its_service_alive_the_watchdog_leaves_the_container_to_it(self):
        process, birth = self.child('0.3')
        container = FakeContainerWatch()
        thread = self.watch(process, birth, container)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(0, container.stops)
        process, birth = self.child()
        thread = self.watch(process, birth, container, deadline=0.5)  # its deadline, service alive
        thread.join(10)
        self.assertEqual(0, container.stops)
        self.assertIsNotNone(process.wait(5))

    def test_the_launcher_gives_the_watchdog_the_container_and_podman_environment(self):
        launcher = stop_launcher(self, grace_seconds=3)
        runner = cr.SubprocessPodman('/opt/example/podman', global_args=('--url', 'unix:///run/x.sock'))
        real = container_launch.ContainerLauncher(cr.ContainerRuntime(CONTAINER_PROJECT, runner=runner),
                                                  launcher.journal, launcher.config)
        started = container_launch.StartedContainer('lnc_' + '1' * 32, 'cocolo-run-' + '2' * 32, '3' * 64,
                                                    '/machine.slice/libpod-3.scope')
        with mock.patch.dict(os.environ, {'HOME': '/var/empty/cocolo-home', 'XDG_RUNTIME_DIR': '/run/user/501'}):
            config = real.watchdog_config(started)
        argument = json.loads(config['argument'])
        self.assertEqual({'project': CONTAINER_PROJECT, 'prefix': cr.DEFAULT_NAME_PREFIX,
                          'podman': '/opt/example/podman', 'global': ['--url', 'unix:///run/x.sock'],
                          'name': started.name, 'grace': 3, 'cgroup': started.cgroup_path, 'parent': os.getpid()},
                         argument)
        self.assertEqual('/var/empty/cocolo-home', config['environment']['HOME'])
        self.assertEqual('/run/user/501', config['environment']['XDG_RUNTIME_DIR'])
        # Over the fake podman there is no client to hand over: the watchdog guards only the attach.
        self.assertIsNone(launcher.watchdog_config(started))

    def test_the_watchdog_stops_through_container_runtime_stop(self):
        config = {'project': CONTAINER_PROJECT, 'prefix': cr.DEFAULT_NAME_PREFIX, 'podman': '/opt/example/podman',
                  'global': [], 'name': 'cocolo-run-' + '2' * 32, 'grace': 2, 'cgroup': None,
                  'parent': os.getppid()}  # in the watchdog, the service is its parent
        watcher = process_tree.container_watcher(json.dumps(config))
        self.assertTrue(watcher.parent_alive())
        self.assertEqual('container_runtime.py', os.path.basename(sys.modules[type(watcher.runtime).__module__]
                                                                  .__file__))
        host = FakeHostFiles()
        podman = TimeoutPodman(host)
        podman.add_container(config['name'], {cr.PROJECT_LABEL: CONTAINER_PROJECT})
        watcher.runtime.runner, watcher.runtime.host_files = podman, host
        result = watcher.stop()
        self.assertEqual('gone', result.outcome)
        self.assertEqual([('stop', '--time=2', config['name'])], podman.commands('stop'))
        for bad in ({**config, 'name': '../x'}, {**config, 'podman': 'podman'}, {**config, 'grace': -1},
                    {k: v for k, v in config.items() if k != 'parent'}):
            with self.assertRaises(ValueError):
                process_tree.container_watcher(json.dumps(bad))


# --------------------------------------------------------------------------------------------
# Real podman
# --------------------------------------------------------------------------------------------

# A main that ignores TERM and a setsid grandchild that ignores TERM, HUP and INT; nothing on stdout.
IGNORER = r"""
import os, signal, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(number, signal.SIG_IGN)
        time.sleep(3600)
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(3600)
"""

WATCHED_SERVICE = textwrap.dedent('''
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from codex_agents import bridge, container_launch, container_runtime as cr
    from codex_agents.launch_journal import LaunchJournal
    from codex_agents.owned_execution import OwnedExecutionKey, OwnedExecutionRegistry
    journal = LaunchJournal(Path(sys.argv[2]))
    runtime = cr.ContainerRuntime(sys.argv[3], name_prefix="cocolo-test-", runner=cr.SubprocessPodman(sys.argv[4]))
    config = container_launch.ContainerLaunchConfig(image=sys.argv[5], seccomp_profile=sys.argv[6],
                                                    name_prefix="cocolo-test-", grace_seconds=1)
    launcher = container_launch.ContainerLauncher(runtime, journal, config)
    token = launcher.intent(run_handle="hrn_" + "b" * 48, attempt=1, host_client="codex-host-v1", lease={},
                            identity_digest="e" * 64, claim_request_id="claim-" + "f" * 32,
                            storage_evidence=b"{}", workspace=None)
    journal.admitted(token, "brg_" + "c" * 32, int(time.time() * 1000) + 600000)
    registry = OwnedExecutionRegistry()
    registry.launch_journal, registry.container_launch = journal, launcher
    ticket = registry.issue(OwnedExecutionKey("prj_" + "a" * 32, "b" * 64, "run:" + "c" * 64, "brg_" + "c" * 32),
                            deadline_ns=time.monotonic_ns() + 600 * 10**9)
    print(json.dumps(journal.entry(token)["container_name"]), flush=True)
    bridge.run_child(["sleep", "600"], environment={"PATH": os.defpath}, cwd=Path(sys.argv[7]), deadline_seconds=600,
                     on_frame=lambda frame: None, _execution=ticket)
''')


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealContainerStopTests(unittest.TestCase):
    """Real containers in the existing podman machine; names and the project label start cocolo-test-."""

    GRACE = 2

    @classmethod
    def setUpClass(cls):
        host = cr.default_host_files(cr.SubprocessPodman(REAL_PODMAN))
        profile = cr.read_default_seccomp_profile(host)
        if isinstance(profile, cr.Refusal):
            raise unittest.SkipTest("the podman host's default seccomp profile is unreadable: " + profile.message)
        cls.folder = os.path.realpath(tempfile.mkdtemp(prefix='cocolo-test-', dir=SHORT_TMP))
        cls.seccomp = os.path.join(cls.folder, 'seccomp.json')
        Path(cls.seccomp).write_text(json.dumps(cr.derive_seccomp_profile(profile)))

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(OSError):
            os.unlink(cls.seccomp)
            os.rmdir(cls.folder)

    def setUp(self):
        self.project = 'cocolo-test-' + secrets.token_hex(4)
        self.addCleanup(self.remove_test_containers)
        self.workspace = private_folder(self)
        self.directory = Path(private_folder(self)) / 'launches'

    def podman(self, *args, timeout=60):
        return subprocess.run([REAL_PODMAN, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def remove_test_containers(self):
        """Remove exactly this test's containers: those carrying its own cocolo-test- project label."""
        listing = self.podman('ps', '--all', f'--filter=label={cr.PROJECT_LABEL}={self.project}', '--format=json')
        for row in json.loads(listing.stdout or '[]'):
            name = row['Names'][0]
            if name.startswith('cocolo-test-') and row['Labels'].get(cr.PROJECT_LABEL) == self.project:
                self.podman('rm', '--force', '--time=0', name)

    def exists(self, name):
        return self.podman('container', 'exists', name).returncode == 0

    def supervised_run(self, command):
        runtime = cr.ContainerRuntime(self.project, name_prefix='cocolo-test-', runner=cr.SubprocessPodman(REAL_PODMAN))
        self.journal = LaunchJournal(self.directory)
        self.addCleanup(self.journal.release)
        config = container_launch.ContainerLaunchConfig(image=REAL_IMAGE, seccomp_profile=self.seccomp,
                                                        grace_seconds=self.GRACE, name_prefix='cocolo-test-')
        launcher = container_launch.ContainerLauncher(runtime, self.journal, config)
        self.addCleanup(launcher.close)
        self.token = write_intent(launcher)
        self.journal.admitted(self.token, BRIDGE, deadline_ms())
        registry = OwnedExecutionRegistry()
        registry.launch_journal, registry.container_launch = self.journal, launcher
        ticket = registry.issue(OwnedExecutionKey(PROJECT, 'b' * 64, 'run:' + 'c' * 64, BRIDGE),
                                deadline_ns=time.monotonic_ns() + 120_000_000_000, run_handle=HANDLE)
        return BridgeRun(self, registry, ticket, command=command, deadline=120).start()

    def stop_ignoring_term(self, reason):
        run = self.supervised_run(('python3', '-c', IGNORER))
        name = self.journal.entry(self.token)['container_name']
        time.sleep(1.0)  # main ignores TERM; its setsid grandchild ignores TERM, HUP and INT
        self.assertEqual('running', self.podman('inspect', name, '--format', '{{.State.Status}}').stdout.strip())
        asked = time.monotonic()
        run.supervisor.request_stop(reason)
        self.assertTrue(wait_until(lambda: self.journal.entry(self.token)['state'] == 'gone',
                                   self.GRACE + 15), self.journal.entry(self.token))
        seconds = time.monotonic() - asked
        outcome = run.join()
        entry = self.journal.entry(self.token)
        self.assertEqual(('gone', reason), (entry['state'], entry['stop_reason']))
        self.assertFalse(self.exists(name))
        self.assertTrue(run.supervisor.snapshot()['cleanup_confirmed'])
        self.assertGreaterEqual(seconds, self.GRACE - 0.5)  # TERM was ignored: the grace ran out first
        self.assertLess(seconds, self.GRACE + 2)
        return outcome, seconds

    def test_real_deadline_stop_of_a_term_ignoring_main_with_a_setsid_grandchild(self):
        outcome, seconds = self.stop_ignoring_term('absolute_deadline')
        self.assertEqual('timed_out', outcome['abort'])
        print(f'\n[C3 real] deadline stop gone {seconds:.2f} s after the request (grace {self.GRACE} s)',
              file=sys.stderr)

    def test_real_cancel_of_a_term_ignoring_main_with_a_setsid_grandchild(self):
        outcome, seconds = self.stop_ignoring_term('cancel_requested')
        self.assertEqual('cancelled', outcome['abort'])
        print(f'\n[C3 real] cancel gone {seconds:.2f} s after the request (grace {self.GRACE} s)', file=sys.stderr)

    def test_real_watchdog_stops_the_container_once_its_service_is_killed(self):
        service = subprocess.Popen(
            [sys.executable, '-B', '-c', WATCHED_SERVICE, str(ORCHESTRATION_ROOT), str(self.directory), self.project,
             REAL_PODMAN, REAL_IMAGE, self.seccomp, self.workspace],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            line = service.stdout.readline().decode()
            self.assertTrue(line, service.stderr.read(4096).decode() if not line else '')
            name = json.loads(line)
            self.assertTrue(wait_until(lambda: self.podman('inspect', name, '--format', '{{.State.Status}}')
                                       .stdout.strip() == 'running', 60))
            time.sleep(1.0)  # the watchdog has acknowledged and watches
        finally:
            os.killpg(service.pid, signal.SIGKILL)  # only the service: the attach and watchdog have their own
            service.wait(10)
            service.stdout.close()
            service.stderr.close()
        killed = time.monotonic()
        self.assertTrue(wait_until(lambda: not self.exists(name), 20), 'the watchdog did not stop the container')
        seconds = time.monotonic() - killed
        print(f'\n[C3 real] watchdog removed the container {seconds:.2f} s after its service was killed',
              file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
