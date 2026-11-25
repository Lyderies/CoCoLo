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
import contextlib
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

from tests.learning_support import attest_provider
from tests.support import REPOSITORY_ROOT
from tests.test_bridge import CLEAN, PARENT_GRANT, git, null_filled, result
from tests.test_project_driver import DriverHarness

from codex_agents import bridge, learning_store, project_driver, service_transport
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.hosts import OWNER_CLIENT
from codex_agents.learning_contracts import canonical_json, parse_bounded_json
from codex_agents.learning_store import LedgerError
from codex_agents.lost_run_contracts import SettleLostInput
from codex_agents.service_enrollment import (
    account_endpoint_path, enrollment_preamble, proof_directory, read_capability, withdraw_proof,
)
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
        with self.ledger._lock:  # TESTSPEED2-2: never read between the statements of an open account write
            return self.ledger._connection.execute(
                'SELECT COUNT(*) FROM active_run_capacity a WHERE a.released_at_ms IS NULL AND NOT ' + crashed,
                parameters).fetchone()[0]

    def claimed_bindings(self):
        """(binding state, abandoned reason, released) of every claimed run, oldest first.

        Corrected for O0b RS-A-2 (requirement): a lost settlement now releases the slot itself, as `lost`,
        written once, so a settled lost run reads (abandoned, service_lost, 1); before, it stayed unreleased."""
        with self.ledger._lock:  # TESTSPEED2-2: see counted()
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

    def run_status(self, orchestration, run_handle):
        """Run-status once the new host is past run_settling (LRS-4, setup only; bounded 30 s)."""
        replies = []
        self.wait_for(lambda: replies.append(self.cli('learning-run-status', '--orchestration-handle', orchestration,
                                                      '--run-handle', run_handle, expected=2))
                      or replies[-1].get('reason_code') != 'run_settling', seconds=30)
        return replies[-1]

    def lost_capacity(self, orchestration, run_handle):
        """The capacity state run-status reports for this lost run ('held' until the sweep settles it)."""
        return self.run_status(orchestration, run_handle)['capacity_state']

    def assert_lost_status(self, orchestration, run_handle, capacity_state):
        status = self.run_status(orchestration, run_handle)
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
        self.host_process = subprocess.Popen([sys.executable, '-X', 'pycache_prefix=' + self.dispatch_pycache, str(FIXTURE), json.dumps(config)],
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
        self.count_sweeps()
        DriverHarness.start_project_host(self, self.project)
        return orchestration, run_handle, self.launches()[0]

    def count_sweeps(self):
        """S13C-1: the sweeps the next host has completed so far (`self.sweeps`: one entry per `_settle_lost` call that
        returned without raising), so a test that watches a hold over "several sweeps" waits for that many sweeps
        instead of a fixed number of seconds."""
        self.sweeps = []
        original = project_driver.RunDriver._settle_lost
        sweeps = self.sweeps

        def counted(driver, *arguments, **options):
            result = original(driver, *arguments, **options)
            sweeps.append(time.monotonic())  # only a sweep that returned; a raising one is not counted
            return result

        patcher = mock.patch.object(project_driver.RunDriver, '_settle_lost', counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def through_sweeps(self, count):
        """Wait until the next host has completed `count` more sweeps (bounded 60 s)."""
        target = len(self.sweeps) + count
        self.wait_for(lambda: len(self.sweeps) >= target, seconds=60)

    @__import__('test_framework').requirements('S13C-1', 'S13C-3', why_not_scenario='Test-speed requirement: these are existing tests whose seconds, interpreters or waits were changed; the requirement is how fast and how reliably they run, not a new outcome (S13C).')
    def test_a_crashed_hosts_finished_child_is_settled_by_the_next_host_within_seconds(self):
        orchestration, run_handle, child = self.crash_while_running(2)
        # The child writes into its dead host's pipe and ends; its watchdog follows.
        started = time.monotonic()
        # Settled when the account counts it no longer and the host reports it released: the host's
        # sweep hands its result over just after the account settles the claim.
        self.wait_for(lambda: self.counted() == 0 and self.lost_capacity(orchestration, run_handle) == 'released',
                      seconds=30)
        self.assertLess(time.monotonic() - started, 25)
        self.assert_process_gone(child)
        self.assertEqual([('abandoned', 'service_lost', 1)], self.claimed_bindings())
        self.assert_lost_status(orchestration, run_handle, 'released')
        # The slot is free: the next child claims it, and the lost one never started again.
        self.assertEqual('result', self.next_run(orchestration, 'next1')['bridge_report']['outcome'])
        self.assertEqual(2, len(self.launches()))
        self.assertEqual(child['pid'], self.launches()[0]['pid'])

    @__import__('test_framework').requirements('S13C-1', 'S13C-3', why_not_scenario='Test-speed requirement: these are existing tests whose seconds, interpreters or waits were changed; the requirement is how fast and how reliably they run, not a new outcome (S13C).')
    def test_a_crashed_hosts_child_that_still_runs_keeps_its_capacity_until_it_ends(self):
        orchestration, run_handle, child = self.crash_while_running(60)
        # The child sleeps on in its own session; the new host must not settle it.
        # S13C-1: held across as many sweeps as the old 6 s held it across (one a second), each one completed.
        target = len(self.sweeps) + 6
        while len(self.sweeps) < target:
            self.assertEqual(1, self.counted())
            seen = len(self.sweeps)
            self.wait_for(lambda: len(self.sweeps) > seen, seconds=60)
        self.assertEqual([('pending', None, 0)], self.claimed_bindings())
        os.killpg(child['pgid'], 0)  # Still there, untouched.
        self.assert_lost_status(orchestration, run_handle, 'held')
        os.killpg(child['pgid'], signal.SIGKILL)
        self.wait_for(lambda: self.counted() == 0, seconds=30)
        self.assertEqual([('abandoned', 'service_lost', 1)], self.claimed_bindings())
        self.assertEqual('result', self.next_run(orchestration, 'next2')['bridge_report']['outcome'])

    @__import__('test_framework').requirements('S13C-1', 'S13C-3', why_not_scenario='Test-speed requirement: these are existing tests whose seconds, interpreters or waits were changed; the requirement is how fast and how reliably they run, not a new outcome (S13C).')
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
        self.count_sweeps()
        DriverHarness.start_project_host(self, self.project)
        self.through_sweeps(5)  # Several sweeps of the new host (S13C-1: five completed, not five seconds).
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
        except PermissionError:
            return True  # The group number now belongs to another user's processes: ours is gone.
        return False


class LedgerReadHelperTests(LostRunHarness):
    """TESTSPEED2-2: the helpers read the account's tables through the collector thread's own connection, which
    that thread's writes (`BEGIN IMMEDIATE` ... `COMMIT`) also use. A read between two statements of one such write
    sees the half-written state (a run's binding already `abandoned/service_lost`, its slot not yet released),
    so under load a poll saw `counted() == 0` with the slot still unreleased. Reads wait for the write instead."""

    @__import__('test_framework').requirements('TESTSPEED2-2', 'TESTSPEED2-3', why_not_scenario='Test-speed requirement: these are existing tests whose seconds and fixture setup were changed; the requirement is how fast and how cheaply they run, not a new outcome (TESTSPEED2).')
    def test_the_ledger_read_helpers_wait_for_a_write_the_collector_has_open(self):
        for name, read in (('counted', self.counted), ('claimed_bindings', self.claimed_bindings)):
            with self.subTest(name):
                seen = []
                reader = threading.Thread(target=lambda: seen.append(read()), daemon=True)
                with self.ledger._lock:  # what the account's write holds from its first to its last statement
                    reader.start()
                    reader.join(1)
                    self.assertTrue(reader.is_alive(), 'the read did not wait for the open write: ' + repr(seen))
                reader.join(30)
                self.assertFalse(reader.is_alive())
                self.assertEqual(1, len(seen))


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
        self.stop_host()
        self.start_host()

    def stop_host(self):
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(30)
        self.assertFalse(host.is_alive())
        self.threads.remove(host)

    def start_host(self):
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
            self.assertIn(self.claimed_bindings()[0][::2], {('abandoned', 1), ('pending', 1)})
        self.assertEqual('result', self.next_run(orchestration, 'after')['bridge_report']['outcome'])
        return orchestration, run_handle

    def leave_a_refused_claim_unfenced(self, count=1, stop=None):
        """Runs whose claims were refused and whose every fence was refused, as on 0.8.22 (C2), up to
        the moment their service stopped: only the next service can close them. The last is returned."""
        from codex_agents import signature_job_intake
        from codex_agents.account_claim_contracts import ClaimError
        from codex_agents.admission_inputs import AdmissionCaptureError
        original = signature_job_intake.SignatureJobIntake.capture_admission

        def claim_over_limit(intake, project_id, provider, work_limits, anchor_ms=None):
            if anchor_ms is not None:
                raise AdmissionCaptureError("admission_capture_limit")
            return original(intake, project_id, provider, work_limits, anchor_ms)
        with mock.patch.object(signature_job_intake.SignatureJobIntake, 'capture_admission', autospec=True,
                               side_effect=claim_over_limit), \
                mock.patch.object(learning_store.LearningLedger, '_fence_claim',
                                  side_effect=ClaimError('claim_busy')):
            for _ in range(count):
                context, route_path, orchestration, run_handle = self.open_run()
                refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle,
                                   expected=2)
                self.assertEqual('admission_capture_limit', refused['reason_code'], refused)
                stuck = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                 '--reason', 'recovery_unavailable', expected=2)
                self.assertEqual('history_conflict', stuck['reason_code'], stuck)
            (stop or self.stop_host)()
        self.assertEqual(['pending'] * count, self.bindings())
        return orchestration, run_handle

    def bindings(self):
        """Binding states, oldest first, read through a second connection: polled while the
        collector's own thread uses its connection, which only that thread may read."""
        import sqlite3
        connection = sqlite3.connect(f"file:{self.state.root / 'learning.sqlite3'}?mode=ro", uri=True, timeout=30)
        try:
            return [row[0] for row in connection.execute(
                "SELECT binding_state FROM host_run_bindings ORDER BY created_at_ms")]
        finally:
            connection.close()

    @__import__('test_framework').requirements('B2-OUTCOME', 'RS-B-5', why_not_scenario='Real CLI and service boundaries check stored-result recheck refusals, immediate recording or orchestration counts after recovery (B2, decision 119 A).')
    def test_C3_a_refused_claim_an_earlier_host_could_not_fence_is_fenced_by_the_next(self):
        # 0.3.3 requirement C3 (owner): on 0.8.22, a live project's refused claim could
        # not be fenced (C2), so its run kept its lease, and with it one of the project's places for
        # unfinished results, for good; no command settled it. The next project service fences every
        # such lease of an earlier instance by itself: the run's binding ends abandoned, its result
        # place is free again, and the abandonment command says the run is already closed.
        from codex_agents import project_service
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        # The next service has room for one unfinished result only: the next run can start only once
        # the earlier lease no longer holds it.
        with mock.patch.object(project_service, 'MAX_ACTIVE_ATTEMPTS', 1):
            self.start_host()
            self.wait_for(lambda: self.bindings() == ['abandoned'], seconds=30)
            closed = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                              '--reason', 'recovery_unavailable')
            self.assertEqual(('already_closed', 'never_claimed'), (closed['classification'], closed['capacity_state']))
            self.assertEqual('result', self.next_run(orchestration, 'after')['bridge_report']['outcome'])
        # C7: the report counts the closed run as abandoned, not pending; B2 (decision 92 A / 119 A): the
        # service records the next run's result itself, so nothing is pending or active.
        status = self.cli('learning-orchestration-status', '--orchestration-handle', orchestration)
        report = self.cli('learning-orchestration-report', '--orchestration-id', status['orchestration_id'])
        self.assertEqual((1, 0, 0), (report['runs']['abandoned'], report['runs']['pending'],
                                     report['runs']['current_active']), report['runs'])
        # Its usage is not known either: abandoned is never counted as fully attributed.
        self.assertNotEqual('complete', report['usage']['child_attribution'])
        self.assertEqual(1, report['usage']['unknown_runs']['input_tokens'])  # B2 (119 A): the recorded run's usage is known

    def test_C3_the_next_service_never_fences_a_claim_request_its_run_was_not_observed_for(self):
        # The account takes the fence's identity from the run's launch observation and checks the
        # whole original claim request against it: if they differ, nothing is fenced or changed.
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        import sqlite3
        connection = sqlite3.connect(self.state.root / 'learning.sqlite3', timeout=30)
        with connection:
            connection.execute("UPDATE launch_observations SET identity_digest=?", ('e' * 64,))
        connection.close()
        original, answers = AccountClient.fence_claim, []

        def answered(client, fence, **options):
            try:
                reply = original(client, fence, **options)
            except AccountError as error:
                answers.append(error.reason_code)
                raise
            answers.append(reply)
            return reply
        with mock.patch.object(AccountClient, 'fence_claim', autospec=True, side_effect=answered):
            self.start_host()
            self.wait_for(lambda: answers, seconds=30)
        self.assertEqual('claim_denial_invalid', answers[0])
        self.assertEqual(['pending'], self.bindings())
        import sqlite3
        connection = sqlite3.connect(f"file:{self.state.root / 'learning.sqlite3'}?mode=ro", uri=True, timeout=30)
        try:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM records WHERE record_type='claim_denial_settlement'").fetchone()[0])
        finally:
            connection.close()

    def closes_after_restart(self, run_handle, orchestration):
        """The next service closes the run: abandoned, already_closed, and its result place free."""
        self.wait_for(lambda: self.bindings()[0] == 'abandoned', seconds=60)
        # The account's fence comes first and the lease's expiry after: until then the run is still
        # being closed (C3d), and the command says so.
        replies = []
        self.wait_for(lambda: replies.append(self.cli_reply(
            'learning-classify-host-run-abandoned', '--run-handle', run_handle, '--reason', 'recovery_unavailable'))
            or replies[-1].get('ready'), seconds=60)
        self.assertEqual(('already_closed', 'never_claimed'),
                         (replies[-1]['classification'], replies[-1]['capacity_state']))
        self.assertTrue(all(reply.get('ready') or reply.get('reason_code') == 'run_settling' for reply in replies))

    def test_C3a_a_failed_history_request_is_asked_again(self):
        from codex_agents import result_persistence
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        original, failed = result_persistence.ResultPersistenceLane._earlier, {'list': 0, 'settle': 0}

        def once(lane, request):
            # Fixture fix for 0.3.4 R2: the lane also takes the service's 'expire' requests now; only the
            # two kinds under test fail once.
            if request.kind in failed and failed[request.kind] == 0:
                failed[request.kind] += 1
                with lane._lock:
                    request.state, request.reason = 'failed', 'history_unavailable'
                return
            return original(lane, request)
        with mock.patch.object(result_persistence.ResultPersistenceLane, '_earlier', autospec=True, side_effect=once):
            self.start_host()
            self.closes_after_restart(run_handle, orchestration)
        self.assertEqual({'list': 1, 'settle': 1}, failed)

    def test_C3b_every_earlier_lease_is_found_whatever_the_next_services_capacity(self):
        # The review: the listing refused outright when there were more earlier leases than the
        # next service may hold entries, and the sweep stopped. Two leases, a service of one entry.
        from codex_agents import project_service
        from codex_agents.result_contracts import ResultCapacity
        self.leave_a_refused_claim_unfenced(count=2)
        small = project_service._result_capacity()
        with mock.patch.object(project_service, '_result_capacity', return_value=ResultCapacity(
                1, small.max_reserved_bytes, small.reservation_ms, small.retention_ms)):
            self.start_host()
            self.wait_for(lambda: self.bindings() == ['abandoned', 'abandoned'], seconds=60)

    def test_C3e_a_lease_the_sweep_cannot_read_never_stops_the_others(self):
        # A live project on 0.8.23: one earlier lease whose orchestration could not be read failed the
        # whole listing, and no run was closed. Two runs in two orchestrations; one record removed.
        import sqlite3
        self.leave_a_refused_claim_unfenced(count=2)
        history = self.state.root / 'projects' / self.project_id / 'history.sqlite3'
        connection = sqlite3.connect(history, timeout=30)
        with connection:
            first = connection.execute("SELECT orchestration_handle FROM project_orchestrations "
                                       "ORDER BY rowid LIMIT 1").fetchone()[0]
            connection.execute("DELETE FROM project_orchestration_checkpoints WHERE orchestration_handle=?", (first,))
            connection.execute("DELETE FROM project_orchestrations WHERE orchestration_handle=?", (first,))
            # B6 (RS-B-B6-1): its register row goes too (a 0.8.23 store had none; one left held is refused at open).
            connection.execute("DELETE FROM project_holds WHERE kind='orchestration' AND identity=?", (first,))
        connection.close()
        self.start_host()
        self.wait_for(lambda: self.bindings() == ['pending', 'abandoned'], seconds=60)

    def test_C3f_a_lease_an_earlier_version_left_is_closed_by_the_next_version(self):
        # The owner's read-only check: the live project's lease was readable and the only
        # pending one, yet it stayed open. It named 0.8.22's runtime; the account takes requests only
        # under its own, so every fence was refused. Here the first services run as an earlier
        # version (another runtime digest throughout), and the next ones as this version.
        import importlib
        earlier = 'e' * 64
        modules = [importlib.import_module('codex_agents.' + name) for name in (
            'account_host', 'account_service', 'client_runtime', 'collector_client', 'collector_runtime',
            'learning_store', 'project_driver', 'project_host_client', 'project_runtime', 'project_service',
            'service_host')] + [sys.modules[name] for name in ('cli', 'tests.test_bridge') if name in sys.modules
                                and hasattr(sys.modules[name], 'COLLECTOR_RUNTIME_DIGEST')]
        self.stop_services()
        with contextlib.ExitStack() as stack:
            for module in modules:
                if hasattr(module, 'COLLECTOR_RUNTIME_DIGEST'):
                    stack.enter_context(mock.patch.object(module, 'COLLECTOR_RUNTIME_DIGEST', earlier))
            self.start_services()
            orchestration, run_handle = self.leave_a_refused_claim_unfenced(stop=self.stop_services)
        self.start_services()
        self.closes_after_restart(run_handle, orchestration)

    def test_C3c_an_unreachable_account_does_not_use_up_the_refusal_retries(self):
        from codex_agents.account_api import AccountError
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        original, calls = AccountClient.fence_claim, []

        def flaky(client, fence, **options):
            calls.append(1)
            if len(calls) <= project_driver.MAX_FENCE_ATTEMPTS + 1:
                raise AccountError('account_unavailable')
            if len(calls) == project_driver.MAX_FENCE_ATTEMPTS + 2:
                raise AccountError('claim_busy')
            return original(client, fence, **options)
        with mock.patch.object(project_driver, 'FENCE_RETRY_SECONDS', 0.01), \
                mock.patch.object(project_driver, 'FENCE_RETRY_MAX_SECONDS', 0.05), \
                mock.patch.object(AccountClient, 'fence_claim', autospec=True, side_effect=flaky):
            self.start_host()
            self.closes_after_restart(run_handle, orchestration)

    def test_O10_E1_an_account_that_stops_mid_fence_does_not_use_up_the_refusal_retries(self):
        from codex_agents.account_api import AccountError
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        original, calls = AccountClient.fence_claim, []
        stops = ('account_closed_without_reply', 'service_refused_busy', 'service_refused_unavailable',
                 'service_refused_late')

        def stopping(client, fence, **options):
            calls.append(1)
            if len(calls) <= project_driver.MAX_FENCE_ATTEMPTS + 1:
                raise AccountError(stops[len(calls) % len(stops)])
            return original(client, fence, **options)
        with mock.patch.object(project_driver, 'FENCE_RETRY_SECONDS', 0.01), \
                mock.patch.object(project_driver, 'FENCE_RETRY_MAX_SECONDS', 0.05), \
                mock.patch.object(AccountClient, 'fence_claim', autospec=True, side_effect=stopping):
            self.start_host()
            self.closes_after_restart(run_handle, orchestration)

    def test_O10_E2_a_lease_the_listing_could_not_read_yet_is_listed_again_and_fenced(self):
        from codex_agents.history_contracts import HistoryError
        from codex_agents.orchestration_store import OrchestrationOwner
        self.leave_a_refused_claim_unfenced(count=2)
        original, calls = OrchestrationOwner.metadata_for, []

        def not_yet(owner, binding):
            calls.append(1)
            if len(calls) <= 3:
                raise HistoryError('history_service_unavailable')
            return original(owner, binding)
        with mock.patch.object(project_driver, 'FENCE_RETRY_SECONDS', 0.01), \
                mock.patch.object(project_driver, 'FENCE_RETRY_MAX_SECONDS', 0.05), \
                mock.patch.object(OrchestrationOwner, 'metadata_for', autospec=True, side_effect=not_yet):
            self.start_host()
            self.wait_for(lambda: self.bindings() == ['abandoned', 'abandoned'], seconds=60)

    def test_C3d_a_run_still_being_closed_says_so(self):
        import threading as threads
        orchestration, run_handle = self.leave_a_refused_claim_unfenced()
        original, release = AccountClient.fence_claim, threads.Event()

        def held(client, fence, **options):
            release.wait(30)
            return original(client, fence, **options)
        with mock.patch.object(AccountClient, 'fence_claim', autospec=True, side_effect=held):
            self.start_host()
            self.wait_for(lambda: self.cli_reply('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                                 '--reason', 'recovery_unavailable').get('reason_code')
                          == 'run_settling', seconds=30)
            release.set()
            self.closes_after_restart(run_handle, orchestration)

    def cli_reply(self, command, *arguments):
        import contextlib
        import io
        from tests.test_bridge import cli as cli_module
        stdout = io.StringIO()
        argv = ["cli.py", command, "--state-root", str(self.state.root), "--project-root", str(self.project),
                *arguments]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(cli_module, "BUNDLE_ROOT", self.bundle), \
                contextlib.redirect_stdout(stdout):
            cli_module.main()
        return json.loads(stdout.getvalue())

    def test_a_run_preparing_when_its_host_stops_holds_nothing_afterwards(self):
        self.stopped_in('preparing', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}], settled=False)
        self.assertEqual(1, len(self.launches()))

    def test_a_claimed_run_not_yet_launched_when_its_host_stops_is_settled(self):
        orchestration, run_handle = self.stopped_in('consuming', [
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        self.assertEqual(1, len(self.launches()))  # Only the next run's child; the claimed one never started.
        self.assertEqual(('abandoned', 'service_lost', 1), self.claimed_bindings()[0])
        self.assert_lost_status(orchestration, run_handle, 'released')

    def test_a_running_child_when_its_host_stops_is_settled(self):
        orchestration, run_handle = self.stopped_in('ready', [
            {'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1}}], reached=self.launches)
        self.assertEqual(2, len(self.launches()))
        self.assert_process_gone(self.launches()[0])
        self.assertEqual(('abandoned', 'service_lost', 1), self.claimed_bindings()[0])

    def test_a_finished_child_not_yet_reported_when_its_host_stops_is_settled(self):
        self.stopped_in('reporting', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        self.assertEqual(2, len(self.launches()))

    def test_a_run_whose_terminal_was_sent_when_its_host_stops_is_settled(self):
        self.stopped_in('terminal', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])

    def test_a_run_releasing_when_its_host_stops_is_released_once(self):
        self.stopped_in('releasing', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        # Released by its own host or settled by the next: either way it no longer counts, once.
        first = self.claimed_bindings()[0]
        self.assertIn(first, {('pending', None, 1), ('abandoned', 'service_lost', 1)})

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
            # B4s (RS-B-B4-4): the run is settled once its journal says so (after the closure is recorded and the
            # workspace removed); a stop before that replays the identical request, by design.
            self.wait_for(lambda: self.journal_states() in ([], ['settled']), seconds=30)
            for _ in range(2):
                self.restart_host()
        self.assertEqual([False, True], [reply['known'] for reply in replies[:2]])
        self.assertEqual(1, len({reply['receipt_digest'] for reply in replies}))
        self.assertEqual(2, len(replies))  # Settled runs are never sent again.
        self.assertEqual(0, self.counted())
        self.assertEqual([('abandoned', 'service_lost', 1)], self.claimed_bindings())
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


class LostRunAuthorityTests(LostRunHarness):
    """Only the project that holds a run's custody, through its project role, can settle it."""

    def admit(self, provider):
        self.second = Path(self.temporary.name) / 'second'
        self.second.mkdir()
        (self.second / 'source.txt').write_text('second marker\n')
        git(self.second, 'init', '-q')
        git(self.second, 'config', 'user.email', 'tests@example.invalid')
        git(self.second, 'config', 'user.name', 'Bridge Tests')
        git(self.second, 'add', '.')
        git(self.second, 'commit', '-qm', 'second')
        fingerprint = super().admit(provider)
        self.second_id = self.ledger.register_project(self.second, now_ms=1)
        identity = self.ledger.observe_provider(self.second_id, provider, project_root=self.tools, environ=CLEAN)[0]
        attest_provider(self.ledger, self.second_id, self.owner_key, provider, identity['account_fingerprint'])
        return fingerprint

    def settle_as(self, entry, *, role, host_client, root, project_id):
        custody = canonical_json({'purpose': 'run', 'lease': entry['lease'], 'storage_evidence': entry['storage_evidence'],
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        endpoint = account_endpoint_path(self.paths)
        here = threading.get_ident()

        def preamble(socket_path, message):
            if threading.get_ident() != here or Path(socket_path) != endpoint:
                return None
            key, proofs = read_capability(self.paths, None, 'account'), proof_directory(socket_path)
            line = enrollment_preamble(key, role, host_client, 1, message, proofs, subject=str(root), context=custody)
            return line, lambda: withdraw_proof(proofs, line)

        evidence = bytes.fromhex(entry['storage_evidence'])
        settle = SettleLostInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                 entry['bridge_run_id'], parse_bounded_json(evidence)['lease']['lease_digest'])
        enrollments = {'test-settle': preamble, **service_transport._ENROLLMENTS}
        with mock.patch.object(service_transport, '_ENROLLMENTS', enrollments):
            return AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=10).settle_lost_run(
                settle, request_id='test-settle-' + entry['bridge_run_id'][4:20])

    def test_only_the_runs_own_project_role_can_settle_it(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'held'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatch = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: self.journal_states() == ['watched'], seconds=30)
        entry = json.loads(next(self.journal().glob('brg_*.json')).read_text())
        refused = []
        for options in (
                # A primary (host role), even with the run's own custody.
                {'role': 'account', 'host_client': entry['host_client'], 'root': self.project,
                 'project_id': self.project_id},
                # The owner's terminal.
                {'role': 'account', 'host_client': OWNER_CLIENT, 'root': self.project, 'project_id': self.project_id},
                # Another project's service, with this project's custody, as itself or as this project.
                {'role': 'project', 'host_client': entry['host_client'], 'root': self.second,
                 'project_id': self.second_id},
                {'role': 'project', 'host_client': entry['host_client'], 'root': self.second,
                 'project_id': self.project_id}):
            with self.assertRaises(AccountError) as caught:
                self.settle_as(entry, **options)
            refused.append(caught.exception.reason_code)
        self.assertEqual(['account_not_authorized'] * 4, refused)
        self.assertEqual([('pending', None, 0)], self.claimed_bindings())
        self.assertEqual(1, self.counted())
        # Its own project role, with its custody, is accepted (the project vouches for the evidence).
        reply = self.settle_as(entry, role='project', host_client=entry['host_client'], root=self.project,
                               project_id=self.project_id)
        self.assertEqual(('settled', False), (reply['state'], reply['known']))
        self.assertEqual([('abandoned', 'service_lost', 1)], self.claimed_bindings())
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        dispatch.communicate(timeout=30)

    def test_no_other_path_can_write_the_lost_run_reason(self):
        context, route_path, orchestration, run_handle = self.open_run()
        with self.assertRaises(LedgerError):
            self.ledger.classify_host_run_abandoned(self.project_id, run_handle=run_handle, reason='service_lost')


class SettleOnExitTests(unittest.TestCase):
    def test_settling_on_exit_waits_for_the_sweep_and_the_account_call_and_writes_nothing_after(self):
        from concurrent.futures import ThreadPoolExecutor
        order = []
        driver = project_driver.RunDriver.__new__(project_driver.RunDriver)
        driver._sweeper, driver._executor = ThreadPoolExecutor(1), ThreadPoolExecutor(1)
        driver._sweeper.submit(lambda: (time.sleep(0.4), order.append('sweep')))
        driver._executor.submit(lambda: (time.sleep(0.4), order.append('account')))
        driver._sweeper.shutdown(wait=False)
        driver._executor.shutdown(wait=False)  # As close() leaves them.
        driver._preamble, driver._owner_name = (lambda *a: None), 'test-exit'
        driver._settle_lost = lambda own: order.append('settle')
        driver._journal = mock.Mock(release=lambda: order.append('release'))
        driver.settle_on_exit()
        self.assertEqual(['account', 'release', 'settle', 'sweep'], sorted(order))
        self.assertEqual(['settle', 'release'], order[2:])


class WorkspaceRemovalTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from tests.support import TEMP_ROOT
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        patcher = mock.patch.object(bridge, 'WORKSPACE_PARENT', str(self.base / 'scratch'))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.base / 'scratch').mkdir()
        self.outside = self.base / 'outside'
        (self.outside / 'prepare-kept').mkdir(parents=True)

    def test_a_workspace_is_removed_only_inside_the_real_scratch_container(self):
        container = self.base / 'scratch' / bridge.SCRATCH_CONTAINER
        container.mkdir()
        (container / 'prepare-own').mkdir()
        project_driver.RunDriver._remove_workspace(str(container / 'prepare-own'))
        self.assertFalse((container / 'prepare-own').exists())
        # A symlink in the container to a folder elsewhere is never followed or removed.
        (container / 'prepare-link').symlink_to(self.outside / 'prepare-kept')
        project_driver.RunDriver._remove_workspace(str(container / 'prepare-link'))
        self.assertTrue((self.outside / 'prepare-kept').is_dir())
        self.assertTrue((container / 'prepare-link').is_symlink())

    def test_a_container_replaced_by_a_symlink_is_refused(self):
        (self.base / 'scratch' / bridge.SCRATCH_CONTAINER).symlink_to(self.outside)
        workspace = self.base / 'scratch' / bridge.SCRATCH_CONTAINER / 'prepare-kept'
        project_driver.RunDriver._remove_workspace(str(workspace))
        self.assertTrue((self.outside / 'prepare-kept').is_dir())


if __name__ == '__main__':
    unittest.main()
