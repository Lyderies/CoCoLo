"""Original account facts in isolated stores; native enrollment remains fake."""
from copy import deepcopy
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_execution_grant as fixtures

from codex_agents.account_claim_contracts import ClaimError
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.history_contracts import HistoryError
from codex_agents.host_result_contracts import HostResultReference
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.learning_contracts import canonical_json, content_digest, external_id, parse_bounded_json
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.result_contracts import ResultObservation
from codex_agents.run_provenance import RunProvenanceInput, seal_run_provenance, verify_run_provenance
from codex_agents.service_transport import ServiceTransportError
from codex_agents.sql_read_budget import SQLReadLimits


class RunProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectExecutionGrantTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.grant = self.base.grant()
        self.request = RunProvenanceInput(self.base.registration, self.grant.registration_digest,
                                         self.fixture.assigned['assignment_id'])
        self.limits = SQLReadLimits(100_000, 1000, 2 * 1_048_576)

    def proof(self, **changes):
        options = dict(runtime_digest=self.base.binding.runtime_digest, caller_scope='a' * 64, request=self.request,
                       storage_binding=self.fixture.scope, storage_evidence=self.base.evidence, work_limits=self.limits)
        options.update(changes)
        return self.base.ledger._attest_run_provenance(self.fixture.project, **options)

    def test_original_registered_claim_admission_and_family_are_stable_metadata(self):
        before = self.base.ledger._connection.total_changes
        value = self.proof()
        checked = verify_run_provenance(self.fixture.key, self.request, self.grant, value)
        self.assertEqual('codex', checked['provenance']['provider'])
        self.assertEqual(self.fixture.base.fixture.fixture.task()['family'], checked['provenance']['task_family'])
        self.assertEqual(self.grant.binding.as_data(), checked['provenance']['binding'])
        with mock.patch.object(self.base.ledger, '_clock_ms', side_effect=AssertionError('clock refreshed')):
            self.assertEqual(value, self.proof())
        self.assertEqual(before, self.base.ledger._connection.total_changes)

    def test_missing_registration_never_registers_or_returns_an_inferred_receipt(self):
        self.base.ledger._connection.execute('DELETE FROM capacity_executions')
        before = self.base.ledger._connection.total_changes
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()
        self.assertEqual(before, self.base.ledger._connection.total_changes)
        self.assertEqual(0, self.base.ledger._connection.execute('SELECT COUNT(*) FROM capacity_executions').fetchone()[0])

    def test_changed_scope_assignment_registration_or_seal_cannot_rebind_original_facts(self):
        for changes in (
            {'caller_scope': 'b' * 64}, {'request': replace(self.request, assignment_id='changed')},
            {'request': replace(self.request, registration_digest='f' * 64)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ClaimError):
                self.proof(**changes)
        original = self.proof()
        for field, value in (('provider', 'claude'), ('task_family', 'verification'),
                             ('registered_assignment_id', 'copied'), ('started_at_ms', True)):
            changed = deepcopy(original)
            changed['provenance'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
                verify_run_provenance(self.fixture.key, self.request, self.grant, changed)

    def test_missing_or_changed_original_admission_is_not_reconstructed(self):
        self.base.ledger._connection.execute("DELETE FROM records WHERE record_type='admission_decision'")
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()

    def managed_project(self, *, record_host_open=True):
        # Synthetic enrollment links the existing isolated account fixture to
        # this project owner; no actual provider/native caller is admitted.
        store = self.fixture.store
        store._orchestration_limits = OrchestrationLimits(1, 8)
        scope = OrchestrationScope(**{name: getattr(self.base.binding, name) for name in SCOPE_FIELDS})
        request = OrchestrationRequest(1, 'orchestration.open', 'fixture-open', scope, {})
        with mock.patch('codex_agents.orchestration_store.secrets.token_hex', return_value=self.base.binding.orchestration_handle[4:]):
            OrchestrationOwner(store).apply(request, client_version='codex-host-v1', before_commit=lambda: None)
        store._allow_legacy_orchestration_reference = False
        if record_host_open:
            opened = OpenProvenanceInput(self.fixture.scope.run_handle, self.fixture.assigned['assignment_id'])
            proof = self.base.ledger._attest_open_provenance(self.fixture.project,
                runtime_digest=self.fixture.scope.runtime_digest, caller_scope='a'*64, request=opened,
                storage_binding=self.fixture.scope, storage_evidence=self.fixture.evidence, work_limits=self.limits)
            store.record_host_open(self.fixture.scope, opened, proof, assignment=self.fixture.assigned,
                task=self.fixture.base.fixture.fixture.task(), route=self.fixture.base.fixture.route,
                before_commit=lambda: None)
        return store

    def test_managed_consumption_requires_original_provenance_and_commits_both_once(self):
        store = self.managed_project()
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            self.base.consume(self.grant)
        self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        proof = self.proof()
        self.assertEqual(self.grant, self.base.consume(self.grant, provenance=proof))
        entry = store.record(store._execution_provenance_id(self.grant))
        self.assertEqual(proof, store._execution_provenance(self.grant))
        with self.assertRaisesRegex(HistoryError, 'history_execution_consumed'):
            self.base.consume(self.grant, provenance=proof)
        self.assertEqual(entry, store.record(entry.record_id))
        self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        with self.assertRaisesRegex(HistoryError, 'history_invalid_input'):
            store.append_record(entry.content, source='host', ingested_at_ms=entry.ingested_at_ms)

    def test_provenance_and_consumption_rollback_together_without_reconstructing_a_missing_companion(self):
        store = self.managed_project()
        original = store._transaction

        @contextmanager
        def rollback():
            with original():
                yield
                raise OSError('PRIVATE-ROLLBACK')

        proof = self.proof()
        with mock.patch.object(store, '_transaction', new=rollback), self.assertRaisesRegex(HistoryError, 'history_unavailable'):
            self.base.consume(self.grant, provenance=proof)
        self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
        self.assertEqual(0, store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='execution_provenance'").fetchone()[0])
        self.base.consume(self.grant, provenance=proof)
        store._connection.execute("DELETE FROM records WHERE record_type='execution_provenance'")
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            store._execution_provenance(self.grant)

    def service(self, *, authorizer=None, limits=True):
        def authorize(command, context):
            if context is not self:
                return None
            return AccountAuthorization(self.fixture.project, self.base.binding.runtime_digest,
                account_request_digest(command), 'project', frozenset({'account.run_provenance'}), 'a' * 64,
                self.fixture.scope, self.base.evidence)
        service = AccountService(self.base.binding.runtime_digest, AccountOwnerViews(self.base.ledger),
                                  authorizer=authorizer or authorize,
                                  run_provenance_work_limits=self.limits if limits else None)
        self.addCleanup(service.close)
        return service, authorize

    def command(self):
        return AccountRequest(1, 'account.run_provenance', 'explicit-read', self.fixture.project,
                              self.base.binding.runtime_digest, self.request.payload())

    def test_api_client_service_returns_exact_original_evidence_without_sql_writes(self):
        service, _ = self.service()
        client = AccountClient(Path('/private/tmp/fixture-provenance.sock'), self.fixture.project,
                                self.base.binding.runtime_digest, timeout=1)
        before = self.base.ledger._connection.total_changes
        with mock.patch('codex_agents.account_api.exchange_frame', side_effect=lambda path, raw, **options: service.handle(raw, self)) as exchange:
            actual = client.run_provenance(self.request, request_id='explicit-read')
        self.assertEqual(self.proof(), actual)
        self.assertEqual(before, self.base.ledger._connection.total_changes)
        self.assertEqual(1, exchange.call_count)
        self.assertEqual(16_384, exchange.call_args.kwargs['request_limit'])
        self.assertEqual(16_384, exchange.call_args.kwargs['response_limit'])

    def test_default_limit_denial_and_current_revocation_never_return_provenance(self):
        command = self.command()
        raw = encode_account_request(command)
        service, authorize = self.service(limits=False)
        with mock.patch.object(service._views, 'run_provenance') as producer:
            with self.assertRaisesRegex(AccountError, 'claim_execution_unavailable'):
                decode_account_response(service.handle(raw, self), command)
            producer.assert_not_called()
        service, _ = self.service()
        count = []

        def close_during_authorization(request, context):
            result = authorize(request, context)
            count.append(True)
            service.close()
            return result

        service._authorizer = close_during_authorization
        with mock.patch.object(service._views, 'run_provenance') as producer:
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(service.handle(raw, self), command)
            producer.assert_not_called()
        service, _ = self.service()
        count.clear()

        def revoke(request, context):
            count.append(True)
            return authorize(request, context) if len(count) == 1 else None

        service._authorizer = revoke
        with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
            decode_account_response(service.handle(raw, self), command)

    def test_sql_budget_exhaustion_refuses_and_restores_original_connection_policy(self):
        connection = self.base.ledger._connection
        def settings():
            return (*(connection.execute('PRAGMA ' + name).fetchone()[0] for name in ('query_only', 'busy_timeout')),
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        before = settings()
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof(work_limits=SQLReadLimits(100_000, 1000, 1))
        self.assertEqual(before, settings())
        self.assertEqual(self.grant.registration_digest, self.proof()['provenance']['registration_digest'])

    def test_valid_mac_for_wrong_provider_family_or_registered_claim_cannot_consume(self):
        store = self.managed_project()
        original = self.proof()['provenance']
        for name, value in (('provider', 'claude'), ('task_family', 'verification'), ('registered_assignment_id', 'foreign')):
            facts = deepcopy(original)
            facts[name] = value
            proof = seal_run_provenance(self.fixture.key, facts)
            with self.subTest(name=name), self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
                self.base.consume(self.grant, provenance=proof)
            self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])

    def test_lost_client_response_is_not_retried_and_original_metadata_is_unchanged(self):
        client = AccountClient(Path('/private/tmp/fixture-provenance.sock'), self.fixture.project,
                                self.base.binding.runtime_digest, timeout=1)
        before = self.proof()
        with mock.patch('codex_agents.account_api.exchange_frame', side_effect=ServiceTransportError('service_unavailable')) as exchange:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.run_provenance(self.request, request_id='explicit-read')
            exchange.assert_called_once()
        self.assertEqual(before, self.proof())

    def test_final_authorizer_closure_or_changed_storage_scope_returns_only_denial(self):
        command = self.command()
        raw = encode_account_request(command)
        for change in ('close', 'storage_binding', 'caller_scope', 'storage_evidence', 'allowed_operations', 'source'):
            service, authorize = self.service()
            calls = []

            def changed(request, context):
                auth = authorize(request, context)
                calls.append(True)
                if len(calls) == 2:
                    if change == 'close':
                        service.close()
                    elif change == 'storage_binding':
                        auth = replace(auth, storage_binding=replace(self.fixture.scope, primary_session_id='foreign'))
                    else:
                        values = {'caller_scope': 'b' * 64, 'storage_evidence': self.base.evidence + b' ',
                                  'allowed_operations': frozenset(), 'source': 'host'}
                        auth = replace(auth, **{change: values[change]})
                return auth

            service._authorizer = changed
            with self.subTest(change=change), self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(service.handle(raw, self), command)
            self.assertEqual(2, len(calls))
            self.assertEqual(1, self.base.ledger._connection.execute('SELECT 1').fetchone()[0])

    def test_close_during_success_reply_construction_returns_only_denial(self):
        service, _ = self.service()
        command = self.command()
        reply = service._reply

        def close_after_encoding(labels, **options):
            encoded = reply(labels, **options)
            if options.get('result') is not None:
                service.close()
            return encoded

        with mock.patch.object(service, '_reply', side_effect=close_after_encoding):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                decode_account_response(service.handle(encode_account_request(command), self), command)

    def test_second_keyed_start_refuses_either_caller_selected_assignment(self):
        row = self.base.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='event' "
            "AND json_extract(CAST(content AS TEXT),'$.event_code')='subagent_started' "
            "AND json_extract(CAST(content AS TEXT),'$.run_id')=?", (self.grant.binding.run_id,)).fetchone()
        changed = parse_bounded_json(row['content'])
        changed['assignment_id'] = 'another-assignment'
        changed['record_id'] = external_id(self.fixture.key, 'evt',
            f"{changed['run_id']}\0{changed['assignment_id']}\0started")
        changed['task_family'] = 'verification'
        changed['task_slice'] = 'verification' + changed['task_slice'][changed['task_slice'].index('-'):]
        self.base.ledger.append(self.fixture.project, changed, producer='host', now_ms=changed['observed_at_ms'])
        for assignment in (self.request.assignment_id, changed['assignment_id']):
            with self.subTest(assignment=assignment), self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
                self.proof(request=replace(self.request, assignment_id=assignment))

    def original_record(self, kind):
        return self.base.ledger._connection.execute(
            'SELECT * FROM records WHERE record_type=? AND json_extract(CAST(content AS TEXT),\'$.run_id\')=?',
            (kind, self.grant.binding.run_id)).fetchone()

    def test_changed_original_admission_or_start_cannot_be_reconstructed(self):
        connection = self.base.ledger._connection
        for kind, field, value in (('admission_decision', 'provider', 'claude'), ('event', 'assignment_id', 'changed')):
            row = self.original_record(kind)
            record = parse_bounded_json(row['content'])
            record[field] = value
            connection.execute('UPDATE records SET content=?,content_digest=? WHERE project_id=? AND record_id=?',
                               (canonical_json(record), content_digest(record), self.fixture.project, row['record_id']))
            with self.subTest(kind=kind), self.assertRaisesRegex(ClaimError, 'claim_execution_invalid'):
                self.proof()
            connection.execute('UPDATE records SET content=?,content_digest=? WHERE project_id=? AND record_id=?',
                               (row['content'], row['content_digest'], self.fixture.project, row['record_id']))
        self.assertEqual(self.grant.registration_digest, self.proof()['provenance']['registration_digest'])

    def test_duplicate_admission_or_missing_start_returns_no_proof(self):
        connection = self.base.ledger._connection
        row = dict(self.original_record('admission_decision'))
        record = parse_bounded_json(row['content'])
        record['record_id'] = 'admission:duplicate'
        row.update(record_id=record['record_id'], content=canonical_json(record), content_digest=content_digest(record),
                   source_sequence=connection.execute('SELECT MAX(source_sequence)+1 FROM records').fetchone()[0])
        connection.execute('INSERT INTO records(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()
        connection.execute('DELETE FROM records WHERE project_id=? AND record_id=?', (self.fixture.project, record['record_id']))
        start = self.original_record('event')
        connection.execute('DELETE FROM records WHERE project_id=? AND record_id=?', (self.fixture.project, start['record_id']))
        with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
            self.proof()

    def test_released_account_metadata_does_not_extend_project_execution_deadline(self):
        original = self.proof()
        connection = self.base.ledger._connection
        # Synthetic historical row, not evidence of actual cleanup/release.
        connection.execute('UPDATE active_run_capacity SET released_at_ms=?,release_digest=? WHERE project_id=? AND run_id=?',
                           (self.grant.deadline_at_ms, 'e' * 64, self.fixture.project, self.grant.binding.run_id))
        self.assertEqual(original, self.proof())
        store = self.managed_project()
        self.fixture.base.stamp = self.grant.deadline_at_ms
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            self.base.consume(self.grant, provenance=original)
        self.assertEqual(0, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])

    def test_missing_original_companion_refuses_known_host_receipt_and_orchestration_close(self):
        from tests.test_bridge import result
        store = self.managed_project()
        self.base.consume(self.grant, provenance=self.proof())
        source = self.fixture.base.fixture
        delivered = store.publish_result(self.base.binding, self.fixture.assigned, canonical_json(result()),
            ResultObservation(self.base.binding.claim_id, self.fixture.base.stamp, False, True, None))
        reference = HostResultReference(self.base.binding, delivered.record_id,
                                        delivered.record_content_digest, delivered.result_digest)
        options = dict(assignment=self.fixture.assigned, task=source.fixture.task(), route=source.route,
                       client_capability=source.fixture.clients['bridge-v1'], before_commit=lambda: None)
        receipt = store.admit_host_result(reference, **options)
        store._connection.execute("DELETE FROM records WHERE record_type='execution_provenance'")
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            store.admit_host_result(reference, **options)
        scope = OrchestrationScope(**{name: getattr(self.base.binding, name) for name in SCOPE_FIELDS})
        close = OrchestrationRequest(1, 'orchestration.close', 'fixture-close', scope, {
            'orchestration_handle': self.base.binding.orchestration_handle, 'expected_sequence': 1,
            'status': 'completed', 'goal_tokens_used': None, 'goal_time_used_seconds': None})
        with self.assertRaisesRegex(HistoryError, 'history_execution_not_ready'):
            OrchestrationOwner(store).apply(close, client_version='codex-host-v1', before_commit=lambda: None)
        outcome = parse_bounded_json(store.record(receipt['admission']['outcome_record_id']).content)
        self.assertEqual('unverified', outcome['admission_state'])
        OrchestrationOwner(store).active_for(self.base.binding)
        self.assertEqual(1, store._connection.execute('SELECT sequence FROM project_orchestrations').fetchone()[0])

    def test_snapshot_work_and_duration_exhaustion_restore_connection_policy(self):
        from codex_agents import account_run_provenance
        connection = self.base.ledger._connection
        original = account_run_provenance._snapshot
        def settings():
            return (connection.execute('PRAGMA query_only').fetchone()[0],
                    connection.execute('PRAGMA busy_timeout').fetchone()[0],
                    connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))

        before = settings()

        def expensive(*args):
            connection.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) '
                               'SELECT SUM(x) FROM n').fetchone()
            return original(*args)

        with mock.patch.object(account_run_provenance, '_snapshot', side_effect=expensive):
            with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
                self.proof(work_limits=SQLReadLimits(1, 1000, 2 * 1_048_576))
        self.assertFalse(connection.in_transaction)
        self.assertEqual(before, settings())
        elapsed = []

        def slow(*args):
            elapsed.append(True)
            return original(*args)

        with mock.patch.object(account_run_provenance, '_snapshot', side_effect=slow), \
                mock.patch('codex_agents.sql_read_budget._clock_ns', side_effect=lambda: 1_000_000_000 if elapsed else 0):
            with self.assertRaisesRegex(ClaimError, 'claim_execution_unavailable'):
                self.proof()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(before, settings())
        self.assertEqual(self.grant.registration_digest, self.proof()['provenance']['registration_digest'])
