"""Batch 0.8.57b: B4s's new account operation meets B4m's reply table and RS-F S1's runtime gate.

`account.lost_run_closure` (RS-B-B4-1) is a NEW operation, so B4m (RS-B-B4m-2) needs exactly its producer's reply
fields in reply_shapes.REPLY_FIELDS, and it mutates the account (it answers a settled lost run's closure for the
project writer), so RS-F S1 (RSF-S1-2) must refuse it on a missing or wrong runtime before any authorization.
Real store and framed handler; no sockets.
"""
import json
import unittest

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
from tests.test_rsf_s1_ingress import StoreFixture, UPDATE_COMMAND, UPGRADE_MESSAGE
from unittest import mock
import sys

from codex_agents import lost_run_closure
from codex_agents.account_api import ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, ACCOUNT_READ_OPERATIONS
from codex_agents.account_service import AccountOwnerViews, AccountService
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.reply_shapes import REPLY_FIELDS, reply_fields_match

sys.path.insert(0, str(REPOSITORY_ROOT.parents[1] / 'scripts'))
from test_framework import requirements  # noqa: E402

OPERATION = 'account.lost_run_closure'


@requirements('RS-B-B4m-2', 'RS-B-B4', why_not_scenario='Table contract against the producer module; no process.')
class LostRunClosureReplyTableTests(unittest.TestCase):
    def test_the_table_row_is_exactly_the_producers_closure_fields(self):
        self.assertEqual({'optional': [], 'required': sorted(lost_run_closure._FIELDS)}, REPLY_FIELDS[OPERATION])
        fields = dict.fromkeys(lost_run_closure._FIELDS)
        self.assertTrue(reply_fields_match(OPERATION, fields))
        self.assertFalse(reply_fields_match(OPERATION, {**fields, 'NEW_FIELD': None}))

    def test_settle_lost_runs_row_is_unchanged(self):
        self.assertEqual({'optional': [], 'required': ['bridge_run_id', 'known', 'receipt_digest', 'run_handle', 'state']},
                         REPLY_FIELDS['account.settle_lost_run'])


@requirements('RS-B-B4', 'RS-B-B4m-2', why_not_scenario='Real store and framed handler; the socket path is test_rsf_s1_ingress_socket.')
class LostRunClosureRuntimeGateTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.authorize = mock.Mock(side_effect=AssertionError('runtime gate must precede authorizer'))
        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=self.authorize)
        self.addCleanup(self.service.close)

    def test_the_closure_is_a_gated_account_mutation(self):
        self.assertIn(OPERATION, ACCOUNT_OPERATIONS)
        self.assertNotIn(OPERATION, ACCOUNT_READ_OPERATIONS)

    def test_a_missing_or_wrong_runtime_is_refused_before_the_authorizer_with_nothing_written(self):
        for fields in ({}, {'runtime_digest': '0' * 64}, {'runtime_digest': []}):
            with self.subTest(fields=fields):
                raw = canonical_json({'protocol_version': ACCOUNT_PROTOCOL_VERSION, 'operation': OPERATION,
                                      'request_id': 'b4s-gate', 'project_id': self.fixture.project_a_id,
                                      'payload': {}, **fields})
                before = self.ledger._connection.total_changes
                response = json.loads(self.service.handle(raw))
                self.assertEqual(before, self.ledger._connection.total_changes)
                self.assertFalse(response['ready'])
                self.assertEqual('runtime_upgrade_required', response['reason_code'])
                self.assertEqual(UPGRADE_MESSAGE, response['message'])
                self.assertEqual(UPDATE_COMMAND, response['update_command'])
        self.authorize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
