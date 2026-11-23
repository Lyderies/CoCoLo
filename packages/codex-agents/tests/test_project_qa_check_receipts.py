"""Primary acceptance cases for original scoped project check receipts.

Native/provider and fixed-runner report custody are explicit fake boundaries;
these cases do not approve real checks, account admission or release.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_operational_qa as fixtures

from codex_agents.history_contracts import HistoryError
from codex_agents import history_api
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.host_result_contracts import OperationalCheckContext
from codex_agents.learning_contracts import canonical_json
from codex_agents import project_qa_checks
from codex_agents.orchestration_api import OrchestrationLimits
from codex_agents.project_history import ProjectHistory


class ProjectCheckReceiptTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectOperationalQATests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.store = self.base.store
        self.report = {'deterministic_checks': [{'check_id': 'fixed-deterministic', 'passed': True,
            'evidence_digest': 'a'*64}], 'security_checks': [{'check_id': 'fixed-security', 'passed': True,
            'evidence_digest': 'b'*64}], 'observed_at_ms': self.base.fixture.base.stamp}

    def issue(self, **changes):
        options = {'request_id': 'original-check-report', 'before_commit': lambda: None}
        options.update(changes)
        return project_qa_checks.issue_check_receipt(self.store, self.base.context,
            self.base.worker_digest, self.base.verifier_digest, self.report, **options)

    def count(self):
        return self.store._connection.execute('SELECT COUNT(*) FROM project_qa_check_receipts').fetchone()[0]

    def test_original_report_and_pair_issue_one_sealed_bounded_receipt(self):
        value = self.issue()
        self.assertEqual({'check_receipt', 'check_report_digest', 'checks_passed', 'expires_at_ms', 'known'}, set(value))
        self.assertTrue(value['checks_passed'])
        self.assertFalse(value['known'])
        self.assertEqual(1, self.count())
        self.assertNotIn(b'PRIVATE-', canonical_json(value))
        self.assertEqual({**value, 'known': True}, self.issue())
        stored = project_qa_checks.read_check_receipt(self.store, value['check_receipt'])
        self.assertEqual(self.report, stored['report'])
        self.assertIsNone(stored['consumed_at_ms'])

    def test_known_receipt_preserves_original_window_without_clock_artifacts_or_mutation(self):
        value = self.issue()
        before = self.store._connection.total_changes
        self.base.fixture.base.stamp = value['expires_at_ms'] + 1
        with mock.patch.object(self.store, '_result_now', side_effect=AssertionError('known clock refreshed')), \
                mock.patch.object(self.store, 'read_result', side_effect=AssertionError('known body reread')):
            self.assertEqual({**value, 'known': True}, self.issue())
        self.assertEqual(before, self.store._connection.total_changes)

    def test_same_request_cannot_replace_original_report(self):
        original = self.issue()
        self.report['deterministic_checks'][0]['evidence_digest'] = 'c'*64
        with self.assertRaises(HistoryError):
            self.issue()
        self.assertEqual(1, self.count())
        stored = project_qa_checks.read_check_receipt(self.store, original['check_receipt'])
        self.assertEqual('a'*64, stored['report']['deterministic_checks'][0]['evidence_digest'])

    def test_report_with_false_checks_is_recorded_without_inventing_qa_acceptance(self):
        self.report['security_checks'][0]['passed'] = False
        value = self.issue()
        self.assertFalse(value['checks_passed'])
        self.assertNotIn('accepted', value)
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM records WHERE source='verifier'").fetchone()[0])

    def test_closed_report_and_exact_integer_time_are_required(self):
        original = deepcopy(self.report)
        for report in ({**original, 'accepted': True}, {**original, 'observed_at_ms': True},
                       {**original, 'deterministic_checks': []},
                       {**original, 'observed_at_ms': original['observed_at_ms'] - 1}):
            self.report = report
            with self.subTest(report=report.keys()), self.assertRaises(HistoryError):
                self.issue()
            self.assertEqual(0, self.count())

    def test_current_revocation_inside_transaction_leaves_no_receipt(self):
        calls = []
        def revoke():
            calls.append(self.store._connection.in_transaction)
            if self.store._connection.in_transaction:
                raise HistoryError('history_not_authorized')
        with self.assertRaises(HistoryError):
            self.issue(before_commit=revoke)
        self.assertIn(True, calls)
        self.assertEqual(0, self.count())

    def test_revocation_after_internal_writes_before_commit_rolls_back_both_journals(self):
        def revoke():
            if self.store._connection.in_transaction and self.count():
                raise HistoryError('history_not_authorized')
        with self.assertRaises(HistoryError):
            self.issue(before_commit=revoke)
        self.assertEqual(0,self.count())
        self.assertEqual(0,self.store._connection.execute('SELECT COUNT(*) FROM project_qa_check_requests').fetchone()[0])

    def test_original_evidence_changed_by_final_authorizer_is_rechecked_before_insert(self):
        def remove():
            if self.store._connection.in_transaction:
                self.store._connection.execute('DELETE FROM project_host_opens WHERE run_handle=?',
                    (self.base.verifier_binding.run_handle,))
        with self.assertRaises(HistoryError):
            self.issue(before_commit=remove)
        self.assertEqual(0, self.count())
        self.assertEqual(2, self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])

    def test_transaction_rollback_and_lost_commit_reply_preserve_exact_request_identity(self):
        transaction = self.store._transaction
        @contextmanager
        def rollback():
            with transaction():
                yield
                raise OSError('fixture-before-commit')
        with mock.patch.object(self.store, '_transaction', new=rollback), self.assertRaises((OSError, HistoryError)):
            self.issue()
        self.assertEqual(0, self.count())
        @contextmanager
        def lost_reply():
            with transaction():
                yield
            raise OSError('fixture-lost-commit-reply')
        with mock.patch.object(self.store, '_transaction', new=lost_reply), self.assertRaises((OSError, HistoryError)):
            self.issue()
        self.assertEqual(1, self.count())
        with mock.patch.object(self.store, '_result_now', side_effect=AssertionError('fresh known clock')):
            self.assertTrue(self.issue()['known'])

    def test_fresh_receipt_never_outlives_original_result_retention(self):
        expires = min(self.store.result_reservation(binding).expires_at_ms
            for binding in (self.base.binding, self.base.verifier_binding))
        self.base.fixture.base.stamp = expires - 1
        self.report['observed_at_ms'] = expires - 1
        value = self.issue()
        self.assertEqual(expires, value['expires_at_ms'])

    def test_pending_and_retained_capacity_refuse_without_replacing_known_rows(self):
        first = self.issue()
        with mock.patch.object(project_qa_checks, 'MAX_PENDING_CHECK_RECEIPTS', 1), self.assertRaises(HistoryError):
            self.issue(request_id='second-report')
        self.assertEqual(1, self.count())
        self.assertEqual({**first, 'known': True}, self.issue())

    def test_absolute_retained_capacity_is_distinct_from_pending_capacity(self):
        first=self.issue()
        with mock.patch.object(project_qa_checks,'MAX_CHECK_RECEIPTS',1),self.assertRaises(HistoryError):
            self.issue(request_id='other-bounded-report')
        self.assertEqual({**first,'known':True},self.issue())
        self.assertEqual(1,self.count())

    def test_034_R6_receipts_and_their_requests_go_after_retention_and_the_ceiling_is_raised(self):
        """0.3.4 R6: in 0.8.29 every receipt ever issued counted against 4,096 and none was removed."""
        from codex_agents.project_qa_checks import QA_RETENTION_MS
        self.assertEqual(16_384, project_qa_checks.MAX_CHECK_RECEIPTS)
        self.issue()

        def rows():
            return tuple(self.store._connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('project_qa_check_receipts', 'project_qa_check_requests'))
        self.assertEqual((1, 1), rows())
        created = self.store._connection.execute('SELECT created_at_ms FROM project_qa_check_receipts').fetchone()[0]
        with mock.patch.object(self.store, '_result_now', return_value=created + QA_RETENTION_MS):
            self.store.expire_results(limit=10)
        self.assertEqual((1, 1), rows())
        with mock.patch.object(self.store, '_result_now', return_value=created + QA_RETENTION_MS + 1):
            self.store.expire_results(limit=10)
        self.assertEqual((0, 0), rows())

    def test_report_byte_limit_refuses_before_original_private_artifact_io(self):
        # Both arrays satisfy the fixed runner's per-array and per-ID bounds;
        # their combined canonical bytes exceed this receipt's storage bound.
        checks=[{'check_id':str(i)+'x'*145,'passed':True,'evidence_digest':'a'*64} for i in range(256)]
        self.report['deterministic_checks']=checks
        self.report['security_checks']=checks
        self.assertGreater(len(canonical_json(self.report)),65_536)
        with mock.patch.object(self.store,'read_result',side_effect=AssertionError('oversize report read private bytes')):
            with self.assertRaises(HistoryError):
                self.issue()
        self.assertEqual(0,self.count())

    def test_changed_report_or_seal_is_corruption_and_never_repaired(self):
        value = self.issue()
        self.store._connection.execute('UPDATE project_qa_check_receipts SET report=?', (canonical_json({}),))
        with self.assertRaises(HistoryError):
            project_qa_checks.read_check_receipt(self.store, value['check_receipt'])
        with self.assertRaises(HistoryError):
            self.issue()
        self.assertEqual(1, self.count())

    def test_missing_original_receipt_cannot_reissue_same_request_identity(self):
        self.issue()
        self.store._connection.execute('DELETE FROM project_qa_check_receipts')
        with self.assertRaises(HistoryError):
            self.issue()
        self.assertEqual(0,self.count())

    def test_missing_request_journal_cannot_reconstruct_from_receipt_row(self):
        value=self.issue()
        self.store._connection.execute('DELETE FROM project_qa_check_requests')
        with self.assertRaises(HistoryError):
            self.issue()
        with self.assertRaises(HistoryError):
            project_qa_checks.read_check_receipt(self.store,value['check_receipt'])
        self.assertEqual(1,self.count())
        self.assertEqual(0,self.store._connection.execute('SELECT COUNT(*) FROM project_qa_check_requests').fetchone()[0])

    def test_explicit_schema6_upgrade_keeps_all_original_rows_and_invents_no_receipts(self):
        from tests.project_schema_fixtures import assert_migrated_rows, legacy_cleanup_layout
        legacy_cleanup_layout(self.store)
        self.store._connection.execute('DROP TABLE project_operational_qa')
        self.store._connection.execute('DROP TABLE project_qa_check_requests')
        self.store._connection.execute('DROP TABLE project_qa_check_receipts')
        self.store._connection.execute('PRAGMA user_version=6')
        names=[row[0] for row in self.store._connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        before={name:[tuple(row) for row in self.store._connection.execute('SELECT * FROM '+name)] for name in names}
        capacity=self.store._result_capacity
        self.store.close()
        path=self.base.fixture.directory/'history.sqlite3'
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.base.fixture.open_history()
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.base.fixture.store=ProjectHistory(self.base.fixture.directory,self.base.binding.project_id,
            result_key=self.base.fixture.key,result_capacity=capacity,
            result_clock_ms=lambda:self.base.fixture.base.stamp,
            orchestration_limits=OrchestrationLimits(1,8),allow_schema_upgrade=True)
        self.store=self.base.fixture.store
        self.assertEqual(20, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) SB: schema 20
        for name,rows in before.items():
            assert_migrated_rows(self, self.store, name, rows)
        self.assertEqual(0,self.count())

    def service(self, authorizer=None):
        token = object()
        context = OperationalCheckContext(self.base.context, self.report)
        def authorize(request, caller):
            if caller is not token:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request),
                'host', frozenset({'history.issue_qa_checks'}), operational_check_context=context)
        handler = HistoryService(self.base.binding.project_id, self.base.binding.runtime_digest,
            lambda: self.store, authorizer=authorizer or authorize)
        self.addCleanup(handler.close)
        return handler, token, authorize

    def command(self, **extra):
        return HistoryRequest(1, 'history.issue_qa_checks', 'api-fixed-check-report', self.base.binding.project_id,
            self.base.binding.runtime_digest, {'worker_binding': self.base.binding.as_data(),
                'verifier_binding': self.base.verifier_binding.as_data(), 'host_outcome_digest': self.base.worker_digest,
                'verifier_host_outcome_digest': self.base.verifier_digest, **extra})

    def test_closed_client_service_receipt_uses_only_independently_held_original_report(self):
        service, token, _ = self.service()
        client = HistoryClient(Path('/private/tmp/qa-receipt.sock'), self.base.binding.project_id,
            self.base.binding.runtime_digest, timeout=1)
        with mock.patch.object(history_api, 'exchange_frame', side_effect=lambda path, raw, **options:
                service.handle(raw, token)) as exchange:
            value = client.issue_operational_check_receipt(self.base.binding, self.base.verifier_binding,
                self.base.worker_digest, self.base.verifier_digest, request_id='api-fixed-check-report')
        exchange.assert_called_once()
        self.assertEqual(1,self.count())
        self.assertNotIn('report', value)
        for extra in ({'report':self.report},{'checks_passed':True},{'accepted':True}):
            with self.subTest(extra=extra.keys()), self.assertRaisesRegex(HistoryError, 'history_invalid_request'):
                self.command(**extra)

    def test_default_receipt_api_denies_before_opening_any_writer(self):
        factory=mock.Mock(return_value=self.store)
        service=HistoryService(self.base.binding.project_id,self.base.binding.runtime_digest,factory)
        self.addCleanup(service.close)
        command=self.command()
        with self.assertRaisesRegex(HistoryError,'history_not_authorized'):
            decode_history_response(service.handle(encode_history_request(command)),command)
        factory.assert_not_called()
        self.assertEqual(0,self.count())

    def test_server_report_changes_before_commit_revoke_mutation(self):
        service, token, authorize = self.service()
        calls=[]
        def change_report(request, caller):
            value=authorize(request,caller)
            calls.append(True)
            if len(calls)==2:
                self.report['security_checks'][0]['passed']=False
            return value
        service._authorizer=change_report
        command=self.command()
        with self.assertRaisesRegex(HistoryError,'history_not_authorized'):
            decode_history_response(service.handle(encode_history_request(command),token),command)
        self.assertEqual(0,self.count())

    def test_receipt_api_closed_response_refuses_coercing_flags_and_times(self):
        from codex_agents.learning_contracts import parse_bounded_json
        service,token,_=self.service()
        command=self.command()
        original=parse_bounded_json(service.handle(encode_history_request(command),token))
        for field,value in (('known',1),('checks_passed',1),('expires_at_ms',True),('check_receipt','copied-label')):
            changed=deepcopy(original)
            changed['result'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(HistoryError,'history_invalid_response'):
                decode_history_response(canonical_json(changed),command)

    def test_close_during_reply_encoding_withholds_success_and_retains_committed_identity(self):
        service,token,_=self.service()
        reply=service._reply
        def close_after_encoding(*args,**options):
            raw=reply(*args,**options)
            if options.get('result') is not None:
                service.close()
            return raw
        command=self.command()
        with mock.patch.object(service,'_reply',side_effect=close_after_encoding):
            with self.assertRaises(HistoryError):
                decode_history_response(service.handle(encode_history_request(command),token),command)
        self.base.fixture.store=self.base.fixture.open_history()
        self.store=self.base.fixture.store
        self.assertEqual(1,self.count())
        self.assertTrue(self.issue(request_id='api-fixed-check-report')['known'])
