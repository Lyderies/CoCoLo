"""Original-run fixed failure metadata with inert providers and synthetic enrollment."""
from dataclasses import asdict
import json
import unittest
from unittest import mock

from tests import test_account_observation_runs as fixtures
from codex_agents import account_api, account_observation, providers
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountClient, AccountError, AccountRequest, account_request_digest,
    decode_account_response, encode_account_request,
)
from codex_agents.account_observation import AccountObservationOwner, run_observation_probe
from codex_agents.account_observation_failure import record_observation_failure
from codex_agents.account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import content_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, DEFAULT_RETENTION_MS, LearningLedger
from codex_agents.observation_failure_contracts import ObservationFailureInput, decode_observation_failure
from codex_agents.provider_observation_contracts import ObservationError
from codex_agents.service_transport import ServiceTransportError
from codex_agents.sql_read_budget import SQLReadLimits


class AccountObservationFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountObservationRunTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner, self.ledger, self.project = self.fixture.owner, self.fixture.ledger, self.fixture.project
        self.source, self.allowed, self.token = 'host', True, object()
        self.jobs = AccountObservationJobs(self.owner, ObservationJobLimits(1, 2, 1, 1))
        # Fixed metadata needs an original configured owner, not live probe workers.
        self.addCleanup(self.jobs.request_close)
        def authorize(request, token):
            if token is not self.token or not self.allowed:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                self.source, ACCOUNT_OPERATIONS, 'b' * 64, observation_context=self.fixture.context)
        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.owner._views,
                                      authorizer=authorize, observation_jobs=self.jobs)
        self.addCleanup(self.service.close)

    def request(self, identity='failure-one', *, phase='before', reason='observation_failed', provider='codex'):
        return AccountRequest(1, 'account.record_observation_unavailable', identity, self.project, COLLECTOR_RUNTIME_DIGEST,
            ObservationFailureInput(provider, self.fixture.handle, phase, reason).payload())

    def call(self, request=None):
        request = request or self.request()
        return decode_account_response(self.service.handle(encode_account_request(request), self.token), request)

    def count(self, table):
        assert table in {'account_observation_origins', 'account_observation_requests', 'launch_claims'}
        return self.ledger._connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_fixed_record_and_known_reply_do_not_probe_or_grant_launch(self):
        with mock.patch.object(providers, 'observe_codex', side_effect=AssertionError('metadata must not probe')):
            fresh = self.call()
            marker = self.fixture.marker()
            self.fixture.stamp += 1
            self.assertEqual({**fresh, 'known': True}, self.call())
        self.assertFalse(fresh['known'])
        self.assertEqual('observation_unavailable', fresh['record']['record_type'])
        self.assertEqual('observation_failed', fresh['record']['reason_code'])
        self.assertEqual(marker, self.fixture.marker())
        self.assertNotEqual(content_digest(fresh['record']), marker[0])
        self.assertEqual(0, self.count('launch_claims'))
        self.assertEqual(1, self.count('account_observation_origins'))
        self.assertEqual(0, self.jobs.snapshot()['jobs'])

    def test_changed_reason_or_probe_identity_conflicts_without_adopting_pending_work(self):
        fresh = self.call()
        with self.assertRaisesRegex(AccountError, 'observation_conflict'):
            self.call(self.request(reason='client_unavailable'))
        pending = self.fixture.reserve('probe-pending')
        marker = self.fixture.marker()
        with self.assertRaisesRegex(AccountError, 'observation_conflict'):
            self.call(self.request('probe-pending'))
        self.assertIs(pending, self.fixture.reserve('probe-pending'))
        self.assertEqual(marker, self.fixture.marker())
        self.assertEqual({**fresh, 'known': True}, self.call())

    def test_before_failure_blocks_older_inflight_probe_and_known_reply_cannot_replace_new_identity(self):
        pending = self.fixture.reserve('old-probe')
        completion = run_observation_probe(pending.capture)
        failure = self.call()
        marker = self.fixture.marker()
        with self.assertRaisesRegex(ObservationError, 'observation_conflict'):
            self.owner.complete(pending, completion)
        self.assertEqual(marker, self.fixture.marker())
        self.fixture.stamp += 1
        latest = self.fixture.reserve('latest-probe')
        self.owner.complete(latest, run_observation_probe(latest.capture))
        marker = self.fixture.marker()
        self.assertEqual({**failure, 'known': True}, self.call())
        self.assertEqual(marker, self.fixture.marker())
        self.call(self.request('after', phase='after'))
        self.assertEqual(marker, self.fixture.marker())

    def test_revocation_after_actual_record_write_rolls_back_original_journals_and_marker(self):
        append = self.ledger._append_checked_locked
        before = self.fixture.provider_count()
        def revoke(*args, **kwargs):
            value = append(*args, **kwargs)
            self.allowed = False
            return value
        with mock.patch.object(self.ledger, '_append_checked_locked', new=revoke):
            with self.assertRaisesRegex(AccountError, 'observation_not_authorized'):
                self.call()
        self.assertEqual(before, self.fixture.provider_count())
        self.assertEqual(0, self.count('account_observation_origins'))
        self.assertEqual(0, self.count('account_observation_requests'))
        self.assertIsNone(self.fixture.marker())

    def test_roles_and_wrong_original_provider_refuse_without_journals(self):
        for source in ('owner', 'hook', 'app_server'):
            self.source = source
            with self.subTest(source=source), self.assertRaisesRegex(AccountError, 'account_not_authorized'):
                self.call()
        self.source = 'project'
        with self.assertRaisesRegex(AccountError, 'observation_not_authorized'):
            self.call(self.request(provider='claude'))
        self.assertEqual(0, self.count('account_observation_origins'))
        self.assertFalse(self.call()['known'])

    def test_root_change_after_write_and_deadline_cutoff_roll_back_every_new_row(self):
        append = self.ledger._append_checked_locked
        before = self.fixture.provider_count()
        for change in ('root', 'deadline'):
            def changed(*args, **kwargs):
                result = append(*args, **kwargs)
                if change == 'root':
                    self.ledger._connection.execute('UPDATE projects SET canonical_root=? WHERE project_id=?',
                        (str(self.fixture.fixture.fixture.tools.parent / 'replacement-root'), self.project))
                else:
                    self.fixture.stamp += self.owner._timeout
                return result
            with self.subTest(change=change), mock.patch.object(self.ledger, '_append_checked_locked', new=changed):
                with self.assertRaisesRegex(AccountError, 'observation_not_authorized'):
                    self.call()
            self.assertEqual(before, self.fixture.provider_count())
            self.assertEqual(0, self.count('account_observation_origins'))
            self.assertEqual(0, self.count('account_observation_requests'))
            self.assertIsNone(self.fixture.marker())

    def test_known_metadata_expiry_and_missing_companions_never_remint(self):
        fresh = self.call()
        with mock.patch.object(self.ledger, '_clock_ms', return_value=fresh['observed_at_ms'] + DEFAULT_RETENTION_MS):
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                self.call()
        self.ledger._connection.execute('DELETE FROM account_observation_requests')
        with self.assertRaisesRegex(AccountError, 'observation_conflict'):
            self.call()
        self.assertEqual(1, self.count('account_observation_origins'))
        self.assertEqual(0, self.count('account_observation_requests'))

    def test_shared_origin_quota_does_not_block_original_known_reply(self):
        with mock.patch.object(account_observation, 'MAX_OBSERVATION_REQUESTS', 1):
            fresh = self.call()
            self.assertEqual({**fresh, 'known': True}, self.call())
            with self.assertRaisesRegex(AccountError, 'observation_full'):
                self.call(self.request('second'))
            with self.assertRaisesRegex(ObservationError, 'observation_full'):
                self.fixture.reserve('new-probe')
        self.assertEqual(1, self.count('account_observation_requests'))

    def test_reply_encoding_cannot_resurrect_deleted_record(self):
        encode = self.service._reply
        def removed(labels, *, result=None, reason=None):
            raw = encode(labels, result=result, reason=reason)
            if result is not None:
                self.ledger._connection.execute("DELETE FROM records WHERE record_type='observation_unavailable'")
            return raw
        with mock.patch.object(self.service, '_reply', new=removed):
            with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
                self.call()
        self.assertEqual(1, self.count('account_observation_requests'))
        with self.assertRaisesRegex(AccountError, 'observation_unavailable'):
            self.call()

    def test_original_committed_failure_survives_reopen_without_reprobe(self):
        fresh = self.call()
        bridge = self.fixture.fixture.fixture
        self.ledger.close()
        reopened = LearningLedger(bridge.state, client_capabilities=bridge.clients)
        self.addCleanup(reopened.close)
        owner = AccountObservationOwner(AccountOwnerViews(reopened), SQLReadLimits(10_000, 1000, 1_048_576))
        reply = record_observation_failure(owner, self.project, COLLECTOR_RUNTIME_DIGEST, 'b' * 64, 'failure-one',
            ObservationFailureInput(**self.request().payload), self.fixture.context, lambda: True)
        self.assertEqual({**fresh, 'known': True}, reply)

    def test_closed_input_and_client_single_exchange_without_fallback(self):
        payload = self.request().payload
        for value in ({**payload, 'text': 'private marker'}, {**payload, 'reason_code': 'private marker'},
                      {**payload, 'phase': 'during'}, {**payload, 'run_handle': None}):
            with self.assertRaisesRegex(ObservationError, 'observation_invalid_request'):
                decode_observation_failure(value)
        client = AccountClient(self.fixture.fixture.fixture.state.socket, self.project, COLLECTOR_RUNTIME_DIGEST)
        def exchange(path, raw, **kwargs):
            return self.service.handle(raw, self.token)
        with mock.patch.object(account_api, 'exchange_frame', side_effect=exchange) as transport:
            reply = client.record_observation_unavailable('codex', run_handle=self.fixture.handle,
                phase='before', reason_code='observation_failed', request_id='client-one')
            self.assertFalse(reply['known'])
            self.assertEqual(1, transport.call_count)
        with mock.patch.object(account_api, 'exchange_frame', side_effect=ServiceTransportError('unavailable')) as transport:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                client.record_observation_unavailable('codex', run_handle=self.fixture.handle,
                    phase='before', reason_code='observation_failed', request_id='lost-reply')
            self.assertEqual(1, transport.call_count)

    def test_malformed_wire_frame_has_fixed_reason_without_sql_or_private_text(self):
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            for change in ({'reason_code': 'PRIVATE-FAILURE-MARKER'}, {'record': {'text': 'PRIVATE-FAILURE-MARKER'}}):
                body = asdict(self.request())
                body['payload'].update(change)
                raw = self.service.handle(json.dumps(body).encode(), self.token)
                self.assertEqual('account_invalid_request', json.loads(raw)['reason_code'])
                self.assertNotIn(b'PRIVATE-FAILURE-MARKER', raw)
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual([], statements)
        self.assertEqual(0, self.count('account_observation_origins'))
