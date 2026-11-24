"""Atomic original project QA; fake native/provider and fixed-runner boundaries."""
from tests.support import SHORT_TMP
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_project_operational_qa as fixtures

from codex_agents.history_contracts import HistoryError
from codex_agents import history_api
from codex_agents.history_api import HistoryClient, HistoryRequest, decode_history_response, encode_history_request, history_request_digest
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest, parse_bounded_json
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.project_history import ProjectHistory
from codex_agents.project_qa_checks import read_check_receipt
from codex_agents.service_transport import ServiceTransportError, MAX_FRAME_BYTES


class ProjectQACommitTests(unittest.TestCase):
    verifier_changes = None

    def setUp(self):
        self.pair = fixtures.ProjectOperationalQATests(methodName='runTest')
        if self.verifier_changes:
            original = self.pair.verifier_result
            self.pair.verifier_result = lambda assignment: {**original(assignment), **deepcopy(self.verifier_changes)}
        self.pair.setUp()
        self.addCleanup(self.pair.doCleanups)
        self.store, self.context = self.pair.store, self.pair.context
        self.worker, self.verifier = self.pair.worker_digest, self.pair.verifier_digest
        self.report = {'deterministic_checks': [{'check_id': 'fixed-project-tests', 'passed': True, 'evidence_digest': 'a'*64}],
            'security_checks': [{'check_id': 'fixed-project-policy', 'passed': True, 'evidence_digest': 'b'*64}],
            'observed_at_ms': self.pair.fixture.base.stamp}
        self.receipt = self.issue()['check_receipt']
        self.original_host_rows = [tuple(row) for row in self.store._connection.execute(
            "SELECT * FROM records WHERE source='host' ORDER BY source_sequence")]

    def issue(self, **changes):
        options = {'request_id': 'original-qa-checks', 'before_commit': lambda: None, **changes}
        return self.store.issue_operational_check_receipt(self.context, self.worker, self.verifier, self.report, **options)

    def commit(self, **changes):
        options = {'before_commit': lambda: None, **changes}
        return self.store.commit_operational_qa(self.context, self.worker, self.verifier, self.receipt, **options)

    def details(self, **changes):
        return self.store.operational_qa_details(self.context, self.worker, self.verifier, self.receipt,
            before_reply=changes.get('before_reply', lambda: None))

    def count(self):
        return self.store._connection.execute('SELECT COUNT(*) FROM project_operational_qa').fetchone()[0]

    def test_actual_original_pair_consumes_receipt_and_appends_qa_outcome_atomically(self):
        value = self.commit()
        self.assertTrue(value['accepted'])
        self.assertFalse(value['known'])
        self.assertEqual((1,1), (value['required_criteria_total'],value['required_criteria_passed']))
        self.assertEqual(1,self.count())
        receipt=read_check_receipt(self.store,self.receipt)
        self.assertEqual(self.pair.fixture.base.stamp,receipt['consumed_at_ms'])
        self.assertEqual(self.original_host_rows,[tuple(row) for row in self.store._connection.execute(
            "SELECT * FROM records WHERE source='host' ORDER BY source_sequence")])
        detail=self.details()
        self.assertEqual({**value,'known':True},detail['summary'])
        self.assertEqual(value['qa_digest'],content_digest(detail['qa']))
        self.assertEqual('verifier',detail['outcome_commit']['source'])
        outcome=parse_bounded_json(self.store.record(value['outcome_record_id']).content)
        self.assertEqual('accepted',outcome['status'])
        self.assertTrue({self.worker,self.verifier,value['qa_digest']} <= set(outcome['evidence_digests']))
        self.assertNotIn(b'PRIVATE-',canonical_json(detail))

    def test_known_retry_and_details_need_no_clock_private_body_or_new_record(self):
        value=self.commit()
        self.pair.fixture.base.stamp=read_check_receipt(self.store,self.receipt)['expires_at_ms']+1
        before=self.store._connection.total_changes
        with mock.patch.object(self.store,'_result_now',side_effect=AssertionError('known clock refreshed')), \
                mock.patch.object(self.store,'read_result',side_effect=AssertionError('known body read')):
            self.assertEqual({**value,'known':True},self.commit())
            self.assertEqual(value['qa_digest'],self.details()['summary']['qa_digest'])
        self.assertEqual(before,self.store._connection.total_changes)

    def test_known_metadata_survives_actual_private_body_retirement_and_owned_pause(self):
        value=self.commit()
        for binding in (self.pair.binding,self.pair.verifier_binding):
            self.store.retire_result(binding)
        self.pair.pause()
        with mock.patch.object(self.store,'_result_now',side_effect=AssertionError('new historical clock')):
            self.assertEqual({**value,'known':True},self.commit())
        self.assertFalse(list(self.pair.fixture.directory.glob('.result-*.json')))

    def test_closed_orchestration_retains_only_historical_qa_without_body_or_clock_refresh(self):
        value=self.commit()
        scope=OrchestrationScope(**{n:getattr(self.pair.binding,n) for n in SCOPE_FIELDS})
        request=OrchestrationRequest(1,'orchestration.close','close-after-qa',scope,
            {'orchestration_handle':self.pair.binding.orchestration_handle,'expected_sequence':1,
             'status':'completed','goal_tokens_used':None,'goal_time_used_seconds':None})
        OrchestrationOwner(self.store).apply(request,client_version='codex-host-v1',before_commit=lambda:None)
        self.store.expire_results(limit=10)
        self.assertFalse(list(self.pair.fixture.directory.glob('.result-*.json')))
        with mock.patch.object(self.store,'_result_now',side_effect=AssertionError('closed QA refreshed a clock')):
            self.assertEqual({**value,'known':True},self.commit())
            self.assertEqual(value['qa_digest'],self.details()['summary']['qa_digest'])

    def test_034_R6_a_qa_record_goes_with_its_receipt_and_request_after_retention(self):
        """0.3.4 R6: QA records were never removed (4,096 per project, ever). After the 90-day
        retention a record goes together with its receipt and request, never alone, since a missing
        QA row beside a consumed receipt is damage."""
        from codex_agents.project_qa_checks import QA_RETENTION_MS
        self.commit()

        def rows():
            return tuple(self.store._connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('project_operational_qa', 'project_qa_check_receipts', 'project_qa_check_requests'))
        self.assertEqual((1, 1, 1), rows())
        ingested = self.store._connection.execute('SELECT ingested_at_ms FROM project_operational_qa').fetchone()[0]
        with mock.patch.object(self.store, '_result_now', return_value=ingested + QA_RETENTION_MS):
            self.store.expire_results(limit=10)
        self.assertEqual((1, 1, 1), rows())
        with mock.patch.object(self.store, '_result_now', return_value=ingested + QA_RETENTION_MS + 1):
            self.store.expire_results(limit=10)
        self.assertEqual((0, 0, 0), rows())

    def test_new_receipt_cannot_replace_the_original_qa_for_one_worker(self):
        value=self.commit()
        second=self.issue(request_id='other-original-qa-checks')['check_receipt']
        with self.assertRaises(HistoryError):
            self.store.commit_operational_qa(self.context,self.worker,self.verifier,second,before_commit=lambda:None)
        self.assertIsNone(read_check_receipt(self.store,second)['consumed_at_ms'])
        self.assertEqual({**value,'known':True},self.commit())
        self.assertEqual(1,self.count())

    def test_transaction_rollback_restores_receipt_qa_record_and_source_sequence(self):
        before=self.store._connection.execute('SELECT next_source_sequence FROM history_identity').fetchone()[0]
        transaction=self.store._transaction
        @contextmanager
        def rollback():
            with transaction():
                yield
                raise OSError('fixture-QA-rollback')
        with mock.patch.object(self.store,'_transaction',new=rollback),self.assertRaises(HistoryError):
            self.commit()
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])
        self.assertEqual(before,self.store._connection.execute('SELECT next_source_sequence FROM history_identity').fetchone()[0])
        self.assertEqual(0,self.store._connection.execute("SELECT COUNT(*) FROM records WHERE source='verifier'").fetchone()[0])

    def test_lost_committed_reply_recovers_same_qa_and_single_consumption(self):
        transaction=self.store._transaction
        @contextmanager
        def lost():
            with transaction():
                yield
            raise OSError('fixture-QA-commit-reply-lost')
        with mock.patch.object(self.store,'_transaction',new=lost),self.assertRaises(HistoryError):
            self.commit()
        self.assertEqual(1,self.count())
        with mock.patch.object(self.store,'_result_now',side_effect=AssertionError('retry clock')):
            self.assertTrue(self.commit()['known'])
        self.assertEqual(1,self.store._connection.execute("SELECT COUNT(*) FROM records WHERE source='verifier'").fetchone()[0])

    def test_missing_qa_after_consumption_is_unknown_and_never_reconstructed(self):
        self.commit()
        self.store._connection.execute('DELETE FROM project_operational_qa')
        with mock.patch.object(self.store,'read_result',side_effect=AssertionError('unknown body reread')):
            with self.assertRaises(HistoryError):
                self.commit()
        self.assertEqual(0,self.count())
        self.assertIsNotNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_missing_superseding_outcome_cannot_reconstruct_from_qa_row(self):
        value=self.commit()
        self.store._connection.execute('DELETE FROM records WHERE record_id=?',(value['outcome_record_id'],))
        with self.assertRaises(HistoryError):
            self.commit()
        with self.assertRaises(HistoryError):
            self.details()
        self.assertEqual(1,self.count())

    def test_missing_original_request_or_admission_refuses_known_qa(self):
        self.commit()
        self.store._connection.execute('DELETE FROM project_qa_check_requests')
        with self.assertRaises(HistoryError):
            self.commit()

    def test_changed_server_assignment_or_pair_cannot_substitute_original_inputs(self):
        self.context=replace(self.context,verifier=replace(self.context.verifier,
            assignment={**self.context.verifier.assignment,'objective':'replacement'}))
        with self.assertRaises(HistoryError):
            self.commit()
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_current_authority_revoked_inside_transaction_rolls_back_consumption(self):
        calls=[]
        def revoke():
            calls.append(self.store._connection.in_transaction)
            if self.store._connection.in_transaction:
                raise HistoryError('history_not_authorized')
        with self.assertRaises(HistoryError):
            self.commit(before_commit=revoke)
        self.assertIn(True,calls)
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_revocation_after_internal_writes_before_commit_rolls_back_all_qa_state(self):
        def revoke():
            if self.store._connection.in_transaction and self.count():
                raise HistoryError('history_not_authorized')
        with self.assertRaises(HistoryError):
            self.commit(before_commit=revoke)
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])
        self.assertEqual(0,self.store._connection.execute("SELECT COUNT(*) FROM records WHERE source='verifier'").fetchone()[0])

    def test_last_authorizer_cannot_remove_original_journal_before_atomic_append(self):
        def remove():
            if self.store._connection.in_transaction:
                self.store._connection.execute('DELETE FROM project_host_opens WHERE run_handle=?',
                    (self.pair.verifier_binding.run_handle,))
        with self.assertRaises(HistoryError):
            self.commit(before_commit=remove)
        self.assertEqual(0,self.count())
        self.assertEqual(2,self.store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_original_result_expiry_prevents_first_qa_but_never_renews_receipt(self):
        self.pair.fixture.base.stamp=read_check_receipt(self.store,self.receipt)['expires_at_ms']
        with self.assertRaises(HistoryError):
            self.commit()
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def changed_worker_event(self):
        identity='host_result_admission:'+keyed_digest(self.pair.fixture.key,
            b'project-host-result-v1\0'+canonical_json(self.pair.binding.as_data()))
        admission=parse_bounded_json(self.store.record(identity).content)
        event=parse_bounded_json(self.store.record(admission['event_record_id']).content)
        event.update(event_code='turn_failed',status='failed')
        admission['event_content_digest']=content_digest(event)
        for record in (event,admission):
            self.store._connection.execute('UPDATE records SET content=?,content_digest=? WHERE record_id=?',
                (canonical_json(record),content_digest(record),record['record_id']))

    def test_changed_host_event_cannot_supply_completed_worker_provenance(self):
        self.changed_worker_event()
        with self.assertRaises(HistoryError):
            self.commit()
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_known_qa_binds_the_original_admission_digest_even_with_recomputed_sha_labels(self):
        self.commit()
        self.changed_worker_event()
        with self.assertRaises(HistoryError):
            self.commit()

    def test_generic_record_append_cannot_mint_verifier_owned_accepted_outcome(self):
        value=self.commit()
        entry=self.store.record(value['outcome_record_id'])
        with self.assertRaises(HistoryError):
            self.store.append_record(entry.content,source='verifier',ingested_at_ms=entry.ingested_at_ms)

    def test_explicit_schema7_upgrade_preserves_original_checks_and_invents_no_qa(self):
        from tests.project_schema_fixtures import assert_migrated_rows, legacy_cleanup_layout
        legacy_cleanup_layout(self.store)
        self.store._connection.execute('DROP TABLE project_operational_qa')
        self.store._connection.execute('PRAGMA user_version=7')
        names=[row[0] for row in self.store._connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        before={n:[tuple(row) for row in self.store._connection.execute('SELECT * FROM '+n)] for n in names}
        capacity=self.store._result_capacity
        self.store.close()
        path=self.pair.fixture.directory/'history.sqlite3'
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError,'history_incompatible'):
            self.pair.fixture.open_history()
        self.assertEqual(digest,hashlib.sha256(path.read_bytes()).hexdigest())
        self.pair.fixture.store=ProjectHistory(self.pair.fixture.directory,self.pair.binding.project_id,
            result_key=self.pair.fixture.key,result_capacity=capacity,
            result_clock_ms=lambda:self.pair.fixture.base.stamp,
            orchestration_limits=OrchestrationLimits(1,8),allow_schema_upgrade=True)
        self.store=self.pair.fixture.store
        self.assertEqual(21, self.store._connection.execute('PRAGMA user_version').fetchone()[0])  # (requirement) B6: schema 21 (decision 131)
        for n,rows in before.items():
            assert_migrated_rows(self, self.store, n, rows)
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def service(self):
        token=object()
        factory=mock.Mock(return_value=self.store)
        def authorize(request,caller):
            if caller is not token:
                return None
            return HistoryAuthorization(request.project_id,request.runtime_digest,history_request_digest(request),
                'host',frozenset({'history.commit_qa','history.qa_details'}),operational_qa_context=self.context)
        service=HistoryService(self.pair.binding.project_id,self.pair.binding.runtime_digest,factory,authorizer=authorize)
        self.addCleanup(service.close)
        return service,token,authorize,factory

    def command(self,operation='history.commit_qa',**extra):
        return HistoryRequest(1,operation,self.receipt,self.pair.binding.project_id,self.pair.binding.runtime_digest,
            {'worker_binding':self.pair.binding.as_data(),'verifier_binding':self.pair.verifier_binding.as_data(),
             'host_outcome_digest':self.worker,'verifier_host_outcome_digest':self.verifier,'check_receipt':self.receipt,**extra})

    def test_closed_api_uses_original_bodies_and_returns_approved_metadata_only(self):
        service,token,_,_=self.service()
        client=HistoryClient(Path('/private/tmp/qa-commit.sock'),self.pair.binding.project_id,
            self.pair.binding.runtime_digest,timeout=1)
        with mock.patch.object(history_api,'exchange_frame',side_effect=lambda path,raw,**options:service.handle(raw,token)) as exchange:
            value=client.commit_operational_qa(self.pair.binding,self.pair.verifier_binding,self.worker,self.verifier,self.receipt)
            details=client.operational_qa_details(self.pair.binding,self.pair.verifier_binding,self.worker,self.verifier,self.receipt)
        self.assertEqual(2,exchange.call_count)
        self.assertTrue(value['accepted'])
        self.assertEqual({**value,'known':True},details['summary'])
        self.assertNotIn(b'PRIVATE-',canonical_json(details))
        for extra in ({'accepted':True},{'report':self.report},{'worker_result':{}},{'assignment':{}},{'live':False}):
            with self.subTest(extra=extra.keys()),self.assertRaisesRegex(HistoryError,'history_invalid_request'):
                self.command(**extra)

    def test_api_default_denial_and_wrong_original_binding_precede_writer_open(self):
        factory=mock.Mock(return_value=self.store)
        service=HistoryService(self.pair.binding.project_id,self.pair.binding.runtime_digest,factory)
        self.addCleanup(service.close)
        command=self.command()
        with self.assertRaisesRegex(HistoryError,'history_not_authorized'):
            decode_history_response(service.handle(encode_history_request(command)),command)
        factory.assert_not_called()
        service,token,_,factory=self.service()
        self.context=replace(self.context,verifier=replace(self.context.verifier,
            binding=replace(self.pair.verifier_binding,supervisor_generation=2)))
        with self.assertRaisesRegex(HistoryError,'history_not_authorized'):
            decode_history_response(service.handle(encode_history_request(command),token),command)
        factory.assert_not_called()
        self.assertEqual(0,self.count())

    def test_full_qa_details_exceed_compact_limit_without_changing_frame_bounds(self):
        for name in ('deterministic_checks','security_checks'):
            self.report[name]=[{'check_id':str(i),'passed':True,'evidence_digest':'a'*64} for i in range(256)]
        self.assertLess(len(canonical_json(self.report)),65_536)
        self.receipt=self.issue(request_id='large-approved-report')['check_receipt']
        value=self.commit()
        service,token,_,_=self.service()
        client=HistoryClient(Path('/private/tmp/qa-large.sock'),self.pair.binding.project_id,self.pair.binding.runtime_digest,timeout=1)
        with mock.patch.object(history_api,'exchange_frame',side_effect=lambda path,raw,**options:service.handle(raw,token)) as exchange:
            detail=client.operational_qa_details(self.pair.binding,self.pair.verifier_binding,self.worker,self.verifier,self.receipt)
        self.assertGreater(len(canonical_json(detail)),16_384)
        self.assertLess(len(canonical_json(detail)),MAX_FRAME_BYTES)
        self.assertEqual(MAX_FRAME_BYTES,exchange.call_args.kwargs['response_limit'])
        self.assertEqual(value['qa_digest'],content_digest(detail['qa']))

    def test_revoke_after_private_body_io_prevents_qa_commit(self):
        service,token,authorize,_=self.service()
        calls=[]
        def revoke(request,caller):
            calls.append(True)
            return authorize(request,caller) if len(calls)==1 else None
        service._authorizer=revoke
        command=self.command()
        with self.assertRaisesRegex(HistoryError,'history_not_authorized'):
            decode_history_response(service.handle(encode_history_request(command),token),command)
        self.assertEqual(0,self.count())
        self.assertIsNone(read_check_receipt(self.store,self.receipt)['consumed_at_ms'])

    def test_pause_during_success_encoding_withholds_fresh_success_but_keeps_original_commit(self):
        service,token,_,_=self.service()
        reply=service._reply
        def pause_after_encoding(*args,**options):
            raw=reply(*args,**options)
            if options.get('result') is not None:
                self.pair.pause()
            return raw
        command=self.command()
        with mock.patch.object(service,'_reply',side_effect=pause_after_encoding):
            with self.assertRaisesRegex(HistoryError,'history_orchestration_state'):
                decode_history_response(service.handle(encode_history_request(command),token),command)
        self.assertEqual(1,self.count())
        self.assertTrue(self.commit()['known'])

    def test_client_lost_committed_response_is_not_automatically_retried(self):
        service,token,_,_=self.service()
        client=HistoryClient(Path('/private/tmp/qa-lost.sock'),self.pair.binding.project_id,self.pair.binding.runtime_digest,timeout=1)
        def lose(path,raw,**options):
            service.handle(raw,token)
            raise ServiceTransportError('transport_unavailable')
        with mock.patch.object(history_api,'exchange_frame',side_effect=lose) as exchange:
            with self.assertRaisesRegex(HistoryError,'history_service_unavailable'):
                client.commit_operational_qa(self.pair.binding,self.pair.verifier_binding,self.worker,self.verifier,self.receipt)
        exchange.assert_called_once()
        self.assertEqual(1,self.count())
        self.assertTrue(self.commit()['known'])

    def test_closed_qa_responses_reject_flags_counts_unknown_fields_and_changed_hashes(self):
        service,token,_,_=self.service()
        command=self.command()
        original=parse_bounded_json(service.handle(encode_history_request(command),token))
        for field,value in (('accepted',1),('required_criteria_total',True),('qa_digest','copied'),('body','PRIVATE-FORGERY')):
            changed=deepcopy(original)
            changed['result'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(HistoryError,'history_invalid_response'):
                decode_history_response(canonical_json(changed),command)


class ProjectNegativeQATests(unittest.TestCase):
    def test_independent_negative_verifier_is_retained_as_rejected_qa_without_repair(self):
        base=ProjectQACommitTests(methodName='runTest')
        base.verifier_changes={'semantic_verdict':'not-ready','execution_state':'completed','admission_state':'unverified',
            'retry':{'attempt':1,'retry_of_assignment_id':None},'evidence':[{'criterion_id':'marker','type':'artifact',
                'reference':'fixture-evidence','outcome':'fail','details':'fixture negative assessment'}],
            'remaining_risks':['fixture unresolved issue']}
        base.setUp()
        self.addCleanup(base.doCleanups)
        value=base.commit()
        self.assertFalse(value['accepted'])
        self.assertTrue(value['verifier_disagreement'])
        self.assertEqual(0,value['required_criteria_passed'])
        outcome=parse_bounded_json(base.store.record(value['outcome_record_id']).content)
        self.assertEqual('rejected',outcome['status'])
        self.assertEqual(base.original_host_rows,[tuple(row) for row in base.store._connection.execute(
            "SELECT * FROM records WHERE source='host' ORDER BY source_sequence")])

    def test_failed_verifier_retains_failed_original_and_never_becomes_accepted(self):
        base=ProjectQACommitTests(methodName='runTest')
        base.verifier_changes={'status':'failed','execution_state':'failed','semantic_verdict':'not-applicable',
            'admission_state':'unverified','retry':{'attempt':1,'retry_of_assignment_id':None},
            'evidence':[],'remaining_risks':['fixture client returned failure']}
        base.setUp()
        self.addCleanup(base.doCleanups)
        value=base.commit()
        self.assertFalse(value['accepted'])
        self.assertTrue(value['verifier_disagreement'])
        self.assertEqual(0,value['required_criteria_passed'])
        self.assertEqual(base.original_host_rows,[tuple(row) for row in base.store._connection.execute(
            "SELECT * FROM records WHERE source='host' ORDER BY source_sequence")])


QA_COMMIT_SERVER = r'''
import json,socket,sys
from tests.test_project_qa_commit import ProjectQACommitTests
fixture=ProjectQACommitTests(methodName='runTest');fixture.setUp()
try:
    service,token,_,_=fixture.service()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.settimeout(15);listener.bind(sys.argv[1]);listener.listen(2)
        print(json.dumps({'endpoint':sys.argv[1],'project':fixture.pair.binding.project_id,
            'runtime':fixture.pair.binding.runtime_digest,'worker':fixture.pair.binding.as_data(),
            'verifier':fixture.pair.verifier_binding.as_data(),'worker_digest':fixture.worker,
            'verifier_digest':fixture.verifier,'receipt':fixture.receipt}),flush=True)
        for _ in range(2):
            connection,_=listener.accept()
            with connection:
                connection.settimeout(5);data=bytearray()
                while not data.endswith(b'\n') and len(data)<16384:
                    part=connection.recv(4096)
                    if not part:raise RuntimeError('fixture_incomplete_request')
                    data.extend(part)
                if not data.endswith(b'\n') or len(data)>16384:raise RuntimeError('fixture_invalid_frame')
                connection.sendall(service.handle(bytes(data[:-1]),token)+b'\n')
finally:fixture.doCleanups()
'''

QA_COMMIT_CLIENT = r'''
import json,sys
from pathlib import Path
class NoSql:
    def find_spec(self,fullname,path=None,target=None):
        if fullname in ('sqlite3','_sqlite3'):raise AssertionError('client imported SQLite')
sys.meta_path.insert(0,NoSql())
from codex_agents.history_api import HistoryClient
from codex_agents.history_contracts import AttemptBinding
v=json.load(sys.stdin);client=HistoryClient(Path(v['endpoint']),v['project'],v['runtime'],timeout=5)
args=(AttemptBinding(**v['worker']),AttemptBinding(**v['verifier']),v['worker_digest'],v['verifier_digest'],v['receipt'])
summary=client.commit_operational_qa(*args);details=client.operational_qa_details(*args)
assert summary['accepted'] and details['summary']=={**summary,'known':True}
print(json.dumps({'summary':summary,'qa_digest':details['summary']['qa_digest'],'sqlite_loaded':'sqlite3' in sys.modules}))
'''


class ProjectQASocketCommitTests(unittest.TestCase):
    def test_actual_separate_writer_and_sqlite_free_client_commit_and_read_qa(self):
        with tempfile.TemporaryDirectory(prefix='qa-commit-',dir=SHORT_TMP) as temporary:
            endpoint=Path(temporary)/'qa.sock'
            program='import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');'+QA_COMMIT_SERVER
            server=subprocess.Popen([sys.executable,'-I','-B','-S','-c',program,str(endpoint)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertTrue(select.select([server.stdout],[],[],20)[0],'fixture server did not become ready')
                line=server.stdout.readline()
                if not line:
                    self.fail(server.stderr.read(3000))
                program='import sys;sys.path.insert(0,'+repr(str(ORCHESTRATION_ROOT))+');'+QA_COMMIT_CLIENT
                client=subprocess.run([sys.executable,'-I','-B','-S','-c',program],input=line,
                    capture_output=True,text=True,timeout=20)
                self.assertEqual(0,client.returncode,client.stderr)
                value=json.loads(client.stdout)
                self.assertFalse(value['sqlite_loaded'])
                self.assertEqual(value['summary']['qa_digest'],value['qa_digest'])
                self.assertNotIn('PRIVATE-',client.stdout)
                self.assertEqual(0,server.wait(timeout=15),server.stderr.read(3000))
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=10)
                server.stdout.close()
                server.stderr.close()
