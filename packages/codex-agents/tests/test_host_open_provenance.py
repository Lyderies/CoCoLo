"""Original pre-claim account rows and fake native enrollment, no model calls."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_storage as fixtures

from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.host_open_contracts import OpenProvenanceInput, checked_open_provenance, seal_open_provenance, verify_open_provenance
from codex_agents.learning_contracts import canonical_json, content_digest, external_id, parse_bounded_json
from codex_agents.service_transport import ServiceTransportError
from codex_agents.sql_read_budget import SQLReadLimits


class HostOpenProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimStorageTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.lease = self.fixture.arm()
        self.request = OpenProvenanceInput(self.fixture.scope.run_handle, self.fixture.assigned['assignment_id'])
        self.limits = SQLReadLimits(100_000, 1000, 2 * 1_048_576)

    def proof(self, **changes):
        options = dict(runtime_digest=self.fixture.scope.runtime_digest, caller_scope='a' * 64, request=self.request,
                       storage_binding=self.fixture.scope, storage_evidence=self.fixture.evidence, work_limits=self.limits)
        options.update(changes)
        return self.fixture.ledger._attest_open_provenance(self.fixture.project, **options)

    def verify(self, proof, **changes):
        options = dict(assignment=self.fixture.assigned, task=self.fixture.base.fixture.fixture.task(),
                       route=self.fixture.base.fixture.route)
        options.update(changes)
        return verify_open_provenance(self.fixture.key, self.request, self.lease, proof, **options)

    def command(self):
        return AccountRequest(1, 'account.open_provenance', 'read-original-open', self.fixture.project,
                              self.fixture.scope.runtime_digest, self.request.payload())

    def service(self, *, limits=True):
        def authorize(command, context):
            if context is not self:
                return None
            return AccountAuthorization(self.fixture.project, self.fixture.scope.runtime_digest,
                account_request_digest(command), 'project', frozenset({'account.open_provenance'}), 'a' * 64,
                self.fixture.scope, self.fixture.evidence)
        service = AccountService(self.fixture.scope.runtime_digest, AccountOwnerViews(self.fixture.ledger),
            authorizer=authorize, open_provenance_work_limits=self.limits if limits else None)
        self.addCleanup(service.close)
        return service, authorize

    def test_original_open_is_read_before_claim_registration_or_new_clock(self):
        connection = self.fixture.ledger._connection
        before = connection.total_changes
        with mock.patch.object(self.fixture.ledger, '_clock_ms', side_effect=AssertionError('clock refreshed')):
            first = self.proof()
            self.assertEqual(first, self.proof())
        self.assertEqual(first, self.verify(first))
        self.assertIsNone(first['provenance']['worker_outcome_digest'])
        self.assertEqual(before, connection.total_changes)
        for table in ('launch_claims', 'active_run_capacity', 'capacity_executions'):
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])
        self.assertFalse(connection.in_transaction)

    def test_missing_original_launch_input_or_start_is_not_inferred(self):
        connection = self.fixture.ledger._connection
        original = connection.execute('SELECT * FROM launch_inputs WHERE run_id=?', (self.fixture.scope.run_id,)).fetchone()
        connection.execute('DELETE FROM launch_inputs WHERE run_id=?', (self.fixture.scope.run_id,))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()
        connection.execute('INSERT INTO launch_inputs VALUES(?,?,?,?,?,?,?,?)', tuple(original))
        identity = self.proof()['provenance']['start_record_id']
        connection.execute('DELETE FROM records WHERE record_id=?', (identity,))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()

    def test_original_runtime_and_independent_expected_scope_must_match(self):
        for changes in ({'storage_binding': replace(self.fixture.scope, primary_session_id='foreign')},
                        {'storage_evidence': None}, {'caller_scope': None},
                        {'request': replace(self.request, run_handle='hrn_'+'b'*48)},
                        {'request': replace(self.request, assignment_id='foreign')}):
            with self.subTest(changes=changes), self.assertRaises(ClaimError):
                self.proof(**changes)
        self.fixture.ledger._connection.execute('UPDATE launch_inputs SET runtime_digest=? WHERE run_id=?',
                                                ('b'*64, self.fixture.scope.run_id))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
            self.proof()

    def test_client_payload_flags_and_changed_original_inputs_confer_no_authority(self):
        proof = self.proof()
        for kind in ('task', 'route', 'assignment'):
            inputs = {'task': self.fixture.base.fixture.fixture.task(), 'route': self.fixture.base.fixture.route,
                      'assignment': self.fixture.assigned}
            changed = deepcopy(inputs[kind])
            changed['objective' if kind != 'route' else 'provider'] = 'changed'
            with self.subTest(kind=kind), self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
                self.verify(proof, **{kind: changed})
        facts = deepcopy(proof['provenance'])
        facts['task_family'] = 'verification'
        with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
            self.verify(seal_open_provenance(self.fixture.key, self.request, facts))
        raw = parse_bounded_json(encode_account_request(self.command()))
        raw['payload']['active'] = True
        with self.assertRaisesRegex(AccountError, 'account_invalid_request'):
            from codex_agents.account_api import decode_account_request
            decode_account_request(canonical_json(raw))

    def test_duplicate_original_start_cannot_be_selected_by_assignment(self):
        connection = self.fixture.ledger._connection
        record = parse_bounded_json(connection.execute('SELECT content FROM records WHERE record_id=?',
            (self.proof()['provenance']['start_record_id'],)).fetchone()[0])
        record['assignment_id'] = 'duplicate'
        record['record_id'] = external_id(self.fixture.key, 'evt', f"{record['run_id']}\0duplicate\0started")
        self.fixture.ledger.append(self.fixture.project, record, producer='host', now_ms=record['observed_at_ms'])
        for assignment in (self.request.assignment_id, 'duplicate'):
            with self.subTest(assignment=assignment), self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
                self.proof(request=replace(self.request, assignment_id=assignment))

    def test_sql_limit_restores_original_policy_and_returns_only_fixed_reason(self):
        connection = self.fixture.ledger._connection
        def policy():
            return (connection.execute('PRAGMA query_only').fetchone()[0], connection.execute('PRAGMA busy_timeout').fetchone()[0],
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        before = policy()
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof(work_limits=SQLReadLimits(100_000, 1000, 1))
        self.assertFalse(connection.in_transaction)
        self.assertEqual(before, policy())
        self.verify(self.proof())

    def test_api_one_bounded_exchange_and_no_retry_after_lost_response(self):
        service, _ = self.service()
        client = AccountClient(Path('/private/tmp/fixture-open.sock'), self.fixture.project, self.fixture.scope.runtime_digest)
        with mock.patch('codex_agents.account_api.exchange_frame', side_effect=lambda path, raw, **kw: service.handle(raw, self)) as exchange:
            self.assertEqual(self.proof(), client.open_provenance(self.request, request_id='original-open'))
            exchange.assert_called_once()
            self.assertEqual(16_384, exchange.call_args.kwargs['request_limit'])
            self.assertEqual(16_384, exchange.call_args.kwargs['response_limit'])
        with mock.patch('codex_agents.account_api.exchange_frame', side_effect=ServiceTransportError('service_unavailable')) as exchange:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.open_provenance(self.request, request_id='lost')
            exchange.assert_called_once()

    def test_default_limit_denial_and_revocation_close_or_evidence_change_refuse(self):
        command = self.command()
        service, _ = self.service(limits=False)
        with mock.patch.object(service._views, 'open_provenance') as producer:
            with self.assertRaisesRegex(AccountError, 'claim_execution_unavailable'):
                decode_account_response(service.handle(encode_account_request(command), self), command)
            producer.assert_not_called()
        for mutation in ('revoke', 'close', 'evidence', 'host'):
            service, authorize = self.service()
            calls = []
            def change(request, context):
                value = authorize(request, context)
                calls.append(True)
                if len(calls) == 2:
                    if mutation == 'revoke':
                        return None
                    if mutation == 'close':
                        service.close()
                    if mutation == 'evidence':
                        return replace(value, storage_evidence=self.fixture.evidence+b' ')
                    if mutation == 'host':
                        return replace(value, source='host')
                return value
            service._authorizer = change
            with self.subTest(mutation=mutation), self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(service.handle(encode_account_request(command), self), command)

    def test_late_service_close_cannot_admit_encoded_success(self):
        service, _ = self.service()
        command = self.command()
        original = service._reply
        def close(labels, **options):
            reply = original(labels, **options)
            if options.get('result') is not None:
                service.close()
            return reply
        with mock.patch.object(service, '_reply', side_effect=close):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(service.handle(encode_account_request(command), self), command)

    def test_boolean_schema_and_added_receipt_fields_are_refused(self):
        value = self.proof()
        for changed in ({**value, 'schema_version': True}, {**value, 'native': True}):
            with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
                checked_open_provenance(changed, self.request, self.fixture.project, self.fixture.scope.runtime_digest)

    def test_known_historical_open_does_not_refresh_after_original_claim(self):
        original = self.proof()
        self.assertEqual('claimed', self.fixture.call()['state'])
        self.assertEqual(original, self.proof())
        self.assertEqual(1, self.fixture.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])


class OriginalVerifierOpenTests(unittest.TestCase):
    """Real stored verifier-open binding from the existing fake-provider flow."""
    def setUp(self):
        from tests.test_bridge import VerifierHandoffTests
        from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding
        self.native = VerifierHandoffTests(methodName='runTest')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.addCleanup(self.native.tearDown)
        worker = self.native.run_native_worker()
        self.digest = worker['recorded']['host_outcome_digest']
        self.native.admit('codex')
        self.task = self.native.verifier_task(self.digest)
        self.route, self.assignment, arguments, route_path = self.native.verifier_inputs(self.task, self.digest)
        opened = self.native.cli('learning-open-host-run', *arguments, '--route', route_path,
                                 '--orchestration-handle', worker['orchestration']['orchestration_handle'])
        self.ledger = LearningLedger(self.native.state, client_capabilities=self.native.clients)
        self.addCleanup(self.ledger.close)
        self.key = self.native.state.digest_key()
        run_id = external_id(self.key, 'run', opened['run_handle'])
        scope = ResultLeaseBinding(self.native.project_id, 'fixture-primary', COLLECTOR_RUNTIME_DIGEST,
            'fixture-supervisor', 1, worker['orchestration']['orchestration_handle'], opened['run_handle'], run_id,
            self.assignment['attempt'], 'future-claim', content_digest({'domain': 'fixture-future-request'}))
        directory = Path(self.native.temporary.name)/'verifier-history'
        directory.mkdir(mode=0o700)
        self.store = ProjectHistory(directory, scope.project_id, result_key=self.key,
            result_capacity=ResultCapacity(2, 2*MAX_RESULT_BODY_BYTES, 120_000, 120_000),
            allow_legacy_orchestration_reference=True)
        self.addCleanup(self.store.close)
        self.store.reserve_result_lease(scope, self.assignment)
        self.lease = self.store.arm_result_lease(scope)
        self.evidence = self.store.attest_result_lease(scope)
        self.request = OpenProvenanceInput(scope.run_handle, self.assignment['assignment_id'], self.digest)

    def proof(self, **changes):
        options = dict(runtime_digest=self.lease.binding.runtime_digest, caller_scope='a'*64, request=self.request,
            storage_binding=self.lease.binding, storage_evidence=self.evidence,
            work_limits=SQLReadLimits(100_000, 1000, 2*1_048_576))
        options.update(changes)
        return self.ledger._attest_open_provenance(self.lease.binding.project_id, **options)

    def test_actual_original_verifier_dependency_is_bound_before_claim(self):
        value = self.proof()
        self.assertEqual(self.digest, value['provenance']['worker_outcome_digest'])
        self.assertIsNotNone(value['provenance']['verification_record_id'])
        self.assertEqual(value, verify_open_provenance(self.key, self.request, self.lease, value,
            assignment=self.assignment, task=self.task, route=self.route))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
            self.proof(request=replace(self.request, worker_outcome_digest=None))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
            self.proof(request=replace(self.request, worker_outcome_digest='f'*64))

    def test_missing_or_duplicate_verifier_open_binding_cannot_be_inferred(self):
        value = self.proof()
        identity = value['provenance']['verification_record_id']
        row = dict(self.ledger._connection.execute('SELECT * FROM records WHERE record_id=?', (identity,)).fetchone())
        record = parse_bounded_json(row['content'])
        record['record_id'] = 'verification_binding:duplicate'
        row.update(record_id=record['record_id'], content=canonical_json(record), content_digest=content_digest(record),
                   source_sequence=self.ledger._connection.execute('SELECT MAX(source_sequence)+1 FROM records').fetchone()[0])
        self.ledger._connection.execute('INSERT INTO records('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()
        self.ledger._connection.execute("DELETE FROM records WHERE record_type='verification_binding'")
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()

    def test_valid_mac_cannot_drop_the_dependency_of_the_original_bound_task(self):
        facts = deepcopy(self.proof()['provenance'])
        for field in ('worker_outcome_digest', 'verification_record_id', 'verification_content_digest'):
            facts[field] = None
        unbound_request = replace(self.request, worker_outcome_digest=None)
        value = seal_open_provenance(self.key, unbound_request, facts)
        with self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
            verify_open_provenance(self.key, unbound_request, self.lease, value,
                                   assignment=self.assignment, task=self.task, route=self.route)
