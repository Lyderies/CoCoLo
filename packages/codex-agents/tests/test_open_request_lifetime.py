"""OP-1 to OP-4: the account's open requests never fill a project for good (the lifetime cap found 2026-11-21).

Every build since the open journal was added (8e618ab) refused a project's 257th open, or its fifth while four
were pending, for ever: the cap counted every request row ever written, and rows were never removed.
OP-1 the open-time cap counts only requests in flight (pending, not past their own deadline), from a partial index.
OP-2 a finished open is purged with its binding and receipt past retention (DESIGN-0.4.0-B0-holds.md revision 3
     item 5); a replay of a purged open answers open_expired, and its request id never reopens.
OP-3 learning-status shows the open requests in flight against their limit (account_capacity.open_requests).
OP-4 a refusal at the in-flight cap says so (open_capacity_full) with its own next step.

Integration stage: the in-process collector, account and project services (as 0.3.4's ResultLimitTests), and the
original-open ledger and signature-job fixtures.
"""
import dataclasses
import itertools
import json
import sqlite3
import unittest
from unittest import mock

from tests.test_append_lookup import AppendLookupTests
from tests.test_account_original_open import OriginalAccountOpenJobTests, OriginalAccountOpenPendingTests
from tests.test_bridge import null_filled, result
from tests.test_project_driver import DriverHarness

from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_open_contracts import OpenError, OpenOwnerLimits
from codex_agents.account_service import AccountOwnerViews
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits

_BATCHES = itertools.count(1)
FAR = 10**13  # Past any open's deadline, binding expiry and retention in these fixtures.


def small_limits(pending, *, context_bytes=1_048_576):
    """Open-owner limits with `pending` in flight. 0.8.38 also had a cap on every request ever kept
    (max_retained); where the class still has it, it is set as small as it may be, so a test shows it."""
    values = {'max_pending': pending, 'max_context_bytes': context_bytes}
    if 'max_retained' in {field.name for field in dataclasses.fields(OpenOwnerLimits)}:
        values['max_retained'] = pending
    return OpenOwnerLimits(**values)


def steps(ledger, sql):
    """SQLite VM steps for one statement on a read-only connection (work, not time)."""
    reader = sqlite3.connect(ledger.state.database.as_uri() + '?mode=ro', uri=True)
    try:
        counted = [0]

        def tick():
            counted[0] += 1
            return 0
        reader.set_progress_handler(tick, 1)
        reader.execute(sql).fetchall()
        return counted[0]
    finally:
        reader.close()


def plan(ledger, sql):
    return ' | '.join(row[3] for row in ledger._connection.execute('EXPLAIN QUERY PLAN ' + sql))


def traced(ledger, action):
    """The statements `action` runs on the ledger's connection, with their values bound."""
    seen = []
    ledger._connection.set_trace_callback(seen.append)
    try:
        action()
    finally:
        ledger._connection.set_trace_callback(None)
    return seen


def finished_requests(ledger, project, total, *, opened_at_ms=1):
    """`total` committed open requests for `project`, written in one statement (the cap's history only)."""
    connection = ledger._connection
    batch = next(_BATCHES)  # each call's own keys (a sweep may remove rows between calls; corrected before code)
    with ledger._lock, connection:
        connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?) "
            "INSERT INTO account_open_requests(project_id,request_key,original_key,parameters_digest,handle_digest,"
            "run_id,state,opened_at_ms,observation_deadline_at_ms,receipt_digest,request_seal) "
            "SELECT ?, printf('k%04d%059d', ?, i), printf('o%04d%059d', ?, i), 'p', printf('h%04d%059d', ?, i), "
            "printf('run-%d-%d', ?, i), 'committed', ?, ?, printf('%064d', i), 's' FROM n",
            (total, project, batch, batch, batch, batch, opened_at_ms, opened_at_ms + 1))


def counts(ledger, handle_digest):
    """The open's own rows: its request, its receipt and its binding."""
    return tuple(ledger._connection.execute(f'SELECT COUNT(*) FROM {table} WHERE handle_digest=?',
                                            (handle_digest,)).fetchone()[0]
                 for table in ('account_open_requests', 'account_open_receipts', 'host_run_bindings'))


class OpenLimitTests(DriverHarness):
    """OP-1 acceptance, with every open limit set to 2 before the services open their stores."""

    def setUp(self):
        from codex_agents import account_host
        real = account_host.OpenOwnerLimits

        def small(*arguments, **options):
            limits = real(*arguments, **options)
            return small_limits(2, context_bytes=limits.max_context_bytes)
        patcher = mock.patch.object(account_host, 'OpenOwnerLimits', small)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_OP_1_more_finished_opens_than_every_open_limit_never_refuse(self):
        """OP-1: four complete lifecycles (open, dispatch, record, close) with the open limits at 2 all succeed.
        Under 0.8.38 the third open was refused open_busy, because the cap counted every request ever made."""
        for number in range(4):
            with self.subTest(lifecycle=number):
                context, route_path, orchestration, run_handle = self.open_run()
                self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
                (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
                dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
                observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
                    {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
                self.cli('learning-record-host-result', *context, '--route', route_path,
                         '--result', self.write('result.json', dispatched['result']), '--observation', observation)
                self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs[-1:], seconds=30)
                self.cli('learning-close-orchestration', '--orchestration-handle', orchestration, '--status', 'completed')
        self.assertEqual(4, self.collector_ledger._connection.execute(
            "SELECT COUNT(*) FROM account_open_requests WHERE state='committed'").fetchone()[0])


class OpenStatusTests(DriverHarness):
    def test_OP_3_learning_status_shows_open_requests_in_flight_against_their_limit(self):
        """OP-3: the account's capacity report names the open requests in flight and their limit."""
        context, route_path, _, run_handle = self.open_run()
        capacity = self.cli('learning-status')['account_capacity']
        self.assertEqual({'live': 0, 'limit': 4}, capacity['open_requests'])  # the open finished


class OpenInFlightTests(unittest.TestCase):
    """OP-1, OP-3 and OP-4 on the original-open journal, before any signature work."""

    def setUp(self):
        self.reference = OriginalAccountOpenPendingTests(methodName='runTest')
        self.reference.setUp()
        self.addCleanup(self.reference.doCleanups)
        self.ledger = self.reference.ledger
        self.project = self.reference.base.scope.project_id

    def another_task(self, name):
        OriginalAccountOpenJobTests.another_task(self, name)

    def first_open(self):
        """One request in flight, and its own deadline."""
        captured, known = self.reference.preflight(limits=small_limits(1))
        self.assertIsNone(known)
        self.another_task('second-task')
        return captured.observation_deadline_at_ms

    def test_OP_1_a_request_past_its_own_deadline_never_blocks_a_new_open(self):
        deadline = self.first_open()
        with mock.patch.object(self.ledger, '_clock_ms', return_value=deadline):
            self.reference.preflight(request_id='second-open', limits=small_limits(1))
        self.assertEqual(2, len(self.reference.pending()))

    def test_OP_1_finished_requests_never_block_a_new_open(self):
        finished_requests(self.ledger, self.project, 300)
        self.reference.preflight(limits=small_limits(1))
        self.assertEqual(301, len(self.reference.pending()))

    def test_OP_4_a_request_at_the_in_flight_cap_is_refused_with_its_own_reason(self):
        deadline = self.first_open()
        with (mock.patch.object(self.ledger, '_clock_ms', return_value=deadline - 1),
              self.assertRaisesRegex(OpenError, 'open_capacity_full')):
            self.reference.preflight(request_id='second-open', limits=small_limits(1))
        self.assertEqual(1, len(self.reference.pending()))  # the refused request left nothing behind

    def test_OP_1_the_in_flight_cap_reads_only_requests_in_flight_as_finished_ones_pile_up(self):
        """OP-1: the cap's work stays flat with history (a partial index on pending requests)."""
        finished_requests(self.ledger, self.project, 1_000)
        statements = traced(self.ledger, lambda: self.reference.preflight(limits=small_limits(4)))
        cap = [s for s in statements if 'account_open_requests' in s and "state='pending'" in s
               and 'observation_deadline_at_ms>' in s and s.lstrip().startswith('SELECT')]
        self.assertEqual(1, len(cap), statements)
        small = steps(self.ledger, cap[0])
        finished_requests(self.ledger, self.project, 20_000)
        large = steps(self.ledger, cap[0])
        self.assertLess(large, 2 * small + 200, (small, large))
        self.assertIn('account_open_requests_in_flight', plan(self.ledger, cap[0]))

    def test_OP_3_capacity_counts_open_requests_in_flight_against_their_limit(self):
        deadline = self.first_open()
        finished_requests(self.ledger, self.project, 50)
        with mock.patch.object(self.ledger, '_clock_ms', return_value=deadline - 1):
            self.assertEqual({'live': 1, 'limit': 4}, self.ledger.account_capacity(self.project)['open_requests'])
        with mock.patch.object(self.ledger, '_clock_ms', return_value=deadline):
            self.assertEqual({'live': 0, 'limit': 4}, self.ledger.account_capacity(self.project)['open_requests'])

    def test_OP_3_status_accepts_the_report_with_open_requests_and_the_earlier_ones(self):
        from codex_agents.client_runtime import _status_account_capacity, _status_account_holds
        old = {'observations': {'live': 1, 'limit': 4096}, 'cleanup_receipts': {'live': 0, 'limit': 4096}}
        b0 = {**old, 'pending_bindings': {'live': 2, 'limit': 4096}, 'billing_probes': {'live': 0, 'limit': 4096}}
        new = {**b0, 'open_requests': {'live': 1, 'limit': 4}}
        for report in (old, b0, new):
            self.assertEqual(report, _status_account_capacity(report))
        held = {'held': 0, 'past_due': 0, 'next_eligible': None, 'deadline': None}
        holds = {'run_slot': held, 'binding': held, 'probe': {**held, 'release_by_evidence': 0}}
        self.assertTrue(_status_account_holds({**new, 'holds': holds})['reported'])
        self.assertIsNone(_status_account_capacity({**new, 'open_requests': {'live': -1, 'limit': 4}}))
        self.assertIsNone(_status_account_capacity({**old, 'open_requests': {'live': 0, 'limit': 4}}))

    def test_OP_4_the_refusals_name_their_next_step(self):
        from codex_agents import account_api
        from codex_agents.refusal_steps import next_step
        full, expired = next_step('open_capacity_full'), next_step('open_expired')
        self.assertIn('learning-status', full)
        self.assertIn('open requests', full)
        self.assertNotIn('busy', full)
        self.assertIn('new run', expired)
        self.assertNotEqual(next_step('open_busy'), full)
        for code in ('open_capacity_full', 'open_expired'):
            self.assertEqual(code, OpenError(code).reason_code)
            self.assertIn(code, account_api.ACCOUNT_REASON_CODES)  # allowed on the wire


class OpenIndexTests(AppendLookupTests):
    def test_OP_1_a_ledger_without_the_open_indexes_gets_them_on_open(self):
        """OP-1/OP-2: an existing ledger (the control project's) gains both partial indexes when it opens, with no
        schema change, as B0's additive indexes do."""
        names = ('account_open_requests_in_flight', 'account_open_requests_finished')
        self.reopen()
        for name in names:
            self.ledger._connection.execute(f'DROP INDEX IF EXISTS {name}')
        version = self.ledger._connection.execute('PRAGMA user_version').fetchone()[0]
        self.reopen()
        present = {row[0] for row in self.ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertLessEqual(set(names), present)
        self.assertEqual(version, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])


class OpenLifetimeJobTests(OriginalAccountOpenJobTests):
    """OP-1 and OP-2 with real signature work: opens that commit, then finish."""

    def setUp(self):
        self.reference = OriginalAccountOpenPendingTests(methodName='runTest')
        self.reference.setUp()
        self.addCleanup(self.reference.doCleanups)
        self.reference.limits = small_limits(1)
        self.ledger = self.reference.ledger
        self.jobs = AccountClaimJobs(AccountOwnerViews(self.ledger), SignatureIntakeLimits(2, 2, 512, 262_144, 2),
                                     SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
                                     self.reference.work, open_limits=self.reference.limits)
        self.jobs.bootstrap()
        self.addCleanup(self.drain)

    def committed(self, request_id='original-account-open'):
        """One open committed through the real signature path; its handle digest and opening time."""
        reply = self.pump(self.submit(request_id=request_id))
        self.assertIsNone(reply.reason)
        self.assertEqual('opened', reply.result['state'])
        row = self.ledger._connection.execute('SELECT handle_digest,opened_at_ms FROM account_open_requests '
                                              'WHERE run_id=?', (reply.result['run_id'],)).fetchone()
        return row[0], row[1]

    def finish(self, handle_digest):
        """The run's binding ends, as a terminal does; the register releases its hold."""
        self.ledger._connection.execute("UPDATE host_run_bindings SET binding_state='terminal',consumed_at_ms="
                                        "created_at_ms+1 WHERE handle_digest=?", (handle_digest,))

    def binding_hold(self, handle_digest):
        return self.ledger._connection.execute("SELECT state FROM run_holds WHERE kind='binding' AND identity=?",
                                               (handle_digest,)).fetchone()

    def test_OP_1_finished_opens_never_block_a_new_open(self):
        """OP-1, with one open allowed in flight: three opens in a row commit. 0.8.38 refused the second."""
        self.committed()
        for name in ('second', 'third'):
            self.another_task(name + '-task')
            self.committed(name + '-open')

    def test_OP_2_a_finished_open_is_purged_with_its_binding_and_receipt_past_retention(self):
        handle, opened = self.committed()
        self.finish(handle)
        from codex_agents.learning_store import HOST_RUN_TTL_MS, OPEN_REQUEST_RETENTION_MS
        due = opened + HOST_RUN_TTL_MS + OPEN_REQUEST_RETENTION_MS
        self.ledger.sweep_account_retention(now_ms=due)
        self.assertEqual((1, 1, 1), counts(self.ledger, handle))  # still inside its retention
        self.ledger.sweep_account_retention(now_ms=due + 1)
        self.assertEqual((0, 0, 0), counts(self.ledger, handle))
        self.assertEqual('released', self.binding_hold(handle)[0])  # the register keeps its tombstone

    def test_OP_2_a_replay_of_a_purged_open_answers_open_expired_and_never_reopens(self):
        handle, _ = self.committed()
        self.finish(handle)
        self.ledger.sweep_account_retention(now_ms=FAR)
        with self.assertRaisesRegex(OpenError, 'open_expired'):
            self.submit()
        self.assertEqual((0, 0, 0), counts(self.ledger, handle))
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_open_requests').fetchone()[0])

    def test_OP_2_an_open_whose_binding_is_still_held_is_kept(self):
        handle, _ = self.committed()
        self.ledger.sweep_account_retention(now_ms=FAR)
        self.assertEqual((1, 1, 1), counts(self.ledger, handle))
        self.assertEqual('held', self.binding_hold(handle)[0])

    def test_OP_2_an_open_whose_run_slot_is_still_held_is_kept(self):
        """B0 revision 3 item 5: a row still used by a capacity predicate stays, whatever its age."""
        client, state, launch = self.managed_lifecycle()
        client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
        # This open's slot (the fixture keeps a legacy run's slot of its own; corrected before code).
        handle = self.ledger._connection.execute(
            'SELECT q.handle_digest FROM account_open_requests q JOIN active_run_capacity a '
            'ON a.project_id=q.project_id AND a.handle_digest=q.handle_digest WHERE a.released_at_ms IS NULL').fetchone()[0]
        self.finish(handle)
        self.ledger.sweep_account_retention(now_ms=FAR)
        self.assertEqual((1, 1, 1), counts(self.ledger, handle))

    def test_OP_2_the_purge_takes_a_bounded_batch(self):
        first, _ = self.committed()
        self.another_task('second-task')
        second, _ = self.committed('second-open')
        for handle in (first, second):
            self.finish(handle)
        self.ledger.sweep_account_retention(now_ms=FAR, batch=1)
        self.assertEqual(1, sum(counts(self.ledger, handle)[0] for handle in (first, second)))
        self.ledger.sweep_account_retention(now_ms=FAR, batch=1)
        self.assertEqual(0, sum(counts(self.ledger, handle)[0] for handle in (first, second)))

    def test_OP_2_the_purge_reads_only_requests_past_retention_as_young_ones_pile_up(self):
        handle, opened = self.committed()
        self.finish(handle)
        project = self.reference.base.scope.project_id
        finished_requests(self.ledger, project, 1_000, opened_at_ms=FAR)
        statements = traced(self.ledger, lambda: self.ledger.sweep_account_retention(now_ms=FAR))
        scan = [s for s in statements if 'FROM account_open_requests' in s and 'opened_at_ms<' in s
                and s.lstrip().startswith('SELECT')]
        self.assertEqual(1, len(scan), statements)
        small = steps(self.ledger, scan[0])
        finished_requests(self.ledger, project, 20_000, opened_at_ms=FAR)
        large = steps(self.ledger, scan[0])
        self.assertLess(large, 2 * small + 200, (small, large))
        self.assertIn('account_open_requests_finished', plan(self.ledger, scan[0]))

    def test_OP_4_a_full_in_flight_cap_reaches_the_caller_with_its_own_reason(self):
        self.reference.preflight(limits=small_limits(1))  # one request in flight, its signature work not started
        self.another_task('second-task')
        with self.assertRaisesRegex(OpenError, 'open_capacity_full'):
            self.submit(request_id='second-open')


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the imported original-open classes run in their own module."""
    suite = unittest.TestSuite()
    for case in (OpenLimitTests, OpenStatusTests, OpenInFlightTests, OpenIndexTests, OpenLifetimeJobTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
