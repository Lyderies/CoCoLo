"""Project lifecycle behavior; explicit fake native authorization, no model calls."""
from contextlib import contextmanager
from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import assignment
from tests.test_project_history import PROJECT_A, PROJECT_B

from codex_agents.history_contracts import HistoryError
from codex_agents.orchestration_api import (
    ORCHESTRATION_OPERATIONS, OrchestrationClient, OrchestrationLimits, OrchestrationRequest, OrchestrationScope,
    decode_orchestration_response, encode_orchestration_request, orchestration_request_digest,
)
from codex_agents.orchestration_service import OrchestrationAuthorization, OrchestrationService
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding
from codex_agents.service_transport import ServiceTransportError

KEY = b'j' * 32  # Disposable fixture key, not owner/native authority.


class ProjectOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'history'
        self.directory.mkdir(mode=0o700)
        self.scope = OrchestrationScope(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1)
        self.now = 1000
        self.limits = OrchestrationLimits(4, 12)
        self.store = self.open_history()
        self.addCleanup(lambda: self.store.close())
        self.token = object()
        self.authorizations = 0
        self.revoke = False
        self.service = self.make_service()

    def open_history(self):
        return ProjectHistory(self.directory, PROJECT_A, result_key=KEY,
                              result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                              result_clock_ms=lambda: self.now, orchestration_limits=self.limits)

    def authorize(self, request, context):
        if context is not self.token:
            return None
        self.authorizations += 1
        if self.revoke and self.authorizations > 1:
            return None
        return OrchestrationAuthorization(self.scope, orchestration_request_digest(request), 'codex-host-v1',
                                          ORCHESTRATION_OPERATIONS)

    def make_service(self):
        service = OrchestrationService(PROJECT_A, self.scope.runtime_digest, lambda: self.store, authorizer=self.authorize)
        self.addCleanup(service.close)
        return service

    def request(self, operation, payload=None, identity='request', scope=None):
        return OrchestrationRequest(1, 'orchestration.' + operation, identity, scope or self.scope,
                                    {} if payload is None else payload)

    def call(self, operation, payload=None, identity='request', scope=None, context=None):
        request = self.request(operation, payload, identity, scope)
        return decode_orchestration_response(self.service.handle(encode_orchestration_request(request),
                                               self.token if context is None else context), request)

    def opened(self):
        return self.call('open', identity='open')['current']['snapshot']

    @staticmethod
    def target(snapshot, **values):
        return {'orchestration_handle': snapshot['orchestration_handle'],
                'expected_sequence': snapshot['sequence'], **values}

    def test_default_denial_and_independent_scope_never_open_the_owner(self):
        factory = mock.Mock()
        service = OrchestrationService(PROJECT_A, self.scope.runtime_digest, factory)
        self.addCleanup(service.close)
        request = self.request('open')
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            decode_orchestration_response(service.handle(encode_orchestration_request(request), self.token), request)
        factory.assert_not_called()
        for scope in (replace(self.scope, project_id=PROJECT_B), replace(self.scope, primary_session_id='foreign'),
                      replace(self.scope, supervisor_instance_id='foreign'), replace(self.scope, supervisor_generation=2)):
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                self.call('open', scope=scope)
        self.assertIsNone(self.service._store)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call('open', context={'role': 'host', 'active': True})

    def test_R6_registration_is_recorded_once_and_a_failure_before_open_never_changes(self):
        handle = self.opened()['orchestration_handle']
        run = ('hrn_' + 'a' * 48, 'run-r6', 1)
        self.store.record_registered_run(handle, *run, instance='supervisor', now_ms=self.now)
        self.store.record_registered_run(handle, *run, instance='supervisor', now_ms=self.now + 5)  # a replay: no change
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.record_registered_run(handle, run[0], 'another-run', 1, instance='supervisor', now_ms=self.now)
        self.store.record_run_failed_before_open(run[0], 1, 'preparation_export_failed', now_ms=self.now + 10)
        self.store.record_run_failed_before_open(run[0], 1, 'preparation_export_failed', now_ms=self.now + 20)
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.store.record_run_failed_before_open(run[0], 1, 'preparation_full', now_ms=self.now + 30)
        with self.assertRaisesRegex(HistoryError, 'history_not_found'):
            self.store.record_run_failed_before_open('hrn_' + 'b' * 48, 1, 'preparation_full', now_ms=self.now)
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            self.store.record_run_failed_before_open(run[0], 1, 'free text with a path /x', now_ms=self.now)
        rows = self.store._connection.execute(
            'SELECT orchestration_handle,run_id,attempt,registered_at_ms,failed_reason,failed_at_ms '
            'FROM project_registered_runs').fetchall()
        self.assertEqual([(handle, 'run-r6', 1, self.now, 'preparation_export_failed', self.now + 10)],
                         [tuple(row) for row in rows])

    def r6_report(self, snapshot):
        from codex_agents.project_orchestration_report import orchestration_report
        return orchestration_report(self.store, snapshot['orchestration_id'])['runs']

    def test_R6_9_only_listed_reasons_are_stored(self):
        handle = self.opened()['orchestration_handle']
        run = 'hrn_' + 'c' * 48
        self.store.record_registered_run(handle, run, 'run-c', 1, instance='supervisor', now_ms=self.now)
        for reason in ('customer_private_project_name', 'open_failed_somewhere'):
            with self.subTest(reason=reason), self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
                self.store.record_run_failed_before_open(run, 1, reason, now_ms=self.now)
        for reason in ('preparation_managed_configuration', 'result_capacity_full', 'run_failed', 'service_stopped'):
            with self.subTest(reason=reason):
                self.store._connection.execute('UPDATE project_registered_runs SET failed_reason=NULL')
                self.store.record_run_failed_before_open(run, 1, reason, now_ms=self.now)

    def test_R6_8_an_earlier_services_unresolved_registrations_are_marked_service_stopped(self):
        handle = self.opened()['orchestration_handle']
        runs = ['hrn_' + str(i) * 48 for i in range(1, 4)]
        self.store.record_registered_run(handle, runs[0], 'run-1', 1, instance='earlier', now_ms=self.now)
        self.store.record_registered_run(handle, runs[1], 'run-2', 1, instance='earlier', now_ms=self.now)
        self.store.record_run_failed_before_open(runs[1], 1, 'run_failed', now_ms=self.now)
        self.store.record_registered_run(handle, runs[2], 'run-3', 1, instance='current', now_ms=self.now)
        self.assertEqual(1, self.store.settle_earlier_registrations('current', now_ms=self.now + 7))
        self.assertEqual(0, self.store.settle_earlier_registrations('current', now_ms=self.now + 8))
        rows = dict(self.store._connection.execute('SELECT run_id,failed_reason FROM project_registered_runs'))
        self.assertEqual({'run-1': 'service_stopped', 'run-2': 'run_failed', 'run-3': None}, rows)

    def test_R6_12_registrations_are_bounded_per_orchestration_and_a_replay_at_the_limit_is_kept(self):
        from codex_agents.project_orchestration_report import MAX_RUNS
        handle = self.opened()['orchestration_handle']
        for i in range(MAX_RUNS):
            self.store.record_registered_run(handle, 'hrn_' + format(i, '048x'), f'run-{i}', 1, instance='s',
                                             now_ms=self.now)
        self.store.record_registered_run(handle, 'hrn_' + format(0, '048x'), 'run-0', 1, instance='s', now_ms=self.now)
        with self.assertRaisesRegex(HistoryError, 'history_learning_capacity'):
            self.store.record_registered_run(handle, 'hrn_' + format(MAX_RUNS, '048x'), 'run-x', 1, instance='s',
                                             now_ms=self.now)

    def test_R6_11_attempts_of_one_run_are_combined_the_same_whatever_the_row_order(self):
        snapshot = self.opened()
        handle = snapshot['orchestration_handle']
        a, b = 'hrn_' + 'd' * 48, 'hrn_' + 'e' * 48
        for order in ((1, 2), (2, 1)):
            with self.subTest(order=order):
                self.store._connection.execute('DELETE FROM project_registered_runs')
                for attempt in order:  # run-d: attempt 1 failed, attempt 2 unresolved
                    self.store.record_registered_run(handle, a, 'run-d', attempt, instance='s', now_ms=self.now)
                    self.store.record_registered_run(handle, b, 'run-e', attempt, instance='s', now_ms=self.now)
                self.store.record_run_failed_before_open(a, 1, 'run_failed', now_ms=self.now)
                self.store.record_run_failed_before_open(b, 1, 'run_failed', now_ms=self.now)
                self.store.record_run_failed_before_open(b, 2, 'result_capacity_full', now_ms=self.now)
                runs = self.r6_report(snapshot)
                self.assertEqual((2, 1, 1), (runs['admitted'], runs['pending'], runs['failed_before_open']), runs)
                self.assertEqual({'result_capacity_full': 1}, runs['failed_before_open_reasons'])

    def test_R6_a_populated_schema_16_store_gains_the_table_only_by_explicit_upgrade(self):
        import sqlite3
        from tests.project_schema_fixtures import legacy_registered_run_layout
        self.opened()
        legacy_registered_run_layout(self.store)
        self.store._connection.execute('PRAGMA user_version=16')
        before = self.store._connection.execute('SELECT * FROM project_orchestration_checkpoints').fetchall()
        self.store.close()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.open_history()
        connection = sqlite3.connect(self.directory / 'history.sqlite3')
        connection.execute('CREATE TRIGGER unexpected AFTER INSERT ON project_learning_activation BEGIN SELECT 1; END')
        connection.commit()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            ProjectHistory(self.directory, PROJECT_A, result_key=KEY, allow_schema_upgrade=True,
                           result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                           result_clock_ms=lambda: self.now, orchestration_limits=self.limits)
        connection.execute('DROP TRIGGER unexpected')
        connection.commit()
        connection.close()
        self.store = ProjectHistory(self.directory, PROJECT_A, result_key=KEY, allow_schema_upgrade=True,
                                    result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
                                    result_clock_ms=lambda: self.now, orchestration_limits=self.limits)
        self.assertEqual(17, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, self.store._connection.execute('SELECT * FROM project_orchestration_checkpoints').fetchall())
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])

    def test_R6_13_retiring_an_attempt_removes_its_registration(self):
        handle = self.opened()['orchestration_handle']
        run = 'hrn_' + 'f' * 48
        self.store.record_registered_run(handle, run, 'run-f', 1, instance='s', now_ms=self.now)
        with self.store._transaction():
            self.store._delete_attempt_rows(run, 1, None)
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0])

    def test_R8a_an_orchestration_opened_under_another_scope_says_so(self):
        snapshot = self.opened()
        # After an update the service runs under a new generation; the caller is authorized for it.
        self.scope = replace(self.scope, supervisor_generation=2)
        with self.assertRaisesRegex(HistoryError, '^history_orchestration_other_scope$'):
            self.call('status', {'orchestration_handle': snapshot['orchestration_handle']}, identity='status-later')
        with self.assertRaisesRegex(HistoryError, '^history_orchestration_other_scope$'):
            self.call('close', self.target(snapshot, status='completed', goal_tokens_used=None,
                                           goal_time_used_seconds=None), identity='close-later')

    def test_monotonic_checkpoints_pause_resume_and_known_current_are_distinct(self):
        initial = self.call('open', identity='open')
        opened = initial['current']['snapshot']
        self.now += 1
        checkpoint = self.call('checkpoint', self.target(opened, goal_tokens_used=10, goal_time_used_seconds=2), 'usage')['current']['snapshot']
        paused = self.call('pause', self.target(checkpoint), 'pause')['current']['snapshot']
        self.assertEqual('paused', paused['status'])
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call('resume', self.target(checkpoint), 'stale-resume')
        resumed = self.call('resume', self.target(paused), 'resume')['current']['snapshot']
        self.assertEqual('active', resumed['status'])
        old = self.call('open', identity='open')
        self.assertEqual(initial['receipt'], old['receipt'])
        self.assertEqual(resumed, old['current']['snapshot'])
        self.assertTrue(old['known'])
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call('checkpoint', self.target(resumed, goal_tokens_used=9, goal_time_used_seconds=2), 'decrease')

    def test_close_is_terminal_and_old_active_receipt_cannot_reopen_it(self):
        first = self.call('open', identity='open')
        opened = first['current']['snapshot']
        closed = self.call('close', self.target(opened, status='completed', goal_tokens_used=12, goal_time_used_seconds=3), 'close')
        self.assertEqual('completed', closed['current']['snapshot']['status'])
        old = self.call('open', identity='open')
        self.assertEqual(first['receipt'], old['receipt'])
        self.assertEqual(closed['current'], old['current'])
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.call('resume', self.target(closed['current']['snapshot']), 'reopen')
        status = self.call('status', {'orchestration_handle': opened['orchestration_handle']}, 'read')
        self.assertIsNone(status['receipt'])
        self.assertEqual(closed['current'], status['current'])

    def test_reserved_close_capacity_and_known_reads_survive_quota(self):
        self.store._orchestration_limits = OrchestrationLimits(1, 3)
        opened = self.opened()
        paused = self.call('pause', self.target(opened), 'pause')['current']['snapshot']
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_full'):
            self.call('resume', self.target(paused), 'resume')
        closed = self.call('close', self.target(paused, status='cancelled', goal_tokens_used=None, goal_time_used_seconds=None), 'close')
        self.assertEqual('cancelled', closed['current']['snapshot']['status'])
        self.assertTrue(self.call('open', identity='open')['known'])
        # Corrected for 0.3.4 R6 (owner, 2026-11-18): the limit counts open or paused orchestrations
        # only, so a new one opens once the only open one is closed. 0.8.29 refused it here, because
        # every orchestration ever opened counted (a lifetime cap of 256 per project).
        self.assertEqual('active', self.call('open', identity='new-open')['current']['snapshot']['status'])

    def test_034_R6_the_orchestration_limit_counts_only_open_ones(self):
        self.store._orchestration_limits = OrchestrationLimits(1, 3)
        first = self.opened()
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_full'):
            self.call('open', identity='second')
        self.call('close', self.target(first, status='completed', goal_tokens_used=None,
                                       goal_time_used_seconds=None), 'close')
        second = self.call('open', identity='second')['current']['snapshot']
        self.assertEqual('active', second['status'])
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_full'):
            self.call('open', identity='third')

    def test_034_R1g_a_closed_orchestration_still_named_never_holds_back_a_later_one(self):
        """R1g (re-review 034-security-2): the purge took the first due orchestrations and only then
        skipped the ones a result row still names, so one named for good starved every later one."""
        from codex_agents.orchestration_store import ORCHESTRATION_RETENTION_MS
        self.store._orchestration_limits = OrchestrationLimits(2, 12)
        handles = []
        for identity in ('first', 'second'):
            opened = self.call('open', identity='open-' + identity)['current']['snapshot']
            self.call('close', self.target(opened, status='completed', goal_tokens_used=None,
                                           goal_time_used_seconds=None), 'close-' + identity)
            handles.append(opened['orchestration_handle'])
        # A result row of the first stays (say, an attempt the account never released).
        self.store._connection.execute(
            "INSERT INTO result_reservations(project_id,claim_id,run_handle,attempt,claim_digest,orchestration_handle,"
            "binding,assignment_digest,created_at_ms,hard_deadline_at_ms,reserved_until_ms,expires_at_ms,state) "
            "VALUES(?,'clm_x','hrn_x',1,?,?,x'00','a',0,0,0,0,'expired')", (PROJECT_A, 'c' * 64, handles[0]))
        self.now += ORCHESTRATION_RETENTION_MS + 1
        owner = self.store._orchestration_owner()
        for _ in range(3):
            owner.purge_closed(self.now, 1)

        def kept(handle):
            return self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations WHERE orchestration_handle=?',
                                                  (handle,)).fetchone()[0]
        self.assertEqual((1, 0), (kept(handles[0]), kept(handles[1])))

    def test_034_R10_an_update_ends_the_earlier_runtimes_open_orchestrations(self):
        """R10 (live 2026-11-19): after an update, nothing could close an orchestration opened under the
        earlier runtime, and it counted as open for good."""
        from dataclasses import replace
        first = self.opened()
        paused = self.call('open', identity='second')['current']['snapshot']
        self.call('pause', self.target(paused), 'pause-second')
        updated = replace(self.scope, runtime_digest='b' * 64)
        other_instance = replace(self.scope, supervisor_instance_id='another', runtime_digest='b' * 64)
        owner = self.store._orchestration_owner()
        self.assertEqual(2, owner._open_count())
        self.assertEqual(0, owner.end_earlier(other_instance, 10))  # Not this service's journals.
        self.assertEqual(0, owner.end_earlier(self.scope, 10))      # Its own scope: nothing earlier.
        self.assertEqual(2, owner.end_earlier(updated, 10))
        self.assertEqual(0, owner._open_count())
        for snapshot in (first, paused):
            ended = owner._current(self.scope, snapshot['orchestration_handle'])['snapshot']
            self.assertEqual('interrupted', ended['status'])
            self.assertIsNotNone(ended['ended_at_ms'])
        self.assertEqual(0, owner.end_earlier(updated, 10))  # Already ended: once only.

    def test_034_R10_a_used_request_id_never_stops_the_end(self):
        """Review 034-r10: the end used the predictable request id 'end-earlier-<handle>'; one already
        used by an old client made every attempt conflict, so the orchestration stayed open for good."""
        from dataclasses import replace
        opened = self.opened()
        handle = opened['orchestration_handle']
        self.call('checkpoint', self.target(opened, goal_tokens_used=0, goal_time_used_seconds=0),
                  'end-earlier-' + handle)
        owner = self.store._orchestration_owner()
        self.assertEqual(1, owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 10))
        self.assertEqual('interrupted', owner._current(self.scope, handle)['snapshot']['status'])

    def test_034_R10_each_pass_tries_at_most_its_limit(self):
        """Review 034-r10: only successful ends counted, so orchestrations kept open by a run not yet
        over were all retried on every pass."""
        from dataclasses import replace
        from unittest import mock
        from codex_agents import orchestration_store
        self.store._orchestration_limits = OrchestrationLimits(4, 12)
        for identity in ('a', 'b', 'c'):
            self.call('open', identity='open-' + identity)
        tries = []

        def pending(owner, request, **options):
            tries.append(request.payload['orchestration_handle'])
            raise HistoryError('history_orchestration_pending')
        owner = self.store._orchestration_owner()
        with mock.patch.object(orchestration_store.OrchestrationOwner, 'apply', pending):
            for _ in range(3):
                self.assertEqual(0, owner.end_earlier(replace(self.scope, runtime_digest='b' * 64), 1))
        self.assertEqual(3, len(tries))
        self.assertEqual(3, len(set(tries)))  # Each pass went on to the next one.

    def test_034_R6_a_closed_orchestration_is_removed_after_its_retention(self):
        from codex_agents.orchestration_store import ORCHESTRATION_RETENTION_MS
        first = self.opened()
        self.call('close', self.target(first, status='completed', goal_tokens_used=None,
                                       goal_time_used_seconds=None), 'close')
        handle = first['orchestration_handle']

        def kept():
            return tuple(self.store._connection.execute(f'SELECT COUNT(*) FROM {table} WHERE orchestration_handle=?',
                                                        (handle,)).fetchone()[0]
                         for table in ('project_orchestrations', 'project_orchestration_checkpoints'))
        before = kept()
        self.assertGreater(before[1], 0)
        self.now += ORCHESTRATION_RETENTION_MS  # Not yet past it.
        self.store.expire_results(limit=10)
        self.assertEqual(before, kept())
        self.now += 1
        self.store.expire_results(limit=10)
        self.assertEqual((0, 0), kept())

    def test_transaction_rollback_and_lost_reply_reconcile_one_checkpoint(self):
        original = self.store._transaction

        @contextmanager
        def rollback():
            with original():
                yield
                raise OSError('PRIVATE-ROLLBACK')

        with mock.patch.object(self.store, '_transaction', new=rollback), self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.call('open', identity='open')
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError('PRIVATE-LOST-REPLY')

        with mock.patch.object(self.store, '_transaction', new=lose_reply), self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.call('open', identity='open')
        self.service.close()
        self.store = self.open_history()
        self.service = self.make_service()
        with mock.patch.object(self.store, '_result_now', side_effect=AssertionError('clock refreshed')):
            known = self.call('open', identity='open')
        self.assertTrue(known['known'])
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestration_checkpoints').fetchone()[0])

    def lease_binding(self, snapshot, number=1):
        return ResultLeaseBinding(**self.scope.as_data(), orchestration_handle=snapshot['orchestration_handle'],
                                  run_handle='hrn_' + f'{number:048x}', run_id='run:' + f'{number:064x}', attempt=1,
                                  claim_request_id=f'claim-{number}', claim_request_digest='e' * 64)

    def test_actual_managed_state_guards_new_work_and_close_refuses_uncertainty(self):
        opened = self.opened()
        binding = self.lease_binding(opened)
        self.store.reserve_result_lease(binding, assignment())
        paused = self.call('pause', self.target(opened), 'pause')['current']['snapshot']
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.store.arm_result_lease(binding)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.store.reserve_result_lease(self.lease_binding(opened, 2), assignment())
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_pending'):
            self.call('close', self.target(paused, status='cancelled', goal_tokens_used=None, goal_time_used_seconds=None), 'close')
        resumed = self.call('resume', self.target(paused), 'resume')['current']['snapshot']
        self.store.arm_result_lease(binding)
        paused = self.call('pause', self.target(resumed), 'pause-again')['current']['snapshot']
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_state'):
            self.store.attest_result_lease(binding)
        self.assertIsInstance(self.store.attest_pending_result_lease(binding), bytes)
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_pending'):
            self.call('close', self.target(paused, status='cancelled', goal_tokens_used=None, goal_time_used_seconds=None), 'close-pending')

    def test_unknown_orchestration_has_no_implicit_legacy_authority(self):
        snapshot = {'orchestration_handle': 'orc_' + '7' * 48}
        with self.assertRaisesRegex(HistoryError, 'history_orchestration_unavailable'):
            self.store.reserve_result_lease(self.lease_binding(snapshot), assignment())

    def test_revocation_before_commit_creates_no_authority(self):
        self.revoke = True
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            self.call('open', identity='open')
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])

    def test_wrong_sequence_or_changed_parameters_never_reuse_a_request_identity(self):
        opened = self.opened()
        first = self.call('checkpoint', self.target(opened, goal_tokens_used=2, goal_time_used_seconds=1), 'usage')
        self.assertTrue(self.call('checkpoint', self.target(opened, goal_tokens_used=2, goal_time_used_seconds=1), 'usage')['known'])
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            self.call('checkpoint', self.target(opened, goal_tokens_used=3, goal_time_used_seconds=1), 'usage')
        self.assertEqual(first['current'], self.call('status', {'orchestration_handle': opened['orchestration_handle']}, 'status')['current'])

    def test_adapter_mutation_or_exception_never_commits(self):
        opened = self.opened()
        payload = self.target(opened, goal_tokens_used=2, goal_time_used_seconds=1)
        request = self.request('checkpoint', payload, 'checkpoint')
        payload['goal_tokens_used'] = 99
        self.assertEqual(2, request.payload['goal_tokens_used'])

        def mutate(command, context):
            authorization = self.authorize(command, context)
            command.payload['goal_tokens_used'] = 99
            return authorization

        for authorizer in (mutate, mock.Mock(side_effect=RuntimeError('PRIVATE-ADAPTER'))):
            with self.subTest(authorizer=authorizer), mock.patch.object(self.service, '_authorizer', authorizer):
                raw = self.service.handle(encode_orchestration_request(request), self.token)
                with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                    decode_orchestration_response(raw, request)
                self.assertNotIn(b'PRIVATE', raw)
            self.assertEqual(1, self.store._connection.execute(
                'SELECT COUNT(*) FROM project_orchestration_checkpoints').fetchone()[0])

    def test_changed_current_or_receipt_cannot_substitute_for_committed_identity(self):
        opened = self.opened()
        pause = self.request('pause', self.target(opened), 'pause')
        self.service.handle(encode_orchestration_request(pause), self.token)
        self.call('open', identity='open')
        raw = self.service.handle(encode_orchestration_request(pause), self.token)
        valid = json.loads(raw)
        decode_orchestration_response(raw, pause)
        mutations = (
            ('current', 'supervisor_generation', True),
            ('current', 'supervisor_generation', 1.0),
            ('receipt', 'primary_session_id', 'foreign'),
        )
        for target, field, value in mutations:
            altered = deepcopy(valid)
            altered['result'][target]['snapshot']['scope'][field] = value
            with self.subTest(target=target, field=field, value=value), self.assertRaisesRegex(
                    HistoryError, 'history_invalid_response'):
                decode_orchestration_response(json.dumps(altered).encode(), pause)
        altered = deepcopy(valid)
        altered['result']['current']['snapshot']['status'] = 'active'
        with self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
            decode_orchestration_response(json.dumps(altered).encode(), pause)

    def test_lost_client_response_never_retries_and_explicit_identity_reconciles(self):
        request = self.request('open', identity='explicit-open')
        client = OrchestrationClient(self.directory / 'owner.sock', self.scope, timeout=1)

        def lost(endpoint, raw, **limits):
            self.assertEqual(16_384, limits['request_limit'])
            self.assertEqual(16_384, limits['response_limit'])
            self.service.handle(raw, self.token)
            raise ServiceTransportError('service_unavailable')

        with mock.patch('codex_agents.orchestration_api.exchange_frame', side_effect=lost) as exchange:
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                client.call('orchestration.open', {}, request_id=request.request_id)
            self.assertEqual(1, exchange.call_count)
        result = decode_orchestration_response(self.service.handle(encode_orchestration_request(request), self.token), request)
        self.assertTrue(result['known'])
        self.assertEqual(1, self.store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])

    def test_wrong_thread_and_closed_service_never_authorize_or_open(self):
        request = self.request('open')
        raw = encode_orchestration_request(request)
        responses = []
        thread = threading.Thread(target=lambda: responses.append(self.service.handle(raw, self.token)))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(0, self.authorizations)
        with self.assertRaisesRegex(HistoryError, 'history_wrong_owner'):
            decode_orchestration_response(responses[0], request)
        self.service.close()
        self.service.close()
        with self.assertRaisesRegex(HistoryError, 'history_wrong_owner'):
            decode_orchestration_response(self.service.handle(raw, self.token), request)
        self.assertEqual(0, self.authorizations)

    def test_history_composition_shares_one_writer_with_separate_authorization(self):
        from codex_agents.history_api import decode_history_response, encode_history_request
        from codex_agents.history_service import HistoryService
        from tests.test_history_api import fixture_authorizer, FIXTURE_CONTEXT, request as history_request

        factory = mock.Mock(return_value=self.store)
        service = HistoryService(PROJECT_A, self.scope.runtime_digest, factory,
                                 authorizer=fixture_authorizer, orchestration_authorizer=self.authorize,
                                 clock_ms=lambda: self.now)
        self.addCleanup(service.close)
        opening = self.request('open', identity='shared-open')
        raw = encode_orchestration_request(opening)
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            decode_orchestration_response(service.handle(raw, FIXTURE_CONTEXT), opening)
        factory.assert_not_called()
        opened = decode_orchestration_response(service.handle(raw, self.token), opening)
        self.assertEqual('active', opened['current']['snapshot']['status'])
        event = history_request()
        recorded = decode_history_response(service.handle(encode_history_request(event), FIXTURE_CONTEXT), event)
        self.assertEqual(1, recorded['source_sequence'])
        factory.assert_called_once_with()
        self.assertIs(service._store, service._orchestrations._store)

    def test_missing_checkpoint_in_committed_chain_fails_closed(self):
        opened = self.opened()
        self.call('checkpoint', self.target(opened, goal_tokens_used=2, goal_time_used_seconds=1), 'usage')
        # Simulated storage damage: current pointer alone cannot prove the chain.
        self.store._connection.execute('PRAGMA foreign_keys=OFF')
        self.store._connection.execute('DELETE FROM project_orchestration_checkpoints WHERE sequence=1')
        self.store._connection.execute('PRAGMA foreign_keys=ON')
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.call('status', {'orchestration_handle': opened['orchestration_handle']}, 'damaged-status')

    def test_stale_pointer_cannot_restore_active_state_after_close(self):
        first = self.call('open', identity='open')['current']
        opened = first['snapshot']
        self.call('close', self.target(opened, status='completed', goal_tokens_used=None, goal_time_used_seconds=None), 'close')
        self.store._connection.execute('UPDATE project_orchestrations SET sequence=1,checkpoint_digest=?',
                                        (first['checkpoint_digest'],))
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.store.reserve_result_lease(self.lease_binding(opened), assignment())

    def test_damaged_request_journal_cannot_rebind_a_sealed_checkpoint(self):
        opened = self.opened()
        command = self.request('pause', self.target(opened), 'pause')
        self.service.handle(encode_orchestration_request(command), self.token)
        changed = self.request('checkpoint', self.target(opened, goal_tokens_used=1, goal_time_used_seconds=1), 'pause')
        self.store._connection.execute('UPDATE project_orchestration_requests SET request_digest=? WHERE sequence=2',
                                        (orchestration_request_digest(changed),))
        with self.assertRaisesRegex(HistoryError, 'history_conflict'):
            decode_orchestration_response(self.service.handle(encode_orchestration_request(changed), self.token), changed)

    def test_client_import_and_unavailable_endpoint_never_load_persistence(self):
        program = r'''
import importlib.abc,json,sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name in {'sqlite3','_sqlite3','codex_agents.project_history','codex_agents.learning_store',
              'codex_agents.orchestration_store','codex_agents.orchestration_service'}:
   raise AssertionError('persistence import')
sys.meta_path.insert(0,Guard())
from codex_agents.orchestration_api import OrchestrationClient,OrchestrationScope
from codex_agents.history_contracts import HistoryError
scope=OrchestrationScope(**json.loads(sys.argv[2]))
try: OrchestrationClient(Path(sys.argv[1]),scope,timeout=0.01).call('orchestration.open',{},request_id='explicit')
except HistoryError as exc: print(json.dumps({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules}))
else: raise SystemExit(1)
'''
        completed = subprocess.run(
            [sys.executable, '-I', '-B', '-S', '-c',
             f'import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});' + program,
             str(self.directory / 'missing.sock'), json.dumps(self.scope.as_data())],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({'reason': 'history_service_unavailable', 'sqlite_loaded': False}, json.loads(completed.stdout))

    def test_closure_during_initial_authorization_never_opens_storage(self):
        factory = mock.Mock(return_value=self.store)

        def authorize(command, context):
            authorization = self.authorize(command, context)
            service.close()
            return authorization

        service = OrchestrationService(PROJECT_A, self.scope.runtime_digest, factory, authorizer=authorize,
                                       close_history=False)
        self.addCleanup(service.close)
        command = self.request('open', identity='closed-during-authorization')
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            decode_orchestration_response(service.handle(encode_orchestration_request(command), self.token), command)
        factory.assert_not_called()

    def test_closure_during_final_authorization_rolls_back_and_refuses_metadata(self):
        opened = self.opened()
        commands = (
            self.request('checkpoint', self.target(opened, goal_tokens_used=1, goal_time_used_seconds=1), 'usage'),
            self.request('status', {'orchestration_handle': opened['orchestration_handle']}, 'status'),
            self.request('open', identity='open'),
        )
        for command in commands:
            calls = []

            def authorize(request, context):
                authorization = self.authorize(request, context)
                calls.append(self.store._connection.in_transaction)
                if len(calls) == 2:
                    service.close()
                return authorization

            service = OrchestrationService(PROJECT_A, self.scope.runtime_digest, lambda: self.store,
                                           authorizer=authorize, close_history=False)
            self.addCleanup(service.close)
            with self.subTest(operation=command.operation), self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                decode_orchestration_response(service.handle(encode_orchestration_request(command), self.token), command)
            self.assertEqual([False, command.operation == 'orchestration.checkpoint'], calls)
            self.assertEqual(1, self.store._connection.execute(
                'SELECT COUNT(*) FROM project_orchestration_checkpoints').fetchone()[0])
