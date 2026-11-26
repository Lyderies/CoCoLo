"""Decision 382: real owner values and reduced independent byte ceilings.

Fixtures seed genuine original records through their writers; no model or socket
is used, and successful serialization is not a claim of native authority.
"""
import json
import sys
import unittest
from unittest import mock

from test_framework import requirements
import tests.support  # noqa: F401
from codex_agents.learning_contracts import canonical_json


@requirements('S15-O0C-BINDINGS', why_not_scenario='Real owner/store writers supply original fixtures; independent reduced byte/freshness budgets are exercised without native enrollment, sockets or model calls.')
class OwnerCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def no_sockets(event, args):
            if event.startswith('socket.'):
                raise AssertionError('S15 owner boundaries must not open sockets')
        sys.addaudithook(no_sockets)

    def fixture(self, module, case):
        value = getattr(module, case)('runTest')
        self.addCleanup(value.doCleanups)
        value.setUp()
        return value

    def byte_cap(self, module, constant, size, operation, error):
        expected = operation()
        with mock.patch.object(module, constant, size):
            self.assertEqual(expected, operation())
        with mock.patch.object(module, constant, size - 1), self.assertRaises(error):
            operation()
        self.assertEqual(expected, operation())

    def test_original_contexts_cross_their_own_snapshot_byte_limits(self):
        from tests import test_account_original_open as fixtures
        from codex_agents import account_open_contracts, open_scope_contracts, host_result_contracts, host_open_contracts
        from tests.test_result_leases import lease_binding
        case = self.fixture(fixtures, 'OriginalAccountOpenPendingTests')
        context = case.context
        for module, constant, value in (
                (account_open_contracts, 'MAX_ACCOUNT_OPEN_CONTEXT_BYTES', context),
                (open_scope_contracts, 'MAX_OPEN_SCOPE_CONTEXT_BYTES', context.prospective),
                (host_result_contracts, 'MAX_HOST_RESULT_CONTEXT_BYTES', context.prospective.worker),
                (host_result_contracts, 'MAX_HOST_RESULT_CONTEXT_BYTES',
                 host_result_contracts.WorkerResultContext(context.prospective.worker.binding, context.prospective.task)),
                (host_open_contracts, 'MAX_HOST_OPEN_CONTEXT_BYTES',
                 host_open_contracts.HostOpenContext(lease_binding(), context.prospective.assignment,
                                                    context.prospective.task, context.prospective.route))):
            with self.subTest(context=type(value).__name__):
                self.byte_cap(module, constant, len(value.snapshot()[0]), value.snapshot,
                              account_open_contracts.OpenError if module is account_open_contracts else
                              __import__('codex_agents.history_contracts', fromlist=['HistoryError']).HistoryError)

    def test_open_scope_proof_and_account_reply_cross_independent_byte_limits(self):
        from tests import test_account_original_open as fixtures
        from codex_agents import account_open_contracts, open_scope_contracts
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'OriginalAccountOpenPendingTests')
        proof = case.context.project_scope
        request = case.request
        self.byte_cap(open_scope_contracts, 'MAX_OPEN_SCOPE_BYTES', len(canonical_json(proof)),
            lambda: open_scope_contracts.checked_open_scope(proof, request.scope, request.orchestration_handle,
                                                            request.inputs_digest), HistoryError)
        reply = dict(state='denied', run_handle=None, run_id=None, inputs_digest=request.inputs_digest,
            scope_digest='1'*64, receipt_digest='2'*64, admission_record_id='admission:fixture',
            admission_content_digest='3'*64, reason_code='provider_identity_missing', recorded_at_ms=1,
            observation_deadline_at_ms=None, known=False)
        self.byte_cap(account_open_contracts, 'MAX_ACCOUNT_OPEN_RECEIPT_BYTES', len(canonical_json(reply)),
            lambda: account_open_contracts.checked_account_open_reply(reply, request), account_open_contracts.OpenError)

    def test_actual_cleanup_and_execution_grant_encoders_and_verifiers_cross_limits(self):
        from tests import test_cleanup_contracts as fixtures
        from codex_agents import cleanup_contracts, execution_grant
        from codex_agents.account_claim_contracts import ClaimError
        case = self.fixture(fixtures, 'CleanupContractTests')
        self.byte_cap(cleanup_contracts, 'MAX_CLEANUP_EVIDENCE_BYTES', len(case.raw),
            lambda: cleanup_contracts.encode_cleanup_evidence(case.key, case.value), cleanup_contracts.CleanupError)
        grant = case.base.grant
        self.byte_cap(execution_grant, 'MAX_EXECUTION_GRANT_BYTES', len(grant.encode()), grant.encode, ClaimError)
        raw = grant.encode()
        self.byte_cap(execution_grant, 'MAX_EXECUTION_GRANT_BYTES', len(raw),
            lambda: execution_grant.verify_execution_grant(case.key, grant.binding, raw), ClaimError)

    def test_actual_storage_evidence_encoder_and_both_readers_cross_limits(self):
        from tests import test_account_claim_storage as fixtures
        from codex_agents import claim_storage
        from codex_agents.account_claim_contracts import ClaimError
        from codex_agents.history_contracts import HistoryError
        from codex_agents.learning_contracts import keyed_digest
        case = self.fixture(fixtures, 'AccountClaimStorageTests')
        lease = case.arm()
        raw = case.evidence
        self.byte_cap(claim_storage, 'MAX_STORAGE_EVIDENCE_BYTES', len(raw),
            lambda: claim_storage.encode_storage_evidence(case.key, lease), HistoryError)
        for operation in (
                lambda: claim_storage.verify_storage_evidence(case.key, case.scope, raw,
                    project_id=case.scope.project_id, runtime_digest=case.scope.runtime_digest,
                    run_handle=case.scope.run_handle, run_id=case.scope.run_id,
                    request_id=case.scope.claim_request_id, request_digest=case.scope.claim_request_digest,
                    assignment_digest=lease.assignment_digest,
                    orchestration_digest=keyed_digest(case.key, case.scope.orchestration_handle),
                    now_ms=lease.created_at_ms, deadline_ms=lease.created_at_ms + 1),
                lambda: claim_storage._verify_historical_storage_evidence(case.key, case.scope, raw,
                                                                         state='claim_pending')):
            self.byte_cap(claim_storage, 'MAX_STORAGE_EVIDENCE_BYTES', len(raw), operation,
                          ClaimError)

    def test_actual_open_provenance_and_assignment_capture_cross_limits(self):
        from tests import test_host_open_provenance as fixtures
        from codex_agents import host_open_contracts
        from codex_agents.account_claim_contracts import ClaimError
        case = self.fixture(fixtures, 'HostOpenProvenanceTests')
        proof = case.proof()
        self.byte_cap(host_open_contracts, 'MAX_OPEN_PROVENANCE_BYTES', len(canonical_json(proof)),
            lambda: host_open_contracts.checked_open_provenance(proof, case.request,
                case.fixture.scope.project_id, case.fixture.scope.runtime_digest), ClaimError)
        self.byte_cap(host_open_contracts, 'MAX_ASSIGNMENT_CANONICAL_BYTES', len(canonical_json(case.fixture.assigned)),
            lambda: case.verify(proof), ClaimError)

    def test_actual_run_provenance_and_retained_record_cross_limits(self):
        from tests import test_run_provenance as fixtures
        from codex_agents import run_provenance, account_run_provenance
        from codex_agents.account_claim_contracts import ClaimError
        case = self.fixture(fixtures, 'RunProvenanceTests')
        proof = case.proof()
        self.byte_cap(run_provenance, 'MAX_RUN_PROVENANCE_BYTES', len(canonical_json(proof)),
            lambda: run_provenance.checked_run_provenance(proof, case.request,
                case.base.binding.project_id, case.base.binding.runtime_digest), ClaimError)
        row = case.base.ledger._connection.execute(
            "SELECT * FROM records WHERE record_id=?", (proof['provenance']['start_record_id'],)).fetchone()
        self.byte_cap(account_run_provenance, 'MAX_RECORD_BYTES', len(row['content']),
            lambda: account_run_provenance._record(row, 'event', 'host', case.base.binding.run_id), ClaimError)

    def test_legacy_and_scalar_claim_contexts_cross_observation_age_independently(self):
        from tests import test_account_claim_storage as fixtures
        from codex_agents import providers
        from codex_agents.learning_contracts import keyed_digest
        case = self.fixture(fixtures, 'AccountClaimStorageTests')
        ledger, launch = case.ledger, case.base.launch
        handle = keyed_digest(case.key, launch.run_handle)
        observed = ledger._connection.execute('SELECT observed_at_ms FROM launch_observations WHERE handle_digest=?',
                                              (handle,)).fetchone()[0]
        with mock.patch.object(providers, 'BILLING_MAX_AGE_MS', 2), ledger._account_transaction():
            for method in (ledger._claim_context_locked, ledger._claim_scalar_context_locked):
                with self.subTest(method=method.__name__):
                    self.assertIsNone(method(case.project, handle, launch, observed + 2)[0])
                    self.assertEqual('claim_observation_missing', method(case.project, handle, launch, observed + 3)[0])
                    self.assertIsNone(method(case.project, handle, launch, observed + 2)[0])

    def test_owner_file_generation_attestation_and_intake_bytes_cross_limits(self):
        from tests import test_signature_job_intake as fixtures
        from codex_agents import learning_store, signature_job_intake
        case = self.fixture(fixtures, 'SignatureJobIntakeTests')
        ledger = case.ledger
        signer = ledger.state._owner_signer_bytes()
        self.byte_cap(learning_store, 'OWNER_SIGNERS_MAX_BYTES', len(signer), ledger.state._owner_signer_bytes, learning_store.StateError)
        self.byte_cap(learning_store, 'OWNER_SIGNERS_MAX_BYTES', len(signer), ledger._owner_generation_locked, learning_store.LedgerError)
        inputs = case.fixture.capture()
        rows = ledger._attestation_rows_locked(case.a, 'codex')
        self.assertTrue(rows)
        self.byte_cap(learning_store, 'MAX_RECORD_BYTES', max(len(r[2]) for r in rows),
            lambda: ledger._attestation_rows_locked(case.a, 'codex'), learning_store.LedgerError)
        operation = lambda: case.intake._validate_inputs(inputs, case.a, 'codex')
        self.byte_cap(signature_job_intake, 'MAX_RECORD_BYTES', max(len(r[2]) for r in inputs.rows),
                      operation, signature_job_intake.SignatureJobError)

    def test_execution_and_bridge_slots_cross_limits_then_release_for_fresh_work(self):
        from tests import test_codex_worker as fixtures
        from codex_agents import owned_execution
        from codex_agents.control_api import ControlError
        case = self.fixture(fixtures, 'CodexWorkerTests')
        with mock.patch.object(owned_execution, 'MAX_OWNED_EXECUTIONS', 2), \
                mock.patch.object(case.monitor, '_maximum', 2):
            first, second = case.prepare(1), case.prepare(2)
            case.returned(first)
            case.returned(second)
            target = __import__('tests.test_bridge_liveness', fromlist=['attempt']).attempt(3)
            key = owned_execution.OwnedExecutionKey(target.project_id, f'{3:064x}', target.run_id, target.claim_id)
            with self.assertRaises(owned_execution.ExecutionCustodyError):
                case.registry.issue(key, deadline_ns=case.deadline, run_handle=target.run_handle)
            # The owner cap is independent of monitor admission. Temporarily
            # permit its third registration while keeping two owner attempts.
            case.monitor._maximum = 3
            case.monitor.register(target, hard_deadline_ns=case.deadline)
            registry = owned_execution.OwnedExecutionRegistry()
            ticket = registry.issue(key, deadline_ns=case.deadline, run_handle=target.run_handle)
            import threading
            supervisor = registry.handoff(ticket, threading.Thread())
            case.monitor._maximum = 2
            with self.assertRaisesRegex(ControlError, 'control_unavailable'):
                case.owner.bind(target, supervisor)
            self.assertEqual(2, len(case.owner._attempts))
            for prepared in (first, second):
                case.registry.acknowledge_release(prepared[2])
                case.owner.retire(prepared[1], case.pool, prepared[3])
                case.owner.run_once()
                case.owner.forget(prepared[1])
            self.assertEqual({}, case.registry._pending)
            self.assertEqual({}, case.owner._attempts)
            # Remove the never-bound monitor fixture without pretending that
            # its separate registry has produced cleanup evidence.
            case.monitor.observe_terminal(target)
            case.monitor.observe_cleanup(target)
            case.monitor.forget(target)
            fresh = case.prepare(4)
            case.returned(fresh)
            self.assertEqual(1, len(case.registry._pending))

    def test_history_record_binding_and_result_file_cross_byte_limits(self):
        from tests import test_project_results as fixtures
        from tests.test_project_history import binding
        from tests.test_result_leases import lease_binding
        from tests.learning_support import event_record
        from codex_agents import project_history, result_contracts
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'ProjectResultTests')
        raw = canonical_json(event_record())
        self.byte_cap(project_history, 'MAX_RECORD_BYTES', len(raw),
                      lambda: case.store._checked_record(raw, 'hook', 1000), HistoryError)
        for capture, value in ((case.store._result_binding, binding()), (case.store._lease_binding, lease_binding())):
            self.byte_cap(project_history, 'MAX_BINDING_BYTES', len(canonical_json(value.as_data())),
                          lambda: capture(value), HistoryError)
        case.reserve()
        delivery = case.commit()
        path, = case.files()
        raw = path.read_bytes()
        self.byte_cap(project_history, 'MAX_RESULT_BODY_BYTES', len(raw),
                      lambda: case.store._result_file_info(path.stat()), HistoryError)
        self.byte_cap(project_history, 'MAX_RESULT_BODY_BYTES', len(raw),
                      lambda: case.store._read_result_file(binding(), delivery.result_digest), HistoryError)
        observation = result_contracts.ResultObservation('brg_'+'a'*32, 1000, False, True, None)
        self.byte_cap(result_contracts, 'MAX_RECORD_BYTES', len(canonical_json(observation.as_data())),
                      observation.as_data, HistoryError)

    def test_original_open_delivery_quota_refuses_excess_and_cancel_take_frees_it(self):
        from tests import test_account_original_open as fixtures
        from codex_agents.account_open_contracts import OpenError
        case = self.fixture(fixtures, 'OriginalAccountOpenJobTests')
        first = case.submit()
        case.another_task('second-bounded-open')
        second = case.submit(request_id='second-bounded-open')
        case.another_task('third-bounded-open')
        with self.assertRaisesRegex(OpenError, 'open_busy'):
            case.submit(request_id='third-bounded-open')
        self.assertEqual(2, len(case.jobs._jobs))
        self.assertEqual('released', case.jobs.cancel(first))
        self.assertIsNotNone(case.jobs.take(first))
        self.assertEqual(1, len(case.jobs._jobs))
        fresh = case.submit(request_id='third-bounded-open')
        self.assertIsNot(first, fresh)
        self.assertEqual(2, len(case.jobs._jobs))
        for ticket in (second, fresh):
            self.assertEqual('released', case.jobs.cancel(ticket))
            self.assertIsNotNone(case.jobs.take(ticket))
        self.assertEqual({}, case.jobs._jobs)
        self.assertEqual({}, case.jobs._counts)

    def test_control_dispatch_orchestration_and_read_response_decoders_cross_frames(self):
        from tests import test_control_api, test_prepared_dispatch, test_project_orchestration, test_result_read_api
        from codex_agents import control_api, control_service, dispatch_api, orchestration_api, orchestration_service, result_read_api, result_read_service
        from codex_agents.history_contracts import HistoryError
        control = self.fixture(test_control_api, 'ControlApiTests')
        request = control.request()
        encoded = control_api.encode_control_request(request)
        raw = control.service.handle(encoded, test_control_api.CONTEXT)
        self.byte_cap(control_api, 'MAX_CONTROL_FRAME_BYTES', len(raw) + 1,
                      lambda: control_api.decode_control_response(raw, request), control_api.ControlError)
        with mock.patch.object(control_service, 'MAX_CONTROL_FRAME_BYTES', len(raw)):
            with self.assertRaisesRegex(control_api.ControlError, 'control_unavailable'):
                control_api.decode_control_response(control.service.handle(encoded, test_control_api.CONTEXT), request)
        dispatch = self.fixture(test_prepared_dispatch, 'PreparedDispatchTests')
        service = dispatch.service(authorizer=dispatch.authorize)
        request = dispatch.request()
        raw = service.handle(dispatch_api.encode_dispatch_request(request), dispatch.marker)
        self.byte_cap(dispatch_api, 'MAX_DISPATCH_FRAME_BYTES', len(raw) + 1,
                      lambda: dispatch_api.decode_dispatch_response(raw, request), dispatch_api.DispatchError)
        orchestration = self.fixture(test_project_orchestration, 'ProjectOrchestrationTests')
        snapshot = orchestration.opened()
        request = orchestration.request('status', {'orchestration_handle': snapshot['orchestration_handle']})
        encoded = orchestration_api.encode_orchestration_request(request)
        raw = orchestration.service.handle(encoded, orchestration.token)
        self.byte_cap(orchestration_api, 'MAX_ORCHESTRATION_FRAME_BYTES', len(raw) + 1,
                      lambda: orchestration_api.decode_orchestration_response(raw, request), HistoryError)
        with mock.patch.object(orchestration_service, 'MAX_ORCHESTRATION_FRAME_BYTES', len(raw)):
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                orchestration_api.decode_orchestration_response(orchestration.service.handle(encoded, orchestration.token), request)
        read = self.fixture(test_result_read_api, 'ResultReadTests')
        request = result_read_api.result_read_request(test_result_read_api.binding(), read.digest)
        encoded = result_read_api.encode_result_read_request(request)
        raw = read.service.handle(encoded, test_result_read_api.CONTEXT)
        self.byte_cap(result_read_api, 'MAX_RESULT_READ_FRAME_BYTES', len(raw) + 1,
                      lambda: result_read_api.decode_result_read_response(raw, request), HistoryError)
        with mock.patch.object(result_read_service, 'MAX_RESULT_READ_FRAME_BYTES', len(raw)):
            with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
                result_read_api.decode_result_read_response(read.service.handle(encoded, test_result_read_api.CONTEXT), request)

    def test_worker_decode_report_and_output_cross_independent_byte_limits(self):
        from tests import test_codex_worker as fixtures
        from codex_agents import codex_worker
        case = self.fixture(fixtures, 'CodexWorkerTests')
        value = {**fixtures.fixtures.result(), 'summary': 'x' * 12000}
        first = case.prepare(value=value, submit=False)
        payload = case.pool._job(first[3]).payload
        self.byte_cap(codex_worker, 'MAX_INPUT_BYTES', len(payload),
                      lambda: codex_worker._decode_input(payload), Exception)
        schema_size = len(json.loads(payload)['schema'].encode())
        self.byte_cap(codex_worker, 'MAX_RESULT_BODY_BYTES', schema_size,
                      lambda: codex_worker._decode_input(payload), codex_worker.CodexWorkerError)
        # The runner seam supplies actual result bytes; process tracking and
        # native cleanup are deliberately outside this independent output cap.
        from types import SimpleNamespace
        import time
        report = dict(provider='codex', requested_model='gpt-6.1-sol', effort='high', wall_seconds=0,
                      read_containment='advisory', rerouted=False, outcome='result', usage=None)
        ticket = SimpleNamespace(deadline_ns=lambda: time.monotonic_ns() + 60_000_000_000)
        def execute():
            return codex_worker._execute(payload, ticket, first[1].project_id)
        with mock.patch.object(codex_worker, '_RUN_CODEX', side_effect=lambda **kwargs: (value, dict(report))):
            packet = execute()
            self.assertIsNone(packet.error_code)
            self.assertFalse(packet.fenced)
            self.byte_cap(codex_worker, 'MAX_REPORT_BYTES', len(packet.report),
                          lambda: codex_worker._safe_report(json.loads(packet.report), 'codex'), codex_worker.CodexWorkerError)
            self.assertGreater(len(packet.body), schema_size)
            with mock.patch.object(codex_worker, 'MAX_RESULT_BODY_BYTES', len(packet.body)):
                self.assertEqual(packet.body, execute().body)
            with mock.patch.object(codex_worker, 'MAX_RESULT_BODY_BYTES', len(packet.body) - 1):
                refused = execute()
                self.assertEqual('worker_input_invalid', refused.error_code)
                self.assertIsNone(refused.body)

    def test_preparation_payload_and_real_build_refuse_past_input_budget(self):
        from tests import test_codex_preparation as fixtures
        from codex_agents import codex_preparation
        case = self.fixture(fixtures, 'CodexPreparationTests')
        operation = lambda: codex_preparation.preparation_payload(case.prospective, case.fixture.project,
                                                                  case.fixture.bundle, fixtures.fixtures.CLEAN)
        raw = operation()
        self.byte_cap(codex_preparation, 'MAX_INPUT_BYTES', len(raw), operation, codex_preparation.PreparationError)
        with mock.patch.object(codex_preparation, 'MAX_INPUT_BYTES', len(raw) - 1):
            artifact, reason, workspace, started = codex_preparation._build(raw, case.scope)
        self.assertIsNone(artifact)
        self.assertEqual('preparation_failed_at_decode', reason)
        self.assertIsNone(workspace)
        self.assertFalse(started)

    def test_orchestration_positions_reports_and_recovery_cross_reduced_counts(self):
        from tests import test_project_orchestration, test_result_leases
        from codex_agents import orchestration_store, project_orchestration_report
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(test_project_orchestration, 'ProjectOrchestrationTests')
        snapshot = case.opened()
        handle = snapshot['orchestration_handle']
        for index in range(3):
            case.store.record_registered_run(handle, 'hrn_'+f'{index:048x}', f'run-{index}', 1,
                                            instance='old', now_ms=case.now)
        with mock.patch.object(project_orchestration_report, 'MAX_RUNS', 2), self.assertRaises(HistoryError):
            case.r6_report(snapshot)
        self.assertEqual(3, case.r6_report(snapshot)['admitted'])
        for left in (2, 1, 0):
            self.assertEqual(1, case.store.settle_earlier_registrations('new', now_ms=case.now + 1, limit=1))
            self.assertEqual(left, case.store._connection.execute(
                'SELECT COUNT(*) FROM project_registered_runs WHERE failed_reason IS NULL').fetchone()[0])
        self.assertEqual(0, case.store.settle_earlier_registrations('new', now_ms=case.now + 1, limit=1))
        leases = self.fixture(test_result_leases, 'ResultLeaseTests')
        from dataclasses import replace
        leases.reopen(result_capacity=replace(leases.fixture.capacity, max_reserved_bytes=4*1048576))
        for index in range(1, 4):
            leases.reserve(index)
        owner = leases.store._orchestration_owner()
        handle = test_result_leases.lease_binding().orchestration_handle
        with mock.patch.object(orchestration_store, 'MAX_LEASE_ROWS', 3):
            self.assertEqual(3, len(owner._positions('result_leases', handle)))
        with mock.patch.object(orchestration_store, 'MAX_LEASE_ROWS', 2), self.assertRaises(HistoryError):
            owner._positions('result_leases', handle)
        self.assertEqual(3, len(owner._positions('result_leases', handle)))

    def test_observation_worker_retained_reply_and_purge_cross_independent_limits(self):
        from tests import test_account_observation as fixtures
        from codex_agents import account_observation, provider_observation_contracts
        case = self.fixture(fixtures, 'AccountObservationTests')
        reservations = []
        for index in range(3):
            reservation = case.reserve('bounded-' + str(index))
            completion = case.probe(reservation)
            self.assertIsNone(completion.reason)
            reservations.append(reservation)
            reply = case.owner.complete(reservation, completion)
        size = len(completion.records)
        with mock.patch.object(account_observation, 'MAX_OBSERVATION_REPLY_BYTES', size):
            self.assertIsNotNone(case.probe(reservations[-1]).records)
        with mock.patch.object(account_observation, 'MAX_OBSERVATION_REPLY_BYTES', size - 1):
            refused = case.probe(reservations[-1])
            self.assertIsNone(refused.records)
            self.assertEqual('observation_unavailable', refused.reason)
        row = case.owner._row(reservation.row['project_id'], reservation.row['request_key'])
        records = case.owner._retained_records(row)
        self.byte_cap(account_observation, 'MAX_OBSERVATION_REPLY_BYTES', max(len(canonical_json(r)) for r in records),
                      lambda: case.owner._retained_records(row), provider_observation_contracts.ObservationError)
        self.byte_cap(provider_observation_contracts, 'MAX_OBSERVATION_REPLY_BYTES', len(canonical_json(reply)),
                      lambda: provider_observation_contracts.checked_observation_reply(reply, case.request),
                      provider_observation_contracts.ObservationError)
        now = max(r.row['deadline_at_ms'] for r in reservations) + account_observation.OBSERVATION_REPLAY_MS + 1
        for remaining in (2, 1, 0):
            self.assertEqual(1, account_observation.purge_old_observations(case.ledger, now, 1))
            self.assertEqual(remaining, case.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_requests').fetchone()[0])
            self.assertEqual(remaining, case.ledger._connection.execute('SELECT COUNT(*) FROM account_observation_origins').fetchone()[0])
        self.assertEqual(0, account_observation.purge_old_observations(case.ledger, now, 1))

    def test_account_output_upgrade_gate_and_repair_receipt_quota_cross_limits(self):
        from tests import test_account_api as fixtures
        from codex_agents import account_api, account_service, learning_store
        case = self.fixture(fixtures, 'AccountApiTests')
        request = case.request()
        encoded = account_api.encode_account_request(request)
        raw = case.service.handle(encoded, fixtures.TOKEN)
        with mock.patch.object(account_service, 'MAX_ACCOUNT_FRAME_BYTES', len(raw) + 1):
            self.assertEqual(raw, case.service.handle(encoded, fixtures.TOKEN))
        with mock.patch.object(account_service, 'MAX_ACCOUNT_FRAME_BYTES', len(raw)):
            with self.assertRaisesRegex(account_api.AccountError, 'account_incompatible'):
                account_api.decode_account_response(case.service.handle(encoded, fixtures.TOKEN), request)
        old = canonical_json(dict(operation='account.claim_launch', runtime_digest='0'*64))
        with mock.patch.object(account_service, 'MAX_ACCOUNT_FRAME_BYTES', len(old) + 1):
            self.assertIsNotNone(case.service.runtime_upgrade_reply(old))
            self.assertIsNone(case.service.runtime_upgrade_reply(old + b' '))
        with mock.patch.object(learning_store, 'MAX_PENDING_REPAIR_RECEIPTS', 2):
            for index in range(2):
                case.ledger.issue_result_repair_receipt(case.projects[0], 'a'*64, 'invalid_shape', now_ms=1000)
            with self.assertRaisesRegex(learning_store.LedgerError, 'capacity is exhausted'):
                case.ledger.issue_result_repair_receipt(case.projects[0], 'a'*64, 'invalid_shape', now_ms=1000)
            receipt = case.ledger.issue_result_repair_receipt(case.projects[0], 'a'*64, 'invalid_shape',
                        now_ms=1000 + learning_store.REPAIR_RECEIPT_TTL_MS)
            self.assertIs(type(receipt), str)
            self.assertEqual(1, case.ledger._connection.execute('SELECT COUNT(*) FROM repair_receipts').fetchone()[0])

    def test_dispatch_metadata_lifetime_refuses_overflow_and_new_service_has_fresh_slots(self):
        from dataclasses import replace
        from tests import test_prepared_dispatch as fixtures
        from codex_agents import prepared_dispatch, dispatch_api
        case = self.fixture(fixtures, 'PreparedDispatchTests')
        service = case.service(authorizer=case.authorize)
        first = case.request()
        self.assertEqual('queued', case.call(service, first)['state'])
        self.assertTrue(case.call(service, first)['known'])
        original = case.context
        case.context = replace(original, binding=replace(original.binding, attempt=2))
        second = case.request()
        with self.assertRaisesRegex(dispatch_api.DispatchError, 'dispatch_full'):
            case.call(service, second)
        self.assertEqual(1, len(service._jobs))
        service.close()
        replacement = self.fixture(fixtures, 'PreparedDispatchTests')
        fresh = replacement.service(authorizer=replacement.authorize)
        self.addCleanup(fresh.close)
        self.assertEqual('queued', replacement.call(fresh)['state'])
        self.assertEqual(1, len(fresh._jobs))
        self.assertEqual({}, replacement.fixture.registry._issued)
        replacement.factory.assert_not_called()


    def test_project_host_socket_path_cap_refuses_before_runtime_or_listener_creation(self):
        from tests import test_service_host, test_project_runtime
        from codex_agents import service_host
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(test_service_host, 'ServiceHostTests')
        factory = mock.Mock(side_effect=AssertionError('oversized host opened history'))
        with mock.patch.object(service_host, 'MAX_SOCKET_PATH_BYTES', 1), self.assertRaises(HistoryError):
            service_host.ProjectServiceHost(case.paths, case.project, case.scope(), factory, test_project_runtime.limits(),
                service_host.ServiceHostLimits(2, 1000000, 2, 1000000, 30000, 2))
        factory.assert_not_called()

    def test_derived_learning_record_feedback_and_membership_cross_byte_limits(self):
        from tests import test_project_learning_corpus as fixtures, test_learning_chunk_membership
        from tests.test_project_learning_live import fixture_record
        from codex_agents import project_learning_live, project_learning_owner, project_learning_chunks
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'ProjectLearningCorpusTests')
        with mock.patch.object(project_learning_live, 'MAX_RECORD_BYTES', len(canonical_json(case.case)) - 1), self.assertRaises(HistoryError):
            project_learning_live.append_learning_records(case.store, [case.case], ingested_at_ms=100)
        self.assertEqual(1, len(project_learning_live.append_learning_records(case.store, [case.case], ingested_at_ms=100)))
        self.assertEqual([], project_learning_live.append_learning_records(case.store, [case.case], ingested_at_ms=101))
        fixture_record(case.store, case.outcome, 'verifier', 100)
        with mock.patch.object(project_learning_owner, 'MAX_RECORD_BYTES', len(canonical_json(case.feedback)) - 1), self.assertRaises(HistoryError):
            project_learning_owner.record_feedback(case.store, case.feedback, b'signature', now_ms=100)
        with mock.patch.object(project_learning_owner, 'MAX_SIGNATURE_BYTES', 2), self.assertRaises(HistoryError):
            project_learning_owner.record_feedback(case.store, case.feedback, b'xxx', now_ms=100)
        self.assertTrue(project_learning_owner.record_feedback(case.store, case.feedback, b'signature', now_ms=100)['stored'])
        self.assertFalse(project_learning_owner.record_feedback(case.store, case.feedback, b'signature', now_ms=101)['stored'])
        membership = self.fixture(test_learning_chunk_membership, 'LearningChunkMembershipTests')
        membership.begin()
        with mock.patch.object(project_learning_chunks, 'MAX_MEMBERSHIP_BYTES', 1), self.assertRaises(HistoryError):
            membership.chunk()
        self.assertEqual(0, membership.store._connection.execute('SELECT COUNT(*) FROM project_learning_chunk_memberships').fetchone()[0])
        membership.chunk()
        self.assertTrue(membership.read()['records'])

    def test_staged_import_pages_and_source_chunk_bytes_cross_limits(self):
        from tests import test_project_learning_corpus, test_learning_source_transfer
        from codex_agents import project_learning_activation, project_learning_corpus, project_learning_source
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(test_project_learning_corpus, 'ProjectLearningCorpusTests')
        with mock.patch.object(project_learning_corpus, 'MAX_IMPORTS', 3):
            for index in range(3):
                identity = 'empty-' + str(index)
                manifest = case.manifest([])
                case.store.begin_learning_import(identity, manifest, now_ms=100, before_commit=lambda: None)
                case.store.finish_learning_import(identity, now_ms=110, before_commit=lambda: None)
        with mock.patch.object(project_learning_corpus, 'MAX_IMPORTS', 1):
            self.assertEqual(('empty-0', 'empty-1'), project_learning_activation.staged_imports(case.store))
        self.assertEqual(3, len(project_learning_activation.staged_imports(case.store)))
        image = self.fixture(test_learning_source_transfer, 'LearningSourceTransferTests')
        image.transfer()
        raw = project_learning_source.read_image_chunk(image.target, 'source', 0)
        self.byte_cap(project_learning_source, 'CHUNK_BYTES', len(raw),
            lambda: project_learning_source.stage_image_chunk(image.target, 'source', 0, raw), HistoryError)

    def test_qa_following_chunk_replay_and_record_contract_cross_byte_limits(self):
        from tests import test_project_learning_qa as fixtures
        from codex_agents import project_learning_qa, operational_qa_contracts
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'ProjectLearningQATests')
        pairs = [(fixtures.synthetic(i), fixtures.synthetic(j, checks=4)) for i in range(1, 5) for j in range(10, 14)]
        small, big = next((a, b) for a, b in pairs if a['qa_id'] < b['qa_id'])
        case.begin(chunks=[[small], [big]])
        first = case.stage(index=0, rows=[small])
        case.stage(index=1, rows=[big])
        with mock.patch.object(project_learning_qa, 'MAX_BYTES', len(small['content'])), self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            case.stage(index=0, rows=[small])
        self.assertEqual(first, case.stage(index=0, rows=[small]))
        value = json.loads(small['content'])
        self.byte_cap(operational_qa_contracts, 'MAX_RECORD_BYTES', len(small['content']),
                      lambda: operational_qa_contracts.checked_operational_qa(value), HistoryError)

    def test_git_patch_merge_path_count_and_committed_executable_cross_limits(self):
        from tests import test_write_merge as fixtures
        from codex_agents import hardened_git, write_merge, learning_engine
        case = self.fixture(fixtures, 'PreconditionTests')
        base = case.fresh()
        relative = 'tool.sh'
        size = (case.project / relative).stat().st_size
        with mock.patch.object(learning_engine, 'MAX_COMMITTED_EXECUTABLE_BYTES', size):
            self.assertTrue(learning_engine._committed_and_clean(fixtures.REAL_GIT, case.project, base, relative))
        with mock.patch.object(learning_engine, 'MAX_COMMITTED_EXECUTABLE_BYTES', size - 1):
            self.assertFalse(learning_engine._committed_and_clean(fixtures.REAL_GIT, case.project, base, relative))
        def change(root):
            (root / 'src/app.py').write_bytes(b'new app\n')
            (root / 'tool.sh').write_bytes(b'#!/bin/sh\necho new\n')
        patch = case.make_patch(base, change)
        with mock.patch.object(hardened_git, 'MAX_PATCH_BYTES', len(patch) - 1):
            refused = hardened_git.build_candidate_tree(case.project, case.stores / 'store', base, patch, executable=fixtures.REAL_GIT)
            self.assertFalse(refused.ok)
            self.assertEqual('patch_too_large', refused.reason)
        with mock.patch.object(hardened_git, 'MAX_PATCH_BYTES', len(patch)):
            candidate = hardened_git.build_candidate_tree(case.project, case.stores / 'store', base, patch, executable=fixtures.REAL_GIT)
            self.assertTrue(candidate.ok)
        with mock.patch.object(write_merge, 'MAX_PATHS', 1):
            case.assert_stopped(case.merge(candidate), 'write_merge_too_large')
        with mock.patch.object(write_merge, 'MAX_PATHS', 2):
            self.assertTrue(case.merge(candidate).ok)

    def test_persistence_capture_defences_refuse_actual_bytes_past_each_limit(self):
        from tests import test_result_persistence as fixtures
        from tests.test_result_leases import lease_binding
        from codex_agents import result_persistence
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'ResultPersistenceTests')
        prepared = case.execution(submit=False)
        handle = case.handles[prepared[3]]
        baseline = case.lane.snapshot(handle)
        for constant, operation in (
                ('MAX_OPEN_PROVENANCE_BYTES', lambda: case.lane.record_host_open(handle, b'xxx')),
                ('MAX_DENIAL_RECEIPT_BYTES', lambda: case.lane.settle_denial(handle, b'xxx')),
                ('MAX_EXECUTION_GRANT_BYTES', lambda: case.lane.consume_execution(handle, b'xxx')),
                ('MAX_RUN_PROVENANCE_BYTES', lambda: case.lane.consume_execution(handle, b'x', provenance=b'xxx')),
                ('MAX_TERMINAL_EVIDENCE_BYTES', lambda: case.lane.acknowledge_terminal(handle, b'xxx')),
                ('MAX_CLEANUP_EVIDENCE_BYTES', lambda: case.lane.acknowledge_release(handle, b'xxx')),
                ('MAX_DENIAL_RECEIPT_BYTES', lambda: case.lane.settle_earlier_lease(lease_binding(), b'xxx'))):
            with self.subTest(constant=constant), mock.patch.object(result_persistence, constant, 2), \
                    mock.patch.object(case.lane, '_allow_local_reference', False), self.assertRaisesRegex(HistoryError, '^history_invalid_input$'):
                operation()
            self.assertEqual(baseline, case.lane.snapshot(handle))
        self.assertFalse((prepared[0] / 'launches').exists())

    def test_actual_denial_and_bound_lease_evidence_cross_reader_byte_limits(self):
        from tests import test_result_denial_lane, test_project_execution_grant
        from codex_agents import claim_denial, project_history
        from codex_agents.account_claim_contracts import ClaimError
        from codex_agents.history_contracts import HistoryError
        denial = self.fixture(test_result_denial_lane, 'ResultDenialLaneTests')
        lease = denial.lane.lease(denial.handle)
        key = denial.account.fixture.key
        self.byte_cap(claim_denial, 'MAX_DENIAL_RECEIPT_BYTES', len(denial.raw),
                      lambda: claim_denial.verify_denial_closure(key, lease, denial.raw), ClaimError)
        bound = self.fixture(test_project_execution_grant, 'ProjectExecutionGrantTests')
        raw = bound.fixture.store.attest_bound_result_lease(bound.fixture.scope)
        self.byte_cap(project_history, 'MAX_STORAGE_EVIDENCE_BYTES', len(raw),
                      lambda: bound.fixture.store.attest_bound_result_lease(bound.fixture.scope), HistoryError)

    def test_backup_plan_worker_artifact_and_account_check_receipts_cross_limits(self):
        from tests import test_learning_source_image_legacy as fixtures
        from codex_agents import learning_store
        from codex_agents.learning_contracts import content_digest
        case = self.fixture(fixtures, 'LearningSourceImageLegacyTests')
        ledger = case.source
        size = learning_store.project_backup_bytes(ledger._connection, case.project, learning_store.LEDGER_SCHEMA_VERSION)
        self.byte_cap(learning_store, 'MAX_PROJECT_BACKUP_BYTES', size,
            lambda: ledger._plan_project_backups([case.project], learning_store.LEDGER_SCHEMA_VERSION), learning_store.LedgerCapacityError)
        receipt = ledger._connection.execute('SELECT * FROM operational_check_receipts LIMIT 1').fetchone()
        options = dict(host_outcome_digest=receipt['host_outcome_digest'],
            verifier_host_outcome_digest=receipt['verifier_host_outcome_digest'], check_report=json.loads(receipt['report']))
        now = options['check_report']['observed_at_ms'] + 1
        # Only the independent active-scope predicate is a fixture result;
        # all receipt population, expiry and writes use the actual owner.
        host = json.loads(ledger._connection.execute('SELECT content FROM records WHERE content_digest=?',
            (receipt['host_outcome_digest'],)).fetchone()[0])
        verifier = json.loads(ledger._connection.execute('SELECT content FROM records WHERE content_digest=?',
            (receipt['verifier_host_outcome_digest'],)).fetchone()[0])
        with mock.patch.object(ledger, '_operational_qa_scope_locked', return_value=(host, verifier, receipt['orchestration_id'])), \
                mock.patch.object(learning_store, 'MAX_PENDING_OPERATIONAL_CHECKS', 2):
            ledger.issue_operational_check_receipt(case.project, now_ms=now, **options)
            ledger.issue_operational_check_receipt(case.project, now_ms=now, **options)
            with self.assertRaisesRegex(learning_store.LedgerError, 'capacity is exhausted'):
                ledger.issue_operational_check_receipt(case.project, now_ms=now, **options)
            ledger.issue_operational_check_receipt(case.project, now_ms=now + learning_store.OPERATIONAL_CHECK_TTL_MS, **options)

    def test_native_worker_result_writer_and_retained_reader_cross_byte_limits(self):
        from types import SimpleNamespace
        from tests import test_account_api as fixtures
        from codex_agents import learning_store
        case = self.fixture(fixtures, 'AccountApiTests')
        ledger = case.ledger
        result = {'fixture': 'retained bytes'}
        digest = 'a' * 64
        size = len(canonical_json(result))
        with mock.patch.object(learning_store, 'MAX_WORKER_RESULT_BYTES', size - 1), self.assertRaises(learning_store.LedgerError):
            ledger._write_worker_result(case.projects[0], digest, result)
        written = ledger._write_worker_result(case.projects[0], digest, result)
        # A metadata adapter supplies the original writer's path/digest to the
        # real file reader. This is no registered outcome or authority claim.
        row = dict(relative_path=written['relative_path'], content_digest=written['result_digest'],
                   source_record_id='reader-fixture', expires_at_ms=1000)
        reader = SimpleNamespace(state=ledger.state, _clock_ms=lambda: 1,
            _connection=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: row)))
        expected = dict(record_id='reader-fixture', evidence_digests=[written['result_digest']])
        self.byte_cap(learning_store, 'MAX_WORKER_RESULT_BYTES', size,
            lambda: learning_store.LearningLedger._read_worker_result_locked(reader, case.projects[0], expected, digest),
            learning_store.LedgerError)

    def test_merge_library_file_and_aggregate_validation_cross_actual_byte_limits(self):
        from tests.merge_library_support import Fixture
        from codex_agents import merge_library as merge
        case = Fixture()
        self.addCleanup(case.close)
        batch = case.batch({'added.txt': b'x' * 4097})
        store = merge._Store(case.project, case.private)
        with mock.patch.object(merge, 'MAX_FILE_BYTES', 4096), self.assertRaisesRegex(merge._Refusal, 'file exceeds'):
            merge._validate(store, batch, case.policy)
        with mock.patch.object(merge, 'MAX_FILE_BYTES', 4097):
            merge._validate(store, batch, case.policy)
        with mock.patch.object(merge, 'MAX_LANDING_BYTES', 4096), self.assertRaisesRegex(merge._Refusal, 'landing exceeds'):
            merge._validate(store, batch, case.policy)
        path = batch.candidate_root / 'added.txt'
        with mock.patch.object(merge, 'MAX_FILE_BYTES', 4097):
            self.assertEqual(b'x' * 4097, store.read(path).data)
        with mock.patch.object(merge, 'MAX_FILE_BYTES', 4096), self.assertRaisesRegex(merge._Refusal, 'file exceeds'):
            store.read(path)

    def test_account_binding_sweep_and_project_qa_purge_drain_past_batch_one(self):
        from tests import test_active_run_capacity, test_project_qa_check_receipts
        from codex_agents import project_qa_checks
        case = self.fixture(test_active_run_capacity, 'ActiveRunCapacityTests')
        case.new_attempt()
        case.new_attempt()
        rows = case.ledger._connection.execute("SELECT expires_at_ms FROM host_run_bindings WHERE binding_state='pending'").fetchall()
        self.assertGreater(len(rows), 1)
        now = max(row[0] for row in rows) + 1
        with mock.patch.object(case.ledger, '_clock_ms', return_value=now):
            for remaining in range(len(rows) - 1, -1, -1):
                self.assertEqual(1, case.ledger.release_due_account_holds(limit=1))
                self.assertEqual(remaining, case.ledger._connection.execute(
                    "SELECT COUNT(*) FROM host_run_bindings WHERE binding_state='pending'").fetchone()[0])
            self.assertEqual(0, case.ledger.release_due_account_holds(limit=1))
        qa = self.fixture(test_project_qa_check_receipts, 'ProjectCheckReceiptTests')
        for index in range(3):
            qa.issue(request_id='bounded-' + str(index))
        now = qa.store._connection.execute('SELECT MAX(created_at_ms) FROM project_qa_check_receipts').fetchone()[0]
        now += project_qa_checks.QA_RETENTION_MS + 1
        for remaining in (2, 1, 0):
            project_qa_checks.purge_old_qa(qa.store, now, 1)
            self.assertEqual(remaining, qa.count())
        self.assertEqual(0, qa.store._connection.execute('SELECT COUNT(*) FROM project_qa_check_requests').fetchone()[0])

    def test_qa_count_cap_refuses_a_genuine_pair_at_zero_then_restoration_commits_it(self):
        from tests import test_project_qa_commit as fixtures
        from codex_agents import project_qa_commit
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'ProjectQACommitTests')
        self.assertEqual(0, case.count())
        with mock.patch.object(project_qa_commit, 'MAX_CHECK_RECEIPTS', 0), self.assertRaisesRegex(HistoryError, 'history_result_capacity'):
            case.commit()
        self.assertEqual(0, case.count())
        case.commit()
        self.assertEqual(1, case.count())

    def test_release_ack_and_claim_preflight_refuse_oversized_bytes_before_authority_work(self):
        from tests import test_account_claim_storage as fixtures
        from codex_agents import project_release_ack, claim_storage
        from codex_agents.account_claim_contracts import ClaimError
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'AccountClaimStorageTests')
        case.arm()
        before = case.store._connection.total_changes
        with mock.patch.object(project_release_ack, 'MAX_ACCOUNT_RELEASE_ACK_BYTES', 2), self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            project_release_ack.record_account_release(case.store, case.scope, b'xxx', registry=None, custody=None)
        self.assertEqual(before, case.store._connection.total_changes)
        with mock.patch.object(claim_storage, 'MAX_STORAGE_EVIDENCE_BYTES', len(case.evidence) - 1), \
                self.assertRaisesRegex(ClaimError, 'claim_storage_invalid'):
            case.ledger._claim_preflight(case.project, runtime_digest=case.scope.runtime_digest, caller_scope='a'*64,
                request_id=case.request.request_id, launch=case.base.launch, storage_required=True,
                storage_binding=case.scope, storage_evidence=case.evidence)
        self.assertEqual('claimed', case.call()['state'])

    def test_project_request_execution_bounds_real_response_and_frees_the_failed_slot(self):
        import time
        from tests import test_history_api as fixtures
        from codex_agents import project_request_queue, history_api
        from codex_agents.history_contracts import HistoryError
        case = self.fixture(fixtures, 'HistoryApiTests')
        command = fixtures.request()
        encoded = history_api.encode_history_request(command)
        expected = case.service.handle(encoded, fixtures.FIXTURE_CONTEXT)
        queue = project_request_queue.ProjectRequestQueue(project_request_queue.ProjectRequestLimits(1, 10000000000))
        self.addCleanup(queue.close)
        handle = queue.prepare(encoded, fixtures.FIXTURE_CONTEXT, deadline_ns=time.monotonic_ns() + 10000000000)
        with mock.patch.object(project_request_queue, 'MAX_FRAME_BYTES', len(expected)):
            queue.execute(handle, case.service)
        with self.assertRaisesRegex(HistoryError, 'history_service_unavailable'):
            queue.take(handle)
        self.assertEqual(0, queue.snapshot()['retained'])
        fresh = queue.prepare(encoded, fixtures.FIXTURE_CONTEXT, deadline_ns=time.monotonic_ns() + 10000000000)
        with mock.patch.object(project_request_queue, 'MAX_FRAME_BYTES', len(expected) + 1):
            queue.execute(fresh, case.service)
        self.assertEqual(expected, queue.take(fresh))
        self.assertEqual(0, queue.snapshot()['retained'])

    def test_container_journal_stop_detail_crosses_bytes_without_claiming_engine_evidence(self):
        from tests import test_container_launch_journal as fixtures
        from codex_agents import launch_journal
        launcher = fixtures.fake_launcher(self)
        token = fixtures.write_intent(launcher)
        value = launch_journal._read_entry(self.journal.directory / (token + '.json'))
        value = {**value, 'stop_detail': 'xx'}
        with mock.patch.object(launch_journal, 'MAX_STOP_DETAIL', 2):
            self.assertIsNotNone(launch_journal._checked_container_entry(value))
            self.assertIsNone(launch_journal._checked_container_entry({**value, 'stop_detail': 'xxx'}))
        self.assertIsNotNone(launch_journal._checked_container_entry(value))

    def test_original_open_receipt_writer_crosses_its_own_byte_limit_with_atomic_rollback(self):
        from tests import test_account_original_open as fixtures
        from codex_agents import account_original_open
        from codex_agents.account_open_contracts import OpenError
        from codex_agents.admission_inputs import AdmissionCaptureLimits
        from codex_agents.claim_admission import prepare_claim_admission
        from codex_agents.signature_worker_pool import SignatureWorkerResult
        case = self.fixture(fixtures, 'OriginalAccountOpenJobTests')
        ticket = case.submit()
        job = case.jobs._job(ticket)
        intake = case.jobs._intake
        captured = intake._jobs[job.signature_handle]
        snapshot, inputs = captured.admission, captured.inputs
        prepared = prepare_claim_admission(snapshot, AdmissionCaptureLimits(intake._limits.rows_per_job, intake._limits.bytes_per_job))
        # Only the fixed verifier's result is a controlled dependency. Every
        # input, fresh read, journal, receipt encoding and rollback is real.
        result = SignatureWorkerResult('signature_execution_completed', tuple(range(len(inputs.rows))), 0, 0, True, prepared)
        original_json = account_original_open.canonical_json
        receipt_bytes = []
        def encode(value):
            raw = original_json(value)
            if type(value) is dict and {'original_key', 'view', 'admission', 'host'} <= value.keys():
                receipt_bytes.append(raw)
            return raw
        def finish():
            return account_original_open.finish_open_from_signature_job(case.ledger, job.context, case.jobs._pool,
                job.signature_handle, before_commit=lambda: None)
        with mock.patch.object(account_original_open, '_returned_admission',
                return_value=(intake, result, inputs, snapshot, captured.work_limits)), \
                mock.patch.object(account_original_open, 'canonical_json', side_effect=encode):
            with mock.patch.object(account_original_open, 'MAX_ACCOUNT_OPEN_RECEIPT_BYTES', 1), self.assertRaisesRegex(OpenError, '^open_failed$'):
                finish()
            self.assertEqual(1, len(receipt_bytes))
            self.assertEqual(0, case.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])
            size = len(receipt_bytes[0])
            with mock.patch.object(account_original_open, 'MAX_ACCOUNT_OPEN_RECEIPT_BYTES', size - 1), self.assertRaisesRegex(OpenError, '^open_failed$'):
                finish()
            with mock.patch.object(account_original_open, 'MAX_ACCOUNT_OPEN_RECEIPT_BYTES', size):
                self.assertEqual('opened', finish()['state'])
            self.assertEqual(1, case.ledger._connection.execute('SELECT COUNT(*) FROM account_open_receipts').fetchone()[0])

    def test_probe_reply_build_crosses_byte_limit_with_genuine_retained_record_references(self):
        from tests import test_s15_probe_caps as fixtures
        from codex_agents import account_billing_admission
        maker = fixtures.RepeatProbeCapTests('runTest')
        self.addCleanup(maker.doCleanups)
        case = maker.fixture(60000)
        reservation = case.reserve('first')
        row = case.owner._row(reservation.project, reservation.key)
        value = case.owner._value(row)
        operation = lambda: case.owner._build_reply(value, known=True, observed_at=case.clock)
        size = len(canonical_json(operation()))
        self.byte_cap(account_billing_admission, 'MAX_PROBE_REPLY_BYTES', size, operation,
                      account_billing_admission.ProbeAdmissionError)

    def test_ingress_owner_group_defence_refuses_one_ticket_past_its_actual_population_limit(self):
        from tests import test_account_claim_jobs as fixtures
        from codex_agents import account_ingress
        from codex_agents.account_api import AccountError
        case = self.fixture(fixtures, 'AccountClaimJobTests')
        ingress = account_ingress.AccountIngress(case.service,
            account_ingress.AccountIngressLimits(4, 1, 5, 1, 1000000000, 2000000))
        # The owner pump is independent of socket I/O. Its queued peer/ticket
        # adapter is controlled to reach the defence behind coordinator quotas.
        ingress._started = True
        maximum = case.jobs._intake._limits.max_projects * case.jobs._intake._limits.max_jobs_per_project
        def peer():
            value = account_ingress._Peer(object(), object(), False, 1000000000000000000)
            value.request = b'fixture request'
            return value
        with mock.patch.object(case.service, 'begin', side_effect=lambda *args, **kwargs: object()), \
                mock.patch.object(case.service, 'take_claim_reply', return_value=None):
            for index in range(maximum):
                ingress._peers[object()] = peer()
                ingress.pump_owner()
                self.assertEqual(index + 1, len(ingress._groups))
            ingress._peers[object()] = peer()
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                ingress.pump_owner()
        self.assertEqual(maximum, len(ingress._groups))
        self.assertTrue(ingress._faulted)
        self.assertEqual({}, case.jobs._jobs)
        self.assertFalse(ingress._thread.is_alive())
