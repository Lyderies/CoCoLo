"""Original open and managed lifecycle; native/provider enrollment is synthetic."""
from dataclasses import replace
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_open_scope as fixtures

from codex_agents.account_open_contracts import AccountOpenContext, AccountOpenInput, OpenError, OpenOwnerLimits
from codex_agents.account_original_open import preflight_open
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.claim_denial import CloseDeniedInput
from codex_agents.execution_grant import ExecutionRegistrationInput, checked_execution_grant
from codex_agents.history_contracts import AttemptBinding
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultReference
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultObservation
from codex_agents.run_provenance import RunProvenanceInput
from codex_agents.terminal_delivery import TerminalSubmissionInput
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_service import AccountOwnerViews
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents import account_api
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request
from codex_agents.service_transport import ServiceTransportError
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.learning_contracts import canonical_json, content_digest, parse_bounded_json
from codex_agents.sql_read_budget import SQLReadLimits


class OriginalAccountOpenPendingTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectOpenScopeTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.ledger = self.base.ledger
        self.limits = OpenOwnerLimits(4, 1_048_576)  # OP-1: no cap on finished requests (0.8.38 kept 16 here)
        self.work = SQLReadLimits(100_000, 1000, 2*1_048_576)
        self.refresh_context()

    def refresh_context(self):
        self.context = AccountOpenContext(self.base.context, self.base.call())
        p = self.base.payload()
        self.request = AccountOpenInput(self.base.scope, p['orchestration_handle'], p['inputs_digest'])

    def preflight(self, **changes):
        options = dict(runtime_digest=self.base.scope.runtime_digest, caller_scope='a'*64,
            request_id='original-account-open', request=self.request, context=self.context, limits=self.limits,
            work_limits=self.work, before_commit=lambda: None)
        options.update(changes)
        return preflight_open(self.ledger, self.base.scope.project_id, **options)

    def pending(self):
        return [dict(row) for row in self.ledger._connection.execute('SELECT * FROM account_open_requests')]

    def test_original_pending_clock_is_durable_before_signatures_without_opening_a_run(self):
        before = {table: self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('host_run_bindings', 'orchestrations', 'records', 'launch_inputs', 'active_run_capacity')}
        start = self.ledger._clock_ms()
        with mock.patch.object(self.ledger, '_clock_ms', return_value=start):
            captured, known = self.preflight()
        self.assertIsNone(known)
        row = self.pending()[0]
        self.assertEqual('pending', row['state'])
        self.assertEqual(start, row['opened_at_ms'])
        self.assertEqual(captured.observation_deadline_at_ms, row['observation_deadline_at_ms'])
        self.assertIsNone(row['receipt_digest'])
        self.assertEqual(before, {table: self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in before})
        self.assertNotIn(captured.run_handle.encode(), canonical_json(row))
        self.assertNotIn(b'objective', canonical_json(row))
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('original clock refreshed')):
            repeated, known = self.preflight()
        self.assertEqual(captured, repeated)
        self.assertIsNone(known)
        self.assertEqual(1, len(self.pending()))

    def test_other_request_or_assignment_labels_cannot_repeat_the_original_task_attempt(self):
        self.preflight()
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('new window')):
            with self.assertRaisesRegex(OpenError, 'open_conflict'):
                self.preflight(request_id='another-request')
        self.base.assignment = {**self.base.assignment, 'assignment_id': 'another-label'}
        self.base.context = replace(self.base.context, assignment=self.base.assignment)
        self.refresh_context()
        with self.assertRaisesRegex(OpenError, 'open_conflict'):
            self.preflight(request_id='another-assignment-label')
        self.assertEqual(1, len(self.pending()))

    def test_changed_original_inputs_or_full_worker_context_conflict_without_new_clock(self):
        self.preflight()
        self.base.assignment = {**self.base.assignment, 'objective': 'different original input'}
        self.base.context = replace(self.base.context, assignment=self.base.assignment)
        self.refresh_context()
        with (mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('new clock')),
                self.assertRaisesRegex(OpenError, 'open_conflict')):
            self.preflight()
        self.assertEqual(1, len(self.pending()))

    def test_project_proof_does_not_cover_a_substituted_full_worker_input(self):
        worker = self.context.prospective.worker
        changed = replace(worker, assignment={**worker.assignment, 'objective': 'substituted worker original'})
        context = replace(self.context, prospective=replace(self.context.prospective, worker=changed))
        with self.assertRaisesRegex(OpenError, 'open_not_authorized'):
            self.preflight(context=context)
        self.assertEqual([], self.pending())

    def test_mutable_server_input_cannot_change_captured_original_bytes_during_intake(self):
        original = self.context.prospective.assignment['objective']
        def change():
            self.context.prospective.assignment['objective'] = 'changed server data'
        captured, _ = self.preflight(before_commit=change)
        data = parse_bounded_json(captured.context_bytes, max_bytes=self.limits.max_context_bytes)
        self.assertEqual(original, data['prospective']['assignment']['objective'])
        # Current authorization must reject that changed context on publication;
        # the persistence primitive itself never adopts its mutable replacement.
        with self.assertRaisesRegex(OpenError, 'open_not_authorized'):
            self.preflight()

    def test_late_revocation_after_pending_write_rolls_back_request_identity(self):
        calls = []
        def revoke():
            calls.append(True)
            if len(calls) == 2:
                raise OpenError('open_not_authorized')
        with self.assertRaisesRegex(OpenError, 'open_not_authorized'):
            self.preflight(before_commit=revoke)
        self.assertEqual([], self.pending())

    def test_last_authorizer_cannot_remove_original_pending_journal(self):
        calls = []
        def remove():
            calls.append(True)
            if len(calls) == 2:
                self.ledger._connection.execute('DELETE FROM account_open_requests')
        with self.assertRaisesRegex(OpenError, 'open_conflict'):
            self.preflight(before_commit=remove)
        self.assertEqual([], self.pending())

    def test_tampered_original_window_is_refused_without_resealing_or_refresh(self):
        captured, _ = self.preflight()
        self.ledger._connection.execute('UPDATE account_open_requests SET observation_deadline_at_ms=observation_deadline_at_ms+1')
        # S1-U10e: a stored journal whose seal no longer verifies is inconsistent, not a conflicting request.
        with (mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('new clock')),
                self.assertRaisesRegex(OpenError, 'open_state_inconsistent')):
            self.preflight()
        self.assertEqual(captured.observation_deadline_at_ms+1, self.pending()[0]['observation_deadline_at_ms'])

    def test_retained_and_pending_caps_preserve_existing_identity(self):
        # Corrected for OP-1 and OP-4 (requirement): the cap on every request kept (a lifetime cap) is gone, and
        # the in-flight cap's refusal names itself (open_capacity_full, not open_busy).
        self.preflight()
        self.base.task = {**self.base.task, 'id': 'independent-second-verifier'}
        self.base.route = {**self.base.route, 'task_id': self.base.task['id']}
        self.base.assignment = {**self.base.assignment, 'task_id': self.base.task['id'], 'assignment_id': 'second-verifier'}
        self.base.context = replace(self.base.context, task=self.base.task, route=self.base.route, assignment=self.base.assignment)
        self.refresh_context()
        with self.assertRaisesRegex(OpenError, 'open_capacity_full'):
            self.preflight(request_id='second-open', limits=OpenOwnerLimits(1, 1_048_576))
        self.assertEqual(1, len(self.pending()))

    def test_native_context_and_copied_byte_quota_refuse_before_clock_or_request_write(self):
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('clock before authority')):
            with self.assertRaisesRegex(OpenError, 'open_invalid_request'):
                self.preflight(context=None)
            with self.assertRaisesRegex(OpenError, 'open_busy'):
                self.preflight(limits=OpenOwnerLimits(1, 1))  # OP-1: the class has no retained cap
        self.assertEqual([], self.pending())

@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())), 'fixed macOS signature worker guard')
class OriginalAccountOpenJobTests(unittest.TestCase):
    def setUp(self):
        self.reference = OriginalAccountOpenPendingTests(methodName='runTest')
        self.reference.setUp()
        self.addCleanup(self.reference.doCleanups)
        self.ledger = self.reference.ledger
        self.jobs = AccountClaimJobs(AccountOwnerViews(self.ledger), SignatureIntakeLimits(2, 2, 512, 262_144, 2),
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
            self.reference.work, open_limits=self.reference.limits)
        self.jobs.bootstrap()
        self.addCleanup(self.drain)

    def drain(self):
        self.jobs.request_close()
        started = time.monotonic()
        while not self.jobs.drain():
            self.assertLess(time.monotonic()-started, 10)
            time.sleep(.002)

    def submit(self, **changes):
        p = self.reference
        options = dict(runtime_digest=p.base.scope.runtime_digest, caller_scope='a'*64, request_id='original-account-open',
            request=p.request, context=p.context, before_commit=lambda: None)
        options.update(changes)
        return self.jobs.submit_open(p.base.scope.project_id, **options)

    def pump(self, ticket):
        started = time.monotonic()
        while True:
            self.jobs.drive()
            reply = self.jobs.take(ticket)
            if reply is not None:
                return reply
            self.assertLess(time.monotonic()-started, 10, 'original open signature worker did not finish')
            time.sleep(.002)

    def another_task(self, name):
        base = self.reference.base
        base.task = {**base.task, 'id': name}
        base.route = {**base.route, 'task_id': name}
        base.assignment = {**base.assignment, 'task_id': name, 'assignment_id': name + '-assignment'}
        base.context = replace(base.context, task=base.task, route=base.route, assignment=base.assignment)
        self.reference.refresh_context()

    def test_B0_1_an_expired_pending_binding_does_not_count_against_the_binding_cap(self):
        """O0b B0-1 (narrowed): the cap on pending bindings, checked when an open finishes, counts only live
        ones (pending and not past expiry), so expired bindings never fill a project for good. Corrected
        before code: the first version of this test hit the preflight request cap instead."""
        from codex_agents import learning_store
        held = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM host_run_bindings WHERE binding_state='pending'").fetchone()[0]  # the fixture's own
        self.enterContext(mock.patch.object(learning_store, 'MAX_PENDING_HOST_RUNS', held + 1))
        self.assertIsNone(self.pump(self.submit()).reason)  # the cap's last live place
        self.another_task('second-verifier')
        refused = self.pump(self.submit(request_id='second-open'))
        # Corrected for OP-4 (requirement): live bindings at their limit is a full cap, not a busy moment.
        self.assertEqual('open_capacity_full', refused.reason)  # control: a live binding still counts
        self.ledger._connection.execute("UPDATE host_run_bindings SET expires_at_ms=1 WHERE binding_state='pending'")
        self.another_task('third-verifier')
        self.assertIsNone(self.pump(self.submit(request_id='third-open')).reason)  # the expired one does not

    def test_RS_A_G12_the_binding_cap_reads_held_register_rows(self):
        """O0b RS-A round 2 G12: by query plan, the cap counts from held register rows (run_holds_due), so a clock
        rollback over released claimed history is a bounded lookup, never a scan of pending source bindings."""
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            self.assertIsNone(self.pump(self.submit()).reason)
        finally:
            self.ledger._connection.set_trace_callback(None)
        # The cap's statement, found by what it reads, not by its FROM clause (corrected with the code: the first
        # filter named the old statement's 'FROM host_run_bindings h'; the requirement, the plan, is unchanged).
        cap = [s for s in statements if 'host_run_bindings h' in s and "binding_state='pending'" in s
               and 'expires_at_ms>' in s and s.lstrip().startswith('SELECT')]
        self.assertEqual(1, len(cap), statements)
        plan = ' | '.join(r[3] for r in self.ledger._connection.execute('EXPLAIN QUERY PLAN ' + cap[0]))
        self.assertIn('run_holds_due', plan)
        self.assertNotIn('host_run_bindings_live', plan)

    def test_actual_registered_signature_return_commits_minimal_original_open_without_central_history_copy(self):
        before = {table: self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('orchestrations', 'orchestration_checkpoints', 'active_run_capacity', 'capacity_executions')}
        starts = self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','verification_binding')").fetchone()[0]
        with (mock.patch.object(self.ledger, '_orchestration_for_handle_locked', side_effect=AssertionError('central lifecycle lookup')),
              mock.patch.object(self.ledger, 'verification_worker_result', side_effect=AssertionError('central result lookup'))):
            reply = self.pump(self.submit())
        self.assertIsNone(reply.reason)
        self.assertEqual('account.open_host_run', reply.operation)
        self.assertEqual('opened', reply.result['state'])
        self.assertFalse(reply.result['known'])
        row = self.ledger._connection.execute("SELECT * FROM host_run_bindings WHERE history_owner='project'").fetchone()
        self.assertEqual(reply.result['run_id'], row['run_id'])
        receipt = self.ledger._connection.execute('SELECT content FROM account_open_receipts').fetchone()[0]
        data = parse_bounded_json(bytes(receipt))
        self.assertEqual(self.reference.base.worker.digest, data['verification_binding']['worker_outcome_digest'])
        self.assertEqual(row['created_at_ms'], data['start_event']['observed_at_ms'])
        self.assertNotIn(reply.result['run_handle'].encode(), bytes(receipt))
        self.assertEqual(starts, self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','verification_binding')").fetchone()[0])
        self.assertEqual(before, {table: self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before})
        with (mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known clock')),
              mock.patch.object(self.ledger, '_prepare_attestations', side_effect=AssertionError('synchronous signatures')),
              mock.patch.object(self.jobs._intake, 'capture_admission', side_effect=AssertionError('new signature work'))):
            known = self.jobs.take(self.submit())
        self.assertEqual({**reply.result, 'known': True}, known.result)

    def wire_service(self, *, configured=True, authorizer=True, open_context=True):
        self.enabled = True
        self.token = object()
        def authorize(request, token):
            if token is not self.token or not self.enabled:
                return None
            return AccountAuthorization(request.project_id, request.runtime_digest, account_request_digest(request),
                'host', frozenset({'account.open_host_run'}), 'a'*64,
                open_context=self.reference.context if open_context else None)
        service = AccountService(self.reference.base.scope.runtime_digest, self.jobs._views,
            authorizer=authorize if authorizer else None, claim_jobs=self.jobs if configured else None)
        self.addCleanup(service.close)
        return service, authorize

    def wire_command(self):
        p = self.reference
        return AccountRequest(1, 'account.open_host_run', 'original-account-open', p.base.scope.project_id,
            p.base.scope.runtime_digest, p.request.payload())

    def pump_wire(self, service, ticket):
        if type(ticket) is bytes:
            return ticket
        started = time.monotonic()
        while True:
            raw = service.take_claim_reply(ticket)
            if raw is not None:
                return raw
            service.drive_claims()
            self.assertLess(time.monotonic()-started, 10)
            time.sleep(.002)

    def managed_lifecycle(self):
        p = self.reference
        original = p.base.worker.fixture
        wall = [original.base.stamp]
        self.enterContext(mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: wall[0]))
        self.enterContext(mock.patch.object(p.base.store, '_result_clock', side_effect=lambda: wall[0]))
        # The real project journal and original worker body remain. A managed
        # account run must not need the legacy central orchestration at all.
        self.ledger._connection.execute('DELETE FROM orchestrations')
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM orchestrations').fetchone()[0])
        state = {'scope': None, 'evidence': None, 'terminal': None}
        token = object()
        operations = frozenset({'account.open_host_run', 'account.claim_launch', 'account.open_provenance',
            'account.register_execution', 'account.run_provenance', 'account.submit_terminal', 'account.close_denied_attempt'})
        def authorize(request, supplied):
            if supplied is not token:
                return None
            source = 'host' if request.operation in {'account.open_host_run', 'account.claim_launch'} else 'project'
            return AccountAuthorization(request.project_id, request.runtime_digest, account_request_digest(request),
                source, operations, 'a'*64, state['scope'], state['evidence'], state['terminal'], p.context)
        service = AccountService(p.base.scope.runtime_digest, self.jobs._views, authorizer=authorize,
            claim_jobs=self.jobs, run_provenance_work_limits=p.work, open_provenance_work_limits=p.work)
        self.addCleanup(service.close)
        def exchange(path, raw, **options):
            ticket = service.begin(raw, token)
            if account_api.decode_account_request(raw).operation == 'account.open_host_run':
                wall[0] += 50  # Signature work consumes the original window.
            return self.pump_wire(service, ticket)
        self.enterContext(mock.patch.object(account_api, 'exchange_frame', side_effect=exchange))
        client = AccountClient(Path('/private/tmp/original-open-lifecycle.sock'), p.base.scope.project_id,
            p.base.scope.runtime_digest, timeout=1)
        opened = client.open_host_run(p.request, request_id='original-account-open')
        self.assertEqual('opened', opened['state'])
        self.assertEqual(original.base.stamp, opened['recorded_at_ms'])
        identity = self.ledger.observe_provider(p.base.scope.project_id, 'codex', project_root=p.base.bridge.tools,
            environ={}, run_handle=opened['run_handle'], phase='before')[0]
        launch = LaunchClaimInput(opened['run_handle'], content_digest(identity))
        claim = AccountRequest(1, 'account.claim_launch', 'managed-original-claim', p.base.scope.project_id,
            p.base.scope.runtime_digest, launch.payload())
        scope = replace(original.scope, run_handle=opened['run_handle'], run_id=opened['run_id'],
            claim_request_id=claim.request_id, claim_request_digest=account_request_digest(claim))
        p.base.store.reserve_result_lease(scope, p.base.assignment)
        p.base.store.arm_result_lease(scope)
        state.update(scope=scope, evidence=p.base.store.attest_pending_result_lease(scope))
        return client, state, launch

    def test_managed_api_open_claim_registration_and_return_need_no_central_orchestration_or_history_copy(self):
        client, state, launch = self.managed_lifecycle()
        p, scope = self.reference, state['scope']
        before = self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','verification_binding','host_outcome')").fetchone()[0]
        request = OpenProvenanceInput(scope.run_handle, p.base.assignment['assignment_id'], p.base.worker.digest)
        proof = client.open_provenance(request, request_id='managed-open-proof')
        p.base.store.record_host_open(scope, request, proof, assignment=p.base.assignment,
            task=p.base.task, route=p.base.route, before_commit=lambda: None)
        receipt = client.claim_launch(launch.run_handle, launch.identity_digest, request_id=scope.claim_request_id)
        self.assertEqual('claimed', receipt['state'])
        binding = AttemptBinding(**{n: getattr(scope, n) for n in LEASE_SCOPE_FIELDS},
            claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
        p.base.store.bind_result_lease(scope, binding, p.base.assignment, hard_deadline_at_ms=receipt['deadline_at_ms'])
        state['evidence'] = p.base.store.attest_bound_result_lease(scope)
        registration = ExecutionRegistrationInput(scope.run_handle, binding.claim_id, binding.claim_digest,
            scope.claim_request_id, launch.identity_digest)
        grant = checked_execution_grant(client.register_execution(**registration.payload(), request_id='managed-register')['grant'])
        provenance = client.run_provenance(RunProvenanceInput(registration, grant.registration_digest,
            p.base.assignment['assignment_id']), request_id='managed-run-proof')
        p.base.store.consume_execution_grant(binding, grant.encode(), task=p.base.task, route=p.base.route, provenance=provenance)
        from tests.test_bridge import result
        body = canonical_json({**result(), 'assignment_id': p.base.assignment['assignment_id'], 'summary': 'PRIVATE-MANAGED-RETURN'})
        delivery = p.base.store.publish_result(binding, p.base.assignment, body,
            ResultObservation(binding.claim_id, p.base.store._result_clock(), False, True, None))
        state['terminal'] = p.base.store.attest_terminal_delivery(binding)
        submission = TerminalSubmissionInput(registration, grant.registration_digest, delivery.record_id,
            delivery.record_content_digest, delivery.result_digest)
        terminal = client.submit_terminal(submission, request_id='managed-terminal')
        self.assertFalse(terminal['known'])
        self.assertTrue(client.submit_terminal(submission, request_id='managed-terminal')['known'])
        admitted = p.base.store.admit_host_result(HostResultReference(binding, delivery.record_id,
            delivery.record_content_digest, delivery.result_digest), assignment=p.base.assignment,
            task=p.base.task, route=p.base.route, client_capability=p.base.bridge.clients['bridge-v1'], before_commit=lambda: None)
        self.assertEqual(binding.run_id, admitted['admission']['run_id'])
        self.assertEqual(before, self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','verification_binding','host_outcome')").fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM orchestrations').fetchone()[0])
        self.assertIsNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity WHERE run_id=?',
            (binding.run_id,)).fetchone()[0])

    def test_missing_managed_open_companion_cannot_downgrade_to_legacy_or_consume_a_claim(self):
        client, state, launch = self.managed_lifecycle()
        self.ledger._connection.execute('DELETE FROM account_open_receipts')
        with self.assertRaises(AccountError):
            client.open_provenance(OpenProvenanceInput(state['scope'].run_handle,
                self.reference.base.assignment['assignment_id'], self.reference.base.worker.digest), request_id='missing-open-proof')
        with self.assertRaisesRegex(AccountError, 'claim_storage_invalid'):
            client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (state['scope'].run_id,)).fetchone()[0])

    def test_missing_original_request_journal_cannot_use_retained_receipt_as_claim_authority(self):
        client, state, launch = self.managed_lifecycle()
        self.ledger._connection.execute('DELETE FROM account_open_requests')
        with self.assertRaisesRegex(AccountError, 'claim_storage_invalid'):
            client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (state['scope'].run_id,)).fetchone()[0])

    def test_changed_origin_with_managed_companions_never_enables_legacy_claim_fallback(self):
        client, state, launch = self.managed_lifecycle()
        self.ledger._connection.execute("UPDATE host_run_bindings SET history_owner='legacy' WHERE run_id=?",
            (state['scope'].run_id,))
        with self.assertRaisesRegex(AccountError, 'claim_storage_invalid'):
            client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (state['scope'].run_id,)).fetchone()[0])

    def test_changed_original_launch_timeout_cannot_extend_a_managed_claim(self):
        client, state, launch = self.managed_lifecycle()
        self.ledger._connection.execute('UPDATE launch_inputs SET hard_timeout_seconds=hard_timeout_seconds+10 WHERE run_id=?',
            (state['scope'].run_id,))
        with self.assertRaisesRegex(AccountError, 'claim_storage_invalid'):
            client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (state['scope'].run_id,)).fetchone()[0])

    def test_known_managed_claim_checks_original_companions_without_clock_or_new_signature_work(self):
        client, state, launch = self.managed_lifecycle()
        scope = state['scope']
        receipt = client.claim_launch(launch.run_handle, launch.identity_digest, request_id=scope.claim_request_id)
        self.assertEqual('claimed', receipt['state'])
        with (mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known clock renewed')),
                mock.patch.object(self.jobs._intake, 'capture_admission', side_effect=AssertionError('known signature work'))):
            self.assertEqual({**receipt, 'known': True}, client.claim_launch(launch.run_handle,
                launch.identity_digest, request_id=scope.claim_request_id))
            for table in ('account_open_receipts', 'account_open_requests'):
                with self.subTest(missing=table):
                    rows = self.ledger._connection.execute('SELECT * FROM '+table).fetchall()
                    try:
                        self.ledger._connection.execute('DELETE FROM '+table)
                        with self.assertRaisesRegex(AccountError, 'claim_storage_invalid'):
                            client.claim_launch(launch.run_handle, launch.identity_digest, request_id=scope.claim_request_id)
                    finally:
                        for row in rows:
                            self.ledger._connection.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')', tuple(row))
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (scope.run_id,)).fetchone()[0])

    def test_managed_denial_fence_and_lease_retirement_need_no_central_orchestration(self):
        client, state, launch = self.managed_lifecycle()
        self.ledger._connection.execute('DELETE FROM owner_attestations')
        scope, store = state['scope'], self.reference.base.store
        lease = store.result_lease(scope)
        denial = client.claim_launch(launch.run_handle, launch.identity_digest, request_id=scope.claim_request_id)
        self.assertEqual('denied', denial['state'])
        closure = CloseDeniedInput(scope.run_handle, launch.identity_digest, scope.claim_request_id,
            denial['receipt_digest'], lease.lease_digest)
        fenced = client.close_denied_attempt(closure, request_id='managed-denial-fence')
        self.assertFalse(fenced['known'])
        retired = store.settle_denied_result_lease(scope, canonical_json(fenced))
        self.assertEqual('expired', retired.state)
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known fence refreshed clock')):
            self.assertTrue(client.close_denied_attempt(closure, request_id='managed-denial-fence')['known'])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM orchestrations').fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims WHERE run_id=?',
            (scope.run_id,)).fetchone()[0])

    def test_wire_default_denial_unconfigured_and_synchronous_paths_never_open_or_prepare(self):
        request = self.wire_command()
        raw = encode_account_request(request)
        # S1-U10a: open workers not configured yet answer open_account_starting (was open_unavailable).
        for configured, authorizer, expected in ((True, False, 'account_not_authorized'), (False, True, 'open_account_starting')):
            service, _ = self.wire_service(configured=configured, authorizer=authorizer)
            with self.subTest(configured=configured), self.assertRaisesRegex(AccountError, expected):
                decode_account_response(service.begin(raw, self.token), request)
        service, _ = self.wire_service()
        with self.assertRaisesRegex(AccountError, 'open_failed'):  # S1-U10f: an open is never synchronous
            decode_account_response(service.handle(raw, self.token), request)
        self.assertEqual([], self.reference.pending())
        self.assertEqual(0, self.jobs.snapshot()['tickets'])

    def test_S1_U10a_an_open_without_its_authorization_context_is_refused_before_readiness_is_told(self):
        request = self.wire_command()
        for configured in (True, False):
            service, _ = self.wire_service(configured=configured, open_context=False)
            with self.subTest(configured=configured), self.assertRaisesRegex(AccountError, '^open_not_authorized$'):
                decode_account_response(self.pump_wire(service, service.begin(encode_account_request(request), self.token)),
                                        request)

    def test_S1_U10a_an_account_that_is_closing_says_so(self):
        request = self.wire_command()
        service, _ = self.wire_service()
        self.jobs.request_close()
        with self.assertRaisesRegex(AccountError, '^open_account_closing$'):
            decode_account_response(self.pump_wire(service, service.begin(encode_account_request(request), self.token)), request)
        self.assertEqual([], self.reference.pending())

    def test_client_lost_reply_requires_explicit_same_identity_and_does_not_repeat_admission(self):
        service, _ = self.wire_service()
        p = self.reference
        client = AccountClient(Path('/private/tmp/original-open.sock'), p.base.scope.project_id, p.base.scope.runtime_digest, timeout=3)
        received = []
        def lost(path, raw, **options):
            received.append(self.pump_wire(service, service.begin(raw, self.token)))
            raise ServiceTransportError('transport_unavailable')
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lost) as transport:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.open_host_run(p.request, request_id='original-account-open')
        self.assertEqual(1, transport.call_count)
        original = decode_account_response(received[0], self.wire_command())
        def known(path, raw, **options):
            return self.pump_wire(service, service.begin(raw, self.token))
        with (mock.patch.object(account_api, 'exchange_frame', side_effect=known) as transport,
              mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known clock')),
              mock.patch.object(self.jobs._intake, 'capture_admission', side_effect=AssertionError('known signature replay'))):
            value = client.open_host_run(p.request, request_id='original-account-open')
        self.assertEqual(1, transport.call_count)
        self.assertEqual(16_384, transport.call_args.kwargs['request_limit'])
        self.assertEqual(16_384, transport.call_args.kwargs['response_limit'])
        self.assertEqual({**original, 'known': True}, value)
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])

    def test_native_revocation_after_pending_insert_rolls_back_before_signature_intake(self):
        service, authorize = self.wire_service()
        calls = []
        def revoke(request, token):
            calls.append(True)
            return authorize(request, token) if len(calls) < 3 else None
        service._authorizer = revoke
        request = self.wire_command()
        with self.assertRaisesRegex(AccountError, 'open_not_authorized'):
            decode_account_response(service.begin(encode_account_request(request), self.token), request)
        self.assertEqual([], self.reference.pending())
        self.assertEqual(0, self.jobs.snapshot()['tickets'])

    def test_native_revocation_after_open_writes_rolls_back_admission_and_keeps_original_pending_clock(self):
        service, authorize = self.wire_service()
        before = self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='admission_decision'").fetchone()[0]
        def revoke(request, token):
            if self.ledger._connection.execute("SELECT 1 FROM host_run_bindings WHERE history_owner='project' LIMIT 1").fetchone():
                return None
            return authorize(request, token)
        service._authorizer = revoke
        request = self.wire_command()
        raw = self.pump_wire(service, service.begin(encode_account_request(request), self.token))
        with self.assertRaisesRegex(AccountError, 'open_not_authorized'):
            decode_account_response(raw, request)
        self.assertEqual(['pending'], [r['state'] for r in self.reference.pending()])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
        self.assertEqual(before, self.ledger._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='admission_decision'").fetchone()[0])

    def test_revocation_during_encoded_reply_withholds_success_and_retains_known_original(self):
        service, _ = self.wire_service()
        request = self.wire_command()
        ticket = service.begin(encode_account_request(request), self.token)
        reply = service._reply
        def encode(*args, **options):
            raw = reply(*args, **options)
            if options.get('result') is not None:
                self.enabled = False
            return raw
        with mock.patch.object(service, '_reply', side_effect=encode):
            raw = self.pump_wire(service, ticket)
        with self.assertRaisesRegex(AccountError, 'open_not_authorized'):
            decode_account_response(raw, request)
        self.assertEqual(['committed'], [r['state'] for r in self.reference.pending()])
        self.enabled = True
        value = decode_account_response(self.pump_wire(service, service.begin(encode_account_request(request), self.token)), request)
        self.assertTrue(value['known'])
