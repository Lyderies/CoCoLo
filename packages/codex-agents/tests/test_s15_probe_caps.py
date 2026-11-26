"""Past-limit repeat-probe generations through the real account owner."""
import sys
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_account_billing_admission as fixtures
from codex_agents import account_billing_admission as admission
from codex_agents.admission_inputs import AdmissionCaptureLimits
from codex_agents.claim_admission import prepare_claim_admission


@requirements('S15-O0C-REPEAT-EXPIRY', why_not_scenario='Real owner reserve/commit writers and retained provider records; reuse is a fixture fact, controlled clocks, no signature-execution or native-authority claim.')
class RepeatProbeCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def no_sockets(event, args):
            if event.startswith('socket.'):
                raise AssertionError('S15 probe boundaries must not open sockets')
        sys.addaudithook(no_sockets)

    def fixture(self, timeout_ms):
        case = fixtures.AccountBillingAdmissionTests('runTest')
        self.addCleanup(case.doCleanups)
        def make_owner(*args, **kwargs):
            return admission.AccountBillingAdmission(*args, **kwargs, timeout_ms=timeout_ms)
        with mock.patch.object(fixtures, 'AccountBillingAdmission', make_owner):
            case.setUp()
        records = case.store_probe_only(case.FAVOURABLE)
        snapshot = case.owner._capture(case.fixture.project_id)
        parsed = prepare_claim_admission(snapshot, AdmissionCaptureLimits(512, 262_144))
        for identity in ('first', 'second', 'third'):
            original = case.reserve(identity)
            # Store the successful-reuse fixture through the real owner writer.
            # The counted generation lifecycle does not test the signature gate.
            with case.ledger._account_transaction():
                row = case.owner._row(original.project, original.key)
                committed = case.owner._commit_reply(row, 'reused', None, [case.identity, *records], parsed.rows)
                self.assertEqual('reused', case.owner._value(committed)['probe'])
            case.owner.release_committed(original)
        self.assertEqual({}, case.owner._pending)
        return case

    def same_request_row(self, timeout_ms):
        case = self.fixture(timeout_ms)
        origin = admission.time.monotonic_ns()
        with mock.patch.object(admission, 'MAX_PENDING_PROBES', 2), \
                mock.patch.object(admission, 'MAX_PENDING_PROJECT_PROBES', 2), \
                mock.patch.object(admission.time, 'monotonic_ns', return_value=origin):
            first, second = case.reserve('first'), case.reserve('second')
            self.assertIs(first, case.reserve('first'))  # live replay owns its generation
            with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_full'):
                case.reserve('third')
            self.assertEqual(2, len(case.owner._pending))
            self.assertEqual({}, case.owner._signatures)  # no running work can justify retention
            self.assertEqual({}, case.owner._permits)
        case.clock = max(first.deadline, second.deadline) + 1
        expired = max(first.deadline_ns, second.deadline_ns) + 1
        with mock.patch.object(admission, 'MAX_PENDING_PROBES', 2), \
                mock.patch.object(admission, 'MAX_PENDING_PROJECT_PROBES', 2), \
                mock.patch.object(admission.time, 'monotonic_ns', return_value=expired):
            fresh = case.reserve('first')
            self.assertGreater(fresh.deadline, case.clock,
                'reserve returned an expired generation instead of ending its unprepared custody')
            self.assertIsNot(first, fresh)
            self.assertNotIn(first, case.owner._reserved_inputs)
            self.assertNotIn(second, case.owner._reserved_inputs)
            self.assertEqual([fresh], list(case.owner._pending.values()))
        self.assertIsNotNone(case.owner.prepare(fresh))  # a fresh generation can capture work
        self.assertEqual(1, len(case.owner._signatures))

    def test_expired_repeat_generations_free_the_count_cap_for_the_same_request(self):
        for timeout_ms in (2, 60_000):
            with self.subTest(timeout_ms=timeout_ms):
                self.same_request_row(timeout_ms)

    def test_expired_repeat_generation_that_cleanup_must_keep_is_refused_not_returned(self):
        case = self.fixture(60_000)
        origin = admission.time.monotonic_ns()
        with mock.patch.object(admission.time, 'monotonic_ns', return_value=origin):
            first = case.reserve('first')
        case.owner._permits[first] = object()  # custody cleanup must keep (a running job)
        case.clock = first.deadline + 1
        for clock_ns, wall in ((first.deadline_ns + 1, first.deadline + 1), (origin, first.deadline + 1)):
            case.clock = wall
            with mock.patch.object(admission.time, 'monotonic_ns', return_value=clock_ns):
                with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_conflict'):
                    case.reserve('first')
            self.assertIs(first, case.owner._pending[(first.project, first.key)])

    def test_wall_clock_only_expiry_without_a_permit_refuses_until_monotonic_cleanup(self):
        case = self.fixture(60_000)
        origin = admission.time.monotonic_ns()
        with mock.patch.object(admission.time, 'monotonic_ns', return_value=origin):
            first = case.reserve('first')
            self.assertEqual({}, case.owner._permits)
            self.assertEqual({}, case.owner._signatures)
            case.clock = first.deadline + 1
            with self.assertRaisesRegex(admission.ProbeAdmissionError, '^probe_conflict$'):
                case.reserve('first')
            self.assertIs(first, case.owner._pending[(first.project, first.key)])
            self.assertIn(first, case.owner._reserved_inputs)
        # The ordinary cleanup uses the monotonic deadline, even without a permit.
        # Once that deadline arrives, it drops the old custody and permits a fresh judgment.
        with mock.patch.object(admission.time, 'monotonic_ns', return_value=first.deadline_ns):
            fresh = case.reserve('first')
            self.assertIsNot(first, fresh)
            self.assertNotIn(first, case.owner._reserved_inputs)
            self.assertEqual([fresh], list(case.owner._pending.values()))
            self.assertGreater(fresh.deadline, case.clock)

    def test_expired_repeat_generations_release_quota_when_a_different_request_retries(self):
        case = self.fixture(60_000)
        origin = admission.time.monotonic_ns()
        with mock.patch.object(admission, 'MAX_PENDING_PROBES', 2), \
                mock.patch.object(admission, 'MAX_PENDING_PROJECT_PROBES', 2), \
                mock.patch.object(admission.time, 'monotonic_ns', return_value=origin):
            first, second = case.reserve('first'), case.reserve('second')
            with self.assertRaisesRegex(admission.ProbeAdmissionError, 'probe_full'):
                case.reserve('third')
        case.clock = max(first.deadline, second.deadline) + 1
        expired = max(first.deadline_ns, second.deadline_ns) + 1
        with mock.patch.object(admission, 'MAX_PENDING_PROBES', 2), \
                mock.patch.object(admission, 'MAX_PENDING_PROJECT_PROBES', 2), \
                mock.patch.object(admission.time, 'monotonic_ns', return_value=expired):
            fresh = case.reserve('third')
            self.assertGreater(fresh.deadline, case.clock)
            self.assertEqual([fresh], list(case.owner._pending.values()))
            self.assertNotIn(first, case.owner._reserved_inputs)
            self.assertNotIn(second, case.owner._reserved_inputs)
        self.assertIsNotNone(case.owner.prepare(fresh))
