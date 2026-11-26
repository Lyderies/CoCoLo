"""Slice B2 fix round 1: authenticated requests and the primary's record command (rev040b2r1b M2, M3, M7).

The request path is the product's: the host's own `_authorize_history` (enrolment, owner label, stored context of the
host class) decides what a request may do, the history service runs it on the project's writer, and the record command
of the client runs against that service. Only the transport is replaced by a direct call (no sockets, no collector),
and the orchestration status and result read of the command are answered by the same store. Time budget: scenario.
"""
import json
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests import test_record_context as rc
from tests.test_b2_service_outcome import B2Fixture, OTHER_RUNTIME, requirement
from tests.test_bridge import PARENT_GRANT

from codex_agents import client_runtime, history_api, service_record
from codex_agents.history_api import HistoryRequest, decode_history_response, encode_history_request
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryService
from codex_agents.hosts import CLAUDE_HOST_CLIENT as CLAUDE, CODEX_HOST_CLIENT as CODEX, OWNER_CLIENT
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationRequest, OrchestrationScope, SCOPE_FIELDS
from codex_agents.orchestration_store import OrchestrationOwner
from codex_agents.service_host import ProjectServiceHost


class Persistence:
    """The lane's reading methods, answered by the writer's store (the host reads them on the writer thread)."""

    def __init__(self, store):
        self.store = store

    def stored_result_context(self, binding, host_class, *, recorded=False):
        return self.store.stored_result_context(binding, host_class=host_class, recorded=recorded)

    def stored_worker_context(self, outcome_digest, host_class, *, orchestration_handle=None):
        return self.store.stored_worker_context(outcome_digest, host_class=host_class,
                                                orchestration_handle=orchestration_handle)

    def stored_attempt_context(self, run_handle, attempt, host_class, inputs_digest):
        return self.store.stored_attempt_context(run_handle, attempt, host_class=host_class, inputs_digest=inputs_digest)


class HostFixture(B2Fixture):
    """The B2 run behind the product host's authorization: a caller is enrolled as a host class, the owner, or not."""

    def serve(self, host_client=CODEX, *, enrolled=True, runtime=None):
        self.store._orchestration_limits = OrchestrationLimits(1, 8)
        token = object()
        entry = SimpleNamespace(kind='project', enrolled=SimpleNamespace(role='host', host_client=host_client,
                                                                         context=None, request_digest='0' * 64))
        stub = SimpleNamespace(_frontend=SimpleNamespace(context=lambda given: entry if enrolled and given is token else None),
                               _driver=object(), _runtime=SimpleNamespace(persistence=Persistence(self.store)))
        stub._stored = types.MethodType(ProjectServiceHost._stored, stub)
        service = HistoryService(self.binding.project_id, runtime or self.binding.runtime_digest, lambda: self.store,
                                 authorizer=lambda request, context: ProjectServiceHost._authorize_history(stub, request, context),
                                 close_history=False)
        self.addCleanup(service.close)
        return service, token

    def ask(self, service, token, operation, identity, payload, runtime=None):
        request = HistoryRequest(1, operation, identity, self.binding.project_id,
                                 runtime or self.binding.runtime_digest, payload)
        return decode_history_response(service.handle(encode_history_request(request), token), request)

    def admit(self, service, token):
        return self.ask(service, token, 'history.admit_host_result', self.binding.claim_id, self.reference.as_data())

    def refusal(self, call):
        with self.assertRaises(HistoryError) as caught:
            call()
        return caught.exception.reason_code


@requirement
class AuthenticatedRequestTests(HostFixture):
    """B2-6: the service's own entry has no request; the primary's request is authorized as before, by exact reason."""

    def test_an_enrolled_primary_records_through_the_service_and_an_earlier_service_record_is_only_retrieved(self):
        service, token = self.serve()
        recorded = self.service_record()
        before = self.counts()
        reply = self.admit(service, token)
        self.assertEqual(recorded['admission'], reply['admission'])
        self.assertEqual(before, self.counts())
        self.assertEqual('service', self.recorder())

    def test_an_enrolled_primary_that_records_first_is_attributed_to_the_primary(self):
        service, token = self.serve()
        self.admit(service, token)
        self.assertEqual('primary', self.recorder())
        self.assertIsNone(self.service_record())

    def test_an_unenrolled_caller_the_owner_label_and_the_other_host_class_are_refused_with_zero_rows(self):
        cases = {'unenrolled': dict(enrolled=False), 'owner': dict(host_client=OWNER_CLIENT),
                 'other host class': dict(host_client=CLAUDE)}
        reasons = {}
        for name, options in cases.items():
            service, token = self.serve(**options)
            before = self.counts()
            reasons[name] = self.refusal(lambda: self.admit(service, token))
            self.assertEqual(before, self.counts(), name)
            self.assertIsNone(self.recorder(), name)
        self.assertEqual({'unenrolled': 'history_not_authorized', 'owner': 'history_not_authorized',
                          'other host class': 'history_not_authorized'}, reasons)

    def test_the_same_refusals_hold_after_the_service_has_recorded_and_nothing_is_retrieved_for_them(self):
        self.service_record()
        before = self.counts()
        for options in (dict(enrolled=False), dict(host_client=OWNER_CLIENT), dict(host_client=CLAUDE)):
            service, token = self.serve(**options)
            self.assertEqual('history_not_authorized', self.refusal(lambda: self.admit(service, token)))
        self.assertEqual(before, self.counts())

    def test_the_service_entry_is_not_a_request_and_a_forged_operation_is_refused_by_the_decoder(self):
        service, token = self.serve()
        with self.assertRaises(HistoryError) as caught:
            self.ask(service, token, 'history.admit_service_result', 'x', {})
        self.assertEqual('history_invalid_request', caught.exception.reason_code)
        self.assertFalse(any('service' in name for name in history_api.HISTORY_OPERATIONS if 'result' in name))

    def test_the_orchestration_report_of_a_service_names_its_own_runtime(self):
        service, token = self.serve(runtime=OTHER_RUNTIME)
        with mock.patch('codex_agents.project_orchestration_report._orchestration') as found:
            snapshot = {'status': 'active', 'started_at_ms': 1, 'ended_at_ms': None, 'observed_at_ms': 1,
                        'sequence': 1, 'goal_tokens_used': None, 'goal_time_used_seconds': None}
            found.return_value = (self.binding.orchestration_handle, [snapshot], 1)
            reply = self.ask(service, token, 'history.orchestration_report', 'orc:' + 'a' * 64,
                             {'orchestration_id': 'orc:' + 'a' * 64}, runtime=OTHER_RUNTIME)
        self.assertEqual({'other_runtime': 1}, reply['recording']['unrecordable'])


@requirement
class ServiceRecordedFeedsTheVerifierTests(HostFixture):
    """B2-11: a service-recorded outcome feeds the verifier's open, the worker input and the routing evidence exactly
    as a primary-recorded one does; the only difference is the marker that it was not reviewed."""

    def surfaces(self, recorded_by):
        service, token = self.serve()
        record = self.service_record if recorded_by == 'service' else self.primary_record
        digest = record()['admission']['host_outcome_digest']
        worker = self.ask(service, token, 'history.worker_input', digest, {'host_outcome_digest': digest})
        context = self.store.stored_worker_context(digest, host_class=CODEX,
                                                   orchestration_handle=self.binding.orchestration_handle)
        return digest, worker, context.snapshot()

    def test_worker_input_and_the_verifier_open_context_are_the_same_for_either_recorder(self):
        other = self.copy_store()
        digest_s, worker_s, context_s = self.surfaces('service')
        self.fixture.store = other
        digest_p, worker_p, context_p = self.surfaces('primary')
        self.assertEqual((digest_s, worker_s, context_s), (digest_p, worker_p, context_p))
        other.close()

    def test_a_verifier_open_for_the_service_recorded_worker_is_authorized(self):
        service, token = self.serve()
        digest = self.service_record()['admission']['host_outcome_digest']
        context = self.store.stored_worker_context(digest, host_class=CODEX,
                                                   orchestration_handle=self.binding.orchestration_handle)
        self.assertEqual(self.binding.as_data(), context.snapshot()[1]['binding'])
        with self.assertRaises(HistoryError):  # another orchestration's verifier cannot use it
            self.store.stored_worker_context(digest, host_class=CODEX, orchestration_handle='orc_' + 'f' * 48)

    def test_routing_evidence_asks_the_same_question_of_either_recorder(self):
        answers = {}
        other = self.copy_store()
        for name, store, record in (('service', other, self.service_record), ('primary', None, self.primary_record)):
            if store is not None:
                self.fixture.store = store
            service, token = self.serve()
            digest = record()['admission']['host_outcome_digest']
            payload = {'host_outcome_digest': digest, 'task_id': self.task['task_id'] if 'task_id' in self.task else self.task['id'], 'family': self.task['family']}
            answers[name] = self.ask(service, token, 'history.routing_evidence', digest, payload)
        self.assertEqual(answers['service'], answers['primary'])
        other.close()

    def test_the_unreviewed_marker_is_on_the_status_the_report_and_the_command_output(self):
        from codex_agents import project_orchestration_report as report
        self.service_record()
        snapshot = {'status': 'active', 'started_at_ms': 1, 'ended_at_ms': None, 'observed_at_ms': 1, 'sequence': 1,
                    'goal_tokens_used': None, 'goal_time_used_seconds': None}
        with mock.patch.object(report, '_orchestration', return_value=(self.binding.orchestration_handle, [snapshot], 1)):
            document = report.orchestration_report(self.store, 'orc:' + 'a' * 64,
                                                   runtime_digest=self.binding.runtime_digest)
        marker = 'recorded by the service, not reviewed by the primary'
        self.assertEqual(marker, document['recording']['recorded'][0]['note'])
        self.assertIn(marker, ' '.join(document['recording']['lines']))
        status = service_record.recording_status(self.store, self.authority())
        self.assertEqual(marker, status['recorded'][0]['note'])


@requirement
class RecordCommandTests(HostFixture):
    """B2-5, B2-10: the primary's record command against the service, each refusal named, the stored rows unchanged."""

    def run_command(self, *, host_client=CODEX, body=None, assignment=None, run_handle=None, enrolled=True):
        service, token = self.serve(host_client, enrolled=enrolled)
        scope = OrchestrationScope(**{name: getattr(self.binding, name) for name in SCOPE_FIELDS})
        store = self.store

        class Status:
            def __init__(self, endpoint, given, *, timeout):
                pass

            def call(self, operation, payload):
                request = OrchestrationRequest(1, operation, 'b2-status', scope, payload)
                return OrchestrationOwner(store).apply(request, client_version=host_client, before_commit=lambda: None)

        class Reads:
            def __init__(self, endpoint, *, timeout):
                pass

            def read_result(self, binding, digest):
                return store.read_result(binding)

        record = json.loads(store.record(self.delivery.record_id).content)
        observation = {'run_handle': run_handle or self.binding.run_handle,
                       'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}],
                       'terminal_state': record['terminal_state']}
        with mock.patch.object(history_api, 'exchange_frame', lambda endpoint, raw, **kw: service.handle(raw, token)), \
                mock.patch.object(client_runtime, 'CollectorPaths', lambda root: root), \
                mock.patch.object(client_runtime, 'read_runtime_hint',
                                  lambda state, project: (scope, Path('/private/tmp/b2-history.sock'),
                                                          Path('/private/tmp/b2-result.sock'))), \
                mock.patch.object(client_runtime, 'enroll_clients', lambda *a, **k: {}), \
                mock.patch.object(client_runtime, 'OrchestrationClient', Status), \
                mock.patch.object(client_runtime, 'ResultReadClient', Reads):
            return client_runtime.record_codex_result_client(
                '/state', '/project', self.task, self.route, assignment or self.assigned, PARENT_GRANT,
                json.loads(self.body) if body is None else body, observation, host_client=host_client)

    def refused(self, reason, **options):
        before = (self.counts(), self.store._connection.execute('SELECT * FROM project_result_recorders').fetchall())
        with self.assertRaises(client_runtime.RuntimeResultError) as caught:
            self.run_command(**options)
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertEqual(before, (self.counts(), self.store._connection.execute(
            'SELECT * FROM project_result_recorders').fetchall()))

    def test_the_command_records_a_published_result_as_the_primary(self):
        reply = self.run_command()
        self.assertTrue(reply['ready'])
        self.assertEqual('primary', self.recorder())
        self.assertNotIn('already_recorded_by', reply)

    def test_the_command_says_who_recorded_when_the_service_was_first(self):
        recorded = self.service_record()
        before = self.counts()
        reply = self.run_command()
        self.assertEqual(recorded['admission'], reply['admission'])
        self.assertEqual('service', reply['already_recorded_by'])
        self.assertEqual('recorded by the service, not reviewed by the primary', reply['recorded_note'])
        self.assertEqual(before, self.counts())  # a retrieval writes nothing
        self.assertEqual('service', self.recorder())

    def test_a_body_that_is_not_the_stored_one_is_refused_by_name_and_writes_nothing(self):
        self.refused('recheck_result_mismatch', body={**json.loads(self.body), 'summary': 'another answer'})
        self.assertEqual('committed', rc.context_row(self.store, *self.attempt)[0])

    def test_other_original_inputs_are_refused_by_name_and_write_nothing(self):
        self.refused('recheck_inputs_mismatch', assignment={**self.assigned, 'objective': 'something else'})

    def test_an_unknown_run_is_refused_as_not_found_and_writes_nothing(self):
        self.refused('dispatch_not_found', run_handle='hrn_' + '9' * 48)

    def test_the_other_host_class_and_an_unenrolled_caller_are_refused_and_write_nothing(self):
        for options in (dict(host_client=CLAUDE), dict(enrolled=False)):
            with self.subTest(options=options):
                before = self.counts()
                with self.assertRaises(Exception) as caught:
                    self.run_command(**options)
                self.assertIn(getattr(caught.exception, 'reason_code', None),
                              {'history_not_authorized', 'dispatch_not_found', 'host_client_mismatch'})
                self.assertEqual(before, self.counts())

    def test_a_service_recorded_run_still_refuses_a_wrong_body_by_name(self):
        self.service_record()
        before = self.counts()
        self.refused('recheck_result_mismatch', body={**json.loads(self.body), 'summary': 'another answer'})
        self.assertEqual(before, self.counts())


class OrchestrationCountsAccessTests(HostFixture):
    """s2fix round 3 (L14.1, decision 185): the counts are subject-scoped: the enrolled primary of this project, or the
    owner, reads them; any other credential is refused with the uniform denial."""

    def counts_call(self, **options):
        service, token = self.serve(**options)
        return self.ask(service, token, 'history.orchestration_counts', 'orchestrations', {})

    def test_the_enrolled_primary_of_this_project_and_the_owner_read_the_counts(self):
        for client in (CODEX, CLAUDE, OWNER_CLIENT):
            with self.subTest(client=client):
                reply = self.counts_call(host_client=client)
                self.assertEqual({'active', 'paused', 'closed'}, set(reply))

    def test_another_projects_credential_and_an_unenrolled_caller_are_refused_uniformly(self):
        reasons = {}
        for name, options in {'unenrolled (another project holds no credential here)': dict(enrolled=False)}.items():
            reasons[name] = self.refusal(lambda: self.counts_call(**options))
        service, token = self.serve()
        reasons['request naming another project'] = self.refusal(lambda: decode_history_response(
            service.handle(encode_history_request(HistoryRequest(
                1, 'history.orchestration_counts', 'orchestrations', 'prj_' + '9' * 32, self.binding.runtime_digest, {})),
                token), HistoryRequest(1, 'history.orchestration_counts', 'orchestrations', 'prj_' + '9' * 32,
                                       self.binding.runtime_digest, {})))
        self.assertEqual({'history_not_authorized'}, set(reasons.values()), reasons)

    def test_learning_status_asks_with_its_real_credential_not_the_owner_label(self):
        import re
        source = (Path(__file__).resolve().parents[1] / 'native-package/.codex/orchestration/cli.py').read_text()
        call = re.search(r'orchestration_counts_client\(args\.state_root, args\.project_root, host_client=([^)]*)\)', source)
        self.assertEqual('label', call.group(1).strip())
