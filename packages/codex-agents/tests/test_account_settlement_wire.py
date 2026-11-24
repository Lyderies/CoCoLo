"""Slice B4s (RS-B-B4-1): the new account operation `account.lost_run_closure` on the wire.

REQUIREMENTS-account-settlement.md. A NEW operation, offered to the project role only; `account.settle_lost_run`'s
reply shape is unchanged (ANALYSIS-0.4.0-rsb-b4-b7.md finding 4). The reply is the closure itself, checked against
the request (project, run handle, claim request, lease digest, bridge run id, request digest) and bounded in size.
Unit stage: encoding and decoding only, no service.
"""
import unittest

import tests.support  # noqa: F401
from tests import test_result_leases as leases

from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, AccountError, AccountRequest, account_request_digest,
    decode_account_response, encode_account_request,
)
from codex_agents.learning_contracts import canonical_json
from codex_agents.lost_run_contracts import SettleLostInput

RUNTIME = 'a' * 64
KEY = b'k' * 32
OPERATION = 'account.lost_run_closure'


def requirements(*ids):
    return __import__('test_framework').requirements(*ids)


@requirements('RS-B-B4')
class LostRunClosureWireTests(unittest.TestCase):
    def setUp(self):
        self.binding = leases.lease_binding(1)
        self.settle = SettleLostInput(self.binding.run_handle, 'd' * 64, self.binding.claim_request_id,
                                      'brg_' + '1' * 32, '2' * 64)
        self.request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, OPERATION, 'lost-' + '0' * 32,
                                      self.binding.project_id, RUNTIME, self.settle.payload())

    def closure(self, **changes):
        from codex_agents.lost_run_closure import lost_run_closure
        options = dict(lease_digest='2' * 64, assignment_digest='3' * 64, bridge_run_id='brg_' + '1' * 32,
                       release_kind='lost', released_at_ms=7, request_digest=account_request_digest(self.request))
        options.update(changes)
        binding = options.pop('binding', self.binding)
        return lost_run_closure(KEY, binding, **options)

    def reply(self, result):
        return canonical_json({'protocol_version': ACCOUNT_PROTOCOL_VERSION, 'request_id': self.request.request_id,
                               'project_id': self.request.project_id, 'runtime_digest': RUNTIME,
                               'request_digest': account_request_digest(self.request), 'ready': True,
                               'result': result, 'reason_code': None})

    def test_B4_1_the_operation_is_new_and_offered_to_the_project_role_only(self):
        from codex_agents.account_host import HOST_OPERATIONS, OWNER_OPERATIONS, PROJECT_OPERATIONS
        self.assertIn(OPERATION, ACCOUNT_OPERATIONS)
        self.assertIn(OPERATION, PROJECT_OPERATIONS)
        self.assertNotIn(OPERATION, HOST_OPERATIONS | OWNER_OPERATIONS)

    def test_B4_1_settle_lost_runs_reply_shape_is_unchanged(self):
        from codex_agents import lost_run_contracts
        self.assertEqual({'state', 'run_handle', 'bridge_run_id', 'receipt_digest', 'known'},
                         lost_run_contracts._REPLY_FIELDS)

    def test_B4_1_the_request_encodes_and_a_malformed_payload_is_refused(self):
        self.assertTrue(encode_account_request(self.request))
        for payload in ({**self.settle.payload(), 'extra': 1}, {**self.settle.payload(), 'lease_digest': 'x'}):
            with self.subTest(payload=sorted(payload)):
                with self.assertRaises(AccountError):
                    AccountRequest(ACCOUNT_PROTOCOL_VERSION, OPERATION, 'lost-1', self.binding.project_id, RUNTIME,
                                   payload)

    def test_B4_1_a_matching_closure_decodes(self):
        value = self.closure()
        self.assertEqual(value, decode_account_response(self.reply(value), self.request))
        released = self.closure(release_kind='released_unacknowledged')
        self.assertEqual(released, decode_account_response(self.reply(released), self.request))

    def test_B4_1_a_closure_for_another_project_handle_claim_or_request_is_refused(self):
        import dataclasses
        other = self.binding.run_handle[:-1] + ('0' if self.binding.run_handle[-1] != '0' else '1')
        for name, value in (
                ('project', self.closure(binding=dataclasses.replace(self.binding, project_id='prj_' + 'f' * 32))),
                ('handle', self.closure(binding=dataclasses.replace(self.binding, run_handle=other))),
                ('claim request', self.closure(binding=dataclasses.replace(self.binding, claim_request_id='other-1'))),
                ('lease', self.closure(lease_digest='9' * 64)),
                ('bridge', self.closure(bridge_run_id='brg_' + '9' * 32)),
                ('request digest', self.closure(request_digest='8' * 64)),
                ('kind', {**self.closure(), 'release_kind': 'completed'}),
                ('extra field', {**self.closure(), 'known': True})):
            with self.subTest(name=name):
                with self.assertRaises(AccountError) as caught:
                    decode_account_response(self.reply(value), self.request)
                self.assertEqual('account_invalid_response', caught.exception.reason_code)

    def test_B4_1_the_closure_is_bounded(self):
        from codex_agents.lost_run_closure import MAX_LOST_RUN_CLOSURE_BYTES, checked_lost_run_closure
        value = self.closure()
        self.assertLessEqual(len(canonical_json(value)), MAX_LOST_RUN_CLOSURE_BYTES)
        self.assertLessEqual(MAX_LOST_RUN_CLOSURE_BYTES, 16_384)  # it fits one account frame
        with self.assertRaises(Exception):
            checked_lost_run_closure({**value, 'request_digest': 'f' * (MAX_LOST_RUN_CLOSURE_BYTES + 1)})


if __name__ == '__main__':
    unittest.main()
