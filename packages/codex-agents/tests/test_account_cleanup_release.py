"""Actual account transactions; synthetic native project authentication only.

The unit fixture constructs cleanup metadata with its disposable key. This is
NOT native cleanup/containment evidence. Real project writer custody is covered
separately by process-composition tests before activation.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_terminal_delivery as fixtures
from codex_agents import account_api
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.cleanup_contracts import CleanupEvidence, encode_cleanup_evidence
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.capacity_status_contracts import CapacityStatusInput
from codex_agents.release_contracts import verify_release_receipt
from codex_agents.service_transport import ServiceTransportError


class AccountCleanupReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AccountTerminalDeliveryTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.key = self.base.fixture.key
        self.binding, self.source, self.caller = self.base.expected, 'project', self.base.caller_scope
        self.terminal = self.base.call()
        self.base.fixture.store.acknowledge_account_terminal(self.base.binding, canonical_json(self.terminal))
        self.cleanup = encode_cleanup_evidence(self.key, CleanupEvidence(self.binding, self.base.grant,
            self.base.delivery, self.terminal['receipt_digest']))
        self.request = AccountRequest(1, 'account.release_capacity', 'original-release', self.binding.project_id,
                                      COLLECTOR_RUNTIME_DIGEST, self.base.submission.payload())
        self.service = self.make_service()

    @property
    def ledger(self):
        return self.base.ledger

    def make_service(self):
        def authorize(request, context):
            if context is self.base.fixture.base.token:
                return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                    self.source, frozenset({'account.release_capacity'}), self.caller, self.binding, self.base.bound,
                    self.base.proof, cleanup_evidence=self.cleanup)
        value = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(value.close)
        return value

    def call(self, request=None):
        return self.base.fixture.base.call(request or self.request, service=self.service)

    def held(self):
        self.base.held()
        self.assertIsNone(self.ledger._connection.execute('SELECT cleanup_digest FROM account_cleanup_receipts').fetchone()[0])

    def test_atomic_release_exact_known_receipt_and_status_without_project_sql_or_ticket(self):
        from codex_agents.project_history import ProjectHistory
        with mock.patch.object(ProjectHistory, '__init__', side_effect=AssertionError('account opened project store')):
            first = self.call()
        self.assertFalse(first['known'])
        self.assertEqual({**first, 'known': True}, self.call())
        evidence = CleanupEvidence(self.binding, self.base.grant, self.base.delivery, self.terminal['receipt_digest'])
        self.assertEqual(first, verify_release_receipt(self.key, evidence, keyed_digest(self.key, self.cleanup), canonical_json(first)))
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))
        self.assertEqual({'state': 'released', 'receipt_digest': first['receipt_digest'], 'released_at_ms': first['released_at_ms']},
            self.ledger._capacity_status(self.binding.project_id,
                CapacityStatusInput(self.binding.run_handle, self.base.binding.claim_id)))
        self.assertEqual(self.terminal['receipt_digest'], self.base.call()['receipt_digest'])
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])
        self.assertNotIn(self.binding.run_handle, str(tuple(self.ledger._connection.execute('SELECT * FROM account_cleanup_receipts').fetchone())))

    def test_native_actor_and_original_binding_are_required_outside_json(self):
        for source in ('host', 'owner', 'hook', 'app_server'):
            self.source = source
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.source = 'project'
        self.binding = replace(self.binding, supervisor_generation=2)
        with self.assertRaises(AccountError):
            self.call()
        self.binding = self.base.expected
        for field, value in (('cleanup_confirmed', True), ('pid', 1), ('cleanup_evidence', self.cleanup.hex())):
            request = self.request
            reply = json.loads(self.service.handle(canonical_json({
                'protocol_version': 1, 'operation': request.operation, 'request_id': request.request_id,
                'project_id': request.project_id, 'runtime_digest': request.runtime_digest,
                'payload': {**request.payload, field: value},
            }), self.base.fixture.base.token))
            self.assertEqual('account_invalid_request', reply['reason_code'])
        self.held()

    def test_release_acknowledgement_rejects_wrong_keys_bindings_shape_types_and_noncanonical_bytes(self):
        from codex_agents.account_claim_contracts import ClaimError
        first = self.call()
        evidence = CleanupEvidence(self.binding, self.base.grant, self.base.delivery, self.terminal['receipt_digest'])
        digest = keyed_digest(self.key,self.cleanup)
        raw = canonical_json(first)
        cases = [b'',b'PRIVATE-ACK',raw+b'\n',b'x'*16385,b'\xff',raw.replace(b'"known":false',b'"known":0')]
        for field in first:
            changed = dict(first)
            changed.pop(field)
            cases.append(canonical_json(changed))
        cases.append(canonical_json({**first,'PRIVATE-EXTRA':True}))
        for stamp in (True, float(first['released_at_ms']), '1', -1, 2**64):
            cases.append(canonical_json({**first,'released_at_ms':stamp}))
        cases.append(canonical_json({**first,'receipt_digest':'f'*64}))
        for candidate in cases:
            with self.assertRaises(ClaimError) as caught:
                verify_release_receipt(self.key,evidence,digest,candidate)
            self.assertEqual(('claim_cleanup_invalid',),caught.exception.args)
            self.assertIsNone(caught.exception.__context__)
        for key in (b'',b'x'*31,b'x'*33,b'x'*32,bytearray(self.key),'PRIVATE-KEY'):
            with self.assertRaises(ClaimError):
                verify_release_receipt(key,evidence,digest,raw)
        with self.assertRaises(ClaimError):
            verify_release_receipt(self.key,evidence,'f'*64,raw)

    def test_missing_malformed_or_resealed_wrong_original_metadata_never_releases(self):
        original = self.cleanup
        changed = json.loads(original)
        changed['cleanup']['terminal_receipt_digest'] = 'f' * 64
        changed['seal'] = keyed_digest(self.key, b'project-cleanup-receipt-v1\0' + canonical_json(changed['cleanup']))
        for raw in (None, b'PRIVATE-CLEANUP', original+b'\n', b'x'*16385, canonical_json(changed)):
            self.cleanup = raw
            with self.assertRaises(AccountError) as caught:
                self.call()
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.held()
        self.cleanup = original

    def test_fresh_release_clock_rollback_refuses_but_known_release_has_no_new_clock(self):
        stamp = self.base.fixture.base.stamp
        self.base.fixture.base.stamp -= 1
        with self.assertRaises(AccountError):
            self.call()
        self.held()
        self.base.fixture.base.stamp = stamp
        first = self.call()
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('known release clock')):
            self.assertEqual({**first, 'known': True}, self.call())

    def test_authority_revocation_after_first_write_rolls_back_all_three_records(self):
        def revoke(sql):
            if sql.lstrip().startswith('UPDATE capacity_completions SET released_at_ms'):
                self.source = 'host'
        self.ledger._connection.set_trace_callback(revoke)
        try:
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        finally:
            self.ledger._connection.set_trace_callback(None)
            self.source = 'project'
        self.held()
        self.assertIsNone(self.ledger._connection.execute('SELECT released_at_ms FROM capacity_completions').fetchone()[0])
        self.assertIsNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity').fetchone()[0])
        self.call()

    def test_lost_committed_reply_and_revocation_recovers_same_original_without_second_release(self):
        original = self.ledger._account_transaction
        committed = []
        @contextmanager
        def revoke_after_release():
            with original():
                yield
            row = self.ledger._connection.execute('SELECT receipt_digest FROM account_cleanup_receipts').fetchone()
            if row is not None and row[0] is not None:
                committed.append(row[0])
                self.source = 'host'
        with mock.patch.object(self.ledger, '_account_transaction', revoke_after_release):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.assertEqual(1, len(committed))
        self.source = 'project'
        first = self.call()
        self.assertTrue(first['known'])
        self.assertEqual(committed[0], first['receipt_digest'])

    def test_missing_or_changed_original_companions_refuse_instead_of_reminting(self):
        for table, mutation in (('capacity_completions', 'DELETE FROM capacity_completions'),
                         ('account_cleanup_receipts', "UPDATE account_cleanup_receipts SET state_seal='"+'f'*64+"'"),
                         ('account_cleanup_receipts', 'UPDATE account_cleanup_receipts SET terminal_record_id=NULL'),
                         ('host_run_bindings', "UPDATE host_run_bindings SET task_digest='"+'f'*64+"'")):
            with self.subTest(mutation=mutation):
                columns = [r[1] for r in self.ledger._connection.execute('PRAGMA table_info('+table+')')]
                original = [tuple(r) for r in self.ledger._connection.execute('SELECT * FROM '+table)]
                try:
                    self.ledger._connection.execute(mutation)
                    self.assertFalse(self.ledger._connection.in_transaction)
                    with self.assertRaises(AccountError):
                        self.call()
                    if table == 'capacity_completions':
                        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_completions').fetchone()[0])
                finally:
                    for values in original:
                        entry = dict(zip(columns, values))
                        identity = (entry['project_id'], entry['handle_digest'])
                        exists = self.ledger._connection.execute('SELECT 1 FROM '+table+' WHERE project_id=? AND handle_digest=?', identity).fetchone()
                        if exists is None:
                            self.ledger._connection.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in values)+')',values)
                        else:
                            self.ledger._connection.execute('UPDATE '+table+' SET '+','.join(n+'=?' for n in columns)
                                +' WHERE project_id=? AND handle_digest=?', (*values, *identity))
                self.held()

    def test_missing_reserved_release_row_refuses_with_autocommitted_corruption(self):
        row = tuple(self.ledger._connection.execute('SELECT * FROM account_cleanup_receipts').fetchone())
        self.ledger._connection.execute('PRAGMA foreign_keys=OFF')
        try:
            self.ledger._connection.execute('DELETE FROM account_cleanup_receipts')
            self.assertFalse(self.ledger._connection.in_transaction)
            with self.assertRaises(AccountError):
                self.call()
            self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])
        finally:
            self.ledger._connection.execute('INSERT INTO account_cleanup_receipts VALUES('+','.join('?' for _ in row)+')',row)
            self.ledger._connection.execute('PRAGMA foreign_keys=ON')
        self.held()

    def test_original_host_mutation_after_last_write_rolls_back_release(self):
        original = self.ledger._connection.execute('SELECT task_digest FROM host_run_bindings').fetchone()[0]
        def corrupt(sql):
            if sql.lstrip().startswith('UPDATE account_cleanup_receipts SET'):
                self.ledger._connection.execute('UPDATE host_run_bindings SET task_digest=?', ('f'*64,))
        self.ledger._connection.set_trace_callback(corrupt)
        try:
            with self.assertRaises(AccountError):
                self.call()
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual(original, self.ledger._connection.execute('SELECT task_digest FROM host_run_bindings').fetchone()[0])
        self.held()

    def test_close_during_reply_encoding_withholds_success_and_preserves_original_release(self):
        original = self.service._reply
        def close_reply(labels, **options):
            value = original(labels, **options)
            if options.get('result', {}).get('state') == 'released':
                self.service.close()
            return value
        with mock.patch.object(self.service, '_reply', side_effect=close_reply):
            with self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.service = self.make_service()
        self.assertTrue(self.call()['known'])

    def test_client_sends_one_bounded_request_and_never_retries_lost_reply(self):
        client = AccountClient(Path('/private/tmp/account-fixture.sock'), self.binding.project_id, COLLECTOR_RUNTIME_DIGEST)
        def lost(path, raw, **options):
            self.service.handle(raw, self.base.fixture.base.token)
            raise ServiceTransportError('transport_unavailable')
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lost) as transport:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.release_capacity(self.base.submission, request_id='release-client')
            self.assertEqual(1, transport.call_count)
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lambda path, raw, **kw:
                               self.service.handle(raw, self.base.fixture.base.token)) as transport:
            self.assertTrue(client.release_capacity(self.base.submission, request_id='release-client')['known'])
            self.assertEqual(1, transport.call_count)

    def test_registered_backup_preserves_original_release_journal_and_standalone_restore(self):
        import runpy
        from tests.support import REPOSITORY_ROOT
        self.call()
        original = tuple(self.ledger._connection.execute('SELECT * FROM account_cleanup_receipts').fetchone())
        path = self.ledger.create_sanitized_backup(self.binding.project_id, file_id='cleanup_release_backup',
            expires_at_ms=self.base.fixture.base.stamp+60_000)
        body = json.loads(path.read_bytes())['body']
        self.assertEqual(11, body['ledger_schema_version'])
        self.assertEqual(1, body['counts']['account_cleanup_receipts'])
        self.assertTrue(self.ledger.verify_sanitized_backup(self.binding.project_id, file_id='cleanup_release_backup')['verified'])
        tool = runpy.run_path(str(REPOSITORY_ROOT/'scripts/backup_installed_project.py'))
        from codex_agents.learning_contracts import validate_learning_record, content_digest
        tool['_verify_relational_restore'](self.ledger._connection, body,
            validate_learning_record=validate_learning_record, content_digest=content_digest)
        self.assertEqual(original, tuple(self.ledger._connection.execute('SELECT * FROM account_cleanup_receipts').fetchone()))

    def test_release_capacity_reservation_failure_happens_before_account_registration(self):
        from tests.test_project_execution_grant import ProjectExecutionGrantTests
        from codex_agents import account_cleanup
        base = ProjectExecutionGrantTests(methodName='runTest')
        base.setUp()
        self.addCleanup(base.doCleanups)
        with mock.patch.object(account_cleanup, 'MAX_RELEASE_RESERVATIONS', 0):
            with self.assertRaisesRegex(AccountError, 'claim_cleanup_unavailable'):
                base.grant()
        self.assertEqual(0, base.ledger._connection.execute('SELECT COUNT(*) FROM capacity_executions').fetchone()[0])
        self.assertEqual(0, base.ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
