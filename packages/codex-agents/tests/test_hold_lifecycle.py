"""O0b B0-6: every hold a child run takes ends by itself (PLAN B0-1 to B0-6; DESIGN-0.4.0-B0-holds.md, B0-6
as revised in revision 3).

Integration stage: the in-process collector, account and project services (the test_result_lifetime fixture).
Each scenario records a baseline, shows it took the hold it targets, stops all client action at the failure
point, lets only production timers and sweeps run to a stated deadline, then checks every count before
teardown. Closing the orchestration stays the primary's step: scenarios check "closable" separately.

This module is written before any B0 code: each failure exit here fails on 0.8.37, by design, and is
listed with the B0 step that makes it pass.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.support import SHORT_TMP
from tests.test_bridge import null_filled, result
from tests.test_container_launch_journal import TimeoutPodman
from tests.test_container_runtime import FakeClock, FakeHostFiles
from tests.test_lost_run_settlement import LostRunAfterStopTests
from tests.test_result_lifetime import ResultLifetimeTests

from codex_agents import codex_preparation, container_launch
from codex_agents import container_runtime as cr


class HoldLifecycleTests(ResultLifetimeTests):
    def held(self):
        """Every hold this harness watches, by name (revision 3's list grows with each B0 step)."""
        ledger = self.collector_ledger._connection
        now = self.collector_ledger_now_ms()
        return {
            'live_results': self.cli('learning-status')['project_capacity']['results']['live'],
            'account_slots': ledger.execute(
                'SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0],
            # B0-1: a pending binding the cap and the drain still count.
            'pending_bindings': ledger.execute(
                "SELECT COUNT(*) FROM host_run_bindings WHERE binding_state='pending' AND expires_at_ms>?",
                (now,)).fetchone()[0],
        }

    def collector_ledger_now_ms(self):
        import time
        return time.time_ns() // 1_000_000

    def test_B0_6_baseline_is_empty(self):
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    def test_B0_1_a_run_refused_before_launch_frees_its_account_binding_by_itself(self):
        """B0-1 (fails on 0.8.37): the binding stays pending for 24 h, held against the cap and every drain.
        Un-skipped by slice B3 (RS-B W5, the B0-6 "never-armed binding" exit): the project withdraws the open."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.assertEqual(1, self.held()['pending_bindings'])  # the hold was taken
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        # No further client action: only the service's own work may free it.
        self.wait_for(lambda: self.held()['pending_bindings'] == 0, seconds=30)
        self.assertEqual({'live_results': 0, 'account_slots': 0, 'pending_bindings': 0}, self.held())

    def test_B0_narrowed_status_shows_a_held_binding(self):
        """O0b B0 (narrowed): learning-status shows each binding still held, so the hold is never invisible."""
        before = self.cli('learning-status')['account_capacity']['pending_bindings']
        self.open_run()
        after = self.cli('learning-status')['account_capacity']['pending_bindings']
        self.assertEqual(before['live'] + 1, after['live'])
        self.assertEqual(4096, after['limit'])

    def test_RS_A_F14_status_shows_the_account_holds_by_kind(self):
        """O0b RS-A round 1 F14 (RS-A-6): learning-status shows this project's account holds by kind, with
        one plain line for the owner."""
        before = self.cli('learning-status')['account_holds']
        self.open_run()
        after = self.cli('learning-status')['account_holds']
        self.assertTrue(after['reported'])
        self.assertEqual(before['kinds']['binding']['held'] + 1, after['kinds']['binding']['held'])
        self.assertEqual({'run_slot', 'binding', 'probe'}, set(after['kinds']))
        self.assertTrue(after['summary'].startswith('Account holds: '), after['summary'])
        self.assertIn('binding', after['summary'])

    def test_B0_6_a_completed_run_frees_every_hold(self):
        """Control: today's successful lifecycle already frees what it took (once recorded)."""
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        self.record(context, route_path, run_handle,
                    self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle))
        self.wait_for(lambda: self.held()['account_slots'] == 0 and self.held()['live_results'] <= 1, seconds=30)


class LostServiceHoldTests(LostRunAfterStopTests):
    """Holds left by a service that stops mid-run (the lost-run fixture restarts it)."""

    def reservation_terminal(self, run_handle):
        """The lost run's result reservation: (state, has a terminal), read without the service."""
        import sqlite3
        (path,) = list(self.state.root.rglob('history.sqlite3'))
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            row = connection.execute('SELECT state, terminal_record_id IS NOT NULL FROM result_reservations '
                                     'WHERE run_handle=?', (run_handle,)).fetchone()
        finally:
            connection.close()
        return None if row is None else (row[0], bool(row[1]))

    @unittest.skip("gates the run supervisor: the owner narrowed B0 on 2026-11-20 (stranded reservations)")
    def test_B0_2_a_reservation_left_by_a_service_that_stopped_gets_its_lost_terminal(self):
        """B0-2 (fails on 0.8.37): the next service settles the lost run at the account, but its result
        reservation keeps no terminal, so the orchestration can never close and the row is never purged."""
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        self.assertIsNotNone(self.reservation_terminal(run_handle))  # the hold was taken
        self.wait_for(lambda: self.reservation_terminal(run_handle)[1], seconds=30)


class LostSlotReleaseTests(LostRunAfterStopTests):
    """O0b RS-A-2: a lost settlement releases the run's slot itself, written once with its kind."""

    def test_RS_A_2_a_lost_run_s_slot_is_released_as_lost_by_its_settlement(self):
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        rows = [dict(row) for row in self.collector_ledger._connection.execute(
            # The kind is in the register row, written with the release (RS-A implementation decisions).
            "SELECT a.released_at_ms, r.release_kind FROM active_run_capacity a JOIN host_run_bindings h "
            "ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest "
            "JOIN run_holds r ON r.kind='run_slot' AND r.project_id=a.project_id AND r.identity=a.handle_digest "
            "WHERE h.binding_state='abandoned' AND h.abandoned_reason='service_lost'")]
        self.assertEqual(1, len(rows), rows)
        self.assertIsNotNone(rows[0]['released_at_ms'])  # written, not re-derived from the binding
        self.assertEqual('lost', rows[0]['release_kind'])


class ContainerRunHoldTests(HoldLifecycleTests):
    """C3 (PLAN-0.4.0-supervisor-slices.md): a live service's own ended run in container mode.

    The project service launches its runs in containers (C2's switch, on for these tests) over a
    fake podman; the run's command runs locally in the container's place, as its attached output.
    """

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.seccomp = os.path.join(os.path.realpath(folder.name), 'seccomp.json')
        Path(self.seccomp).write_text('{}')
        self.podman_host = FakeHostFiles()
        self.podman = TimeoutPodman(self.podman_host)
        clock = FakeClock()  # the primitive's own polling only; back-off and sweeps run on real time

        def launcher_for(project_id, journal, settings=None):
            runtime = cr.ContainerRuntime(project_id, runner=self.podman, host_files=self.podman_host,
                                          monotonic=clock.monotonic, sleep=clock.sleep)
            config = container_launch.ContainerLaunchConfig(image='python:3.12-slim', seccomp_profile=self.seccomp,
                                                            grace_seconds=1)
            return container_launch.ContainerLauncher(runtime, journal, config, attach=self.attached)

        patcher = mock.patch.object(container_launch, 'launcher_for', launcher_for)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def attached(self, started):
        """The command the container was created with, run locally in its place."""
        args = self.podman.containers[started.name].args
        image = next(index for index, arg in enumerate(args) if index and not arg.startswith('-'))
        return list(args[image + 1:])

    def running_run(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'quiet'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: any(c.status == 'running' for c in self.podman.containers.values()), seconds=30)
        self.assertEqual(1, self.held()['account_slots'])  # the hold was taken
        return context, route_path, orchestration, run_handle, process

    def container_holds(self):
        return self.cli('learning-status')['container_holds']

    def test_C3_a_cancelled_run_frees_its_slot_once_its_container_is_gone(self):
        context, route_path, orchestration, run_handle, process = self.running_run()
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        process.communicate(timeout=60)
        self.assertEqual([], [name for name, c in self.podman.containers.items()])  # stopped and removed
        # Only the live service's own work may free it: no restart.
        self.wait_for(lambda: self.held()['account_slots'] == 0, seconds=30)
        self.assertEqual(0, self.container_holds()['counts']['stop_failed'])

    def test_C3_a_container_that_does_not_stop_keeps_its_slot_until_a_retried_stop_proves_it_gone(self):
        context, route_path, orchestration, run_handle, process = self.running_run()
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        process.communicate(timeout=60)
        self.wait_for(lambda: self.container_holds()['counts']['stop_failed'] == 1, seconds=30)
        status = self.cli('learning-status')
        [failed] = status['container_holds']['stop_failed']
        self.assertEqual('cancel_requested', failed['reason'])
        self.assertTrue(any(failed['container'] in line and 'could not be stopped' in line
                            for line in status['account_health']['attention']), status['account_health'])
        # Held while the container is not proven gone, though its run ended.
        import time
        time.sleep(3)  # several sweeps and retried stops
        self.assertEqual(1, self.held()['account_slots'])
        # The container goes: a retried stop proves it, and the live service releases the slot by itself.
        self.podman.stop_does_nothing = self.podman.rm_fails = False
        self.wait_for(lambda: self.held()['account_slots'] == 0, seconds=60)
        self.assertEqual(0, self.container_holds()['counts']['stop_failed'])


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited ones run in their own modules."""
    suite = unittest.TestSuite()
    for case in (HoldLifecycleTests, LostServiceHoldTests, LostSlotReleaseTests, ContainerRunHoldTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
