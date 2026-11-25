"""O0c U4 independent past-cap execution coverage; 3.3 recovery is separate."""
import unittest
from unittest import mock
from test_framework import requirements
from tests import test_account_billing_execution as execution_fixtures
from tests import test_bridge as bridge_fixtures
from tests.test_u4_probe_capacity import fixture, holds
from codex_agents import account_billing_admission as admission


class LiveProbeCapacityTests(unittest.TestCase):
    @requirements('O0C-U4-2', why_not_scenario='The running child and exact cap refusal isolate evidence-held capacity from historical request counts.')
    def test_a_running_model_stays_charged_while_reservations_expire(self):
        case = fixture(self, execution_fixtures.AccountBillingExecutionTests)
        self.enterContext(mock.patch.object(admission, 'MAX_PROBE_REQUESTS', 2))
        handle, permit = case.prepare([bridge_fixtures.PROBE_INIT, {'sleep': 30},
                                      bridge_fixtures.FAVOURABLE, bridge_fixtures.result_frame('OK')])
        supervisor = case.executions.launch(handle, permit)
        self.addCleanup(supervisor.request_stop)
        case.until(lambda: supervisor.snapshot()['state'] == 'active')
        self.assertFalse(supervisor.snapshot()['root_waited'])
        for number in range(3):
            original = case.owner.reserve(case.fixture.project_id, 'a' * 64, 'b' * 64,
                'expired-' + str(number), case.context, before_commit=lambda: True)
            with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_full'):
                case.owner.reserve(case.fixture.project_id, 'a' * 64, 'b' * 64,
                    'over-cap', case.context, before_commit=lambda: True)
            case.clock = original.deadline + admission.PROBE_ORPHAN_GRACE_MS + 1
            with mock.patch.object(case.ledger, '_capacity_clock_reference', return_value=0):
                self.assertEqual(1, case.ledger.release_due_account_holds(limit=1))
                self.assertEqual(0, case.ledger.release_due_account_holds(limit=1))
            self.assertEqual(1, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
            self.assertFalse(supervisor.snapshot()['root_waited'])
            self.assertFalse(case.executions.child_ended(permit.reservation))
        self.assertEqual(1, len(case.model_invocations()))
        supervisor.request_stop()
        case.until(lambda: supervisor.snapshot()['cleanup_confirmed']
                   and case.executions.child_ended(permit.reservation))
        case.owner.settle_ended(permit.reservation, 'probe_step_failed')
        settled = holds(case.ledger)
        self.assertEqual(0, case.ledger.account_capacity(case.fixture.project_id)['billing_probes']['live'])
        with mock.patch.object(case.ledger, '_capacity_clock_reference', return_value=0):
            self.assertEqual(0, case.ledger.release_due_account_holds(limit=64))
        self.assertEqual(settled, holds(case.ledger))


