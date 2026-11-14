"""A project service that stops or crashes while its run holds account capacity.

The next service of the project settles that run by itself as soon as kernel
evidence shows the child, its process group and session, and its watchdog are
gone: the run ends abandoned with an unknown outcome and stops counting against
the machine and account limits, exactly once. While anything of the child may
still run, or when the evidence cannot be read, the capacity stays held and only
the timed rule (an hour past the hard deadline on both clocks) frees it.

The crash tests SIGKILL a real project host process (`tests/crash_host_fixture.py`);
the stop tests stop an in-process host the way SIGTERM does.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPOSITORY_ROOT
from tests.test_bridge import PARENT_GRANT, null_filled, result
from tests.test_project_driver import DriverHarness

from codex_agents import learning_store, project_driver
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.active_run_capacity import CRASHED_CLAIM_GRACE_MS, ActiveRunLimits, crashed_claim
from codex_agents.client_runtime import runtime_hint_path

FIXTURE = Path(__file__).resolve().with_name('crash_host_fixture.py')
ONE_SLOT = ActiveRunLimits(1, 1)


class LostRunHarness(DriverHarness):
    """One slot on the machine, so a run still counted turns the next one away."""

    def setUp(self):
        patcher = mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ONE_SLOT)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def counted(self, *, later_ms=0):
        """Claims the account's limits count now (or `later_ms` from now, on both clocks)."""
        crashed, parameters = crashed_claim('a', self.ledger._clock_ms() + later_ms,
                                            time.monotonic_ns() + later_ms * 1_000_000)
        return self.ledger._connection.execute(
            'SELECT COUNT(*) FROM active_run_capacity a WHERE a.released_at_ms IS NULL AND NOT ' + crashed,
            parameters).fetchone()[0]

    def claimed_bindings(self):
        """(binding state, abandoned reason, released) of every claimed run, oldest first."""
        return [tuple(row) for row in self.ledger._connection.execute(
            'SELECT h.binding_state,h.abandoned_reason,a.released_at_ms IS NOT NULL FROM active_run_capacity a '
            'JOIN host_run_bindings h ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest '
            'ORDER BY a.claimed_at_ms')]

    def launches(self):
        return [i for i in self.invocations('codex') if 'exec' in i['argv']]

    def journal(self):
        return self.paths.root / 'projects' / self.project_id / 'launches'

    def journal_states(self):
        states = []
        for path in sorted(self.journal().glob('brg_*.json')):
            try:
                states.append(json.loads(path.read_text())['state'])
            except (OSError, ValueError, KeyError):
                states.append(None)
        return states

    def next_run(self, orchestration, name):
        """A fresh run of the same orchestration; it needs the one slot."""
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        task = self.write(name + '-task.json', {**self.task(), 'id': 'task-' + name})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': name, 'task_id': 'task-' + name}
        context = ('--task', task, '--assignment', self.write(name + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(name + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled({**result(), 'assignment_id': name})))
        return self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])

    def assert_lost_status(self, orchestration, run_handle, capacity_state):
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle,
                          expected=2)
        self.assertEqual((False, 'run_lost', 'abandoned', 'unknown', capacity_state),
                         (status['ready'], status['reason_code'], status['terminal_state'], status['outcome'],
                          status['capacity_state']))
        self.assertNotIn('health', status)


class LostRunAfterCrashTests(LostRunHarness):
    """The project host is a real process, killed with SIGKILL while its child runs."""

    def setUp(self):
        self.host_process = None
        with mock.patch.object(DriverHarness, 'start_project_host',
                               lambda harness, project, stop=None: harness.start_host_process()):
            super().setUp()

    def start_host_process(self):
        base = Path(self.temporary.name)
        config = {'base': str(base), 'state': str(self.state.root), 'project': str(self.project),
                  'bundle': str(self.bundle)}
        log = open(base / 'crash-host.log', 'ab')
        self.addCleanup(log.close)
        self.host_process = subprocess.Popen([sys.executable, '-B', str(FIXTURE), json.dumps(config)],
                                             cwd=REPOSITORY_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        process = self.host_process
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait(10)))
        self.wait_for(lambda: runtime_hint_path(self.paths, self.project).exists() or process.poll() is not None,
                      seconds=30)
        self.assertIsNone(process.poll(), (base / 'crash-host.log').read_text(errors='replace'))

    def crash_while_running(self, sleep_seconds):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': sleep_seconds},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(self.launches, seconds=30)
        # Mid-run: the watchdog has acknowledged the child's tree. (A crash in the moment
        # before that acknowledgement leaves the run to the timed rule.)
        self.wait_for(lambda: self.journal_states() == ['watched'], seconds=30)
        self.assertEqual(1, self.counted())
        self.host_process.kill()  # SIGKILL: no cleanup of any kind runs in the host.
        self.host_process.wait(10)
        dispatch.communicate(timeout=30)
        DriverHarness.start_project_host(self, self.project)
        return orchestration, run_handle, self.launches()[0]

    def test_a_crashed_hosts_finished_child_is_settled_by_the_next_host_within_seconds(self):
        orchestration, run_handle, child = self.crash_while_running(2)
        # The child writes into its dead host's pipe and ends; its watchdog follows.
        started = time.monotonic()
        self.wait_for(lambda: self.counted() == 0, seconds=30)
        self.assertLess(time.monotonic() - started, 25)
        self.assert_process_gone(child)
        self.assertEqual([('abandoned', 'service_lost', 0)], self.claimed_bindings())
        self.assert_lost_status(orchestration, run_handle, 'released')
        # The slot is free: the next child claims it, and the lost one never started again.
        self.assertEqual('result', self.next_run(orchestration, 'next1')['bridge_report']['outcome'])
        self.assertEqual(2, len(self.launches()))
        self.assertEqual(child['pid'], self.launches()[0]['pid'])

    def test_a_crashed_hosts_child_that_still_runs_keeps_its_capacity_until_it_ends(self):
        orchestration, run_handle, child = self.crash_while_running(60)
        # The child sleeps on in its own session; the new host must not settle it.
        end = time.monotonic() + 6
        while time.monotonic() < end:
            self.assertEqual(1, self.counted())
            time.sleep(0.25)
        self.assertEqual([('pending', None, 0)], self.claimed_bindings())
        os.killpg(child['pgid'], 0)  # Still there, untouched.
        self.assert_lost_status(orchestration, run_handle, 'held')
        os.killpg(child['pgid'], signal.SIGKILL)
        self.wait_for(lambda: self.counted() == 0, seconds=30)
        self.assertEqual([('abandoned', 'service_lost', 0)], self.claimed_bindings())
        self.assertEqual('result', self.next_run(orchestration, 'next2')['bridge_report']['outcome'])

    def test_missing_or_unreadable_launch_evidence_falls_back_to_the_timed_rule(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(self.launches, seconds=30)
        self.wait_for(lambda: self.journal_states() == ['watched'], seconds=30)
        entries = [path for path in self.journal().iterdir() if path.name.startswith('brg_')]
        self.assertEqual(1, len(entries))
        self.host_process.kill()
        self.host_process.wait(10)
        dispatch.communicate(timeout=30)
        entries[0].write_text(entries[0].read_text()[:40])  # Damaged: no longer evidence of anything.
        child = self.launches()[0]
        self.wait_for(lambda: self.gone(child), seconds=30)
        DriverHarness.start_project_host(self, self.project)
        time.sleep(5)  # Several sweeps of the new host.
        self.assertEqual(1, self.counted())
        self.assertEqual([('pending', None, 0)], self.claimed_bindings())
        self.assertTrue(entries[0].exists())  # Left as it was found.
        # Only the timed rule frees it: an hour past its hard deadline on both clocks.
        deadline = self.ledger._connection.execute('SELECT deadline_at_ms FROM active_run_capacity').fetchone()[0]
        self.assertEqual(1, self.counted(later_ms=deadline - self.ledger._clock_ms()))
        self.assertEqual(0, self.counted(later_ms=deadline - self.ledger._clock_ms() + CRASHED_CLAIM_GRACE_MS + 1000))

    @staticmethod
    def gone(child):
        try:
            os.killpg(child['pgid'], 0)
        except ProcessLookupError:
            return True
        return False


class LostRunAfterStopTests(LostRunHarness):
    """The project host stops (as on SIGTERM) with a run in each state, then a new one starts."""

    def setUp(self):
        self._host_stop = threading.Event()
        original = DriverHarness.start_project_host

        def first(harness, project, stop=None):
            return original(harness, project, stop or self._host_stop)

        with mock.patch.object(DriverHarness, 'start_project_host', first):
            super().setUp()

    def stop_services(self):
        self._host_stop.set()
        super().stop_services()

    def restart_host(self):
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(30)
        self.assertFalse(host.is_alive())
        self.threads.remove(host)
        self._host_stop = threading.Event()
        DriverHarness.start_project_host(self, self.project, self._host_stop)

    def stopped_in(self, state, frames, *, settled=True, reached=None):
        """Hold the run in `state`, stop its host, start the next one; the slot must come back."""
        original, seen = project_driver.RunDriver._advance, []

        def held(driver, run):
            if run.state == state:
                seen.append(state)
                return None
            return original(driver, run)

        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', frames)
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.object(project_driver.RunDriver, '_advance', held):
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(lambda: seen and (reached is None or reached()), seconds=30)
            self.restart_host()
        dispatch.communicate(timeout=30)
        self.wait_for(lambda: self.counted() == 0, seconds=30)
        if settled:
            self.assertIn(self.claimed_bindings()[0][::2], {('abandoned', 0), ('pending', 1)})
        self.assertEqual('result', self.next_run(orchestration, 'after')['bridge_report']['outcome'])
        return orchestration, run_handle

    def test_a_run_preparing_when_its_host_stops_holds_nothing_afterwards(self):
        self.stopped_in('preparing', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}], settled=False)
        self.assertEqual(1, len(self.launches()))

    def test_a_claimed_run_not_yet_launched_when_its_host_stops_is_settled(self):
        orchestration, run_handle = self.stopped_in('consuming', [
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        self.assertEqual(1, len(self.launches()))  # Only the next run's child; the claimed one never started.
        self.assertEqual(('abandoned', 'service_lost', 0), self.claimed_bindings()[0])
        self.assert_lost_status(orchestration, run_handle, 'released')

    def test_a_running_child_when_its_host_stops_is_settled(self):
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        self.assertEqual(2, len(self.launches()))
        self.assert_process_gone(self.launches()[0])
        self.assertEqual(('abandoned', 'service_lost', 0), self.claimed_bindings()[0])

    def test_a_finished_child_not_yet_reported_when_its_host_stops_is_settled(self):
        self.stopped_in('reporting', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        self.assertEqual(2, len(self.launches()))

    def test_a_run_whose_terminal_was_sent_when_its_host_stops_is_settled(self):
        self.stopped_in('terminal', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])

    def test_a_run_releasing_when_its_host_stops_is_released_once(self):
        self.stopped_in('releasing', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        # Released by its own host or settled by the next: either way it no longer counts, once.
        first = self.claimed_bindings()[0]
        self.assertIn(first, {('pending', None, 1), ('abandoned', 'service_lost', 0)})

    def test_a_lost_reply_and_repeated_restarts_settle_the_run_exactly_once(self):
        replies, original = [], AccountClient.settle_lost_run

        def lossy(client, *args, **options):
            reply = original(client, *args, **options)
            replies.append(reply)
            if len(replies) == 1:
                raise AccountError('account_unavailable')  # Committed, but the reply never arrived.
            return reply

        with mock.patch.object(AccountClient, 'settle_lost_run', lossy):
            context, route_path, orchestration, run_handle = self.open_run()
            self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
                                  {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
            (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
            dispatch = self.separate_dispatch(context, route_path, run_handle)
            self.wait_for(self.launches, seconds=30)
            self.restart_host()
            dispatch.communicate(timeout=30)
            self.wait_for(lambda: len(replies) >= 2, seconds=30)
            for _ in range(2):
                self.restart_host()
        self.assertEqual([False, True], [reply['known'] for reply in replies[:2]])
        self.assertEqual(1, len({reply['receipt_digest'] for reply in replies}))
        self.assertEqual(2, len(replies))  # Settled runs are never sent again.
        self.assertEqual(0, self.counted())
        self.assertEqual([('abandoned', 'service_lost', 0)], self.claimed_bindings())
        self.assert_lost_status(orchestration, run_handle, 'released')
        self.assertEqual('result', self.next_run(orchestration, 'again')['bridge_report']['outcome'])
        self.assertEqual(2, len(self.launches()))

    def test_a_released_run_leaves_no_launch_evidence_behind(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.assertEqual('result', self.cli('dispatch', *context, '--route', route_path,
                                            '--run-handle', run_handle)['bridge_report']['outcome'])
        self.wait_for(lambda: self.claimed_bindings()[0][2] == 1, seconds=30)
        self.wait_for(lambda: not [p for p in self.journal().iterdir() if p.name.startswith('brg_')], seconds=10)
        self.restart_host()
        self.assertEqual([('pending', None, 1)], self.claimed_bindings())


if __name__ == '__main__':
    unittest.main()
