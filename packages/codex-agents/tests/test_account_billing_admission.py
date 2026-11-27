"""Real registered signature returns; model launch/native enrollment remain gated."""
import json
import secrets
from copy import copy
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_providers as fixtures

from codex_agents.account_billing_admission import AccountBillingAdmission, ProbeAdmission, ProbeAdmissionError
from codex_agents.account_observation import ObservationContext
from codex_agents.account_service import AccountOwnerViews
from codex_agents.active_run_capacity import ActiveRunLimits
from codex_agents.billing_probe_schema import assert_billing_probe_schema
from codex_agents.learning_contracts import canonical_json
from codex_agents import learning_store
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.providers import claude_rate_limit_records
from codex_agents.signature_job_intake import SignatureJobIntake, SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerPool, SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits
from tests.learning_support import backup_body


@unittest.skipUnless((sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())),
                     'original fixed macOS signature worker guard')
class AccountBillingAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdmissionTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        self.ledger = self.fixture.ledger
        self.clock = self.fixture.clock+100
        self.clock_patch = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.identity = self.fixture.observe('claude', now_ms=self.clock)[0]
        self.fixture.attest('claude', self.identity['account_fingerprint'])
        assert_billing_probe_schema(self.ledger._connection)
        self.views = AccountOwnerViews(self.ledger)
        self.intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.pool = SignatureWorkerPool(self.intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.pool.bootstrap()
        self.addCleanup(self.stop)
        self.owner = AccountBillingAdmission(self.views, self.pool, SQLReadLimits(100_000, 1000, 2*1_048_576))
        self.context = ObservationContext(str(self.fixture.project.resolve()))
        self.allowed = True

    def stop(self):
        if self.pool is None:
            return
        self.pool.request_close()
        end = time.monotonic()+10
        while any(s.thread.is_alive() for s in self.pool._slots):
            self.pool.poll()
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)

    def reserve(self, identity='probe-original', **changes):
        options = dict(before_commit=lambda: self.allowed)
        options.update(changes)
        return self.owner.reserve(self.fixture.project_id, 'a'*64, 'b'*64, identity, self.context, **options)

    def prepared(self, original):
        handle = self.owner.prepare(original)
        self.pool.offer_next()
        end = time.monotonic()+10
        while handle not in self.pool._returned:
            self.pool.poll()
            self.assertLess(time.monotonic(), end, 'original signature return expired')
            time.sleep(.002)
        return handle

    def rows(self):
        return [dict(r) for r in self.ledger._connection.execute('SELECT * FROM account_billing_probe_requests')]

    def test_actual_registered_signatures_commit_one_probe_admission_without_launching_a_model(self):
        original = self.reserve()
        self.assertIs(original, self.reserve())
        self.assertEqual('reserved', self.rows()[0]['state'])
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.owner.prepare(copy(original))
        handle = self.prepared(original)
        with mock.patch.object(self.ledger, '_prepare_attestations', side_effect=AssertionError('inline SSH')):
            admitted = self.owner.admit(original)
        self.assertIs(type(admitted), ProbeAdmission)
        self.assertEqual(self.identity['account_fingerprint'], admitted.account_fingerprint)
        self.assertEqual('admitted', self.rows()[0]['state'])
        self.assertIs(admitted, self.owner.admit(original))
        self.assertNotIn(handle, self.pool._returned)
        self.assertEqual(1, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertNotIn(b'client_path', canonical_json(self.rows()))

    def earlier_probes(self, count, state):
        """`count` earlier probe requests of this project, finished ('committed') or still in progress."""
        connection = self.ledger._connection
        with self.ledger._lock, connection:
            for number in range(count):
                key = f'{state}-{number}'.ljust(64, '0')
                connection.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                                   (self.fixture.project_id, key, 'p' * 64, 1, 's' * 64))
                connection.execute('INSERT INTO account_billing_probe_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.fixture.project_id, key, 'p' * 64, 'r' * 64, 'c' * 64, 1, 2, state, None, None, None,
                     '[]' if state == 'committed' else None, 's' * 64))

    def test_B0_5_finished_probes_do_not_count_against_the_probe_cap_and_still_replay(self):
        """O0b B0-5 (narrowed): only probes in progress count, so finished ones never fill a project for
        good; a finished probe still replays its stored answer (review 040-b0n-code: through the real flow)."""
        from codex_agents import account_billing_admission
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)  # in progress: admitted
        second = self.reserve('second')
        self.prepared(second)
        denied = self.owner.admit(second)  # finished: committed with its stored answer
        self.assertEqual(['admitted', 'committed'], [r['state'] for r in self.rows()])
        self.enterContext(mock.patch.object(account_billing_admission, 'MAX_PROBE_REQUESTS', 2))
        self.reserve('third')  # one in progress, so a second fits; the finished one does not count
        self.assertEqual({**denied, 'known': True}, self.reserve('second'))  # the replay is unchanged

    def test_B0_5_probes_in_progress_still_fill_the_cap(self):
        from codex_agents import account_billing_admission
        self.enterContext(mock.patch.object(account_billing_admission, 'MAX_PROBE_REQUESTS', 2))
        self.earlier_probes(2, 'reserved')
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_full'):
            self.reserve()

    def test_RS_A_2_a_reserved_probe_past_its_deadline_is_closed_unlaunched_and_stops_counting(self):
        """O0b RS-A-2: a probe never admitted could not have started a model; past its deadline the
        supervisor closes it with a fixed denial, its origin kept, and it no longer counts."""
        original = self.reserve()
        self.assertEqual(['reserved'], [r['state'] for r in self.rows()])
        self.clock = original.deadline + 1
        with mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
            self.assertEqual(1, self.ledger.release_due_account_holds(limit=64))
        row = self.rows()[0]
        self.assertEqual('committed', row['state'])
        self.assertEqual(1, self.ledger._connection.execute(
            'SELECT COUNT(*) FROM account_billing_probe_origins').fetchone()[0])  # the origin is kept
        self.assertEqual(0, self.ledger.account_capacity(self.fixture.project_id)['billing_probes']['live'])
        replay = self.reserve()  # the same request now replays its stored answer
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (replay['probe'], replay['reason_code'], replay['known']))

    def test_RS_A_F5_an_expired_probe_records_the_time_it_was_closed(self):
        """Round 1 F5: the register's release time is the decision's, never the probe's creation time."""
        original = self.reserve()
        self.clock = original.deadline + 1
        with mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
            self.ledger.release_due_account_holds(limit=64)
        self.assertEqual(self.clock, self.ledger._connection.execute(
            "SELECT released_at_ms FROM run_holds WHERE kind='probe'").fetchone()[0])

    def test_RS_A_F6_expired_probes_free_their_owner_memory_so_new_ones_fit(self):
        """Round 1 F6: a project's eight expired reservations, prepared or not, never block a ninth."""
        from codex_agents.account_billing_admission import MAX_PENDING_PROJECT_PROBES
        originals = [self.reserve('probe-%d' % n) for n in range(MAX_PENDING_PROJECT_PROBES)]
        self.prepared(originals[0])  # one with pooled signature work
        with self.assertRaises(ProbeAdmissionError):
            self.reserve('one-too-many')
        self.clock = max(o.deadline for o in originals) + 1
        with mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
            self.assertEqual(MAX_PENDING_PROJECT_PROBES, self.ledger.release_due_account_holds(limit=64))
        fresh = self.reserve('after-expiry')
        self.assertEqual('reserved', next(r for r in self.rows() if r['state'] == 'reserved')['state'])
        self.assertEqual([fresh], list(self.owner._reserved_inputs))
        self.assertEqual({}, self.owner._signatures)

    def expiring_with_work(self):
        """O0b RS-A round 2 G3: two reservations, one whose signature work has returned and one whose work is
        queued (prepared, never offered); the clock then passes both deadlines."""
        returned_original, queued_original = self.reserve('probe-returned'), self.reserve('probe-queued')
        returned = self.prepared(returned_original)
        queued = self.owner.prepare(queued_original)
        self.assertIn(queued, self.intake._jobs)
        self.clock = max(returned_original.deadline, queued_original.deadline) + 1
        return returned, queued

    def sweep(self, *, thread=False):
        def run():
            with mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
                outcome.append(self.ledger.release_due_account_holds(limit=64))
        outcome = []
        if not thread:
            run()
        else:  # the collector's sweep thread, not the owner's
            import threading
            worker = threading.Thread(target=run)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        return outcome[0] if outcome else None

    def assertForgotten(self, returned, queued):
        self.assertEqual(({}, {}, {}), (self.owner._pending, self.owner._reserved_inputs, self.owner._signatures))
        self.assertNotIn(returned, self.pool._returned)  # returned work discarded
        self.assertNotIn(queued, self.intake._jobs)  # queued work cancelled
        self.assertNotIn(queued, self.pool._returned)

    def assertKept(self, returned, queued):
        self.assertEqual(2, len(self.owner._pending))
        self.assertEqual(2, len(self.owner._reserved_inputs))
        self.assertEqual({returned, queued}, set(self.owner._signatures.values()))
        self.assertIn(returned, self.pool._returned)
        self.assertIn(queued, self.intake._jobs)

    def test_RS_A_G3_a_committed_sweep_frees_owner_memory_with_no_later_request(self):
        """Round 2 G3: right after the sweep commits the expired probes, the owner has forgotten them and their
        signature work, with no further request (F6 alone waited for a new reservation)."""
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep())
        self.assertEqual(['committed', 'committed'], [r['state'] for r in self.rows()])
        self.assertForgotten(returned, queued)

    def test_RS_A_G3_a_sweep_on_another_thread_is_forgotten_on_the_owner_s_next_step(self):
        """Round 2 G3: the collector sweeps on its own thread; the owner touches its memory and the pool only on
        its own thread, at its next step, with no request."""
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep(thread=True))
        self.assertKept(returned, queued)  # nothing touched from the sweep's thread
        self.owner.forget_expired()
        self.assertForgotten(returned, queued)

    def test_RS_A_G3_a_replay_of_the_expired_request_alone_frees_owner_memory(self):
        """Round 2 G3: a replay of the expired request (no new reservation) frees the memory too."""
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep(thread=True))
        replay = self.reserve('probe-returned')
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (replay['probe'], replay['reason_code'], replay['known']))
        self.assertForgotten(returned, queued)

    def test_RS_A_G9_a_known_reply_for_the_expired_request_frees_owner_memory(self):
        """Round 2 G9 (3): reading the expired request's known reply frees the memory, as reserve()'s replay does."""
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep(thread=True))
        reply = self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'probe-returned', self.context,
                                       before_commit=lambda: self.allowed)
        self.assertEqual(('denied', 'probe_unlaunched_expired', True),
                         (reply['probe'], reply['reason_code'], reply['known']))
        self.assertForgotten(returned, queued)

    def test_RS_A_G3_a_rolled_back_sweep_keeps_owner_memory(self):
        """Round 2 G3: when the sweep's transaction rolls back after closing the probes, the rows stay reserved
        and the owner keeps their memory and work, on the sweep and on the owner's next step."""
        from codex_agents import account_billing_admission
        returned, queued = self.expiring_with_work()
        real = account_billing_admission.close_expired_reserved

        def close_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('injected after the close')
        with mock.patch.object(account_billing_admission, 'close_expired_reserved', side_effect=close_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.sweep()
        self.assertEqual(['reserved', 'reserved'], [r['state'] for r in self.rows()])
        self.assertKept(returned, queued)
        self.owner.forget_expired()
        self.assertKept(returned, queued)

    def failing_once(self, name):
        """The pool's `name` raises on its first call, then works (a transient pool fault)."""
        real, calls = getattr(self.pool, name), []

        def call(handle):
            calls.append(handle)
            if len(calls) == 1:
                raise RuntimeError(f'injected {name} failure')
            return real(handle)
        return mock.patch.object(self.pool, name, side_effect=call), calls

    def assertForgetRetried(self, name, handle, returned, queued):
        """Round 3 (review 040-rsa-code-r3-2): a pool failure while forgetting keeps the original and its
        handle mapping (the retry target) and the notice, so the next step retries and frees the work."""
        patch, calls = self.failing_once(name)
        with patch:
            with self.assertRaisesRegex(RuntimeError, f'injected {name}'):
                self.owner.forget_expired()
            self.assertIn(handle, self.owner._signatures.values())  # kept for the retry
            self.assertTrue(self.owner._closed_notice.is_set())
            self.owner.forget_expired()
        self.assertEqual(2, calls.count(handle))
        self.assertForgotten(returned, queued)

    def test_RS_A_G9_a_failed_discard_while_forgetting_keeps_its_handle_and_is_retried(self):
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep(thread=True))
        self.assertForgetRetried('discard', returned, returned, queued)

    def test_RS_A_G9_a_failed_cancel_while_forgetting_keeps_its_handle_and_is_retried(self):
        returned, queued = self.expiring_with_work()
        self.assertEqual(2, self.sweep(thread=True))
        self.assertForgetRetried('cancel', queued, returned, queued)

    def test_second_request_cannot_take_the_account_bootstrap_or_refresh_original_deadline(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        second = self.reserve('second')
        self.prepared(second)
        denied = self.owner.admit(second)
        self.assertEqual('denied', denied['probe'])
        self.assertEqual('probe_cooldown', denied['reason_code'])
        self.assertEqual({**denied, 'known': True}, self.reserve('second'))
        self.assertEqual(original.deadline, self.reserve().deadline)
        self.assertEqual(['admitted', 'committed'], [r['state'] for r in self.rows()])

    def test_D40_a_real_problem_during_the_cooldown_is_reported_before_probe_cooldown(self):
        """Owner decision 40 (a): the refusal names the real problem first; a plain cooldown still says probe_cooldown."""
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        second = self.reserve('second')
        self.prepared(second)
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test'}):
            denied = self.owner.admit(second)
        self.assertEqual('denied', denied['probe'], denied)
        self.assertEqual('provider_api_key_present', denied['reason_code'], denied)
        third = self.reserve('third')
        self.prepared(third)
        self.assertEqual('probe_cooldown', self.owner.admit(third)['reason_code'])

    def test_changed_raw_evidence_after_signature_return_refuses_without_spending_bootstrap(self):
        original = self.reserve()
        self.prepared(original)
        self.clock += 1
        self.fixture.observe('claude', now_ms=self.clock)
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.admit(original)
        self.assertEqual('reserved', self.rows()[0]['state'])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_late_permission_revocation_rolls_back_both_admission_and_request_transition(self):
        original = self.reserve()
        self.prepared(original)
        store = self.ledger._store_probe_admission_locked
        def revoke(*args, **options):
            store(*args, **options)
            self.allowed = False
        with mock.patch.object(self.ledger, '_store_probe_admission_locked', new=revoke):
            with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
                self.owner.admit(original)
        self.assertEqual('reserved', self.rows()[0]['state'])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_recent_billing_reuses_original_records_without_probe_admission_and_known_retry_needs_no_worker(self):
        records = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'},
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=self.clock,
            probe_id=secrets.token_hex(32))
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
        original = self.reserve()
        self.prepared(original)
        reused = self.owner.admit(original)
        self.assertEqual('reused', reused['probe'])
        self.assertEqual([self.identity, *records], reused['records'])
        again = self.reserve()  # decision 44 (BR-1): a repeat of a stored yes is judged again, never read back
        self.prepared(again)
        self.assertEqual({**reused, 'known': True}, self.owner.admit(again))
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def store_probe_then_run(self, run_info, *, probe_age=2000, run_age=500):
        """A probe's records, then a finished Claude child's own (run-tagged) records, both inside the reuse window."""
        fingerprint = self.identity['account_fingerprint']
        favourable = {'status': 'allowed', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'}
        probe = claude_rate_limit_records(favourable, account_fingerprint=fingerprint,
                                         observed_at_ms=self.clock-probe_age, probe_id=secrets.token_hex(32))
        run = claude_rate_limit_records(run_info, account_fingerprint=fingerprint, observed_at_ms=self.clock-run_age,
                                        run_id='run-finished-1')
        self.ledger._store_provider_observations(self.fixture.project_id, probe, timestamp=self.clock-probe_age)
        self.ledger._store_provider_observations(self.fixture.project_id, run, timestamp=self.clock-run_age)
        return probe, run

    def test_T1_a_finished_childs_favourable_records_do_not_break_reuse_of_the_probe_records(self):
        """R1: the child's run-tagged records are not probe evidence; the reply holds the probe's records only."""
        probe, run = self.store_probe_then_run({'status': 'allowed', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'})
        original = self.reserve()
        self.prepared(original)
        reused = self.owner.admit(original)
        self.assertEqual('reused', reused['probe'])
        self.assertEqual([self.identity, *probe], reused['records'])
        self.assertTrue(all(r.get('run_id') is None and r.get('phase') is None for r in reused['records']))

    def test_T3_a_newer_more_restrictive_run_record_stops_reuse_and_admission_is_re_evaluated(self):
        """R2 (safety): overage in use / paid usage usable / non-ample headroom in a newer run record stops reuse;
        admission is re-evaluated and the probe launches only if admitted, otherwise a named denial."""
        cases = {'overage in use': ({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'},
                                    'paid_usage_usable'),
                 'headroom rejected': ({'status': 'rejected', 'isUsingOverage': False, 'overageStatus': 'rejected',
                                        'overageDisabledReason': 'org_level_disabled'}, 'provider_exhausted')}
        for name, (info, reason) in cases.items():
            with self.subTest(name):
                self.tearDown_probe_state()
                self.store_probe_then_run(info)
                original = self.reserve(f'probe-{name}'.replace(' ', '-'))
                self.prepared(original)
                result = self.owner.admit(original)
                self.assertIs(type(result), dict, result)
                self.assertEqual('denied', result['probe'])
                self.assertEqual(reason, result['reason_code'], result)

    def test_T3b_paid_usage_usable_alone_in_a_newer_run_record_stops_reuse(self):
        """R2 isolation: only paid_usage_usable is set (not overage in use, headroom ample)."""
        _, run = self.store_probe_then_run({'status': 'allowed', 'isUsingOverage': False, 'overageStatus': 'allowed'})
        billing = [r for r in run if r.get('record_type') == 'provider_billing' or 'paid_usage_usable' in r]
        self.assertEqual(1, len(billing), run)
        self.assertIs(True, billing[0]['paid_usage_usable'])
        self.assertIs(False, billing[0]['is_using_overage'])
        self.assertEqual('during', billing[0]['phase'])
        original = self.reserve('probe-paid-usable-only')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertNotEqual('reused', result['probe'])
        self.assertEqual('denied', result['probe'])
        self.assertEqual('paid_usage_usable', result['reason_code'], result)

    def test_T3c_constrained_headroom_in_a_newer_run_record_stops_reuse_and_the_probe_launches_fresh(self):
        """R2: headroom that is not ample (allowed_warning) stops reuse; admission passes, so a fresh probe is launched."""
        _, run = self.store_probe_then_run({'status': 'allowed_warning', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'})
        billing = [r for r in run if 'paid_usage_usable' in r]
        headroom = [r for r in run if 'paid_usage_usable' not in r and r.get('status') is not None]
        self.assertEqual(1, len(billing), run)
        self.assertIs(False, billing[0]['paid_usage_usable'])
        self.assertIs(False, billing[0]['is_using_overage'])
        self.assertEqual('during', billing[0]['phase'])
        self.assertEqual(['allowed_warning'], [r['status'] for r in headroom], run)
        original = self.reserve('probe-constrained')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), ProbeAdmission, result)

    def test_T5_a_run_record_in_the_same_millisecond_as_the_probe_record_stops_reuse(self):
        """R2 boundary: observed_at equal to the probe's own reading is not older, so it still restricts."""
        self.store_probe_then_run({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'},
                                  probe_age=1000, run_age=1000)
        original = self.reserve('probe-same-ms')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertNotEqual('reused', result['probe'])
        self.assertEqual('paid_usage_usable', result['reason_code'], result)

    def test_T5b_a_same_millisecond_constrained_headroom_run_record_stops_reuse_and_the_probe_launches_fresh(self):
        """R2 boundary on headroom alone: equal ages, allowed_warning, both billing flags false."""
        _, run = self.store_probe_then_run({'status': 'allowed_warning', 'isUsingOverage': False,
            'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'},
            probe_age=1000, run_age=1000)
        billing = [r for r in run if 'paid_usage_usable' in r]
        headroom = [r for r in run if 'paid_usage_usable' not in r and r.get('status') is not None]
        self.assertEqual(1, len(billing), run)
        self.assertIs(False, billing[0]['paid_usage_usable'])
        self.assertIs(False, billing[0]['is_using_overage'])
        self.assertEqual('during', billing[0]['phase'])
        self.assertEqual(['allowed_warning'], [r['status'] for r in headroom], run)
        original = self.reserve('probe-same-ms-constrained')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), ProbeAdmission, result)

    def store_probe_only(self, info, *, age=2000):
        """A probe's own records (no run), inside the reuse window."""
        records = claude_rate_limit_records(info, account_fingerprint=self.identity['account_fingerprint'],
                                            observed_at_ms=self.clock-age, probe_id=secrets.token_hex(32))
        self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock-age)
        return records

    FAVOURABLE = {'status': 'allowed', 'isUsingOverage': False,
                  'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'}

    def test_B2_a_probe_that_saw_paid_usage_is_refused_on_that_probe_and_on_every_reuse(self):
        """B1/B2 (safety): reuse skips only the provider call, never the judgment; no run record is involved."""
        self.store_probe_only({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'})
        for number in range(2):
            original = self.reserve(f'probe-overage-{number}')
            self.prepared(original)
            result = self.owner.admit(original)
            self.assertIs(type(result), dict, result)
            self.assertEqual('denied', result['probe'], result)
            self.assertEqual('paid_usage_usable', result['reason_code'], result)

    def test_B3_an_api_key_at_reuse_time_is_refused_with_its_own_reason_before_any_provider_call(self):
        self.store_probe_only(self.FAVOURABLE)
        original = self.reserve('probe-api-key')
        self.prepared(original)
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test'}):
            result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual('denied', result['probe'], result)
        self.assertEqual('provider_api_key_present', result['reason_code'], result)
        self.assertEqual([], [r for r in self.rows() if r['state'] == 'admitted'])

    def test_B3_an_api_key_with_no_probe_record_is_also_refused_with_its_own_reason(self):
        original = self.reserve('probe-api-key-fresh')
        self.prepared(original)
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test'}):
            result = self.owner.admit(original)
        self.assertEqual('denied', result['probe'], result)
        self.assertEqual('provider_api_key_present', result['reason_code'], result)

    def test_B3_an_unreviewed_client_version_at_reuse_time_is_refused_even_with_a_fresh_probe_record(self):
        from codex_agents.admission_inputs import AdmissionInputPolicy
        self.store_probe_only(self.FAVOURABLE)
        with mock.patch.object(self.views, '_claim_policy', return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None)):
            original = self.reserve('probe-version')
            self.prepared(original)
            result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual('denied', result['probe'], result)
        self.assertEqual('client_version_unreviewed', result['reason_code'], result)

    def test_B3_a_missing_attestation_at_reuse_time_is_refused_even_with_a_fresh_probe_record(self):
        self.store_probe_only(self.FAVOURABLE)
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute('DELETE FROM owner_attestations')
        original = self.reserve('probe-attestation')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual('denied', result['probe'], result)
        self.assertEqual('billing_attestation_missing', result['reason_code'], result)

    def test_B4_regression_guard_a_clean_reuse_still_answers_reused_without_a_new_probe_admission(self):
        # A no-regression guard: it may pass on the code before the fix (not a red-first proof).
        records = self.store_probe_only(self.FAVOURABLE)
        original = self.reserve('probe-clean')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual('reused', result['probe'], result)
        self.assertEqual([self.identity, *records], result['records'])
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='probe_admission'").fetchone()[0])

    def test_H1_a_stored_reused_answer_is_not_replayed_after_its_request_window(self):
        self.store_probe_only(self.FAVOURABLE)
        original = self.reserve('probe-replay')
        self.prepared(original)
        self.assertEqual('reused', self.owner.admit(original)['probe'])
        later = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'},
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=self.clock)
        self.ledger._store_provider_observations(self.fixture.project_id, later, timestamp=self.clock)
        # Decision 44 (BR-1, BR-2, BR-4): a repeat is judged again on the records at hand, inside the window and
        # after it; the paid usage stored later is the newest billing record, so both judgments name it.
        for _ in range(2):
            again = self.reserve('probe-replay')
            self.prepared(again)
            result = self.owner.admit(again)
            self.assertEqual(('denied', 'paid_usage_usable'), (result['probe'], result['reason_code']), result)
            self.clock += self.owner._timeout+1
        self.assertIsNone(self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'probe-replay',
                                                 self.context, before_commit=lambda: True))

    def test_H1_r2_paid_usage_after_a_reused_answer_is_never_replayed_as_reused_on_either_entry_point(self):
        self.store_probe_only(self.FAVOURABLE)
        original = self.reserve('probe-replay-paid')
        self.prepared(original)
        self.assertEqual('reused', self.owner.admit(original)['probe'])
        later = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'},
            account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=self.clock)
        self.ledger._store_provider_observations(self.fixture.project_id, later, timestamp=self.clock)
        again = self.reserve('probe-replay-paid')  # decision 44 (BR-1, BR-4): judged again, never replayed as reused
        self.prepared(again)
        result = self.owner.admit(again)
        self.assertEqual(('denied', 'paid_usage_usable'), (result['probe'], result['reason_code']), result)
        self.assertIsNone(self.owner.known_reply(self.fixture.project_id, 'a'*64, 'b'*64, 'probe-replay-paid',
                                                 self.context, before_commit=lambda: True))

    def test_M1a_paid_usage_appearing_during_a_cooldown_is_refused_paid_usage_usable(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)  # the bootstrap probe is admitted: the five-minute cooldown now runs
        for number, run_id in enumerate((None, 'run-paid')):
            records = claude_rate_limit_records({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'},
                account_fingerprint=self.identity['account_fingerprint'], observed_at_ms=self.clock,
                run_id=run_id, phase=None if run_id is None else 'during')
            self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
            second = self.reserve(f'second-{number}')
            self.prepared(second)
            denied = self.owner.admit(second)
            self.assertEqual(('denied', 'paid_usage_usable'), (denied['probe'], denied['reason_code']), denied)

    def test_LOW_an_api_key_denial_during_a_cooldown_is_persisted_with_its_own_reason(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        second = self.reserve('second')
        self.prepared(second)
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test'}):
            self.assertEqual('provider_api_key_present', self.owner.admit(second)['reason_code'])
        stored = [json.loads(bytes(r[0])) for r in self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type='probe_admission'")]
        self.assertEqual(['pass', 'deny'], [r['decision'] for r in stored], stored)
        self.assertEqual('provider_api_key_present', stored[1]['reason_code'], stored)

    def test_H1_a_stored_denial_is_still_replayed(self):
        self.store_probe_only({'status': 'allowed', 'isUsingOverage': True, 'overageStatus': 'allowed'})
        original = self.reserve('probe-replay-deny')
        self.prepared(original)
        denied = self.owner.admit(original)
        self.assertEqual('denied', denied['probe'], denied)
        self.assertEqual({**denied, 'known': True}, self.reserve('probe-replay-deny'))

    def test_PC6_cooldown_reply_names_latest_pass_retry_and_replays_it(self):
        from datetime import datetime, timezone
        first = self.reserve('pc6-first')
        self.prepared(first)
        self.assertIs(type(self.owner.admit(first)), ProbeAdmission)
        passed_at = self.clock
        self.clock += 10_000
        self.fixture.observe('claude', now_ms=self.clock)
        second = self.reserve('pc6-second')
        self.prepared(second)
        reply = self.owner.admit(second)
        self.assertEqual('probe_cooldown', reply['reason_code'])
        expected = datetime.fromtimestamp((passed_at + 300_000)/1000, timezone.utc).isoformat().replace('+00:00', 'Z')
        self.assertEqual(expected, reply['retry_at'])
        self.assertEqual({**reply, 'known': True}, self.reserve('pc6-second'))

    # ---- PC-1..PC-2 (2026-11-22 probe reuse): a run record as constrained as the probe's own reading ----------
    CONSTRAINED = {'status': 'allowed_warning', 'isUsingOverage': False, 'overageStatus': 'rejected',
                   'overageDisabledReason': 'org_level_disabled',
                   'unifiedWindows': {'five_hour': {'utilization': 0.38, 'status': 'allowed'},
                                      'seven_day': {'utilization': 0.74, 'status': 'allowed_warning'}}}

    def store_probe_info_then_run(self, probe_info, run_info, *, probe_age, run_age):
        fingerprint = self.identity['account_fingerprint']
        probe = claude_rate_limit_records(probe_info, account_fingerprint=fingerprint,
                                          observed_at_ms=self.clock-probe_age, probe_id='a'*64)
        self.ledger._store_provider_observations(self.fixture.project_id, probe, timestamp=self.clock-probe_age)
        if run_info is not None:
            run = claude_rate_limit_records(run_info, account_fingerprint=fingerprint,
                                            observed_at_ms=self.clock-run_age, run_id='run-finished-1')
            self.ledger._store_provider_observations(self.fixture.project_id, run, timestamp=self.clock-run_age)
        return probe

    def test_PC1_a_run_record_as_constrained_as_the_probe_does_not_block_reuse(self):
        """PC-1: probe allowed_warning (5h 38%, weekly 74%), the child's record the same state at +30 s, the
        request at +78 s: the reuse proceeds (re-evaluated and gated) instead of a fresh probe and a cooldown."""
        probe = self.store_probe_info_then_run(self.CONSTRAINED, self.CONSTRAINED, probe_age=78_000, run_age=48_000)
        original = self.reserve('probe-pc1')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual('reused', result['probe'], result)
        self.assertEqual([self.identity, *probe], result['records'])

    def test_PC2_an_ample_probe_and_a_constrained_run_record_still_blocks_reuse(self):
        self.store_probe_info_then_run(self.FAVOURABLE, self.CONSTRAINED, probe_age=78_000, run_age=48_000)
        original = self.reserve('probe-pc2-a')
        self.prepared(original)
        self.assertIs(type(self.owner.admit(original)), ProbeAdmission)

    def test_PC2_a_constrained_probe_and_an_exhausted_run_record_still_blocks_reuse(self):
        exhausted = {**self.CONSTRAINED, 'status': 'rejected'}
        self.store_probe_info_then_run(self.CONSTRAINED, exhausted, probe_age=78_000, run_age=48_000)
        original = self.reserve('probe-pc2-b')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual(('denied', 'provider_exhausted'), (result['probe'], result['reason_code']), result)

    def test_PC2_a_constrained_probe_and_a_paid_usage_run_record_still_blocks_reuse(self):
        paid = {**self.CONSTRAINED, 'isUsingOverage': True, 'overageStatus': 'allowed'}
        self.store_probe_info_then_run(self.CONSTRAINED, paid, probe_age=78_000, run_age=48_000)
        original = self.reserve('probe-pc2-c')
        self.prepared(original)
        result = self.owner.admit(original)
        self.assertIs(type(result), dict, result)
        self.assertEqual(('denied', 'paid_usage_usable'), (result['probe'], result['reason_code']), result)

    def test_PC2_an_equal_state_does_not_reuse_a_stale_or_future_stamped_probe(self):
        for name, age in (('stale', 300_001), ('future', -5_000)):
            with self.subTest(name):
                self.tearDown_probe_state()
                self.store_probe_info_then_run(self.CONSTRAINED, self.CONSTRAINED, probe_age=age, run_age=age-30_000)
                original = self.reserve(f'probe-pc2-{name}')
                self.prepared(original)
                result = self.owner.admit(original)
                self.assertNotEqual('reused', result['probe'] if type(result) is dict else None, result)

    # ---- MED-1 (fix round 1): the baseline is the headroom the SAME probe wrote as the reused billing ---------
    def store_head_only(self, info, *, age):
        """One headroom record (no billing) of another probe, stamped `age` before the clock (negative: later)."""
        head = claude_rate_limit_records(info, account_fingerprint=self.identity['account_fingerprint'],
                                         observed_at_ms=self.clock-age)[0]
        self.ledger._store_provider_observations(self.fixture.project_id, [head], timestamp=self.clock-age)

    def test_MED1_a_constrained_headroom_of_another_probe_is_not_the_baseline_of_an_ample_probe(self):
        """An ample probe P at 0, a constrained headroom of another probe Q, a constrained run record at +40 s,
        the request after: the run is worse than P, so the reuse is stopped whatever Q's stamp is."""
        cases = (  # name, Q's age, run age, request age (all ages are before the clock; the request is `request`)
            ('q-at-plus-30', 48_000, 38_000, 78_000),
            ('q-at-exactly-60-s', 18_000, 8_000, 78_000),
            ('q-stamped-after-the-request', -5_000, 5_000, 45_000),
            ('q-with-the-probes-own-stamp', 78_000, 38_000, 78_000),
        )
        for name, q_age, run_age, probe_age in cases:
            with self.subTest(name):
                self.tearDown_probe_state()
                self.store_probe_info_then_run(self.FAVOURABLE, self.CONSTRAINED, probe_age=probe_age, run_age=run_age)
                self.store_head_only(self.CONSTRAINED, age=q_age)
                original = self.reserve(f'probe-med1-{name}')
                self.prepared(original)
                result = self.owner.admit(original)
                self.assertNotEqual('reused', result['probe'] if type(result) is dict else None, result)

    def tearDown_probe_state(self):
        connection = self.ledger._connection
        with self.ledger._lock, connection:
            connection.execute("DELETE FROM records WHERE record_type IN ('provider_billing','provider_headroom')")

    def test_default_denial_and_tampered_journal_cannot_start_signature_work(self):
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
            self.reserve(before_commit=None)
        self.assertEqual([], self.rows())
        original = self.reserve()
        self.ledger._connection.execute('UPDATE account_billing_probe_requests SET deadline_at_ms=deadline_at_ms+1')
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.prepare(original)
        self.assertEqual(0, len(self.intake._jobs))

    def test_admitted_probe_counts_against_shared_machine_and_account_capacity(self):
        original = self.reserve()
        self.prepared(original)
        with mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(3, 1)):
            self.owner.admit(original)
            with self.ledger._account_transaction():
                with self.assertRaisesRegex(ClaimError, 'claim_busy'):
                    self.ledger._check_active_capacity_locked('claude', self.identity['account_fingerprint'])
                self.ledger._check_active_capacity_locked('codex', 'c'*64)
                self.ledger._check_active_capacity_locked('claude', 'c'*64)
            with mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(1, 1)):
                with self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, 'claim_busy'):
                    self.ledger._check_active_capacity_locked('codex', 'c'*64)
        self.clock += 1_000_000
        with self.assertRaisesRegex(learning_store.LedgerError, 'project_has_active_billing_probe'):
            self.ledger.delete_project(self.fixture.project_id)
        self.assertEqual('admitted', self.rows()[0]['state'])

    def test_expired_original_cannot_start_new_signature_work_or_get_a_new_window(self):
        original = self.reserve()
        self.clock = original.deadline
        self.assertIs(original, self.reserve())
        with self.assertRaisesRegex(ProbeAdmissionError, 'probe_conflict'):
            self.owner.prepare(original)
        self.assertEqual(0, len(self.intake._jobs))
        self.assertEqual(original.deadline, self.rows()[0]['deadline_at_ms'])

    def test_mutated_original_native_deadline_or_authorizer_cannot_prepare_signatures(self):
        for field, value in (('deadline_ns', time.monotonic_ns()+120_000_000_000),
                             ('authorize', lambda: True)):
            with self.subTest(field=field):
                original = self.reserve('mutated-'+field)
                object.__setattr__(original, field, value)
                with self.assertRaisesRegex(ProbeAdmissionError, 'probe_not_authorized'):
                    self.owner.prepare(original)
        self.assertEqual(0, self.intake.snapshot().jobs)

    def test_standalone_backup_preserves_genuine_sealed_admission_and_rejects_missing_request(self):
        from copy import deepcopy
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        before = self.rows()
        path = self.ledger.create_sanitized_backup(self.fixture.project_id, file_id='original_probe',
            expires_at_ms=time.time_ns()//1_000_000+60_000)
        body = backup_body(path, self.fixture.project_id)
        self.assertEqual(18, body['ledger_schema_version'])  # O0b RS-A (requirement)
        self.assertTrue(self.ledger.verify_sanitized_backup(self.fixture.project_id, file_id='original_probe')['verified'])
        broken = deepcopy(body)
        broken['tables']['account_billing_probe_requests'] = []
        broken['counts']['account_billing_probe_requests'] = 0
        with self.assertRaises(learning_store.LedgerError):  # F3-10: moved from the removed format-2 check
            learning_store._verify_project_backup_body(broken)
        self.assertEqual(before, self.rows())

    def restart(self):
        """The collector restarts: the ledger reopens and a new probe owner holds no original."""
        self.stop()
        self.pool = None
        self.ledger.close()
        reopened = learning_store.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.ledger = self.fixture.ledger = reopened
        self.clock_patch = mock.patch.object(reopened, '_clock_ms', side_effect=lambda: self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.views = AccountOwnerViews(reopened)
        self.intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.pool = SignatureWorkerPool(self.intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.pool.bootstrap()
        self.owner = AccountBillingAdmission(self.views, self.pool, SQLReadLimits(100_000, 1000, 2*1_048_576))

    def sweep_now(self):
        with mock.patch.object(self.ledger, '_capacity_clock_reference', return_value=0):
            return self.ledger.release_due_account_holds(limit=64)

    def test_O0c_P6_an_admitted_probe_of_an_earlier_collector_holds_its_slot_no_longer_than_deadline_plus_grace(self):
        """O0c P6: machine limit 1 (and account limit 1); a probe admitted before a restart has no original
        left in memory and nothing to settle it. Within deadline + grace it is kept (a model may still be
        ending); after it the sweep closes it as a denial, and a claim and a new probe succeed."""
        from codex_agents.account_billing_admission import PROBE_ORPHAN_GRACE_MS
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        deadline = original.deadline
        account = self.identity['account_fingerprint']
        with mock.patch.object(learning_store, 'INSTALLED_ACTIVE_RUN_LIMITS', ActiveRunLimits(1, 1)):
            self.restart()
            self.clock = deadline + PROBE_ORPHAN_GRACE_MS
            self.assertEqual(0, self.sweep_now())  # not yet: the grace is not over
            with self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, 'claim_busy'):
                self.ledger._check_active_capacity_locked('claude', account)
            self.assertEqual('admitted', self.rows()[0]['state'])
            self.clock = deadline + PROBE_ORPHAN_GRACE_MS + 1
            self.assertEqual(1, self.sweep_now())
            row = self.rows()[0]
            self.assertEqual('committed', row['state'])
            self.assertEqual(self.clock, self.ledger._connection.execute(
                "SELECT released_at_ms FROM run_holds WHERE kind='probe'").fetchone()[0])
            with self.ledger._account_transaction():
                self.ledger._check_active_capacity_locked('claude', account)  # a claim now fits
                self.ledger._check_active_capacity_locked('codex', 'c'*64)
            replay = self.reserve()  # the same request replays its stored answer: never relaunched
            self.assertEqual(('denied', 'probe_admitted_expired', True),
                             (replay['probe'], replay['reason_code'], replay['known']))
            self.assertEqual(0, self.ledger.account_capacity(self.fixture.project_id)['billing_probes']['live'])
            again = self.reserve('probe-after')  # and a new probe of the account is admitted, not probe_busy
            self.assertEqual('reserved', self.rows()[-1]['state'])
            self.assertIsNotNone(again)

    def test_O0c_P6_an_admitted_probe_this_collector_still_holds_is_never_closed_by_time(self):
        """M10 stays true: an original in this process's memory (its child may still run) is settled only by
        its own proof, however late."""
        from codex_agents.account_billing_admission import PROBE_ORPHAN_GRACE_MS
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        self.clock = original.deadline + 10 * PROBE_ORPHAN_GRACE_MS
        self.assertEqual(0, self.sweep_now())
        self.assertEqual('admitted', self.rows()[0]['state'])
        self.assertFalse(self.ledger.account_holds_due())  # nothing the sweep would release: no idle-check spin
        self.restart()
        self.assertTrue(self.ledger.account_holds_due())  # an orphan is due

    def test_restart_preserves_admitted_unknown_without_recreating_a_launchable_original(self):
        original = self.reserve()
        self.prepared(original)
        self.owner.admit(original)
        before = self.rows()
        self.stop()
        self.pool = None
        self.ledger.close()
        reopened = learning_store.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.ledger = self.fixture.ledger = reopened
        self.views = AccountOwnerViews(reopened)
        self.intake = SignatureJobIntake(self.views, SignatureIntakeLimits(2, 4, 512, 262_144, 2))
        self.pool = SignatureWorkerPool(self.intake,
            SignatureWorkerLimits(2, 4*1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000))
        self.pool.bootstrap()
        restarted = AccountBillingAdmission(self.views, self.pool, SQLReadLimits(100_000, 1000, 2*1_048_576))
        with (mock.patch.object(reopened, '_clock_ms', side_effect=AssertionError('restart window')),
                self.assertRaisesRegex(ProbeAdmissionError, 'probe_unavailable')):
            restarted.reserve(self.fixture.project_id, 'a'*64, 'b'*64, 'probe-original',
                ObservationContext(self.context.root), before_commit=lambda: True)
        self.assertEqual(before, self.rows())
