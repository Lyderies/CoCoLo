"""CoCoLo 0.4.0 billing safety: BR-1..BR-14 (owner decisions 43 and 44, tests before code).

Design: a saved answer is never trusted on its own. Every repeat of a request gets a fresh judgment of
the current records; only the side effects (provider call, one-time allowances) happen once.

The interface these tests assume, and nothing more (the code unit implements exactly this):

  I1  `AccountBillingAdmission.known_reply(...)` returns None for a committed 'reused' or 'completed'
      row whose answer is a success (the "judge again" signal). A committed denial, and a completed row
      that carries a failure reason, are still returned as stored (they are sticky, BR-6).
  I2  `AccountBillingAdmission.reserve(...)` for such a row returns a NEW `_Reservation` (a new judgment
      of the same request key), not the stored reply. It then goes through `prepare` and `admit` like a
      fresh request, and `admit` returns a reply dict: 'reused' / 'completed' with known=True when the
      fresh judgment passes on records fresh enough to reuse; 'denied' with the fresh reason when it
      refuses; 'denied' with reason `probe_replay_stale` when passing would need a new provider call.
      An admit that would pass with a new probe NEVER launches for a repeat. The stored row (its history)
      is not overwritten by a repeat's refusal.
  I3  `probe_replay_stale` is a valid reason of a denied reply (`checked_billing_probe_reply`).
  I4  A provider API key refuses a request at every stage (reserve, a repeat, known_reply, prepare,
      admit) with `provider_api_key_present`, before any signature work: either as a denied reply or as
      `ProbeAdmissionError('provider_api_key_present')` (the reason must then be one the error keeps).
  I5  Where the owner judges, it calls `account_billing_admission.evaluate_admission` (BR-14 puts its
      test-only opt-in in front of that one function; no opt-in exists in product code).

Records are the real ledger's, the signature path is the real worker pool. The only seams are the
ledger's own clock (`ledger._clock_ms`, the one the code reads), call-through spies and injected faults.
"""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_billing_admission as _owner_tests
from tests import test_account_billing_jobs as _service_tests

from codex_agents import account_billing_admission
from codex_agents.account_api import AccountError
from codex_agents.account_billing_admission import PROBE_REUSE_MS, ProbeAdmission, ProbeAdmissionError
from codex_agents.admission_inputs import AdmissionInputPolicy
from codex_agents.billing_probe_contracts import BillingProbeError, checked_billing_probe_reply
from codex_agents.learning_contracts import canonical_json
from codex_agents.providers import claude_rate_limit_records
from codex_agents.sql_read_budget import SQLReadError, SQLReadLimits

PLATFORM = unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin'
    and Path('/usr/bin/sandbox-exec').is_file())), 'original fixed macOS signature worker guard')
FAVOURABLE = {'status': 'allowed', 'isUsingOverage': False, 'overageStatus': 'rejected',
              'overageDisabledReason': 'org_level_disabled'}
PAID = {'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'}
EXHAUSTED = {'status': 'rejected', 'isUsingOverage': False, 'overageStatus': 'rejected',
             'overageDisabledReason': 'org_level_disabled'}
KEY = {'ANTHROPIC_API_KEY': 'sk-test'}


@PLATFORM
class RejudgeOwnerBase(unittest.TestCase):
    """The real owner, ledger, signature pool and records of test_account_billing_admission."""
    setUp = _owner_tests.AccountBillingAdmissionTests.setUp
    stop = _owner_tests.AccountBillingAdmissionTests.stop
    reserve = _owner_tests.AccountBillingAdmissionTests.reserve
    prepared = _owner_tests.AccountBillingAdmissionTests.prepared
    rows = _owner_tests.AccountBillingAdmissionTests.rows
    store_probe_only = _owner_tests.AccountBillingAdmissionTests.store_probe_only
    FAVOURABLE = FAVOURABLE

    # ---- records, clock, effects -------------------------------------------------------------------
    def store(self, info, *, age=0, run_id=None):
        """Provider records observed `age` ms before the clock, stored now (ingested at the clock)."""
        options = {} if run_id is None else {'run_id': run_id}
        records = claude_rate_limit_records(info, account_fingerprint=self.identity['account_fingerprint'],
                                            observed_at_ms=self.clock-age, **options)
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
        return records

    def observe_identity(self, **changes):
        """The job's own identity observation, taken at the clock."""
        return self.fixture.observe('claude', now_ms=self.clock, **changes)

    def passes(self):
        return sum(json.loads(bytes(r[0]))['decision'] == 'pass' for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission'"))

    def effects(self):
        """What a repeat must not change when it only refuses: passing admissions, holds, launch claims, rows."""
        connection = self.ledger._connection
        count = lambda sql: connection.execute(sql).fetchone()[0]  # noqa: E731
        return {'pass_admissions': self.passes(), 'holds': count('SELECT COUNT(*) FROM run_holds'),
                'launch_claims': count('SELECT COUNT(*) FROM launch_claims'),
                'admitted_rows': count("SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'")}

    def row_answer(self, identity):
        """The stored answer of the (only) row that holds this request's history."""
        found = [json.loads(r['references_json']) for r in self.rows() if r['references_json']]
        self.assertTrue(found, 'no stored answer')
        return found

    def spy_capture(self):
        """Call-through spy: how many signature jobs (fresh judgments) were started."""
        return self.enterContext(mock.patch.object(self.intake, 'capture_admission',
                                                   wraps=self.intake.capture_admission))

    # ---- the saved answers -------------------------------------------------------------------------
    def saved_reused(self, identity='saved'):
        self.store_probe_only(FAVOURABLE)
        original = self.reserve(identity)
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertEqual('reused', answer['probe'], answer)
        return answer

    def saved_denied(self, info, identity='saved'):
        self.store_probe_only(info)
        original = self.reserve(identity)
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertEqual('denied', answer['probe'], answer)
        return answer

    def commit_completed(self, identity, reason, selected=()):
        """A finished probe's committed row, written the way the execution owner writes it (history)."""
        original = self.reserve(identity)
        refs = []
        for record in selected:
            digest = self.ledger._connection.execute('SELECT content_digest FROM records WHERE record_id=?',
                                                     (record['record_id'],)).fetchone()[0]
            refs.append({'project_id': self.fixture.project_id, 'record_id': record['record_id'],
                         'content_digest': digest})
        value = {'probe': 'completed', 'reason_code': reason, 'records': refs, 'completion_digest': 'd'*64}
        row = self.owner._row(self.fixture.project_id, original.key)
        with self.ledger._account_transaction():
            self.owner._update(row, state='committed', references_json=canonical_json(value).decode())
        self.owner.release_committed(original)
        return original

    def completed_success(self, identity='saved'):
        records = self.store_probe_only(FAVOURABLE)
        self.commit_completed(identity, None, [self.identity, *records])

    # ---- the repeat: the smallest interface the design implies (I1, I2) ----------------------------
    def repeat(self, identity='saved', *, identity_changes=None):
        """reserve (again) -> the job's identity observation -> prepare -> admit. A refusal that
        raises is reported as {'probe': 'error', 'reason_code': ...}; a pass that would launch a new
        provider call as {'probe': 'would_launch'}."""
        try:
            out = self.reserve(identity)
        except ProbeAdmissionError as error:
            return {'probe': 'error', 'reason_code': error.reason_code, 'stage': 'reserve'}
        if type(out) is dict:
            return out
        try:
            self.observe_identity(**(identity_changes or {}))
            self.prepared(out)
            result = self.owner.admit(out)
        except ProbeAdmissionError as error:
            return {'probe': 'error', 'reason_code': error.reason_code, 'stage': 'admit'}
        return {'probe': 'would_launch', 'reason_code': None} if type(result) is ProbeAdmission else result

    def assertDenied(self, reason, answer):
        self.assertEqual(('denied', reason), (answer['probe'], answer.get('reason_code')), answer)

    YES = frozenset({'reused', 'completed', 'would_launch'})

    def append_any(self, info, **options):
        """Provider records stored from anywhere: inside the owner's open transaction (a callback) or outside it."""
        records = claude_rate_limit_records(info, account_fingerprint=self.identity['account_fingerprint'],
                                            observed_at_ms=self.clock, **options)
        if self.ledger._connection.in_transaction:
            for record in records:
                self.ledger._append_checked_locked(self.fixture.project_id, record, producer='provider',
                                                   timestamp=self.clock)
        else:
            self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)


class BR1FreshJudgmentOfEveryInput(RejudgeOwnerBase):
    """BR-1: every 'yes' is a fresh judgment of ALL current inputs; each input changed alone decides."""

    def after_saved_answer(self):
        answer = self.saved_reused()
        self.clock += 1000
        return answer

    def test_BR1_an_unchanged_repeat_is_judged_again_not_read_from_storage(self):
        saved = self.after_saved_answer()
        capture = self.spy_capture()
        again = self.repeat()
        self.assertEqual(('reused', True), (again['probe'], again.get('known')), again)
        self.assertEqual(saved['records'], again['records'])
        self.assertEqual(1, capture.call_count, 'a repeat of a yes must start a fresh judgment (signature work)')

    def test_BR1_known_reply_never_returns_a_stored_yes(self):
        self.after_saved_answer()
        got = self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                     before_commit=lambda: True)
        self.assertIsNone(got, 'a committed yes is judged again; it is never served from storage')

    def test_BR1_paid_or_overage_usage_after_the_saved_answer_refuses(self):
        self.after_saved_answer()
        self.store(PAID)
        self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR1_headroom_exhausted_after_the_saved_answer_refuses(self):
        self.after_saved_answer()
        self.store(EXHAUSTED)
        self.assertDenied('provider_exhausted', self.repeat())

    def test_BR1_a_subscription_that_is_gone_refuses(self):
        self.after_saved_answer()
        self.assertDenied('claude_subscription_missing', self.repeat(identity_changes={'subscriptionType': 'free'}))

    def test_BR1_a_different_account_without_attestation_refuses(self):
        self.after_saved_answer()
        answer = self.repeat(identity_changes={'email': 'other@example.invalid', 'orgId': 'org-other'})
        self.assertDenied('billing_attestation_missing', answer)

    def test_BR1_a_missing_attestation_refuses(self):
        self.after_saved_answer()
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute('DELETE FROM owner_attestations')
        self.assertDenied('billing_attestation_missing', self.repeat())

    def test_BR1_a_changed_policy_version_range_refuses(self):
        self.after_saved_answer()
        with mock.patch.object(self.views, '_claim_policy', return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None)):
            self.assertDenied('client_version_unreviewed', self.repeat())

    def test_BR1_a_changed_signer_never_answers_yes(self):
        self.after_saved_answer()
        with mock.patch.object(self.ledger, '_current_signer_bytes', return_value=b'x'*32):
            answer = self.repeat()
        self.assertNotIn(answer['probe'], {'reused', 'completed', 'would_launch'}, answer)

    def test_BR1_a_provider_api_key_after_the_saved_answer_refuses(self):
        self.after_saved_answer()
        with mock.patch.dict(os.environ, KEY):
            self.assertDenied('provider_api_key_present', self.repeat())

    def test_BR1_a_completed_runs_repeat_is_judged_again_too(self):
        self.completed_success()
        capture = self.spy_capture()
        again = self.repeat()
        self.assertEqual(('completed', True), (again['probe'], again.get('known')), again)
        self.assertEqual(1, capture.call_count)


class BR2LateAndEqualRestrictions(RejudgeOwnerBase):
    """BR-2: a restricting record counts when stored late, even with an earlier or equal time."""

    def test_BR2_paid_usage_stored_later_with_an_earlier_stamp_refuses_every_repeat(self):
        self.saved_reused()
        self.clock += 1000
        self.store(PAID, age=3500)  # observed before the cited records (clock-1000-3500 < clock-2000)
        for _ in range(2):  # a stated end: two repeats
            self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR2_paid_usage_stored_later_with_an_equal_stamp_refuses_every_repeat(self):
        self.saved_reused()
        cited = self.clock - 2000
        self.clock += 1000
        self.store(PAID, age=self.clock - cited)  # observed exactly when the cited records were
        for _ in range(2):
            self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR2_the_duplicate_request_path_never_answers_yes_after_a_late_restriction(self):
        self.saved_reused()
        self.clock += 1000
        self.store(PAID, age=3500)
        got = self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                     before_commit=lambda: True)
        self.assertTrue(got is None or got['probe'] != 'reused', got)
        self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR2_old_evidence_stored_late_is_not_made_fresh_by_its_storage_time(self):
        """Mutation-guard (restamping): a reading 10 minutes old, stored just now, is not reusable."""
        self.store(FAVOURABLE, age=2*PROBE_REUSE_MS)
        original = self.reserve('stored-late')
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertIs(type(answer), ProbeAdmission, 'a stale reading must not be reused: it needs a new probe')


class BR3ClockTrouble(RejudgeOwnerBase):
    """BR-3: clock trouble never makes old evidence fresh."""

    def saved_at(self, age):
        """A saved yes whose cited billing is `age` ms old at the moment of the repeat."""
        self.saved_reused()
        cited = self.clock - 2000
        self.clock = cited + age
        return cited

    def test_BR3_one_millisecond_inside_the_reuse_age_still_reuses(self):
        self.saved_at(PROBE_REUSE_MS-1)
        again = self.repeat()
        self.assertEqual(('reused', True), (again['probe'], again.get('known')), again)

    def test_BR3_exactly_the_reuse_age_needs_a_new_probe_so_the_repeat_is_refused_as_stale(self):
        self.saved_at(PROBE_REUSE_MS)
        before = self.effects()
        self.assertDenied('probe_replay_stale', self.repeat())
        self.assertEqual(before, self.effects())

    def test_BR3_expired_cited_records_refuse_with_a_named_reason_not_a_generic_failure(self):
        self.saved_reused()
        self.clock += 1000
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("UPDATE records SET expires_at_ms=? WHERE record_type='provider_billing'",
                                            (self.clock-1,))
        answer = self.repeat()
        self.assertDenied('probe_replay_stale', answer)

    def test_BR3_a_backward_clock_step_never_answers_yes(self):
        self.saved_reused()
        self.clock -= 5000  # before the cited readings
        before = self.effects()
        answer = self.repeat()
        # the cited readings are now in the future: they are not evidence, passing would need a new probe
        self.assertDenied('probe_replay_stale', answer)
        self.assertEqual(before, self.effects())

    def test_BR3_a_future_observation_is_never_reused_as_fresh_evidence(self):
        """Mutation-guard: a reading stamped after now is not evidence of now."""
        self.store(FAVOURABLE, age=-10_000)
        original = self.reserve('future')
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertIsNot(type(answer), dict, answer)  # not reused; a fresh probe (conservative) is admitted

    def test_BR3_a_future_observation_cited_by_a_saved_answer_does_not_stay_a_yes(self):
        self.saved_reused()
        self.clock += 1000
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("UPDATE records SET observed_at_ms=observed_at_ms+100000 "
                                            "WHERE record_type='provider_billing'")
        self.assertDenied('probe_replay_stale', self.repeat())


class BR4UnrelatedFavourableRecords(RejudgeOwnerBase):
    """BR-4: unrelated favourable records never cause a false refusal."""

    def test_BR4_a_favourable_run_record_after_the_saved_answer_changes_nothing(self):
        saved = self.saved_reused()
        self.clock += 1000
        self.store(FAVOURABLE, age=0, run_id='run-finished-1')
        again = self.repeat()
        self.assertEqual(('reused', True), (again['probe'], again.get('known')), again)
        self.assertEqual(saved['records'], again['records'])

    def test_BR4_favourable_probe_records_at_the_cited_time_and_later_change_nothing(self):
        self.saved_reused()
        cited = self.clock - 2000
        self.clock += 1000
        for stamp in (cited, self.clock):  # equal to, and after, the cited readings; a stated end: two
            self.store({**FAVOURABLE, 'utilization': .3}, age=self.clock - stamp)
        again = self.repeat()
        self.assertEqual(('reused', True), (again['probe'], again.get('known')), again)


class BR5OnceAndOnlyOnce(RejudgeOwnerBase):
    """BR-5, owner level: the owner never spends a bootstrap allowance or a probe for a repeat."""

    def test_BR5_a_repeat_after_a_bootstrap_probe_is_admitted_never_spends_another_admission(self):
        original = self.reserve('bootstrap')
        self.prepared(original)
        self.assertIs(type(self.owner.admit(original)), ProbeAdmission)
        self.assertEqual(1, self.passes())
        for _ in range(2):  # a stated end: two repeats, one of them after a restart of the owner's memory
            again = self.reserve('bootstrap')
            self.assertIs(again, original)
        self.assertEqual(1, self.passes())
        self.assertEqual(['admitted'], [r['state'] for r in self.rows()])

    def test_BR5_a_repeat_admit_and_a_restart_after_a_bootstrap_probe_never_spend_the_allowance_again(self):
        """Meaningful under a mutation that spends the allowance a second time: the repeat goes through
        reserve AND admit, and the owner's memory is then lost (a restart); a stated end: two repeats."""
        original = self.reserve('bootstrap')
        self.prepared(original)
        permit = self.owner.admit(original)
        self.assertIs(type(permit), ProbeAdmission)
        before = self.effects()
        self.assertEqual(1, before['pass_admissions'])
        for _ in range(2):
            again = self.reserve('bootstrap')
            self.assertIs(again, original)
            self.assertIs(self.owner.admit(again), permit)
            self.assertEqual(before, self.effects())
        self.owner._pending.clear()  # the restart: no original can be recreated
        with self.assertRaises(ProbeAdmissionError):
            self.reserve('bootstrap')
        self.assertEqual(before, self.effects())
        self.assertEqual(['admitted'], [r['state'] for r in self.rows()])

    def test_BR5_a_stale_repeat_spends_no_admission_and_opens_no_hold(self):
        self.saved_reused()
        self.clock += PROBE_REUSE_MS + 10_000
        before = self.effects()
        for _ in range(2):
            self.repeat()
        self.assertEqual(before, self.effects())


class BR6RefusalsAreSticky(RejudgeOwnerBase):
    """BR-6: a refusal stays a refusal, even after favourable evidence."""

    def test_BR6_a_denial_stays_a_denial_after_the_restriction_is_gone_and_favourable_evidence_arrives(self):
        self.saved_denied(PAID)
        self.clock += 1000
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute(
                "DELETE FROM records WHERE record_type IN ('provider_billing','provider_headroom')")
        self.store(FAVOURABLE)
        again = self.repeat()
        self.assertDenied('paid_usage_usable', again)
        self.assertTrue(again['known'], again)

    def test_BR6_a_completed_run_with_a_failure_reason_stays_refused_after_favourable_evidence(self):
        self.commit_completed('saved', 'probe_incomplete')
        self.clock += 1000
        self.store(FAVOURABLE)
        again = self.repeat()
        self.assertEqual(('completed', 'probe_incomplete', True),
                         (again['probe'], again.get('reason_code'), again.get('known')), again)


class BR7AuthorizationBeforeWorkAndAfterTheFinalCallback(RejudgeOwnerBase):
    """BR-7: binding and authorization first; whatever the final callback changes is re-checked."""

    def test_BR7_a_withdrawn_authorization_refuses_a_repeat_with_no_signature_work(self):
        self.saved_reused()
        capture = self.spy_capture()
        self.allowed = False
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.reserve('saved')
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                   before_commit=lambda: self.allowed)
        self.assertEqual(0, capture.call_count)

    def test_BR7_a_changed_project_root_refuses_a_repeat_with_no_signature_work(self):
        from codex_agents.account_observation import ObservationContext
        self.saved_reused()
        capture = self.spy_capture()
        other = ObservationContext(str(Path(self.fixture.project).resolve().parent))
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.owner.reserve(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', other, before_commit=lambda: True)
        self.assertEqual(0, capture.call_count)

    def final_callback_changes(self, change):
        """A repeat whose caller authorization (the final callback) changes the records, once the
        judgment has been made, from inside the owner's transaction."""
        self.saved_reused()
        self.clock += 1000
        real, judged, fired = account_billing_admission.evaluate_admission, [], []

        def spy(**kwargs):  # call-through: the real evaluator is used
            result = real(**kwargs)
            judged.append(result)
            return result

        def callback():
            if judged and not fired:
                fired.append(True)
                change()
            return True

        self.enterContext(mock.patch.object(account_billing_admission, 'evaluate_admission', side_effect=spy))
        out = self.reserve('saved', before_commit=callback)
        if type(out) is dict:
            return {**out, 'fired': False}
        self.observe_identity()
        self.prepared(out)
        try:
            result = self.owner.admit(out)
        except ProbeAdmissionError as error:
            return {'probe': 'error', 'reason_code': error.reason_code, 'fired': bool(fired)}
        return {'probe': 'would_launch' if type(result) is ProbeAdmission else result['probe'],
                'reason_code': None if type(result) is ProbeAdmission else result.get('reason_code'),
                'fired': bool(fired)}

    def append(self, info):
        records = claude_rate_limit_records(info, account_fingerprint=self.identity['account_fingerprint'],
                                            observed_at_ms=self.clock)
        for record in records:  # in the owner's open transaction: a locked helper, no connection context
            self.ledger._append_checked_locked(self.fixture.project_id, record, producer='provider',
                                               timestamp=self.clock)

    def test_BR7_paid_usage_added_in_the_final_callback_is_never_a_yes(self):
        answer = self.final_callback_changes(lambda: self.append(PAID))
        self.assertTrue(answer['fired'], 'the final callback never ran after the judgment')
        self.assertNotIn(answer['probe'], {'reused', 'completed', 'would_launch'}, answer)

    def test_BR7_a_policy_changed_in_the_final_callback_is_never_a_yes(self):
        patch = mock.patch.object(self.views, '_claim_policy', return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None))
        answer = self.final_callback_changes(patch.start)
        self.addCleanup(patch.stop)
        self.assertTrue(answer['fired'])
        self.assertNotIn(answer['probe'], {'reused', 'completed', 'would_launch'}, answer)

    def test_BR7_an_attestation_removed_in_the_final_callback_is_never_a_yes(self):
        answer = self.final_callback_changes(
            lambda: self.ledger._connection.execute('DELETE FROM owner_attestations'))
        self.assertTrue(answer['fired'])
        self.assertNotIn(answer['probe'], {'reused', 'completed', 'would_launch'}, answer)


class BR8PublishChecksTheCommittedAnswer(RejudgeOwnerBase):
    """BR-8, owner level: integrity only; a waiver never allows paid usage."""

    def test_BR8_a_tampered_committed_answer_is_refused_never_served(self):
        self.saved_reused()
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("UPDATE account_billing_probe_requests SET references_json=?",
                (canonical_json({'probe': 'denied', 'reason_code': 'probe_cooldown', 'records': []}).decode(),))
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                   before_commit=lambda: True)

    def test_BR8_a_waiver_never_allows_paid_usage(self):
        records = self.store(PAID)
        billing = next(r for r in records if r['record_type'] == 'provider_billing')
        self.assertIs(True, billing['paid_usage_usable'])
        self.assertIs(False, self.ledger.billing_waived_by_attestation(self.fixture.project_id, billing))
        original = self.reserve('waiver')
        self.prepared(original)
        self.assertDenied('paid_usage_usable', self.owner.admit(original))


class BR9CompletedHistoryVersusReadyNow(RejudgeOwnerBase):
    """BR-9: 'this run completed' (history) and 'ready now' (current) stay separate."""

    def test_BR9_a_restriction_after_a_completed_run_refuses_readiness_and_keeps_the_history(self):
        self.completed_success()
        history = self.rows()[0]['references_json']
        self.clock += 1000
        self.store(PAID)
        self.assertDenied('paid_usage_usable', self.repeat())
        self.assertEqual(history, self.rows()[0]['references_json'], 'the completed run history was overwritten')
        self.assertEqual('completed', json.loads(history)['probe'])


class BR10ApiKeyAtEveryStage(RejudgeOwnerBase):
    """BR-10: a provider API key is refused with provider_api_key_present at every stage, before signature work."""

    def reason(self, call):
        try:
            answer = call()
        except ProbeAdmissionError as error:
            return error.reason_code
        return answer.get('reason_code') if type(answer) is dict and answer.get('probe') == 'denied' else repr(answer)[:60]

    def test_BR10_reserve_of_a_new_request_with_a_key_is_refused_with_its_own_reason(self):
        capture = self.spy_capture()
        with mock.patch.dict(os.environ, KEY):
            self.assertEqual('provider_api_key_present', self.reason(lambda: self.reserve('key-new')))
        self.assertEqual(0, capture.call_count)

    def test_BR10_reserve_of_a_repeat_with_a_key_is_refused_with_its_own_reason(self):
        self.saved_reused()
        capture = self.spy_capture()
        with mock.patch.dict(os.environ, KEY):
            self.assertEqual('provider_api_key_present', self.reason(lambda: self.reserve('saved')))
        self.assertEqual(0, capture.call_count)

    def test_BR10_known_reply_with_a_key_is_refused_with_its_own_reason(self):
        self.saved_reused()
        with mock.patch.dict(os.environ, KEY):
            self.assertEqual('provider_api_key_present', self.reason(lambda: self.owner.known_reply(
                self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context, before_commit=lambda: True)))

    def test_BR10_prepare_with_a_key_is_refused_before_any_signature_work(self):
        original = self.reserve('key-prepare')
        capture = self.spy_capture()
        with mock.patch.dict(os.environ, KEY):
            self.assertEqual('provider_api_key_present', self.reason(lambda: self.owner.prepare(original)))
        self.assertEqual(0, capture.call_count)

    def test_BR10_admit_with_a_key_is_refused_with_its_own_reason_and_recorded_as_a_denial(self):
        original = self.reserve('key-admit')
        self.prepared(original)
        with mock.patch.dict(os.environ, KEY):
            self.assertEqual('provider_api_key_present', self.reason(lambda: self.owner.admit(original)))
        stored = [json.loads(bytes(r[0])) for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission'")]
        self.assertEqual([('deny', 'provider_api_key_present')], [(r['decision'], r['reason_code']) for r in stored])

    def test_BR10_a_refusal_for_a_key_never_blocks_a_later_fresh_request(self):
        self.saved_reused()
        with mock.patch.dict(os.environ, KEY):
            self.reason(lambda: self.reserve('saved'))
        later = self.repeat('a-new-request')
        self.assertNotEqual('provider_api_key_present', later.get('reason_code'), later)


class BR11JudgedInputsAndTheReplyContract(RejudgeOwnerBase):
    """BR-11: the full judged set is kept internally; the reply's short list follows its contract."""

    def test_BR11_more_than_three_judged_inputs_still_give_a_contract_reply(self):
        """Mutation-guard: five judged provider records, a reply of at most three that passes its contract."""
        for age in (4000, 3000, 2000):  # three headroom + billing readings; a stated end
            self.store({**FAVOURABLE, 'utilization': .1 + age/100_000}, age=age)
        original = self.reserve('many')
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertEqual('reused', answer['probe'], answer)
        self.assertLessEqual(len(answer['records']), 3)
        checked_billing_probe_reply(answer)

    def test_BR11_headroom_stored_out_of_time_order_still_gives_a_contract_reply_citing_real_records(self):
        self.store({**FAVOURABLE, 'utilization': .2}, age=1000)
        self.store({**FAVOURABLE, 'utilization': .1}, age=3000)  # stored later, observed earlier
        original = self.reserve('reordered')
        self.prepared(original)
        answer = self.owner.admit(original)
        self.assertEqual('reused', answer['probe'], answer)
        checked_billing_probe_reply(answer)
        newest = max(r['observed_at_ms'] for r in answer['records'] if r['record_type'] == 'provider_headroom')
        self.assertEqual(self.clock-1000, newest, 'the cited headroom is the newest observation, not the last stored')

    def test_BR11_a_deleted_cited_record_refuses_a_repeat_with_a_named_reason_not_a_generic_failure(self):
        self.saved_reused()
        self.clock += 1000
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("DELETE FROM records WHERE record_type='provider_billing'")
        self.assertDenied('probe_replay_stale', self.repeat())


class BR12FailuresFailClosed(RejudgeOwnerBase):
    """BR-12: bounded failures; no stale yes escapes a capture, signature, lock or budget failure."""

    def faulted_repeat(self, patcher):
        self.saved_reused()
        self.clock += 1000
        before = self.effects()
        row = self.rows()[0]
        with patcher:
            try:
                answer = self.repeat()
            except Exception as error:  # a fault may surface as any error: still no yes
                answer = {'probe': 'raised', 'reason_code': type(error).__name__}
        self.assertNotIn(answer['probe'], {'reused', 'completed', 'would_launch'}, answer)
        self.assertEqual(before, self.effects())
        self.assertEqual(row, self.rows()[0], 'a failed repeat changed the saved row')
        self.assertLessEqual(len(self.owner._pending), 1, 'a failed repeat left unbounded memory')
        return answer

    def recovers(self):
        again = self.repeat()
        self.assertEqual(('reused', True), (again['probe'], again.get('known')), again)

    def test_BR12_a_capture_failure_is_no_yes_and_the_next_repeat_recovers(self):
        self.faulted_repeat(mock.patch.object(self.intake, 'capture_admission', side_effect=RuntimeError('capture')))
        self.recovers()

    def test_BR12_a_signature_failure_is_no_yes_and_the_next_repeat_recovers(self):
        self.faulted_repeat(mock.patch.object(self.pool, 'result', side_effect=RuntimeError('signature')))
        self.recovers()

    def test_BR12_a_busy_ledger_lock_is_no_yes_and_the_next_repeat_recovers(self):
        self.faulted_repeat(mock.patch.object(self.ledger, '_account_transaction', side_effect=SQLReadError('sql_read_busy')))
        self.recovers()

    def test_BR12_an_exhausted_read_budget_is_no_yes(self):
        self.faulted_repeat(mock.patch.object(self.owner, '_limits', SQLReadLimits(1, 1, 1)))

    def test_BR12_a_writer_racing_the_judgment_never_leaves_a_stale_yes(self):
        """A paid-usage writer starts as the judgment does and finishes (it waits for the ledger lock); a
        stated end: the writer is joined within 10 s. After both, every repeat refuses."""
        self.saved_reused()
        self.clock += 1000
        real, written = account_billing_admission.evaluate_admission, []

        def writer():
            records = claude_rate_limit_records(PAID, account_fingerprint=self.identity['account_fingerprint'],
                                                observed_at_ms=self.clock)
            self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
            written.append(True)

        thread = threading.Thread(target=writer)

        started = []

        def spy(**kwargs):
            if not started:
                started.append(True)
                thread.start()
            return real(**kwargs)

        with mock.patch.object(account_billing_admission, 'evaluate_admission', side_effect=spy):
            self.repeat()
        if started:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        else:  # no judgment ran (the stored answer was served): the writer simply runs after it
            writer()
        self.assertEqual([True], written)
        self.assertDenied('paid_usage_usable', self.repeat())


class BR13StaleRepeat(RejudgeOwnerBase):
    """BR-13: a repeat that would need a new provider call is refused with its own reason, changing nothing."""

    def test_BR13_probe_replay_stale_is_a_valid_denied_reply_reason(self):
        value = {'probe': 'denied', 'reason_code': 'probe_replay_stale', 'records': [], 'known': True,
                 'observed_at_ms': 10}
        self.assertEqual(value, checked_billing_probe_reply(value))

    def test_BR13_a_stale_repeat_is_refused_by_name_with_zero_effects_and_a_new_request_id_proceeds(self):
        self.saved_reused()
        self.clock += PROBE_REUSE_MS + 10_000
        saved_row = self.rows()[0]
        before = self.effects()
        self.assertDenied('probe_replay_stale', self.repeat())
        self.assertEqual(before, self.effects())
        self.assertEqual(saved_row, self.rows()[0], 'the saved request row changed')
        fresh = self.repeat('a-new-request-id')  # a new attempt uses a new request id and may probe
        self.assertEqual('would_launch', fresh['probe'], fresh)
        self.assertEqual(before['pass_admissions']+1, self.passes())


class BR14PaidUsageIsAPolicyInput(RejudgeOwnerBase):
    """BR-14: whether paid usage is allowed is a policy input judged fresh on every request.

    The opt-in below is a TEST-ONLY fixture (a decorator in front of the real evaluator, I5). Nothing
    like it exists in product config, and the shipping policy refuses paid usage."""

    class OptIn:
        def __init__(self):
            self.opted_in, self.limit, self.spent = False, 100, 0

        def allows(self):
            return self.opted_in and self.spent < self.limit

    def install_optin(self):
        policy, real = self.OptIn(), account_billing_admission.evaluate_admission

        def judged(**kwargs):
            decision = real(**kwargs)
            if decision['reason_code'] == 'paid_usage_usable' and policy.allows():
                without_paid = [(r, d) for r, d in kwargs['billing'] if r.get('paid_usage_usable') is not True]
                decision = real(**{**kwargs, 'billing': without_paid})
            return decision

        self.enterContext(mock.patch.object(account_billing_admission, 'evaluate_admission', side_effect=judged))
        return policy

    def test_BR14_the_shipping_policy_refuses_paid_usage_fresh_and_on_every_repeat(self):
        self.store_probe_only(PAID)
        original = self.reserve('shipping')
        self.prepared(original)
        self.assertDenied('paid_usage_usable', self.owner.admit(original))
        self.assertDenied('paid_usage_usable', self.repeat('shipping'))

    def test_BR14_the_shipping_policy_refuses_paid_usage_that_appears_after_a_saved_yes(self):
        self.saved_reused()
        self.clock += 1000
        self.store(PAID)
        self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR14_the_shipping_config_has_no_paid_usage_opt_in(self):
        from tests.support import ORCHESTRATION_ROOT
        text = '\n'.join(p.read_text(errors='ignore') for p in (ORCHESTRATION_ROOT / 'config').glob('*.json'))
        for name in ('paid_usage_opt_in', 'paid_usage_allowed', 'spending_limit', 'opt_in'):
            self.assertNotIn(name, text)

    def test_BR14_test_only_optin_allows_paid_usage_only_while_opted_in_and_under_the_limit(self):
        policy = self.install_optin()
        self.store_probe_only(PAID)
        steps = (('not opted in', False, 0, 'denied'), ('opted in', True, 0, 'reused'),
                 ('limit reached', True, 100, 'denied'))  # a stated end: three steps
        for number, (name, opted_in, spent, expected) in enumerate(steps):
            with self.subTest(name):
                policy.opted_in, policy.spent = opted_in, spent
                original = self.reserve(f'optin-{number}')
                self.prepared(original)
                self.assertEqual(expected, self.owner.admit(original)['probe'])
                # (a denial leaves a cooldown-free state: only 'reused' or 'denied' are possible here)

    def test_BR14_withdrawing_the_optin_after_a_saved_answer_refuses_every_repeat(self):
        policy = self.install_optin()
        policy.opted_in = True
        self.store_probe_only(PAID)
        original = self.reserve('saved')
        self.prepared(original)
        self.assertEqual('reused', self.owner.admit(original)['probe'])
        self.clock += 1000
        policy.opted_in = False
        self.assertDenied('paid_usage_usable', self.repeat())

    def test_BR14_reaching_the_limit_after_a_saved_answer_refuses_every_repeat(self):
        policy = self.install_optin()
        policy.opted_in = True
        self.store_probe_only(PAID)
        original = self.reserve('saved')
        self.prepared(original)
        self.assertEqual('reused', self.owner.admit(original)['probe'])
        self.clock += 1000
        policy.spent = policy.limit
        self.assertDenied('paid_usage_usable', self.repeat())


# ------------------------------------------------------------------------------------------------------
@PLATFORM
class RejudgeServiceTests(unittest.TestCase):
    """The same rules through the real service, jobs, workers and a fake provider client."""
    setUp = _service_tests.AccountBillingJobTests.setUp
    stop = _service_tests.AccountBillingJobTests.stop
    request = _service_tests.AccountBillingJobTests.request
    begin = _service_tests.AccountBillingJobTests.begin
    until = _service_tests.AccountBillingJobTests.until
    take = _service_tests.AccountBillingJobTests.take
    launches = _service_tests.AccountBillingJobTests.launches

    def fingerprint(self):
        row = self.ledger._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='provider_identity' "
            "ORDER BY rowid DESC LIMIT 1", (self.fixture.project_id,)).fetchone()
        return json.loads(bytes(row[0]))['account_fingerprint']

    def store(self, info, *, age=0):
        observed = time.time_ns() // 1_000_000 - age
        records = claude_rate_limit_records(info, account_fingerprint=self.fingerprint(), observed_at_ms=observed)
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=observed)
        return records

    def passes(self):
        return sum(json.loads(bytes(r[0]))['decision'] == 'pass' for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission'"))

    def answers(self):
        return [json.loads(r[0]) for r in self.ledger._connection.execute(
            "SELECT references_json FROM account_billing_probe_requests WHERE state='committed'")]

    def test_BR5_the_provider_is_called_once_across_repeats_a_lost_reply_and_concurrent_retries(self):
        ticket = self.begin()
        self.assertIs(ticket, self.begin())  # concurrent retries while in flight share one job
        self.assertIs(ticket, self.begin())
        first = self.take(ticket)
        self.assertEqual('completed', first['probe'])
        self.assertEqual(1, len(self.launches()))
        for _ in range(3):  # a stated end: three repeats after a lost reply
            again = self.take(self.begin())
            self.assertTrue(again['known'], again)
            self.assertIn(again['probe'], {'completed', 'reused'}, again)
        self.assertEqual(1, len(self.launches()))
        self.assertEqual(1, self.passes())
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM account_billing_probe_requests WHERE state='admitted'").fetchone()[0])

    def test_BR9_and_BR1_a_completed_run_then_paid_usage_keeps_the_history_and_refuses_readiness(self):
        self.assertEqual('completed', self.take(self.begin())['probe'])
        self.assertEqual(1, len(self.launches()))
        history = self.answers()
        self.store(PAID)
        again = self.take(self.begin())
        self.assertEqual(('denied', 'paid_usage_usable'), (again['probe'], again['reason_code']), again)
        self.assertEqual(1, len(self.launches()), 'a repeat launched the provider again')
        self.assertIn('completed', [a['probe'] for a in self.answers()], 'the completed history is gone')
        self.assertEqual(history[0], [a for a in self.answers() if a['probe'] == 'completed'][0])

    def test_BR4_and_BR8_unrelated_favourable_evidence_between_commit_and_publish_is_no_false_conflict(self):
        self.store(FAVOURABLE, age=2000)
        real, added = self.billing.take, []

        def take(ticket, **options):
            reply = real(ticket, **options)
            if reply is not None and not added:  # the job is committed and ready; publication is next
                added.append(self.store({**FAVOURABLE, 'utilization': .3}, age=0))
            return reply

        self.enterContext(mock.patch.object(self.billing, 'take', side_effect=take))
        answer = self.take(self.begin())
        self.assertTrue(added, 'the hook never ran between commit and publish')
        self.assertEqual(('reused', False), (answer['probe'], answer['known']), answer)
        self.assertEqual([], self.launches())


# ------------------------------------------------------------------------------------------------------
# Owner decision 47: ONE more round, tests first (review billing-reuse-r4: H1, H3, identity, M1-M3, M4).

class H3NoUncheckedCallbackAfterTheFinalComparison(RejudgeOwnerBase):
    """BR-7/BR-8 (H3): whatever the LAST authorization callback of a judgment changes is checked after it,
    never answered over. The change is made by the last callback the owner makes, found by a dry run."""

    def admit_repeat(self, change, fire_at):
        """A repeat's admit; the `fire_at`-th authorization callback made inside it runs `change`."""
        state = {'inside': False, 'n': 0, 'fired': False}

        def callback():
            if state['inside']:
                state['n'] += 1
                if state['n'] == fire_at:
                    state['fired'] = True
                    change()
            return True

        out = self.reserve('saved', before_commit=callback)
        self.observe_identity()
        self.prepared(out)
        state['inside'] = True
        try:
            result = self.owner.admit(out)
        except ProbeAdmissionError as error:
            result = {'probe': 'error', 'reason_code': error.reason_code}
        finally:
            state['inside'] = False
        return result, state

    def last_callback_changes(self, change):
        self.saved_reused()
        self.clock += 1000
        dry, counted = self.admit_repeat(lambda: None, None)
        self.assertEqual('reused', dry['probe'], dry)
        self.assertGreaterEqual(counted['n'], 1, 'admit made no authorization callback')
        result, state = self.admit_repeat(change, counted['n'])
        self.assertTrue(state['fired'], 'the last callback never ran')
        return result

    def test_H3_paid_usage_added_by_the_last_callback_of_admit_is_never_a_yes(self):
        answer = self.last_callback_changes(lambda: self.append_any(PAID))
        self.assertNotIn(answer['probe'], self.YES, answer)

    def test_H3_a_policy_changed_by_the_last_callback_of_admit_is_never_a_yes(self):
        patch = mock.patch.object(self.views, '_claim_policy', return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None))
        self.addCleanup(patch.stop)
        answer = self.last_callback_changes(patch.start)
        self.assertNotIn(answer['probe'], self.YES, answer)

    def test_H3_an_attestation_removed_by_the_last_callback_of_admit_is_never_a_yes(self):
        answer = self.last_callback_changes(lambda: self.ledger._connection.execute('DELETE FROM owner_attestations'))
        self.assertNotIn(answer['probe'], self.YES, answer)

    def verify_with_last_callback(self, change):
        """verify_committed (the publication check) of a repeat's yes; the last callback it makes runs `change`."""
        self.saved_reused()
        self.clock += 1000
        out = self.reserve('saved')
        self.observe_identity()
        self.prepared(out)
        reply = self.owner.admit(out)
        self.assertEqual('reused', reply['probe'], reply)
        state = {'n': 0, 'fire_at': None, 'fired': False}

        def callback():
            state['n'] += 1
            if state['n'] == state['fire_at']:
                state['fired'] = True
                change()
            return True

        def verify():
            state['n'] = 0
            return self.owner.verify_committed(self.fixture.project_id, 'a'*64, 'b'*64, 'saved', self.context,
                                               before_commit=callback, reply=reply, history=out.history)

        self.assertIs(True, verify())
        state['fire_at'] = state['n']
        try:
            verify()
        except ProbeAdmissionError as error:
            self.assertTrue(state['fired'])
            return error.reason_code
        self.assertTrue(state['fired'], 'the last callback never ran')
        return True

    def test_H3_paid_usage_added_by_the_last_callback_of_the_publication_check_is_refused(self):
        self.assertEqual('probe_conflict', self.verify_with_last_callback(lambda: self.append_any(PAID)))

    def test_H3_an_attestation_removed_by_the_last_callback_of_the_publication_check_is_refused(self):
        self.assertEqual('probe_conflict', self.verify_with_last_callback(
            lambda: self.ledger._connection.execute('DELETE FROM owner_attestations')))


class IdentityAgeOnARepeat(RejudgeOwnerBase):
    """M-identity (BR-1): the job takes an identity observation for every generation, a repeat's too (the
    local client's `auth status --json` and `--version`: no model call, nothing spent; proved end to end in
    RejudgeDeliveryTests). The owner measures a reading's age against the judgment's time, not the
    reservation's: a reading that was inside the reuse age when the repeat began, but is not when it is
    judged, is no evidence of the login at that time."""

    def test_identity_age_is_measured_at_judgment_time_not_at_reservation_time(self):
        self.saved_reused()
        self.clock += PROBE_REUSE_MS - 10_000
        self.store(FAVOURABLE)  # fresh billing; the identity is the old one, 10 s inside the reuse age
        out = self.reserve('saved')
        self.clock += 20_000  # judged later than reserved: the reading is now 10 s past the reuse age
        self.prepared(out)
        before = self.effects()
        self.assertDenied('probe_replay_stale', self.owner.admit(out))
        self.assertEqual(before, self.effects())


class BR2LateRunRecords(RejudgeOwnerBase):
    """M4: BR-2's late restrictions as run records (the insertion-order branch only handles records with a
    run_id); added next to the originals, which stay as they are."""

    def test_BR2_a_run_record_with_paid_usage_stored_late_with_an_earlier_stamp_refuses_every_repeat(self):
        self.saved_reused()
        self.clock += 1000
        self.store(PAID, age=3500, run_id='run-late-1')
        for _ in range(2):
            self.assertNotIn(self.repeat()['probe'], self.YES)

    def test_BR2_a_run_record_with_exhausted_headroom_stored_late_with_an_earlier_stamp_refuses_every_repeat(self):
        self.saved_reused()
        self.clock += 1000
        self.store(EXHAUSTED, age=3500, run_id='run-late-2')
        for _ in range(2):
            self.assertNotIn(self.repeat()['probe'], self.YES)


# ------------------------------------------------------------------------------------------------------
class _Hook:
    """Stands in for the caller's `allowed` flag: every authorization callback reads it, so it sees them."""

    def __init__(self):
        self.armed, self.calls, self.fire_at, self.action, self.fired = False, 0, None, None, False

    def __bool__(self):
        if self.armed:
            self.calls += 1
            if self.calls == self.fire_at:
                self.fired = True
                self.action()
        return True


@PLATFORM
class RejudgeDeliveryTests(unittest.TestCase):
    """Decision 47: delivery through the real service, jobs, workers and fake client."""
    setUp = _service_tests.AccountBillingJobTests.setUp
    stop = _service_tests.AccountBillingJobTests.stop
    request = _service_tests.AccountBillingJobTests.request
    begin = _service_tests.AccountBillingJobTests.begin
    until = _service_tests.AccountBillingJobTests.until
    take = _service_tests.AccountBillingJobTests.take
    launches = _service_tests.AccountBillingJobTests.launches
    fingerprint = RejudgeServiceTests.fingerprint
    store = RejudgeServiceTests.store
    YES = frozenset({'reused', 'completed'})

    def outcome(self, ticket, request=None):
        try:
            return self.take(ticket, request)
        except AccountError as error:
            return {'probe': 'error', 'reason_code': str(error)}

    def ready(self, ticket):
        self.until(lambda: self.billing._jobs[ticket].state == 'ready')

    def ready_reused(self, identity='probe-first'):
        self.store(FAVOURABLE, age=2000)
        request = self.request(identity)
        ticket = self.begin(request)
        self.ready(ticket)
        self.assertEqual('reused', self.billing._jobs[ticket].result['probe'])
        return ticket, request

    def assertRefusedNotYes(self, answer, reason=None):
        self.assertNotIn(answer['probe'], self.YES, answer)
        if reason is not None:
            self.assertEqual(('denied', reason), (answer['probe'], answer.get('reason_code')), answer)

    # ---- H1: a ready job is judged again before its yes is delivered ----------------------------------
    def test_H1_a_ready_yes_is_judged_again_when_paid_usage_arrives_before_the_first_take(self):
        ticket, request = self.ready_reused()
        self.store(PAID)
        self.assertIs(ticket, self.begin(request))  # a duplicate submit returns the same ticket
        self.assertRefusedNotYes(self.outcome(ticket, request), 'paid_usage_usable')

    def test_H1_a_ready_completed_run_is_judged_again_when_paid_usage_arrives_before_the_first_take(self):
        request = self.request()
        ticket = self.begin(request)
        self.ready(ticket)
        self.assertEqual('completed', self.billing._jobs[ticket].result['probe'])
        self.assertEqual(1, len(self.launches()))
        self.store(PAID)
        self.assertRefusedNotYes(self.outcome(ticket, request), 'paid_usage_usable')
        self.assertEqual(1, len(self.launches()), 'judging again launched the provider again')

    def test_H1_a_ready_yes_is_judged_again_when_the_attestation_is_removed_before_the_first_take(self):
        ticket, request = self.ready_reused()
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute('DELETE FROM owner_attestations')
        self.assertRefusedNotYes(self.outcome(ticket, request), 'billing_attestation_missing')

    def test_H1_guard_a_ready_yes_whose_inputs_did_not_change_is_delivered_without_a_second_judgment(self):
        ticket, request = self.ready_reused()
        capture = self.enterContext(mock.patch.object(self.claims._intake, 'capture_admission',
                                                      wraps=self.claims._intake.capture_admission))
        answer = self.take(ticket, request)
        self.assertEqual(('reused', False), (answer['probe'], answer['known']), answer)
        self.assertEqual(0, capture.call_count)

    def test_H1_a_yes_whose_inputs_keep_changing_ends_in_a_named_refusal_not_a_loop(self):
        ticket, request = self.ready_reused()
        real, stored = self.billing.take, []

        def take(t, **options):
            if self.billing._jobs[t].state == 'ready':  # evidence arrives before every delivery
                stored.append(self.store({**FAVOURABLE, 'utilization': .3 + len(stored)/100}, age=0))
            return real(t, **options)

        with mock.patch.object(self.billing, 'take', side_effect=take):
            answer = self.outcome(ticket, request)
        self.assertEqual({'probe': 'error', 'reason_code': 'probe_replay_stale'}, answer)
        self.assertEqual(3, len(stored), 'two new judgments, then the named refusal')
        self.assertEqual([], self.launches())

    # ---- M-identity through the service --------------------------------------------------------------
    def test_identity_a_repeat_observes_the_identity_again_and_sees_a_subscription_that_is_gone(self):
        self.store(FAVOURABLE, age=2000)
        self.assertEqual('reused', self.take(self.begin())['probe'])
        status = self.fixture.tools / 'claude-status.json'
        status.write_text(json.dumps({**json.loads(status.read_text()), 'subscriptionType': 'free'}))
        observed = self.enterContext(mock.patch.object(self.observations, 'submit', wraps=self.observations.submit))
        answer = self.outcome(self.begin())
        self.assertEqual(1, observed.call_count, 'a repeat took no identity observation of its own')
        self.assertRefusedNotYes(answer, 'claude_subscription_missing')

    # ---- M1: publication is bound to the committed row and the reply of its judgment generation ------
    def after_take(self, action):
        real, done = self.billing.take, []

        def take(ticket, **options):
            reply = real(ticket, **options)
            if reply is not None and not done:
                done.append(True)
                action(reply)
            return reply

        self.enterContext(mock.patch.object(self.billing, 'take', side_effect=take))
        return done

    def test_M1_publication_refuses_a_different_validly_sealed_row_that_yields_the_same_reply(self):
        self.store(FAVOURABLE, age=2000)
        owner = self.billing._owner

        def reseal(reply):
            row = dict(self.ledger._connection.execute('SELECT * FROM account_billing_probe_requests').fetchone())
            with self.ledger._account_transaction():
                owner._update(row, deadline_at_ms=row['deadline_at_ms']+1)  # not part of the reply

        done = self.after_take(reseal)
        answer = self.outcome(self.begin())
        self.assertTrue(done)
        self.assertNotIn(answer['probe'], self.YES, answer)

    def test_M1_publication_refuses_a_repeat_reply_that_differs_from_the_one_its_judgment_built(self):
        self.store(FAVOURABLE, age=2000)
        self.assertEqual('reused', self.take(self.begin())['probe'])

        def alter(reply):
            reply.result['observed_at_ms'] += 1  # the history row is unchanged; the reply is not what was judged

        done = self.after_take(alter)
        answer = self.outcome(self.begin())
        self.assertTrue(done)
        self.assertNotIn(answer['probe'], self.YES, answer)

    # ---- H3 / M2 at publication: nothing unchecked after the last callback ----------------------------
    def publish_with_last_callback(self, identity, action=None, fire_at=None):
        hook = self.allowed
        hook.calls, hook.fired, hook.fire_at, hook.action = 0, False, fire_at, action
        ticket, request = self.ready_reused(identity)
        real = self.billing.take

        def take(t, **options):
            reply = real(t, **options)
            if reply is not None:
                hook.armed = True  # publication starts here
            return reply

        with mock.patch.object(self.billing, 'take', side_effect=take):
            answer = self.outcome(ticket, request)
        hook.armed = False
        return answer, hook

    def last_callback_changes(self, action):
        self.allowed = _Hook()
        dry, hook = self.publish_with_last_callback('probe-dry')
        self.assertEqual('reused', dry['probe'], dry)
        self.assertGreaterEqual(hook.calls, 1)
        answer, hook = self.publish_with_last_callback('probe-real', action, hook.calls)
        self.assertTrue(hook.fired, 'the last callback never ran')
        return answer

    def test_H3_paid_usage_added_by_the_last_publication_callback_is_never_published_as_a_yes(self):
        self.assertNotIn(self.last_callback_changes(lambda: self.store(PAID))['probe'], self.YES)

    def test_M2_a_waiver_revoked_by_the_last_publication_callback_is_never_published(self):
        answer = self.last_callback_changes(lambda: self.ledger._connection.execute('DELETE FROM owner_attestations'))
        self.assertNotIn(answer['probe'], self.YES, answer)


if __name__ == '__main__':
    unittest.main()
