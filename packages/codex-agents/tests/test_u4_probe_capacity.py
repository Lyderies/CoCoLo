"""O0c U4: retained expired-reserved history beyond a reduced project cap.

The admitted-row control is not evidence of a running child. Real child and
product checks live in separate short modules; journal recovery awaits 3.3.
"""
import unittest
from unittest import mock
from test_framework import requirements
from tests import test_rsf_s5_probe as store_fixtures
from codex_agents import account_billing_admission as admission


def fixture(test, kind):
    case = kind(methodName='runTest')
    test.addCleanup(case.doCleanups)
    test.addCleanup(case.tearDown)
    case.setUp()
    return case


def holds(ledger):
    return [dict(row) for row in ledger._connection.execute(
        "SELECT * FROM run_holds WHERE kind='probe' ORDER BY identity")]


class ProbeCapacityTests(unittest.TestCase):
    @requirements('O0C-U4-1', why_not_scenario='Separate past-cap checks identify the completion, denial and expiry boundary without a combined recovery campaign.')
    def test_expired_reservations_past_the_cap_release_once_with_a_live_control(self):
        case = fixture(self, store_fixtures._StoreFixture)
        self.enterContext(mock.patch.object(admission, 'MAX_PROBE_REQUESTS', 2))
        control = case.reserve('admitted-control')
        case.admit(control)
        history = []
        for number in range(3):
            identity = 'expired-' + str(number)
            expired = case.reserve(identity)
            with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_full'):
                case.reserve('over-cap')
            case.clock = expired.deadline + 1
            self.assertEqual(1, case.sweep_now())
            after = holds(case.ledger)
            self.assertEqual(0, case.sweep_now())
            self.assertEqual(after, holds(case.ledger))
            self.assertEqual(1, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
            reply = case.reserve(identity)
            self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                             (reply['probe'], reply['reason_code'], reply['known']))
            history.append((identity, reply))
        rows = case.rows()
        self.assertEqual(4, len(rows))
        self.assertEqual(['admitted'], [r['state'] for r in rows if r['request_key'] == control.key])
        self.assertEqual(4, case.ledger._connection.execute(
            'SELECT COUNT(*) FROM account_billing_probe_origins').fetchone()[0])
        case.restart()
        self.assertEqual(rows, case.rows())
        for identity, reply in history:
            self.assertEqual(reply, case.reserve(identity))
        # Reopen alone does not release this admitted row (no sweep is called).
        self.assertEqual(1, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
        self.assertIsNotNone(case.reserve('fresh-after-history'))

