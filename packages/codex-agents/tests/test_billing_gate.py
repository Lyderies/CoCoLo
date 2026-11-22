"""CoCoLo 0.4.0 billing final gate: BR-16..BR-22 (owner decision 50, tests before code).

Design under test (ANALYSIS-0.4.0-billing-callbacks.md, approved as decision 50): ONE final gate is the only thing
that can build a yes. It runs after ALL caller callbacks (untrusted), compares everything the judgment used with the
current state (the judgment's own snapshot, never a baseline captured late), re-tests freshness, and nothing external
runs after it. A ready job whose inputs moved is judged again once at take and refused at publish; launch gates
again; one deny record per reason per account per window; the ledger's own reuse path goes through the same gate.
Subscription-only: paid usage is always refused.

The interface these tests assume, and nothing more:
  G1  `AccountBillingAdmission._gate` / `_gate_permit` are the gate functions (the structural check finds them by name).
  G2  `account_billing_admission.Yes` is the sealed yes the gate builds; `verify_committed` returns one for a yes.
  G3  `verify_committed` refuses (probe_conflict) a yes it has no judgment for.
  G4  `admission_metadata.reuse_gate` is the shared gate of the ledger's own reuse path.
  G5  A denial's record is stored at most once per (reason, account) per `PROBE_COOLDOWN_MS` (the window); records
      keep their ordinary retention (DEFAULT_RETENTION_MS).

Tests:  Part A (BR-16/17/18/19)  the injection matrix and its named cases (owner and service, real records)
        Part B (BR-20)           the registry and the structural check, with violators it must catch
        Part C (BR-21)           the ledger's own reuse path
        Part D (BR-22)           the deny-record bound
"""
import ast
import inspect
import json
import os
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_billing_rejudge as _rj
from tests import test_providers as fixtures

from codex_agents import account_billing_admission, account_billing_execution, account_billing_jobs, account_service
from codex_agents import admission_metadata, learning_store
from codex_agents.account_api import AccountError
from codex_agents.account_billing_admission import PROBE_COOLDOWN_MS, PROBE_REUSE_MS, ProbeAdmission, ProbeAdmissionError
from codex_agents.admission_inputs import AdmissionInputPolicy
from codex_agents.learning_store import DEFAULT_RETENTION_MS
from codex_agents.contracts import ContractError
from codex_agents.providers import AdmissionDenied, claude_rate_limit_records
from tests.learning_support import trusted_tools

PLATFORM = _rj.PLATFORM
FAVOURABLE, PAID, KEY = _rj.FAVOURABLE, _rj.PAID, _rj.KEY
YES_KINDS = frozenset({'reused', 'completed'})
# Callback invocations the matrix aims at, per path: what f0f43fbd made (the final one of the permit path and the
# second of the publication check ran after the last comparison) or what the gate design makes, whichever is more.
# A path that now makes fewer callbacks skips the rest, after its control counted them.
MAX_K = {'reply_of_a_repeat': 3, 'reply_of_a_new_request': 3, 'permit_of_a_new_probe': 4, 'permit_returned_again': 2,
         'launch_of_a_permit': 1, 'publication_check_of_a_yes': 2}
SERVICE_MAX_K = 5


def is_yes(result):
    return type(result) is ProbeAdmission or (isinstance(result, dict) and result.get('probe') in YES_KINDS) or (
        isinstance(result, dict) and result.get('probe') == 'ok')


# ======================================================================================================
# Part A: the injection matrix
# ======================================================================================================
class Tap:
    """The caller's authorization callback: counts its invocations while armed and runs `action` at the k-th."""

    def __init__(self):
        self.armed, self.n, self.fire_at, self.action, self.effect, self.fired, self.took_effect = (
            False, 0, None, None, None, False, None)

    def __call__(self):
        if self.armed:
            self.n += 1
            if self.n == self.fire_at:
                self.fired = True
                self.action()
                self.took_effect = self.effect()
        return True

    def aim(self, k, mutation):
        self.n, self.fire_at, self.action, self.effect = 0, k, mutation[0], mutation[1]
        self.fired, self.took_effect = False, None


class ServiceTap(_rj._Hook):
    """The service-level callback: the `allowed` flag every authorization callback reads."""

    def __init__(self):
        super().__init__()
        self.effect, self.took_effect = None, None

    def __bool__(self):
        if self.armed:
            self.calls += 1
            if self.calls == self.fire_at:
                self.fired = True
                self.action()
                self.took_effect = self.effect()
        return True

    def aim(self, k, mutation):
        self.calls, self.fire_at, self.action, self.effect = 0, k, mutation[0], mutation[1]
        self.fired, self.took_effect = False, None


MUTATIONS = ('paid usage', 'late paid usage', 'api key', 'attestation removed', 'unreviewed version', 'signer')


class MutationsOwner:
    """The mutations, as (apply, took_effect) pairs, made at a callback. Each one is undone with the test."""

    def billing_rows(self):
        return self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='provider_billing'").fetchone()[0]

    def late_paid(self):
        """Paid usage stored now but stamped earlier than every record the yes cites (BR-2)."""
        records = claude_rate_limit_records(PAID, account_fingerprint=self.fingerprint_of(),
                                            observed_at_ms=self.stamp()-3500)
        if self.ledger._connection.in_transaction:
            for record in records:
                self.ledger._append_checked_locked(self.project_id_of(), record, producer='provider',
                                                   timestamp=self.stamp())
        else:
            self.ledger._store_provider_observations(self.project_id_of(), records, timestamp=self.stamp())

    def mutation(self, name):
        if name == 'paid usage':
            before = self.billing_rows()
            return (lambda: self.paid_now()), (lambda: self.billing_rows() > before)
        if name == 'late paid usage':
            before = self.billing_rows()
            return self.late_paid, (lambda: self.billing_rows() > before)
        if name == 'api key':
            patch = mock.patch.dict(os.environ, KEY)
            self.addCleanup(patch.stop)
            return patch.start, (lambda: bool(os.environ.get('ANTHROPIC_API_KEY')))
        if name == 'attestation removed':
            sql = 'SELECT COUNT(*) FROM owner_attestations'
            return ((lambda: self.ledger._connection.execute('DELETE FROM owner_attestations')),
                    (lambda: self.ledger._connection.execute(sql).fetchone()[0] == 0))
        if name == 'unreviewed version':
            patch = mock.patch.object(self.views_of(), '_claim_policy',
                                      return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None))
            self.addCleanup(patch.stop)
            return patch.start, (lambda: self.views_of()._claim_policy('claude').minimum_version == '999.0.0')
        if name == 'signer':
            patch = mock.patch.object(self.ledger, '_current_signer_bytes', return_value=b'x'*32)
            self.addCleanup(patch.stop)
            return patch.start, (lambda: self.ledger._current_signer_bytes() == b'x'*32)
        raise AssertionError(name)


@PLATFORM
class OwnerMatrixBase(_rj.RejudgeOwnerBase, MutationsOwner):
    """Paths through the real owner. Every path is run once clean (the control: a yes, its callbacks counted),
    then once per mutation and callback index k in a fresh world, with the k-th callback making the mutation."""
    counted = {}  # path -> callback invocations, from the control

    def fingerprint_of(self):
        return self.identity['account_fingerprint']

    def project_id_of(self):
        return self.fixture.project_id

    def views_of(self):
        return self.views

    def stamp(self):
        return self.clock

    def paid_now(self):
        self.append_any(PAID)

    # ---- the paths ---------------------------------------------------------------------------------
    def admit_armed(self, original, tap):
        tap.armed = True
        try:
            return self.owner.admit(original)
        except Exception as error:  # any refusal or failure is no yes
            return {'probe': 'error', 'reason_code': getattr(error, 'reason_code', type(error).__name__)}
        finally:
            tap.armed = False

    def path_reply_of_a_repeat(self, tap):
        self.saved_reused()
        self.clock += 1000
        original = self.reserve('saved', before_commit=tap)
        self.observe_identity()
        self.prepared(original)
        return self.admit_armed(original, tap)

    def path_reply_of_a_new_request(self, tap):
        self.store_probe_only(FAVOURABLE)
        original = self.reserve('fresh', before_commit=tap)
        self.prepared(original)
        return self.admit_armed(original, tap)

    def path_permit_of_a_new_probe(self, tap):
        original = self.reserve('permit', before_commit=tap)
        self.prepared(original)
        return self.admit_armed(original, tap)

    def path_permit_returned_again(self, tap):
        original = self.reserve('permit', before_commit=tap)
        self.prepared(original)
        self.assertIs(ProbeAdmission, type(self.owner.admit(original)))
        return self.admit_armed(original, tap)

    def path_launch_of_a_permit(self, tap):
        original = self.reserve('permit', before_commit=tap)
        self.prepared(original)
        permit = self.owner.admit(original)
        tap.armed = True
        try:
            self.owner._execution(permit, launching=True)
            return {'probe': 'ok'}
        except Exception as error:
            return {'probe': 'error', 'reason_code': getattr(error, 'reason_code', type(error).__name__)}
        finally:
            tap.armed = False

    def path_publication_check_of_a_yes(self, tap):
        self.saved_reused()
        self.clock += 1000
        original = self.reserve('saved')
        self.observe_identity()
        self.prepared(original)
        reply = self.owner.admit(original)
        self.assertEqual('reused', reply['probe'], reply)
        judgment = self.owner.judgment_of(original)
        tap.armed = True
        try:
            self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                        before_commit=tap, reply=reply, history=original.history, judgment=judgment)
            return {'probe': 'ok'}
        except Exception as error:
            return {'probe': 'error', 'reason_code': getattr(error, 'reason_code', type(error).__name__)}
        finally:
            tap.armed = False

    PATHS = ('reply_of_a_repeat', 'reply_of_a_new_request', 'permit_of_a_new_probe', 'permit_returned_again',
             'launch_of_a_permit', 'publication_check_of_a_yes')

    # ---- the matrix --------------------------------------------------------------------------------
    def control(self, path):
        """The clean run: a yes, and how many callbacks it made. A fresh world follows it."""
        if path not in OwnerMatrixBase.counted:
            tap = Tap()
            result = getattr(self, 'path_'+path)(tap)
            self.assertTrue(is_yes(result), f'{path}: the clean control is not a yes: {result}')
            self.assertGreaterEqual(tap.n, 1, f'{path}: the path made no authorization callback')
            OwnerMatrixBase.counted[path] = tap.n
            self.setUp()  # the control used up this world
        return OwnerMatrixBase.counted[path]

    def injected(self, path, name, k):
        count = self.control(path)
        if k > count:
            self.skipTest(f'{path} makes only {count} authorization callbacks')
        tap = Tap()
        tap.aim(k, self.mutation(name))
        result = getattr(self, 'path_'+path)(tap)
        self.assertTrue(tap.fired, f'{path}: callback {k} of {count} was never reached')
        self.assertTrue(tap.took_effect, f'{path}: the mutation "{name}" did not take effect')
        self.assertFalse(is_yes(result), f'{path}: "{name}" made by callback {k} of {count} still gave a yes: {result}')


def add_matrix(cls, paths, prefix='test_matrix'):
    for path in paths:
        for name in MUTATIONS:
            for k in range(1, MAX_K[path]+1):
                def test(self, path=path, name=name, k=k):
                    self.injected(path, name, k)
                test.__name__ = f'{prefix}_{path}_{name.replace(" ", "_")}_k{k}'
                setattr(cls, test.__name__, test)
    return cls


@PLATFORM
class OwnerMatrix(OwnerMatrixBase):
    def test_control_every_path_gives_a_yes_when_nothing_is_changed(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                self.control(path)


add_matrix(OwnerMatrix, OwnerMatrixBase.PATHS)


@PLATFORM
class OwnerMatrixHasTeeth(OwnerMatrixBase):
    """The harness is not vacuous: with the gate taken away, an injection at the last callback is a yes."""

    def test_without_the_gate_the_last_callback_of_a_new_probe_can_still_add_paid_usage(self):
        count = self.control('permit_of_a_new_probe')
        tap = Tap()
        tap.aim(count, self.mutation('paid usage'))
        stub = mock.MagicMock(name='gate removed')
        with mock.patch.object(account_billing_admission.AccountBillingAdmission, '_gate', stub, create=True), \
                mock.patch.object(account_billing_admission.AccountBillingAdmission, '_gate_permit', stub, create=True):
            result = self.path_permit_of_a_new_probe(tap)
        self.assertTrue(tap.fired and tap.took_effect)
        self.assertTrue(is_yes(result), 'with no gate the injected paid usage must show up as a yes (the harness sees it)')


# ---- named cases: the requirements, one by one -----------------------------------------------------------
@PLATFORM
class BR16JudgmentRelativeComparison(OwnerMatrixBase):
    """BR-16: the publication check compares with the judgment's snapshot, not with a baseline taken after the
    first callback."""

    def test_BR16_paid_usage_added_by_the_first_callback_of_the_publication_check_is_refused(self):
        self.control('publication_check_of_a_yes')
        tap = Tap()
        tap.aim(1, self.mutation('paid usage'))
        result = self.path_publication_check_of_a_yes(tap)
        self.assertTrue(tap.fired and tap.took_effect)
        self.assertFalse(is_yes(result), result)

    def test_BR16_paid_usage_stored_with_an_earlier_stamp_by_the_first_callback_is_refused(self):
        self.control('publication_check_of_a_yes')
        tap = Tap()
        tap.aim(1, self.mutation('late paid usage'))
        result = self.path_publication_check_of_a_yes(tap)
        self.assertTrue(tap.fired and tap.took_effect)
        self.assertFalse(is_yes(result), result)

    def test_BR16_paid_usage_stored_before_the_publication_check_began_is_refused_not_taken_as_its_baseline(self):
        """Not through a callback at all: the records changed after the judgment and before the check starts."""
        self.saved_reused()
        self.clock += 1000
        original = self.reserve('saved')
        self.observe_identity()
        self.prepared(original)
        reply = self.owner.admit(original)
        judgment = self.owner.judgment_of(original)
        self.append_any(PAID)
        with self.assertRaises(ProbeAdmissionError):
            self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                        before_commit=lambda: True, reply=reply, history=original.history,
                                        judgment=judgment)


@PLATFORM
class BR17NoCallerCodeAfterTheGate(OwnerMatrixBase):
    """BR-17: nothing the caller supplies runs after the check that builds a yes."""

    def test_BR17_paid_usage_added_by_the_callback_after_the_commit_gives_no_permit(self):
        count = self.control('permit_of_a_new_probe')
        tap = Tap()
        tap.aim(count, self.mutation('paid usage'))  # the last callback the admit makes
        result = self.path_permit_of_a_new_probe(tap)
        self.assertTrue(tap.fired and tap.took_effect, 'the last callback was never reached')
        self.assertIsNot(ProbeAdmission, type(result), 'a permit was built although paid usage arrived in the last callback')

    def test_BR17_the_admit_of_a_new_probe_makes_no_callback_after_its_commit(self):
        """The same fact, stated on the order of events: the last callback runs before the unit is made durable."""
        tap = Tap()
        original = self.reserve('permit', before_commit=tap)
        self.prepared(original)
        tap.armed = True
        durable = []
        real = self.ledger.state.harden_database_files

        def harden():
            durable.append(tap.n)  # the unit is made durable right after this
            return real()
        with mock.patch.object(self.ledger.state, 'harden_database_files', side_effect=harden):
            self.owner.admit(original)
        tap.armed = False
        self.assertTrue(durable)
        self.assertEqual(tap.n, durable[-1], 'a callback ran after the admission was made durable')


@PLATFORM
class BR18FreshnessAtTheGate(OwnerMatrixBase):
    """BR-18: reuse age and the identity window are tested again at the gate, with the deadline still valid."""

    def repeat_with_clock_step(self, *, fresh_identity, fresh_billing, step):
        """A repeat whose cited identity (or billing) is `step`-ish inside its age when judged; the last callback
        moves the clock `step` ms on (the deadline, 60 s after the reservation, is still ahead)."""
        count = self.control('reply_of_a_repeat')
        self.saved_reused()
        self.clock += PROBE_REUSE_MS - 10_000
        if fresh_billing:
            self.store(FAVOURABLE)
        else:
            self.store(FAVOURABLE, age=PROBE_REUSE_MS - 10_000)  # inside the reuse age by 10 s
        tap = Tap()
        original = self.reserve('saved', before_commit=tap)
        if fresh_identity:
            self.observe_identity()
        self.prepared(original)
        start = self.clock
        tap.aim(count, (lambda: setattr(self, 'clock', start + step), lambda: self.clock == start + step))
        result = self.admit_armed(original, tap)
        self.assertTrue(tap.fired and tap.took_effect, 'the clock step was not made')
        self.assertLess(step, 60_000, 'the deadline must still be valid')
        return result

    def test_BR18_an_identity_that_left_its_window_while_the_callbacks_ran_gives_no_yes(self):
        result = self.repeat_with_clock_step(fresh_identity=False, fresh_billing=True, step=20_000)
        self.assertFalse(is_yes(result), result)

    def test_BR18_billing_that_left_the_reuse_age_while_the_callbacks_ran_gives_no_yes(self):
        result = self.repeat_with_clock_step(fresh_identity=True, fresh_billing=False, step=20_000)
        self.assertFalse(is_yes(result), result)

    def test_BR18_guard_the_same_repeat_without_a_clock_step_is_a_yes(self):
        result = self.repeat_with_clock_step(fresh_identity=True, fresh_billing=True, step=0)
        self.assertEqual('reused', result['probe'], result)

    def test_BR18_a_clock_stepped_back_by_a_callback_gives_no_yes(self):
        count = self.control('reply_of_a_repeat')
        self.saved_reused()
        self.clock += 1000
        tap = Tap()
        original = self.reserve('saved', before_commit=tap)
        self.observe_identity()
        self.prepared(original)
        start = self.clock
        tap.aim(count, (lambda: setattr(self, 'clock', start - 5000), lambda: self.clock == start - 5000))
        result = self.admit_armed(original, tap)
        self.assertTrue(tap.fired and tap.took_effect)
        self.assertFalse(is_yes(result), result)


@PLATFORM
class BR19EveryPositiveBoundary(OwnerMatrixBase):
    """BR-19: a yes is sealed at every boundary where it appears: admit, repeat-admit, launch, take, publish."""

    def test_BR19_a_repeat_admit_of_an_admitted_row_is_judged_again_not_returned_from_memory(self):
        original = self.reserve('permit')
        self.prepared(original)
        permit = self.owner.admit(original)
        self.assertIs(ProbeAdmission, type(permit))
        self.append_any(PAID)
        with self.assertRaises(ProbeAdmissionError):
            self.owner.admit(original)

    def test_BR19_a_repeat_admit_with_a_late_stamped_restriction_gives_no_permit(self):
        original = self.reserve('permit')
        self.prepared(original)
        self.assertIs(ProbeAdmission, type(self.owner.admit(original)))
        self.store(PAID, age=3500)
        with self.assertRaises(ProbeAdmissionError):
            self.owner.admit(original)

    def test_BR19_a_launch_after_a_changed_input_is_refused(self):
        original = self.reserve('permit')
        self.prepared(original)
        permit = self.owner.admit(original)
        self.append_any(PAID)
        with self.assertRaises(ProbeAdmissionError):
            self.owner._execution(permit, launching=True)

    def test_BR19_a_launch_after_the_identity_left_its_window_is_refused(self):
        original = self.reserve('permit')
        self.prepared(original)
        permit = self.owner.admit(original)
        self.clock -= 5000  # an identity stamped after now is no evidence of now
        with self.assertRaises(Exception):
            self.owner._execution(permit, launching=True)

    def test_BR19_the_sealed_yes_exists_and_the_publication_check_of_a_yes_returns_one(self):
        sealed = getattr(account_billing_admission, 'Yes', None)
        self.assertIsNotNone(sealed, 'there is no sealed yes type')
        self.saved_reused()
        self.clock += 1000
        original = self.reserve('saved')
        self.observe_identity()
        self.prepared(original)
        reply = self.owner.admit(original)
        judgment = self.owner.judgment_of(original)
        verdict = self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                              before_commit=lambda: True, reply=reply, history=original.history,
                                              judgment=judgment)
        self.assertIsInstance(verdict, sealed)

    def test_BR19_a_yes_with_no_judgment_to_gate_it_against_is_refused(self):
        self.saved_reused()
        self.clock += 1000
        original = self.reserve('saved')
        self.observe_identity()
        self.prepared(original)
        reply = self.owner.admit(original)
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                        before_commit=lambda: True, reply=reply, history=original.history)

    def test_BR19_a_denial_needs_no_judgment_to_be_published(self):
        """Guard: only a yes is gated; a refusal is still published from its committed row."""
        self.store_probe_only(PAID)
        original = self.reserve('denied')
        self.prepared(original)
        reply = self.owner.admit(original)
        self.assertEqual('denied', reply['probe'], reply)
        self.assertTrue(self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'denied', self.context,
                                                    before_commit=lambda: True, reply=reply))


# ---- the service: take and publish -----------------------------------------------------------------
@PLATFORM
class ServiceMatrix(unittest.TestCase, MutationsOwner):
    setUp = _rj.RejudgeDeliveryTests.setUp
    stop = _rj.RejudgeDeliveryTests.stop
    request = _rj.RejudgeDeliveryTests.request
    begin = _rj.RejudgeDeliveryTests.begin
    until = _rj.RejudgeDeliveryTests.until
    take = _rj.RejudgeDeliveryTests.take
    launches = _rj.RejudgeDeliveryTests.launches
    fingerprint = _rj.RejudgeDeliveryTests.fingerprint
    store = _rj.RejudgeDeliveryTests.store
    outcome = _rj.RejudgeDeliveryTests.outcome
    ready = _rj.RejudgeDeliveryTests.ready
    ready_reused = _rj.RejudgeDeliveryTests.ready_reused
    counted = {}

    def fingerprint_of(self):
        return self.fingerprint()

    def project_id_of(self):
        return self.fixture.project_id

    def views_of(self):
        return self.billing._owner._views

    def stamp(self):
        return time.time_ns() // 1_000_000

    def paid_now(self):
        self.store(PAID)

    def late_paid(self):
        if self.ledger._connection.in_transaction:
            records = claude_rate_limit_records(PAID, account_fingerprint=self.fingerprint(),
                                                observed_at_ms=self.stamp()-3500)
            for record in records:
                self.ledger._append_checked_locked(self.fixture.project_id, record, producer='provider',
                                                   timestamp=self.stamp())
        else:
            self.store(PAID, age=3500)

    def deliver(self, tap, identity):
        """A ready yes, then take and publish with the callbacks counted (and the k-th one mutating)."""
        self.allowed = tap
        ticket, request = self.ready_reused(identity)
        tap.armed = True
        try:
            return self.outcome(ticket, request)
        finally:
            tap.armed = False

    def injected(self, name, k):
        if 'service' not in ServiceMatrix.counted:
            tap = ServiceTap()
            answer = self.deliver(tap, 'probe-control')
            self.assertEqual('reused', answer['probe'], answer)
            self.assertGreaterEqual(tap.calls, 1)
            ServiceMatrix.counted['service'] = tap.calls
        count = ServiceMatrix.counted['service']
        if k > count:
            self.skipTest(f'take and publish make only {count} authorization callbacks')
        tap = ServiceTap()
        tap.aim(k, self.mutation(name))
        answer = self.deliver(tap, f'probe-{name}-{k}'.replace(' ', '-'))
        self.assertTrue(tap.fired, f'callback {k} of {count} was never reached')
        self.assertTrue(tap.took_effect, f'the mutation "{name}" did not take effect')
        self.assertNotIn(answer['probe'], YES_KINDS, f'"{name}" made by callback {k} of {count} was published as a yes')

    def test_BR16_paid_usage_added_by_before_publish_is_refused(self):
        """before_publish is the first callback of publication (the service reads it before the publication check)."""
        self.allowed = tap = ServiceTap()
        ticket, request = self.ready_reused('probe-bp')
        real = self.billing.take
        seen = {}

        def take(t, **options):
            reply = real(t, **options)
            if reply is not None:
                seen['taken'] = tap.calls
                tap.armed, tap.calls = True, 0
                tap.aim(1, self.mutation('paid usage'))  # the first callback after take is before_publish
            return reply

        with mock.patch.object(self.billing, 'take', side_effect=take):
            answer = self.outcome(ticket, request)
        tap.armed = False
        self.assertTrue(tap.fired and tap.took_effect, 'before_publish was never reached')
        self.assertNotIn(answer['probe'], YES_KINDS, answer)

    # ---- BR-18 at take and publish; the writer in between -------------------------------------------
    def stepped_clock(self, step):
        base = lambda: time.time_ns() // 1_000_000  # noqa: E731
        return mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: base() + self.step_ms)

    def test_BR18_billing_that_left_the_reuse_age_while_the_job_was_ready_is_not_delivered_as_a_yes(self):
        self.step_ms = 0
        self.store(FAVOURABLE, age=PROBE_REUSE_MS - 20_000)  # 20 s inside the reuse age
        request = self.request('probe-expiring')
        with self.stepped_clock(0):
            ticket = self.begin(request)
            self.ready(ticket)
            self.assertEqual('reused', self.billing._jobs[ticket].result['probe'])
            self.step_ms = 40_000  # past the reuse age; the 60 s deadline is still ahead
            answer = self.outcome(ticket, request)
        self.assertNotIn(answer['probe'], YES_KINDS, answer)

    def test_BR18_billing_that_left_the_reuse_age_between_take_and_publish_is_refused(self):
        self.step_ms = 0
        self.store(FAVOURABLE, age=PROBE_REUSE_MS - 20_000)
        request = self.request('probe-expiring-2')
        real = self.billing.take

        def take(t, **options):
            reply = real(t, **options)
            if reply is not None:
                self.step_ms = 40_000
            return reply

        with self.stepped_clock(0), mock.patch.object(self.billing, 'take', side_effect=take):
            ticket = self.begin(request)
            self.ready(ticket)
            answer = self.outcome(ticket, request)
        self.assertEqual(40_000, self.step_ms, 'the clock was not stepped between take and publish')
        self.assertNotIn(answer['probe'], YES_KINDS, answer)

    def test_BR18_guard_without_a_clock_step_the_same_job_is_delivered(self):
        self.store(FAVOURABLE, age=PROBE_REUSE_MS - 20_000)
        request = self.request('probe-expiring-3')
        ticket = self.begin(request)
        self.ready(ticket)
        self.assertEqual('reused', self.outcome(ticket, request)['probe'])

    def test_BR_a_writer_thread_between_take_and_publish_is_refused_at_publish(self):
        """Re-judged once at take, refused at publish: a restriction that arrives after take is not judged
        again; the publication is refused."""
        ticket, request = self.ready_reused('probe-writer')
        real, judged = self.billing.take, []

        def take(t, **options):
            reply = real(t, **options)
            if reply is not None and not judged:
                judged.append(True)
                writer = threading.Thread(target=lambda: self.store(PAID))
                writer.start()
                writer.join(10)
            return reply

        capture = self.enterContext(mock.patch.object(self.claims._intake, 'capture_admission',
                                                      wraps=self.claims._intake.capture_admission))
        with mock.patch.object(self.billing, 'take', side_effect=take):
            answer = self.outcome(ticket, request)
        self.assertTrue(judged)
        self.assertNotIn(answer['probe'], YES_KINDS, answer)
        self.assertEqual(0, capture.call_count, 'publication judged again; it must only refuse')


for _name in MUTATIONS:
    for _k in range(1, SERVICE_MAX_K+1):
        def _test(self, _name=_name, _k=_k):
            self.injected(_name, _k)
        _test.__name__ = f'test_matrix_service_take_and_publish_{_name.replace(" ", "_")}_k{_k}'
        setattr(ServiceMatrix, _test.__name__, _test)


# ======================================================================================================
# Part B: BR-20, the registry and the structural check
# ======================================================================================================
CALLBACK_NAMES = frozenset({'callback', 'before_commit', 'before_publish', 'authorize'})

# Every function that runs, or hands on, a caller's callback (found by name); a new one must be added here,
# on purpose, or the check fails. The gate functions are never among them.
CALLBACK_POINTS = {
    'account_billing_admission': {
        'AccountBillingAdmission._authorize', 'AccountBillingAdmission.reserve',
        'AccountBillingAdmission._refuse_unusable', 'AccountBillingAdmission._refuse_repeat_key',
        'AccountBillingAdmission._reserve_repeat', 'AccountBillingAdmission._original',
        'AccountBillingAdmission._admit', 'AccountBillingAdmission.known_reply',
        'AccountBillingAdmission.verify_committed'},
    'account_billing_jobs': {
        'AccountBillingJobs._authorize', 'AccountBillingJobs.submit', 'AccountBillingJobs._begin_judgment',
        'AccountBillingJobs._rejudge', 'AccountBillingJobs._step', 'AccountBillingJobs.take'},
    'account_billing_execution': {'AccountBillingExecutions._commit'},
    'account_service': {'AccountService._billing_reply', 'AccountService._take_claim_reply'},
}
# The functions that can hand out a yes, and the gate each must call after its callbacks. After the last gate call
# of a path nothing may run caller code, and nothing listed in `AFTER` may be read.
POSITIVE_SITES = {
    'account_billing_admission': {
        'AccountBillingAdmission._admit': {'gates': {'_gate', '_gate_permit'}, 'after': set()},
        'AccountBillingAdmission.verify_committed': {'gates': {'_gate'}, 'after': set()},
        'AccountBillingAdmission._execution': {'gates': {'_gate_permit'}, 'after': set()},
    },
    'account_service': {
        'AccountService._billing_reply': {'gates': {'verify_committed'},
                                          'after': {'billing_waivers', '_provenance_live'}},
    },
    'learning_store': {
        'LearningLedger.observe_claude_billing': {'gates': {'reuse_gate'}, 'after': set()},
    },
}
SEAL_BUILDERS = {'account_billing_admission': ('Yes', {'AccountBillingAdmission._seal_yes'})}


def functions(tree):
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f'{node.name}.{member.name}', member
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def callback_refs(function):
    """Every mention (call, alias, argument, attribute) of a callback name in a function."""
    found = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in CALLBACK_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in CALLBACK_NAMES:
            found.append((node.lineno, node.attr))
    return found


def called_names(node):
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            target = call.func
            if isinstance(target, ast.Attribute):
                yield target.attr, call
            elif isinstance(target, ast.Name):
                yield target.id, call


def block_path(block, target):
    """[(block, index), ...] from the function body down to the statement holding `target`."""
    for index, statement in enumerate(block):
        if not any(node is target for node in ast.walk(statement)):
            continue
        children = [getattr(statement, name) for name in ('body', 'orelse', 'finalbody') if getattr(statement, name, None)]
        children += [handler.body for handler in getattr(statement, 'handlers', [])]
        for child in children:
            inner = block_path(child, target)
            if inner:
                return [(block, index)] + inner
        return [(block, index)]
    return []


def after_gate(function, gate_call):
    """The statements that can still run after this gate call on its path (a block that returns or raises ends it)."""
    path = block_path(function.body, gate_call)
    tail = []
    for block, index in reversed(path):
        rest = block[index+1:]
        tail.extend(rest)
        if any(isinstance(statement, (ast.Return, ast.Raise)) for statement in rest):
            break
    return tail


def closure_of(tree):
    """Functions that run a callback, directly or through another function of the module (by name)."""
    table = dict(functions(tree))
    direct = {q for q, f in table.items() if callback_refs(f)}
    closure = {q.split('.')[-1] for q in direct}
    changed = True
    while changed:
        changed = False
        for q, f in table.items():
            name = q.split('.')[-1]
            if name not in closure and any(n in closure for n, _ in called_names(f)):
                closure.add(name)
                changed = True
    return closure


def check_source(source, *, registry, positive, seal=None):
    """The structural check. Returns the list of violations (empty = fine)."""
    tree = ast.parse(source)
    table = dict(functions(tree))
    problems = []
    direct = {q for q, f in table.items() if callback_refs(f)}
    for q in sorted(direct - registry):
        problems.append(f'unregistered callback point: {q}')
    for q in sorted(registry - direct):
        problems.append(f'stale registry entry (no callback there): {q}')
    closure = closure_of(tree)
    for q, rule in positive.items():
        if q not in table:
            problems.append(f'positive site is missing: {q}')
            continue
        function = table[q]
        gates = [call for name, call in called_names(function) if name in rule['gates']]
        if not gates:
            problems.append(f'no gate in positive site: {q}')
            continue
        for gate in gates:
            for statement in after_gate(function, gate):
                for line, name in callback_refs(statement):
                    problems.append(f'{q}: callback "{name}" mentioned after the gate (line {line})')
                for node in ast.walk(statement):
                    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
                    if name in closure and name not in rule['gates']:
                        problems.append(f'{q}: "{name}" (it can run caller code) used after the gate (line {node.lineno})')
                    if name in rule['after']:
                        problems.append(f'{q}: "{name}" used after the gate (line {node.lineno})')
    if seal is not None:
        kind, builders = seal
        for q, function in table.items():
            for name, call in called_names(function):
                if name == kind and q not in builders:
                    problems.append(f'{q}: a {kind} is built outside the gate (line {call.lineno})')
    return problems


def module_source(module):
    return inspect.getsource(module)


class BR20StructuralCheck(unittest.TestCase):
    MODULES = {'account_billing_admission': account_billing_admission, 'account_billing_jobs': account_billing_jobs,
               'account_billing_execution': account_billing_execution, 'account_service': account_service,
               'learning_store': learning_store}

    def test_BR20_the_registry_of_callback_points_matches_the_code(self):
        for name, registry in CALLBACK_POINTS.items():
            with self.subTest(module=name):
                self.assertEqual([], check_source(module_source(self.MODULES[name]), registry=registry, positive={}))

    def test_BR20_every_positive_site_gates_after_its_callbacks_and_nothing_runs_after_the_gate(self):
        for name, positive in POSITIVE_SITES.items():
            with self.subTest(module=name):
                registry = CALLBACK_POINTS.get(name)
                source = module_source(self.MODULES[name])
                problems = check_source(source, registry=registry if registry is not None else set(), positive=positive,
                                        seal=SEAL_BUILDERS.get(name))
                problems = [p for p in problems if 'callback point' not in p and 'registry entry' not in p
                            ] if registry is None else problems
                self.assertEqual([], problems)

    def test_BR20_a_sealed_yes_is_built_only_by_the_gate(self):
        source = module_source(account_billing_admission)
        tree = ast.parse(source)
        table = dict(functions(tree))
        self.assertIn('AccountBillingAdmission._seal_yes', table, 'the one place a yes is built')
        builders = {q for q, f in table.items() for name, _ in called_names(f) if name == '_seal_yes'}
        self.assertEqual({'AccountBillingAdmission._gate', 'AccountBillingAdmission._gate_permit'}, builders)
        for gate in ('AccountBillingAdmission._gate', 'AccountBillingAdmission._gate_permit'):
            self.assertIn(gate, table)
            self.assertFalse({n for n, _ in called_names(table[gate])} & {'_authorize', '_original', 'reserve'},
                             f'{gate} must not run caller code')
            self.assertEqual([], callback_refs(table[gate]), f'{gate} must not mention a callback')

    def test_BR20_no_judgment_current_and_no_baseline_capture_are_left(self):
        source = module_source(account_billing_admission)
        self.assertNotIn('def judgment_current', source, 'the take check is the gate now')
        verify = dict(functions(ast.parse(source)))['AccountBillingAdmission.verify_committed']
        captures = [name for name, _ in called_names(verify) if name == '_capture']
        self.assertEqual([], captures, 'the publication check compares with the judgment, not a capture of its own')

    # ---- the check itself, against violators it must catch -------------------------------------------
    POSITIVE = {'C.make': {'gates': {'gate'}, 'after': {'read_waiver'}}}
    REGISTRY = {'C.authorize_it', 'C.make'}

    def violations(self, body):
        source = textwrap.dedent('''
            class C:
                def authorize_it(self, callback):
                    return callback()
                def make(self, before_commit):
                    self.authorize_it(before_commit)
        ''') + textwrap.indent(textwrap.dedent(body), '        ')
        return check_source(source, registry=self.REGISTRY, positive=self.POSITIVE)

    def test_BR20_the_checker_accepts_a_correct_site(self):
        self.assertEqual([], self.violations('''
            self.gate()
            return 1
        '''))

    def test_BR20_the_checker_catches_a_missing_gate(self):
        self.assertTrue(any('no gate' in p for p in self.violations('return 1\n')))

    def test_BR20_the_checker_catches_a_callback_called_after_the_gate(self):
        self.assertTrue(any('after the gate' in p for p in self.violations('''
            self.gate()
            self.authorize_it(before_commit)
        ''')))

    def test_BR20_the_checker_catches_an_aliased_callback_after_the_gate(self):
        for body in ('''
            self.gate()
            alias = before_commit
            alias()
        ''', '''
            self.gate()
            run = self.authorize_it
            run(None)
        ''', '''
            self.gate()
            callbacks = [before_commit]
            callbacks[0]()
        '''):
            with self.subTest(body=body):
                self.assertTrue(any('after the gate' in p for p in self.violations(body)), body)

    def test_BR20_the_checker_catches_a_callback_after_the_commit_that_follows_a_gate_inside_a_with_block(self):
        self.assertTrue(any('after the gate' in p for p in self.violations('''
            with self.transaction():
                self.gate()
            self.authorize_it(before_commit)
            return 1
        ''')))

    def test_BR20_a_branch_that_returns_right_after_its_gate_does_not_hide_a_later_path(self):
        self.assertTrue(any('after the gate' in p for p in self.violations('''
            if self.early:
                self.gate()
                return 1
            self.gate()
            self.authorize_it(before_commit)
        ''')))

    def test_BR20_a_branch_that_returns_right_after_its_gate_is_not_blamed_for_the_other_path(self):
        self.assertEqual([], self.violations('''
            if self.early:
                self.gate()
                return 1
            self.authorize_it(before_commit)
            self.gate()
            return 2
        '''))

    def test_BR20_the_checker_catches_a_read_that_must_come_before_the_gate(self):
        self.assertTrue(any('read_waiver' in p for p in self.violations('''
            self.gate()
            self.read_waiver()
        ''')))

    def test_BR20_the_checker_catches_an_unregistered_callback_point(self):
        source = textwrap.dedent('''
            class C:
                def authorize_it(self, callback):
                    return callback()
                def make(self, before_commit):
                    self.gate()
                def sneaky(self, before_publish):
                    before_publish()
        ''')
        problems = check_source(source, registry=self.REGISTRY, positive=self.POSITIVE)
        self.assertTrue(any('unregistered callback point: C.sneaky' in p for p in problems), problems)

    def test_BR20_the_checker_catches_a_yes_built_outside_the_gate(self):
        source = textwrap.dedent('''
            class C:
                def _seal(self):
                    return Yes()
                def other(self):
                    return Yes()
        ''')
        problems = check_source(source, registry=set(), positive={}, seal=('Yes', {'C._seal'}))
        self.assertEqual(1, len(problems), problems)
        self.assertIn('C.other', problems[0])

    def test_BR20_a_gate_that_is_removed_from_the_real_code_is_caught(self):
        source = module_source(account_billing_admission)
        stripped = source.replace('self._gate(', 'self._removed(').replace('self._gate_permit(', 'self._removed(')
        problems = check_source(stripped, registry=CALLBACK_POINTS['account_billing_admission'],
                                positive=POSITIVE_SITES['account_billing_admission'])
        self.assertTrue(any('no gate in positive site' in p for p in problems), problems)


# ======================================================================================================
# Part C: BR-21, the ledger's own reuse path (LearningLedger.observe_claude_billing)
# ======================================================================================================
class LedgerReuseBase(unittest.TestCase):
    setUp = fixtures.AdmissionTests.setUp
    tearDown = fixtures.AdmissionTests.tearDown
    tick = fixtures.AdmissionTests.tick
    observe = fixtures.AdmissionTests.observe
    attest = fixtures.AdmissionTests.attest

    def ready_account(self, *, probe_records=True):
        self.identity = self.observe('claude')[0]
        self.attest('claude', self.identity['account_fingerprint'])
        self.fingerprint = self.identity['account_fingerprint']
        if probe_records:
            self.store(FAVOURABLE)

    def store(self, info, *, age=0, **options):
        at = self.clock - age
        records = claude_rate_limit_records(info, account_fingerprint=self.fingerprint, observed_at_ms=at, **options)
        self.ledger._store_provider_observations(self.project_id, records, timestamp=at)
        return records

    def probe(self, *, environ=None, plus=1000):
        with trusted_tools(self.tools), mock.patch.object(self.ledger, '_clock_ms', return_value=self.clock+plus), \
                mock.patch('codex_agents.claude_host.check_backend'), \
                mock.patch('codex_agents.bridge.claude_billing_probe', return_value=([FAVOURABLE_INFO], None)):
            return self.ledger.observe_claude_billing(self.project_id, project_root=self.tools,
                                                      environ={} if environ is None else environ)

    def denials(self, reason=None):
        rows = [json.loads(bytes(r[0])) for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission' AND source='lifecycle'")]
        return [r for r in rows if r['decision'] == 'deny' and (reason is None or r['reason_code'] == reason)]


FAVOURABLE_INFO = FAVOURABLE


class BR21LedgerReuse(LedgerReuseBase):
    def test_BR21_guard_clean_probe_records_are_reused(self):
        self.ready_account()
        answer = self.probe()
        self.assertEqual('reused', answer['probe'], answer)

    def test_BR21_a_caller_supplied_environ_cannot_hide_the_process_api_key_on_a_reused_answer(self):
        """Reproduced on f0f43fbd: with ANTHROPIC_API_KEY set in the process, environ={} gives 'reused'."""
        self.ready_account()
        with mock.patch.dict(os.environ, KEY):
            with self.assertRaises(AdmissionDenied) as raised:
                self.probe(environ={})
        self.assertEqual('provider_api_key_present', raised.exception.reason_code)

    def test_BR21_a_key_in_the_passed_environ_is_still_refused(self):
        self.ready_account()
        with self.assertRaises((AdmissionDenied, ContractError)):  # the observation refuses a key by name
            self.probe(environ=dict(KEY))

    def test_BR21_the_cited_records_are_the_probe_s_own_and_never_a_run_record(self):
        """Reproduced on f0f43fbd: headroom[-1:] and the latest billing may be a finished child's (run_id) records,
        which the reply contract of the account path rejects."""
        self.ready_account()
        self.clock += 10
        self.store(FAVOURABLE, run_id='run-x', phase='during')
        answer = self.probe()
        self.assertEqual('reused', answer['probe'], answer)
        cited = [r for r in answer['records'] if r['record_type'] != 'provider_identity']
        self.assertTrue(cited)
        self.assertEqual([None] * len(cited), [r.get('run_id') for r in cited])
        self.assertEqual([None] * len(cited), [r.get('phase') for r in cited])

    def test_BR21_a_run_record_cannot_refresh_the_reuse_age_of_the_probe_s_billing(self):
        self.ready_account()
        self.clock += PROBE_REUSE_MS + 60_000   # the probe's own billing is now past the reuse age
        self.store(FAVOURABLE, run_id='run-y', phase='during')  # a child's fresh record must not stand in for it
        self.assertNotEqual('reused', self.probe(plus=1000)['probe'])

    def test_BR21_a_newer_run_record_that_is_not_ample_stops_the_reuse_as_it_does_on_the_account_path(self):
        self.ready_account()
        self.clock += 10
        warning = {**FAVOURABLE, 'status': 'allowed_warning'}
        self.store(warning, run_id='run-z', phase='during')
        self.assertNotEqual('reused', self.probe()['probe'])

    # ---- PC-1..PC-2 (2026-11-22 probe reuse): the same rule on the ledger path -----------------------------
    CONSTRAINED = {**FAVOURABLE, 'status': 'allowed_warning',
                   'unifiedWindows': {'five_hour': {'utilization': 0.38, 'status': 'allowed'},
                                      'seven_day': {'utilization': 0.74, 'status': 'allowed_warning'}}}

    def probe_then_run(self, probe_info, run_info, *, between=30_000):
        self.ready_account(probe_records=False)
        self.store(probe_info)
        self.clock += between
        self.store(run_info, run_id='run-pc', phase='during')

    def test_PC1_a_run_record_as_constrained_as_the_probe_does_not_block_the_ledger_reuse(self):
        self.probe_then_run(self.CONSTRAINED, self.CONSTRAINED)
        answer = self.probe(plus=48_000)
        self.assertEqual('reused', answer['probe'], answer)

    def test_PC2_an_ample_probe_and_a_constrained_run_record_still_stop_the_ledger_reuse(self):
        self.probe_then_run(FAVOURABLE, self.CONSTRAINED)
        self.assertNotEqual('reused', self.probe(plus=48_000)['probe'])

    def test_PC2_a_constrained_probe_and_an_exhausted_run_record_still_stop_the_ledger_reuse(self):
        self.probe_then_run(self.CONSTRAINED, {**self.CONSTRAINED, 'status': 'rejected'})
        with self.assertRaises(AdmissionDenied) as raised:
            self.probe(plus=48_000)
        self.assertEqual('provider_exhausted', raised.exception.reason_code)

    def test_PC2_a_constrained_probe_and_a_paid_usage_run_record_still_stop_the_ledger_reuse(self):
        self.probe_then_run(self.CONSTRAINED, {**self.CONSTRAINED, 'isUsingOverage': True, 'overageStatus': 'allowed'})
        with self.assertRaises(AdmissionDenied) as raised:
            self.probe(plus=48_000)
        self.assertEqual('paid_usage_usable', raised.exception.reason_code)

    def test_PC2_an_equal_state_does_not_reuse_a_stale_probe_on_the_ledger_path(self):
        self.probe_then_run(self.CONSTRAINED, self.CONSTRAINED)
        self.assertNotEqual('reused', self.probe(plus=PROBE_REUSE_MS + 1_000)['probe'])

    # ---- the shared gate function itself (G4): a reason code, or None when the yes may be built ----------
    def gate(self, **changes):
        arguments = dict(environ={}, process_environ={}, identity_at=1_000_000, reuse_at=1_000_000,
                         now_ms=1_100_000, since_ms=1_000_000-PROBE_REUSE_MS, reuse_ms=PROBE_REUSE_MS)
        arguments.update(changes)
        gate = getattr(admission_metadata, 'reuse_gate', None)
        self.assertIsNotNone(gate, 'there is no shared gate function')
        return gate(**arguments)

    def test_BR21_the_shared_gate_passes_clean_fresh_inputs(self):
        self.assertIsNone(self.gate())

    def test_BR21_the_shared_gate_refuses_a_key_in_either_environment(self):
        self.assertEqual('provider_api_key_present', self.gate(environ=dict(KEY)))
        self.assertEqual('provider_api_key_present', self.gate(process_environ=dict(KEY)))

    def test_BR21_the_shared_gate_refuses_billing_past_the_reuse_age_or_from_the_future(self):
        self.assertEqual('billing_unverified', self.gate(reuse_at=1_100_000-PROBE_REUSE_MS))   # exactly the age
        self.assertEqual('billing_unverified', self.gate(reuse_at=1_100_001))                  # stamped after now
        self.assertEqual('billing_unverified', self.gate(reuse_at=None))

    def test_BR21_the_shared_gate_refuses_an_identity_outside_its_window(self):
        self.assertEqual('provider_identity_missing', self.gate(identity_at=1_000_000-PROBE_REUSE_MS-1))
        self.assertEqual('provider_identity_missing', self.gate(identity_at=1_100_001))
        self.assertEqual('provider_identity_missing', self.gate(identity_at=None))

    def test_BR21_the_ledger_reuse_goes_through_the_shared_gate(self):
        self.assertTrue(hasattr(admission_metadata, 'reuse_gate'), 'there is no shared gate function')
        self.ready_account()
        with mock.patch.object(admission_metadata, 'reuse_gate', side_effect=AssertionError('gate reached')) as gate:
            with self.assertRaisesRegex(AssertionError, 'gate reached'):
                self.probe()
        gate.assert_called()


# ======================================================================================================
# Part D: BR-22, one deny record per reason per account per window
# ======================================================================================================
@PLATFORM
class BR22OwnerDenyRecords(_rj.RejudgeOwnerBase):
    def deny_records(self, reason=None):
        rows = [json.loads(bytes(r[0])) for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission' AND source='lifecycle'")]
        return [r for r in rows if r['decision'] == 'deny' and (reason is None or r['reason_code'] == reason)]

    def refused(self, identity):
        original = self.reserve(identity)
        self.observe_identity()
        self.prepared(original)
        return self.owner.admit(original)

    def without_attestation(self):
        self.ledger._connection.execute('DELETE FROM owner_attestations')

    def test_BR22_many_refusals_for_one_reason_and_account_store_one_record_per_window(self):
        self.without_attestation()
        for number in range(5):
            self.clock += 1  # distinct timestamps: each would be a new record without the bound
            self.assertDenied('billing_attestation_missing', self.refused(f'deny-{number}'))
        self.assertEqual(1, len(self.deny_records('billing_attestation_missing')))

    def test_BR22_every_refused_request_still_gets_its_own_named_reply(self):
        self.without_attestation()
        replies = [self.refused(f'deny-{number}') for number in range(3)]
        self.assertEqual(['billing_attestation_missing'] * 3, [r['reason_code'] for r in replies])
        self.assertEqual(['denied'] * 3, [r['probe'] for r in replies])

    def test_BR22_another_reason_or_another_account_is_its_own_record(self):
        self.without_attestation()
        self.assertDenied('billing_attestation_missing', self.refused('first'))
        self.fixture.attest('claude', self.identity['account_fingerprint'])
        self.store(PAID)
        self.assertDenied('paid_usage_usable', self.refused('second'))
        self.assertDenied('paid_usage_usable', self.refused('third'))
        self.assertEqual(['billing_attestation_missing', 'paid_usage_usable'],
                         sorted(r['reason_code'] for r in self.deny_records()))

    def test_BR22_a_new_window_stores_a_new_record(self):
        self.without_attestation()
        self.assertDenied('billing_attestation_missing', self.refused('first'))
        self.clock += PROBE_COOLDOWN_MS + 1
        self.assertDenied('billing_attestation_missing', self.refused('second'))
        self.assertEqual(2, len(self.deny_records('billing_attestation_missing')))

    def test_BR22_a_repeat_refused_for_an_api_key_stores_one_record_per_window(self):
        self.saved_reused()
        for number in range(4):
            self.clock += 1
            with mock.patch.dict(os.environ, KEY):
                self.assertDenied('provider_api_key_present', self.repeat())
        self.assertEqual(1, len(self.deny_records('provider_api_key_present')))

    def test_BR22_stored_records_keep_the_ordinary_retention(self):
        self.without_attestation()
        self.refused('first')
        rows = self.ledger._connection.execute(
            "SELECT observed_at_ms,expires_at_ms FROM records WHERE record_type='probe_admission'").fetchall()
        self.assertTrue(rows)
        for observed, expires in rows:
            self.assertEqual(observed + DEFAULT_RETENTION_MS, expires)

    def test_BR22_a_refusal_never_costs_the_probe_cooldown_or_a_pass(self):
        """Guard: only denial records are bounded; a pass admission is always stored."""
        self.without_attestation()
        self.refused('first')
        self.fixture.attest('claude', self.identity['account_fingerprint'])
        original = self.reserve('permit')
        self.observe_identity()
        self.prepared(original)
        self.assertIs(ProbeAdmission, type(self.owner.admit(original)))
        self.assertEqual(1, self.passes())


class BR22LedgerDenyRecords(LedgerReuseBase):
    def test_BR22_the_ledger_stores_one_deny_record_per_reason_and_account_per_window(self):
        self.identity = self.observe('claude')[0]  # no attestation
        for number in range(4):
            self.clock += 1
            with self.assertRaises(AdmissionDenied):
                self.probe(plus=1000+number)
        self.assertEqual(1, len(self.denials('billing_attestation_missing')))

    def test_BR22_the_ledger_stores_a_new_record_in_a_new_window(self):
        self.identity = self.observe('claude')[0]
        with self.assertRaises(AdmissionDenied):
            self.probe()
        self.clock += PROBE_COOLDOWN_MS + 1
        with self.assertRaises(AdmissionDenied):
            self.probe()
        self.assertEqual(2, len(self.denials('billing_attestation_missing')))


if __name__ == '__main__':
    unittest.main()
