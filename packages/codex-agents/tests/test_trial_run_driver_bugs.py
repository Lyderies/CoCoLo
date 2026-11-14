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
from tests.test_project_driver import ClaudeChildHarness, DriverHarness

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

    def test_a_project_keeps_more_than_four_recorded_results(self):
        # Third trial: each kept result reserved the largest possible body for hours, so a
        # project's fifth run could not reserve space (history_result_capacity at arm).
        orchestration = None
        for index in range(6):
            name = 'many%d' % index
            context, route_path, orchestration, run_handle, _, _ = self.opened(
                name, orchestration=orchestration, task_changes={'id': 'task-' + name},
                assignment_changes={'assignment_id': name, 'task_id': 'task-' + name})
            self.child_answers(name)
            before = len(self.stopped_runs)
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
            self.assertEqual(0, code, 'run %d refused: %r; driver stopped at %r' % (
                index + 1, report, self.stopped_runs[before:]))
            self.record(name, context, route_path, run_handle, report)
        self.assertEqual(6, len(self.executions()))

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

    # 4. A run that can never launch closes by itself; the primary runs no command for it.
    def watched_runs(self):
        """The driver's own runs by run handle, and how often each gave its preparation back."""
        from codex_agents import project_driver
        seen, given_back = {}, {}
        advance, give_back = project_driver.RunDriver._advance, project_driver.RunDriver._give_back

        def watch(driver, run):
            seen[run.key[0]] = (driver, run)
            return advance(driver, run)

        def count(driver, run):
            held = run.prepared is not None
            give_back(driver, run)
            if held and run.prepared is None:
                given_back[run.key[0]] = given_back.get(run.key[0], 0) + 1
        for name, replacement in (('_advance', watch), ('_give_back', count)):
            patcher = mock.patch.object(project_driver.RunDriver, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return seen, given_back

    def lease_state(self, run_handle):
        import sqlite3
        store = sqlite3.connect('file:' + str(next(self.state.root.glob('projects/*/history.sqlite3'))) + '?mode=ro',
                                uri=True)
        try:
            return [row[0] for row in store.execute('SELECT state FROM result_leases WHERE run_handle=?', (run_handle,))]
        finally:
            store.close()

    def denied_at_claim(self, name='denied'):
        """A run the account itself refuses at claim, with a denied receipt (stale billing, here)."""
        from codex_agents import providers
        context, route_path, orchestration, run_handle, _, _ = self.opened(name)
        self.child_answers()
        with mock.patch.object(providers, 'evaluate_launch', return_value='billing_unverified'):
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        return code, report, (context, route_path, orchestration, run_handle)

    def assert_closed(self, runs, given_back, run_handle, reason):
        driver, run = runs[run_handle]
        self.assertEqual(('failed', reason, True), (run.state, run.reason, run.closed))
        self.assertIsNone(run.prepared)
        self.assertEqual(1, given_back.get(run_handle), 'the preparation is given back exactly once')
        self.assertIsNone(run.receipt)
        self.assertEqual(['expired'], self.lease_state(run_handle))
        self.assertFalse(driver.busy())

    def next_run_launches(self, before):
        context, route_path, _, run_handle, _, _ = self.opened(
            'next', task_changes={'id': 'task-next'}, assignment_changes={'assignment_id': 'next', 'task_id': 'task-next'})
        self.child_answers('next')
        code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(0, code, 'next run refused: %r; driver stopped at %r' % (report, self.stopped_runs))
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(before + 1, len(self.executions()))

    def test_a_run_denied_at_claim_closes_by_itself_and_frees_what_it_held(self):
        runs, given_back = self.watched_runs()
        code, report, (_, _, orchestration, run_handle) = self.denied_at_claim()
        self.assertEqual((2, 'billing_unverified'), (code, report.get('reason_code')), report)
        self.assertEqual([], self.executions())
        self.assertIn(('driver', 'claiming', 'closing'), self.stopped_runs)
        self.assert_closed(runs, given_back, run_handle, 'billing_unverified')
        # The account fenced it: nothing pending, and the orchestration closes without any command for the run.
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual((0, 1), (lifecycle['host_runs_pending'], lifecycle['host_runs_abandoned']), lifecycle)
        closed = self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'])
        self.next_run_launches(0)

    def test_a_run_failing_before_its_claim_was_sent_closes_by_itself(self):
        from codex_agents.account_api import AccountClient, AccountError
        runs, given_back = self.watched_runs()
        context, route_path, orchestration, run_handle, _, _ = self.opened('unsent')
        self.child_answers()
        claims, claim = [], AccountClient.claim_launch

        def refused(*_args, **_kwargs):
            raise AccountError('account_not_authorized')

        def counted(client, *args, **options):
            claims.append(args)
            return claim(client, *args, **options)
        with mock.patch.object(AccountClient, 'open_provenance', refused), \
                mock.patch.object(AccountClient, 'claim_launch', counted):
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual((2, 'account_not_authorized'), (code, report.get('reason_code')), report)
        self.assertEqual([], claims)
        self.assertEqual([], self.executions())
        self.assertIn(('recording_open', 'account_not_authorized'), self.stopped_runs)
        self.assert_closed(runs, given_back, run_handle, 'account_not_authorized')
        self.assert_fenced(*runs[run_handle])
        closed = self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'])
        code, abandoned = self.attempt('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                       '--reason', 'host_confirmed_gone')
        self.assertEqual((0, 'already_closed'), (code, abandoned.get('classification')), abandoned)
        self.next_run_launches(0)

    def late_claim(self, driver, run):
        """The run's own claim sent again after it closed, as a delayed duplicate would arrive."""
        from codex_agents.account_api import AccountClient
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        custody = driver._custody(run, storage=run.evidence)

        def claim():  # On the driver's own account thread, enrolled exactly as its claim was.
            call = driver._call
            call.role, call.host_client, call.context = 'account', run.host_client, custody
            try:
                client = AccountClient(driver._account, run.scope.project_id, COLLECTOR_RUNTIME_DIGEST, timeout=30)
                return client.claim_launch(run.key[0], run.identity_digest, request_id=run.claim_request_id)
            finally:
                call.role = call.host_client = call.context = None
        from codex_agents import service_transport
        # Only the driver enrolls it: no earlier command's client enrollment may answer for it.
        with mock.patch.dict(service_transport._ENROLLMENTS, {driver._owner_name: driver._preamble}, clear=True):
            return driver._executor.submit(claim).result(timeout=60)

    def assert_fenced(self, driver, run):
        """The account fenced the run: nothing pending, and its own claim is refused from now on."""
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual((0, 1), (lifecycle['host_runs_pending'], lifecycle['host_runs_abandoned']), lifecycle)
        late = self.late_claim(driver, run)
        self.assertEqual(('denied', 'claim_not_open'), (late['state'], late['reason_code']), late)
        self.assertEqual([], self.executions())

    def assert_not_freed(self, runs, run_handle, orchestration):
        _, run = runs[run_handle]
        self.assertEqual(('failed', False), (run.state, run.closed))
        self.assertEqual(['claim_pending'], self.lease_state(run_handle))
        code, refused = self.attempt('learning-close-orchestration', '--orchestration-handle', orchestration,
                                     '--status', 'failed')
        self.assertEqual((2, 'history_orchestration_pending'), (code, refused.get('reason_code')), refused)
        code, abandoned = self.attempt('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                       '--reason', 'host_confirmed_gone')
        self.assertEqual((2, 'history_conflict'), (code, abandoned.get('reason_code')), abandoned)

    def test_a_claim_refused_without_a_receipt_is_fenced_and_closes_by_itself(self):
        # The refusal committed nothing; the account's fence makes that permanent and proves it.
        runs, given_back = self.watched_runs()
        code, report, (_, _, orchestration, run_handle) = self.forced_refusal_at_claim()
        self.assertEqual((2, 'claim_storage_expired'), (code, report.get('reason_code')), report)
        self.assertIn(('driver', 'fencing', 'settling'), self.stopped_runs)
        self.assert_closed(runs, given_back, run_handle, 'claim_storage_expired')
        self.assert_fenced(*runs[run_handle])
        closed = self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'])
        self.next_run_launches(0)

    def lost_claim_reply(self, name, *, committed):
        from codex_agents.account_api import AccountClient, AccountError
        context, route_path, orchestration, run_handle, _, _ = self.opened(name)
        self.child_answers()
        claim = AccountClient.claim_launch

        def lost(client, *args, **options):
            if committed:
                claim(client, *args, **options)  # The account admits it; the reply never arrives intact.
            raise AccountError('account_invalid_response')
        with mock.patch.object(AccountClient, 'claim_launch', lost):
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        if committed:
            return orchestration, run_handle, context, route_path, code, report
        self.assertEqual((2, 'account_invalid_response'), (code, report.get('reason_code')), report)
        self.assertEqual([], self.executions())
        return orchestration, run_handle

    def test_a_claim_whose_reply_was_lost_after_it_was_admitted_launches_exactly_once(self):
        # The fence finds the admitted claim and changes nothing at the account; the run, which
        # never launched, goes on as if the lost reply had arrived, and its waiting dispatch gets it.
        runs, given_back = self.watched_runs()
        orchestration, run_handle, context, route_path, code, report = self.lost_claim_reply('lost', committed=True)
        self.assertEqual(0, code, report)
        self.assertEqual('result', report['bridge_report']['outcome'])
        self.assertEqual(1, len(self.executions()))
        self.assertEqual({}, given_back)  # Released through its normal path, never given back early.
        self.record('lost', context, route_path, run_handle, report)
        # Never launched again: a second dispatch of the handle is refused.
        again, _ = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertNotEqual(0, again)
        self.assertEqual(1, len(self.executions()))

    def test_a_claim_whose_reply_was_lost_before_it_was_admitted_is_fenced_and_closes(self):
        runs, given_back = self.watched_runs()
        orchestration, run_handle = self.lost_claim_reply('lost', committed=False)
        self.assert_closed(runs, given_back, run_handle, 'account_invalid_response')
        self.assert_fenced(*runs[run_handle])
        closed = self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'])
        self.next_run_launches(0)

    def test_a_run_refused_at_claim_closes_without_the_primary_and_then_blocks_nothing(self):
        code, report, (context, route_path, orchestration, run_handle) = self.denied_at_claim()
        self.assertNotEqual(0, code, 'precondition: the run must be refused at claim')
        # No pause and no command for the run: it is already closed, and the command says so if called.
        code, abandoned = self.attempt('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                       '--reason', 'host_confirmed_gone')
        self.assertEqual(0, code, abandoned)
        self.assertEqual('already_closed', abandoned['classification'])
        target = ('--orchestration-handle', orchestration)
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


class ClaudeChildRunClosingTests(ClaudeChildHarness):
    """A bridged Claude child the account denies closes by itself like a Codex one."""
    attempt = TrialRunDriverTests.attempt
    executions = TrialRunDriverTests.executions
    watched_runs = TrialRunDriverTests.watched_runs
    lease_state = TrialRunDriverTests.lease_state
    assert_closed = TrialRunDriverTests.assert_closed

    def test_a_claude_run_denied_at_claim_closes_by_itself(self):
        from codex_agents import providers
        runs, given_back = self.watched_runs()
        context, route_path, orchestration, run_handle = self.open_claude_run()
        with mock.patch.object(providers, 'evaluate_launch', return_value='billing_unverified'):
            code, report = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual((2, 'billing_unverified'), (code, report.get('reason_code')), report)
        self.assertEqual([], [i for i in self.invocations('claude') if '-p' in i['argv']])
        self.assert_closed(runs, given_back, run_handle, 'billing_unverified')
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual((0, 1), (lifecycle['host_runs_pending'], lifecycle['host_runs_abandoned']), lifecycle)
        closed = self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'failed')
        self.assertEqual('failed', closed['current']['snapshot']['status'])


if __name__ == '__main__':
    unittest.main()
