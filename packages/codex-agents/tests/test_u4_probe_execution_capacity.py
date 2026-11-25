"""O0c U4 independent past-cap execution coverage; 3.3 recovery is separate."""
import time
import unittest
from unittest import mock
from test_framework import requirements
from tests import test_account_billing_admission as admission_fixtures
from tests import test_account_billing_execution as execution_fixtures
from tests.test_u4_probe_capacity import fixture
from codex_agents import account_billing_admission as admission


class DeniedProbeCapacityTests(unittest.TestCase):
    @requirements('O0C-U4-1', why_not_scenario='Separate past-cap checks identify the completion, denial and expiry boundary without a combined recovery campaign.')
    def test_denials_past_the_cap_keep_the_admitted_control_and_replay(self):
        case = fixture(self, admission_fixtures.AccountBillingAdmissionTests)
        self.enterContext(mock.patch.object(admission, 'MAX_PROBE_REQUESTS', 2))
        control = case.reserve('admitted-control')
        case.prepared(control)
        case.owner.admit(control)
        history = []
        for number in range(3):
            identity = 'denied-' + str(number)
            original = case.reserve(identity)
            case.prepared(original)
            reply = case.owner.admit(original)
            self.assertEqual(('denied', 'probe_cooldown'), (reply['probe'], reply['reason_code']))
            self.assertEqual({**reply, 'known': True}, case.reserve(identity))
            self.assertEqual(1, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
            history.append((identity, reply))
        rows = case.rows()
        case.restart()
        self.assertEqual(rows, case.rows())
        for identity, reply in history:
            self.assertEqual({**reply, 'known': True}, case.reserve(identity))
        case.reserve('fresh-after-denials')
        with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_full'):
            case.reserve('over-cap')


class CompletedProbeCapacityTests(unittest.TestCase):
    @requirements('O0C-U4-1', why_not_scenario='Separate past-cap checks identify the completion, denial and expiry boundary without a combined recovery campaign.')
    def test_completed_history_past_the_cap_still_replays(self):
        case = fixture(self, execution_fixtures.AccountBillingExecutionTests)
        self.enterContext(mock.patch.object(admission, 'MAX_PROBE_REQUESTS', 2))
        self.enterContext(mock.patch.object(time, 'time_ns', side_effect=lambda: case.clock * 1_000_000))
        identity = ['completed-0']
        case.reserve = lambda: case.owner.reserve(case.fixture.project_id, 'a' * 64, 'b' * 64,
            identity[0], case.context, before_commit=lambda: case.allowed)
        rows = []
        # The shared fixture's row() reads the first row of the table, which is right for its single-lifecycle
        # tests. With history kept, the lifecycle under test is the one not yet committed (else the newest).
        case.row = lambda: dict(case.ledger._connection.execute(
            "SELECT * FROM account_billing_probe_requests ORDER BY state='committed', created_at_ms DESC, request_key DESC LIMIT 1").fetchone())
        for number in range(3):
            identity[0] = 'completed-' + str(number)
            handle, _, supervisor = case.launch()
            reply = case.executions.complete(handle)
            self.assertEqual('completed', reply['probe'])
            self.assertEqual('released', supervisor.snapshot()['state'])
            self.assertEqual(0, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
            rows.append([dict(r) for r in case.ledger._connection.execute(
                'SELECT * FROM account_billing_probe_requests ORDER BY request_key')])
            self.assertEqual({**reply, 'known': True}, case.admit(case.reserve()))
            self.assertEqual(number + 1, len(case.model_invocations()))
            if number < 2:
                # Advance real policy windows on a deterministic wall clock; no policy is disabled.
                case.clock += max(admission.PROBE_REUSE_MS, admission.PROBE_COOLDOWN_MS) + 1
        final = [dict(r) for r in case.ledger._connection.execute(
            'SELECT * FROM account_billing_probe_requests ORDER BY request_key')]
        self.assertEqual(3, len(final))
        self.assertEqual(rows[-1], final)  # replay preserves the historical sealed rows
        self.assertTrue(all(r['state'] == 'committed' for r in final))
        self.assertEqual(3, len(case.model_invocations()))


