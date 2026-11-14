"""Regressions a real install trial found in the project host's run driver.

Each test drives the product path through the in-process harness (collector,
account and project hosts as threads, the CLI in-process) and asserts what the
primary sees. They are written to fail on the code the trial ran.
"""
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from tests.test_bridge import PARENT_GRANT, cli, null_filled, result
from tests.test_project_driver import DriverHarness

ACCEPTED = [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]


class TrialRunDriverTests(DriverHarness):

    def attempt(self, command, *arguments, project=None):
        """Run one command; returns (exit code, report) without asserting either."""
        stdout = io.StringIO()
        argv = ['cli.py', command, '--state-root', str(self.state.root),
                '--project-root', str(project or self.project), *arguments]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle), \
                contextlib.redirect_stdout(stdout):
            code = cli.main()
        return code, json.loads(stdout.getvalue())

    def executions(self):
        return [i for i in self.invocations('codex') if 'exec' in i['argv']]

    def standard_assignment(self, route, **changes):
        """An assignment that keeps the route's own timeout, as the documentation's worked example does."""
        assigned = self.assignment_for(route, **changes)
        assigned['bounds'] = {**assigned['bounds'], 'timeout_seconds': route['bounds']['timeout_seconds']}
        return assigned

    def opened(self, name, *, task_changes=None, standard=False, orchestration=None, assignment_changes=None):
        """Route, assign and open one run; returns (context, route path, orchestration handle, run handle)."""
        task = {**self.task(), **(task_changes or {})}
        task_path = self.write(name + '-task.json', task)
        route = self.cli('learning-route', '--task', task_path)
        changes = dict(assignment_changes or {})
        assigned = (self.standard_assignment if standard else self.assignment_for)(route, **changes)
        context = ('--task', task_path, '--assignment', self.write(name + '-assignment.json', assigned),
                   '--parent-grant', self.write(name + '-grant.json', PARENT_GRANT))
        route_path = self.write(name + '-route.json', route)
        if orchestration is None:
            orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.assertEqual('admitted', opened['admission'])
        return context, route_path, orchestration, opened['run_handle'], route, assigned

    def child_answers(self, assignment_id='assignment-1'):
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(
            json.dumps(null_filled({**result(), 'assignment_id': assignment_id})))

    def refused_at_claim(self):
        """A standard-timeout run, dispatched once; on the trial's code it is refused at claim."""
        context, route_path, orchestration, run_handle, route, assigned = self.opened('standard', standard=True)
        self.assertEqual(21600, route['bounds']['timeout_seconds'])
        self.assertEqual(21600, assigned['bounds']['timeout_seconds'])
        self.child_answers()
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        return code, report, (context, route_path, orchestration, run_handle)

    def test_a_run_failing_after_its_claim_was_admitted_keeps_its_preparation(self):
        # Local custody must match the account's: an admitted claim keeps the preparation.
        from codex_agents import project_driver
        failed = []
        original = project_driver.RunDriver._fail

        def fail_after_claim(driver, run, reason):
            failed.append((run.receipt is not None, run.prepared is not None))
            original(driver, run, reason)
            failed.append((run.receipt is not None, run.prepared is not None))
        context, route_path, _, run_handle, _, _ = self.opened('claimed')
        self.child_answers()
        bind = project_driver.RunDriver._advance

        def break_after_claim(driver, run):
            if run.state == 'binding':
                raise RuntimeError('fault after the claim was admitted')
            return bind(driver, run)
        with mock.patch.object(project_driver.RunDriver, '_fail', fail_after_claim), \
                mock.patch.object(project_driver.RunDriver, '_advance', break_after_claim):
            code, _ = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertNotEqual(0, code)
        self.assertIn((True, True), failed[1::2])  # Still prepared after the failure.

    def test_the_result_reservation_outlasts_the_longest_route_time_limit(self):
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents import project_service
        policy = json.loads((ORCHESTRATION_ROOT / 'config' / 'routing-policy.json').read_text())
        limits = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'timeout_seconds' and type(item) is int:
                        limits.append(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(policy)
        self.assertEqual(max(limits), project_service.MAX_ROUTE_TIMEOUT_SECONDS)
        self.assertGreater(project_service.RESULT_RESERVATION_MS, max(limits) * 1000)

    def test_dispatch_names_every_reason_the_run_driver_gives(self):
        from codex_agents import client_runtime, project_driver
        self.assertLessEqual(project_driver.REASON_CODES, client_runtime.START_REASONS)

    def forced_refusal_at_claim(self):
        """A run refused at claim, as every standard-timeout run was on the trial's code (now
        fixed): the claim's storage check is made to refuse it once, the same way."""
        from codex_agents import claim_storage
        from codex_agents.account_claim_contracts import ClaimError

        def expired(*_args, **_kwargs):
            raise ClaimError('claim_storage_expired')
        with mock.patch.object(claim_storage, 'verify_storage_evidence', expired):
            return self.refused_at_claim()

    def record(self, name, context, route_path, run_handle, dispatched):
        observation = self.write(name + '-observation.json', {'run_handle': run_handle, 'result_attempts': ACCEPTED})
        return self.cli('learning-record-host-result', *context, '--route', route_path,
                        '--result', self.write(name + '-result.json', dispatched['result']),
                        '--observation', observation)

    # 1. The route's own timeout must be launchable.
    def test_a_run_with_the_routes_own_timeout_is_claimed_and_launched(self):
        code, report, _ = self.refused_at_claim()
        self.assertEqual(0, code, 'dispatch refused: %r; driver stopped at %r' % (report, self.stopped_runs))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(self.executions()))

    # 2. One project host must be able to run more than one child.
    def test_a_run_after_one_refused_at_claim_still_prepares_and_launches(self):
        code, report, _ = self.forced_refusal_at_claim()
        self.assertNotEqual(0, code, 'precondition: the run must be refused at claim')
        self.assertIn(('claiming', 'claim_storage_expired'), self.stopped_runs)
        context, route_path, _, run_handle, _, _ = self.opened(
            'next', task_changes={'id': 'task-next'}, assignment_changes={'assignment_id': 'next', 'task_id': 'task-next'})
        self.child_answers('next')
        before = len(self.stopped_runs)
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'second run refused: %r; driver stopped at %r' % (report, self.stopped_runs[before:]))
        self.assertEqual('result', report['bridge_report']['outcome'])

    def test_a_second_run_right_after_a_recorded_one_prepares_and_launches(self):
        context, route_path, orchestration, run_handle, _, _ = self.opened('first')
        self.child_answers()
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        self.record('first', context, route_path, run_handle, dispatched)
        # The primary goes straight on to its next child, as the trial's primary did.
        context, route_path, _, run_handle, _, _ = self.opened(
            'second', orchestration=orchestration, task_changes={'id': 'task-second'},
            assignment_changes={'assignment_id': 'second', 'task_id': 'task-second'})
        self.child_answers('second')
        before = len(self.stopped_runs)
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'second run refused: %r; driver stopped at %r' % (report, self.stopped_runs[before:]))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(2, len(self.executions()))

    def test_a_second_run_opened_before_the_first_is_recorded_launches_once_it_is(self):
        context, route_path, orchestration, run_handle, _, _ = self.opened('first')
        self.child_answers()
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        # The next child is routed and opened first; the record and the dispatch then follow at once.
        second, second_route, _, second_handle, _, _ = self.opened(
            'second', orchestration=orchestration, task_changes={'id': 'task-second'},
            assignment_changes={'assignment_id': 'second', 'task_id': 'task-second'})
        self.record('first', context, route_path, run_handle, dispatched)
        self.child_answers('second')
        before = len(self.stopped_runs)
        code, report = self.attempt('dispatch', *second, '--route', second_route, '--run-handle', second_handle)
        self.assertEqual(0, code, 'second run refused: %r; driver stopped at %r' % (report, self.stopped_runs[before:]))
        self.assertEqual('result', report['bridge_report']['outcome'])

    def test_a_second_run_dispatched_before_the_first_is_recorded_launches(self):
        context, route_path, orchestration, run_handle, _, _ = self.opened('first')
        self.child_answers()
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        # The primary starts its next child before recording the first one's result.
        second, second_route, _, second_handle, _, _ = self.opened(
            'second', orchestration=orchestration, task_changes={'id': 'task-second'},
            assignment_changes={'assignment_id': 'second', 'task_id': 'task-second'})
        self.child_answers('second')
        before = len(self.stopped_runs)
        code, report = self.attempt('dispatch', *second, '--route', second_route, '--run-handle', second_handle)
        self.assertEqual(0, code, 'second run refused: %r; driver stopped at %r' % (report, self.stopped_runs[before:]))
        self.assertEqual('result', report['bridge_report']['outcome'])

    def test_a_verifier_right_after_its_recorded_worker_prepares_and_launches(self):
        context, route_path, orchestration, run_handle, _, _ = self.opened('worker')
        self.child_answers()
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        digest = self.record('worker', context, route_path, run_handle, dispatched)['host_outcome_digest']
        context, route_path, run_handle = self.open_verifier(orchestration, digest)
        self.child_answers('verify-assignment-1')
        before = len(self.stopped_runs)
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'verifier refused: %r; driver stopped at %r' % (report, self.stopped_runs[before:]))
        self.assertEqual('result', report['bridge_report']['outcome'])

    def verifier_context(self, digest):
        task = {**self.task(), 'id': 'verify-1', 'objective': 'Independently verify the research result',
                'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': digest, 'owned_paths': ['source.txt']}
        task_path = self.write('verifier-task.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = self.assignment_for(route, assignment_id='verify-assignment-1', task_id='verify-1',
                                       objective=task['objective'], context=[digest], owned_paths=['source.txt'],
                                       verification_required=route['verification_required'])
        context = ('--task', task_path, '--assignment', self.write('verifier-assignment.json', assigned),
                   '--parent-grant', self.write('verifier-grant.json', PARENT_GRANT))
        return context, self.write('verifier-route.json', route)

    def open_verifier(self, orchestration, digest):
        context, route_path = self.verifier_context(digest)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        return context, route_path, opened['run_handle']

    # 3. dispatch must say why the host refused.
    def test_dispatch_reports_the_drivers_reason_when_preparation_fails(self):
        code, report, _ = self.forced_refusal_at_claim()
        self.assertEqual(2, code, 'precondition: the run must be refused at claim: %r' % (report,))
        self.assertIn(('claiming', 'claim_storage_expired'), self.stopped_runs)
        self.assertEqual('claim_storage_expired', report.get('reason_code'), report)

    def test_dispatch_reports_run_unavailable_when_the_host_has_no_preparation_room(self):
        self.forced_refusal_at_claim()
        context, route_path, _, run_handle, _, _ = self.opened(
            'next', task_changes={'id': 'task-next'}, assignment_changes={'assignment_id': 'next', 'task_id': 'task-next'})
        self.child_answers('next')
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        if code == 0:
            self.skipTest('the second run launched; bug 2 is fixed, so there is no refusal to report')
        self.assertEqual('run_unavailable', report.get('reason_code'), report)

    # 4. A run refused at claim must be closable by the primary.
    @unittest.expectedFailure  # Open: closing it needs the account to confirm the refused claim (see CHANGELOG).
    def test_a_run_refused_at_claim_can_be_abandoned_and_then_blocks_nothing(self):
        code, report, (context, route_path, orchestration, run_handle) = self.forced_refusal_at_claim()
        self.assertNotEqual(0, code, 'precondition: the run must be refused at claim')
        target = ('--orchestration-handle', orchestration)
        self.cli('learning-pause-orchestration', *target)
        code, abandoned = self.attempt('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                       '--reason', 'host_confirmed_gone')
        self.assertEqual(0, code, abandoned)
        self.assertEqual('abandoned', abandoned['classification'])
        # Nothing waits on it any more: the orchestration closes and the ledger has no pending run.
        self.cli('learning-resume-orchestration', *target)
        closed = self.cli('learning-close-orchestration', *target, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'] if 'current' in closed else closed['status'])
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual(0, lifecycle['host_runs_pending'], lifecycle)

    # 5. Verifier pre-checks must not need the legacy ledger to itself.
    def test_verifier_prechecks_succeed_while_another_child_runs(self):
        context, route_path, orchestration, run_handle, _, _ = self.opened('worker')
        self.child_answers()
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        digest = self.record('worker', context, route_path, run_handle, dispatched)['host_outcome_digest']
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        # Another child is running while the primary checks its verifier's inputs.
        busy, busy_route, _, busy_handle, _, _ = self.opened(
            'busy', orchestration=orchestration, task_changes={'id': 'task-busy'},
            assignment_changes={'assignment_id': 'busy', 'task_id': 'task-busy'})
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(
            json.dumps(null_filled({**result(), 'assignment_id': 'busy'})))
        process = self.separate_dispatch(busy, busy_route, busy_handle)
        self.wait_for(lambda: len(self.executions()) == 2, seconds=30)
        target = ('--orchestration-handle', orchestration, '--run-handle', busy_handle)
        try:
            verifier, _ = self.verifier_context(digest)
            validated = self.attempt('validate-assignment', *verifier)
            preflight = self.attempt('result-preflight', *verifier)
            self.assertEqual((0, 0), (validated[0], preflight[0]),
                             'validate-assignment: %r; result-preflight: %r' % (validated[1], preflight[1]))
            self.assertTrue(preflight[1]['ready'])
            # The running child was not disturbed and the collector was not stopped under it.
            status = self.cli('learning-run-status', *target)
            self.assertEqual(('running', None), (status['cancellation_state'], status['stop_reason']))
        finally:
            self.cli('learning-cancel-run', *target)
            process.communicate(timeout=30)


if __name__ == '__main__':
    unittest.main()
