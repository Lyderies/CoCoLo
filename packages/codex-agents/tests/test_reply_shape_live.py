"""LR-1: the frozen reply-field table is proven against the REAL service producers.

The hand-written table in reply_shapes.py once missed account.provider_state_snapshot and only a socket
test noticed. Here every operation's reply is taken where the real AccountService / HistoryService build it
(their reply constructors, wrapped to record the operation and the reply fields), while existing test modules
drive those services (some over their real sockets, most in-process through handle()). Each recorded reply
must satisfy reply_fields_match, every operation in both tables must have been produced at least once, and every
optional field the table allows must have been seen on a real reply.
"""
import collections
import io
from threading import local as ThreadSlot
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
import sys

from codex_agents import account_service, history_api, history_service
from codex_agents.account_api import account_request_digest, decode_account_request
from codex_agents.reply_shapes import REPLY_FIELDS, reply_fields_match

sys.path.insert(0, str(REPOSITORY_ROOT.parents[1] / 'scripts'))
from test_framework import requirements  # noqa: E402

# Existing modules that drive the real services; together they produce every operation.
DRIVERS = (
    ('account/history', (
        'test_account_api', 'test_history_api', 'test_billing_probe_api', 'test_capacity_status_api',
        'test_open_withdrawal', 'test_account_open_schema', 'test_project_worker_result',
        'test_project_qa_check_receipts', 'test_project_qa_commit', 'test_project_history',
        'test_result_read_api', 'test_account_terminal_delivery', 'test_account_cleanup_release',
        'test_account_observation', 'test_account_claim_operation', 'test_account_original_open',
        'test_holds_report')),
    ('outcomes/lanes', (
        'test_b2_service_outcome', 'test_lost_run_settlement', 'test_account_observation_failure',
        'test_account_billing_execution', 'test_run_holds', 'test_project_orchestration',
        'test_claim_denial_recovery', 'test_project_driver')),
    ('host open/billing', (
        'test_project_host_open', 'test_host_open_lane', 'test_required_host_open',
        'test_account_billing_admission', 'test_account_billing_jobs', 'test_account_slot_health')),
)
# Operations that could not be driven live would be named here and derived from the 8d4a7ce3 producer source.
SOURCE_ONLY = frozenset()
# Replies a driver test fabricates on purpose to prove the client refuses them; not producer output.
SYNTHETIC = {('account.project', ('CANARY-private',))}


class Recorder:
    def __init__(self):
        self.seen = collections.defaultdict(set)
        self.bad = collections.defaultdict(set)
        self.operations = {}
        self.thread = ThreadSlot()
        self.driver_failures = []

    def note(self, operation, result):
        if operation is None or type(result) is not dict:
            return
        fields = tuple(sorted(result))
        if (operation, fields) in SYNTHETIC:
            return
        (self.seen if reply_fields_match(operation, result) else self.bad)[operation].add(fields)

    def patches(self):
        account, history = account_service.AccountService, history_service.HistoryService
        handle_request, reply = account._handle_request, account._reply
        billing, history_handle, history_reply = account._billing_reply, history.handle, history._reply

        def seen_request(this, raw, *a, **k):
            try:
                request = decode_account_request(raw)
                self.operations[account_request_digest(request)] = request.operation
            except Exception:
                pass
            return handle_request(this, raw, *a, **k)

        def billing_reply(this, *a, **k):
            self.thread.operation = 'account.billing_probe'
            try:
                return billing(this, *a, **k)
            finally:
                self.thread.operation = None

        def account_reply(labels, *, result=None, reason=None):
            if result is not None:
                self.note(getattr(self.thread, 'operation', None) or self.operations.get(labels.get('request_digest')), result)
            return reply(labels, result=result, reason=reason)

        def seen_history(this, raw, *a, **k):
            try:
                request = history_api.decode_history_request(raw)
                self.operations[history_api.history_request_digest(request)] = request.operation
            except Exception:
                pass
            return history_handle(this, raw, *a, **k)

        def history_reply_(this, identity, *, request_digest=None, result=None, **k):
            if result is not None:
                self.note(self.operations.get(request_digest), result)
            return history_reply(this, identity, request_digest=request_digest, result=result, **k)

        return (mock.patch.object(account, '_handle_request', seen_request),
                mock.patch.object(account, '_billing_reply', billing_reply),
                mock.patch.object(account, '_reply', staticmethod(account_reply)),
                mock.patch.object(history, 'handle', seen_history),
                mock.patch.object(history, '_reply', history_reply_))


@requirements('RS-B-B4m-2', why_not_scenario='Drives the real services through existing test modules and checks every reply they produce.')
class LiveReplyShapeTests(unittest.TestCase):
    recorder = None

    @classmethod
    def observed(cls):
        if cls.recorder is None:
            recorder = Recorder()
            patches = recorder.patches()
            for patch in patches:
                patch.start()
            try:
                loader = unittest.defaultTestLoader
                for group, modules in DRIVERS:
                    suite = loader.loadTestsFromNames([f'tests.{name}' for name in modules])
                    stream = io.StringIO()
                    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
                    if not result.wasSuccessful():
                        recorder.driver_failures.append((group, stream.getvalue()[-4000:]))
            finally:
                for patch in reversed(patches):
                    patch.stop()
            cls.recorder = recorder
        return cls.recorder

    def test_every_replayed_driver_group_passed(self):
        # A driver that fails after producing a reply must not let the shape proof pass (B4m confirming check).
        self.assertEqual([], [group for group, _ in self.observed().driver_failures],
                         '\n'.join(output for _, output in self.observed().driver_failures))

    def test_every_real_reply_matches_the_table(self):
        self.assertEqual({}, {k: sorted(v) for k, v in self.observed().bad.items()})

    def test_every_operation_in_both_tables_was_produced_live(self):
        missing = set(REPLY_FIELDS) - set(self.observed().seen) - SOURCE_ONLY
        self.assertEqual(set(), missing, 'Operations never produced by a real service reply.')

    def test_every_optional_field_was_seen_on_a_real_reply(self):
        seen = self.observed().seen
        for operation, row in REPLY_FIELDS.items():
            for name in row['optional']:
                with self.subTest(operation=operation, field=name):
                    self.assertTrue(any(name in fields for fields in seen[operation]), 'Optional field never produced.')

    def test_real_replies_carry_no_field_outside_the_table(self):
        for operation, variants in self.observed().seen.items():
            row = REPLY_FIELDS[operation]
            for fields in variants:
                with self.subTest(operation=operation, fields=fields):
                    self.assertLessEqual(set(row['required']), set(fields))
                    self.assertLessEqual(set(fields), set(row['required']) | set(row['optional']))


if __name__ == '__main__':
    unittest.main()
