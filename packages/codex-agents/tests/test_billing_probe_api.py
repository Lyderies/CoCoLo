"""Closed billing wire/client without provider, database or model execution."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

from tests.learning_support import provider_fixture
from codex_agents import account_api
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, decode_account_request
from codex_agents.billing_probe_contracts import BillingProbeError, checked_billing_probe_reply
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.providers import claude_rate_limit_records, parse_claude_auth_status
from codex_agents.service_transport import ServiceTransportError


class BillingProbeApiTests(unittest.TestCase):
    def setUp(self):
        self.identity = parse_claude_auth_status(provider_fixture('claude-auth-status.json'),
            digest_key=b'k'*32, observed_at_ms=20, client=('/usr/bin/claude', 'f'*64))
        self.info = {'status': 'allowed', 'rateLimitType': 'five_hour', 'utilization': .2,
                     'isUsingOverage': False, 'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'}
        self.head, self.bill = claude_rate_limit_records(self.info,
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=30)
        self.value = {'probe': 'completed', 'reason_code': None, 'records': [self.identity, self.head, self.bill],
                      'known': False, 'observed_at_ms': 10}
        self.client = AccountClient(Path('/private/tmp/unused-billing.sock'), 'prj_'+'1'*32, 'a'*64, timeout=1)

    def reply(self, raw, result=None):
        request = decode_account_request(raw)
        return canonical_json({'protocol_version': 1, 'request_id': request.request_id,
            'project_id': request.project_id, 'runtime_digest': request.runtime_digest,
            'request_digest': account_request_digest(request), 'ready': True,
            'result': self.value if result is None else result, 'reason_code': None})

    def test_one_explicit_empty_request_accepts_different_original_record_timestamps(self):
        sent = []
        def exchange(path, raw, **limits):
            sent.append(decode_account_request(raw))
            return self.reply(raw)
        with mock.patch.object(account_api, 'exchange_frame', side_effect=exchange):
            self.assertEqual(self.value, self.client.billing_probe(request_id='fixed-request'))
        self.assertEqual(1, len(sent))
        self.assertEqual(('account.billing_probe', 'fixed-request', {}),
                         (sent[0].operation, sent[0].request_id, sent[0].payload))

    def test_request_cannot_select_provider_root_model_command_deadline_or_authority(self):
        for key in ('provider', 'root', 'model', 'command', 'deadline_ns', 'sql', 'authorized', 'cleanup_confirmed'):
            with self.subTest(key=key), self.assertRaises(AccountError):
                AccountRequest(1, 'account.billing_probe', 'fixed', 'prj_'+'1'*32, 'a'*64, {key: True})

    def test_transport_failure_does_one_exchange_without_replacement_request(self):
        with mock.patch.object(account_api, 'exchange_frame', side_effect=ServiceTransportError('transport_unavailable')) as exchange:
            with self.assertRaisesRegex(AccountError, 'account_unavailable'):
                self.client.billing_probe(request_id='one-original')
        self.assertEqual(1, exchange.call_count)

    def test_partial_failure_retains_paid_restriction_and_refuses_clean_billing_refresh(self):
        _, paid = claude_rate_limit_records({**self.info, 'isUsingOverage': True},
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=30)
        partial = {**self.value, 'reason_code': 'probe_incomplete', 'records': [self.identity, paid]}
        self.assertIs(partial, checked_billing_probe_reply(partial))
        with self.assertRaises(BillingProbeError):
            checked_billing_probe_reply({**partial, 'records': [self.identity, self.bill]})

    def test_fingerprint_canonical_id_duplicate_type_or_raw_answer_cannot_be_smuggled(self):
        changed = {**self.bill, 'account_fingerprint': 'b'*64}
        changed['record_id'] = 'provider_billing:'+content_digest({k: v for k, v in changed.items() if k != 'record_id'})
        cases = [{**self.value, 'records': [self.identity, self.head, changed]},
            {**self.value, 'records': [self.identity, self.head, {**self.bill, 'record_id': 'provider_billing:'+'0'*64}]},
            {**self.value, 'records': [self.identity, self.bill, self.bill]},
            {**self.value, 'answer': 'PRIVATE-MODEL-ANSWER'}, {**self.value, 'known': 1},
            {**self.value, 'observed_at_ms': True}]
        for value in cases:
            with self.subTest(keys=sorted(value)), self.assertRaises(BillingProbeError):
                checked_billing_probe_reply(value)

    def test_waiver_map_requires_exact_returned_billing_ids_and_native_booleans(self):
        checked_billing_probe_reply({**self.value, 'billing_waivers': {self.bill['record_id']: False}})
        for waivers in ({}, {'unknown': False}, {self.bill['record_id']: 0}):
            with self.subTest(waivers=waivers), self.assertRaises(BillingProbeError):
                checked_billing_probe_reply({**self.value, 'billing_waivers': waivers})

    def test_closed_denial_and_reused_shapes_preserve_probe_semantics(self):
        denied = {**self.value, 'probe': 'denied', 'reason_code': 'probe_cooldown', 'records': []}
        self.assertEqual(denied, checked_billing_probe_reply(denied))
        checked_billing_probe_reply({**self.value, 'probe': 'reused'})
        for value in ({**denied, 'reason_code': 'PRIVATE unknown'}, {**denied, 'records': [self.identity]},
                      {**self.value, 'probe': 'reused', 'reason_code': 'probe_incomplete'}):
            with self.assertRaises(BillingProbeError):
                checked_billing_probe_reply(value)

    def test_malformed_reply_is_fenced_by_the_actual_account_client(self):
        changed = deepcopy(self.value)
        changed['records'][1]['phase'] = 'during'
        with mock.patch.object(account_api, 'exchange_frame', side_effect=lambda path, raw, **kw: self.reply(raw, changed)):
            with self.assertRaisesRegex(AccountError, 'account_invalid_response'):
                self.client.billing_probe(request_id='fenced')
