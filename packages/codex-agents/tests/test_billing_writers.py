"""CoCoLo 0.4.0 billing safety BR-15 (owner decision 44; writer check ANALYSIS-0.4.0-billing-writers.md).

The owner signer and the policy can change outside the ledger lock, and some multi-record billing writes
are not one unit. Requirement: every judgment re-checks signer and policy immediately before it commits,
signer writes take the ledger lock, and records that must stay together are written in one transaction.
"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.learning_support import generate_test_owner_key
from tests.test_billing_rejudge import FAVOURABLE, PLATFORM, RejudgeOwnerBase

from codex_agents import learning_store
from codex_agents.account_billing_admission import ProbeAdmissionError
from codex_agents.collector_paths import StateError
from codex_agents.admission_inputs import AdmissionInputPolicy
from codex_agents.providers import claude_rate_limit_records


@PLATFORM
class BR15SignerPolicyAndAtomicWrites(RejudgeOwnerBase):

    def after_the_final_recheck(self, change):
        """A fresh request whose inputs change right after the owner's last in-transaction capture
        comparison (the second `_fresh`), i.e. between the final check and the commit."""
        self.store_probe_only(FAVOURABLE)
        original = self.reserve('br15')
        self.prepared(original)
        real, calls = self.owner._fresh, []

        def fresh(*args, **kwargs):
            out = real(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                change()
            return out

        with mock.patch.object(self.owner, '_fresh', side_effect=fresh):
            try:
                answer = self.owner.admit(original)
            except ProbeAdmissionError as error:
                return {'probe': 'error', 'reason_code': error.reason_code}
        self.assertEqual(2, len(calls), 'the owner did not run its final check')
        return answer

    def test_BR15_a_signer_changed_after_the_final_check_is_never_a_committed_yes(self):
        patch = mock.patch.object(self.ledger, '_current_signer_bytes', return_value=b'x'*32)
        answer = self.after_the_final_recheck(patch.start)
        self.addCleanup(patch.stop)
        self.assertNotIn(answer['probe'], {'reused', 'completed'}, answer)
        self.assertNotEqual(['committed'], [r['state'] for r in self.rows()], 'a yes was committed')

    def test_BR15_a_policy_changed_after_the_final_check_is_never_a_committed_yes(self):
        patch = mock.patch.object(self.views, '_claim_policy', return_value=AdmissionInputPolicy('999.0.0', '1000.0.0', None))
        answer = self.after_the_final_recheck(patch.start)
        self.addCleanup(patch.stop)
        self.assertNotIn(answer['probe'], {'reused', 'completed'}, answer)
        self.assertNotEqual(['committed'], [r['state'] for r in self.rows()], 'a yes was committed')

    def test_BR15_the_signer_file_is_written_under_the_ledger_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public = generate_test_owner_key(Path(directory))
        held = []
        real = self.ledger.state._write_owner_signers

        def write(content):
            held.append(self.ledger._lock._is_owned())
            return real(content)

        with mock.patch.object(self.ledger.state, '_write_owner_signers', side_effect=write):
            self.ledger.replace_owner_key(public, now_ms=self.clock)
        self.assertEqual([True], held, 'the signer file was written outside the ledger lock')

    def test_BR15_a_failure_between_the_records_of_one_billing_write_leaves_neither(self):
        records = claude_rate_limit_records(FAVOURABLE, account_fingerprint=self.identity['account_fingerprint'],
                                            observed_at_ms=self.clock)
        self.assertGreaterEqual(len(records), 2)
        count = lambda: self.ledger._connection.execute(  # noqa: E731
            "SELECT COUNT(*) FROM records WHERE record_type IN ('provider_billing','provider_headroom')").fetchone()[0]
        before, real, calls = count(), self.ledger._append_checked_locked, []

        def append(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError('killed between the two records')
            return real(*args, **kwargs)

        with mock.patch.object(self.ledger, '_append_checked_locked', side_effect=append):
            with self.assertRaises(RuntimeError):
                self.ledger._store_provider_observations(self.fixture.project_id, records, timestamp=self.clock)
        self.assertEqual(before, count(), 'one record of a two-record billing write stayed visible')

    # ---- decision 47, M3: the signer turn is held until the transaction has committed -----------------
    def admit_fresh(self):
        self.store_probe_only(FAVOURABLE)
        original = self.reserve('br15-turn')
        self.prepared(original)
        return original

    def test_BR15_M3_the_signer_turn_is_held_until_the_transaction_has_committed_and_taken_after_the_ledger_lock(self):
        original = self.admit_fresh()
        events, state = [], self.ledger.state
        real_turn, real_transaction = state._owner_key_turn, self.ledger._account_transaction

        @contextlib.contextmanager
        def turn():
            with real_turn():
                events.append('turn_taken')
                yield
                events.append('turn_released')  # appended while it is still held; released right after

        @contextlib.contextmanager
        def transaction():
            events.append('ledger_lock_and_begin')
            with real_transaction():
                yield
            events.append('committed')

        with mock.patch.object(state, '_owner_key_turn', side_effect=turn), \
                mock.patch.object(self.ledger, '_account_transaction', side_effect=transaction):
            self.assertEqual('reused', self.owner.admit(original)['probe'])
        self.assertIn('turn_taken', events)
        self.assertLess(events.index('ledger_lock_and_begin'), events.index('turn_taken'), 'lock order: ledger first')
        self.assertLess(events.index('committed'), events.index('turn_released'),
                        'the signer turn ended before the transaction committed')

    def test_BR15_M3_a_signer_writer_cannot_get_in_between_the_last_check_and_the_commit(self):
        """A writer that holds only the owner-key turn (first setup, another process) tries to take it right
        after the last signer/policy comparison, the moment before the commit."""
        original = self.admit_fresh()
        after_guard, outcome = [], []
        real_check, real_harden = self.owner._inputs_unchanged, self.ledger.state.harden_database_files

        def check(snapshot):
            real_check(snapshot)
            after_guard.append(True)

        def harden():
            if after_guard and not outcome:
                try:
                    with self.ledger.state._owner_key_turn():
                        outcome.append('writer got in')
                except StateError:
                    outcome.append('writer blocked')
            return real_harden()

        with mock.patch.object(learning_store, 'OWNER_KEY_TURN_SECONDS', 0.05), \
                mock.patch.object(self.owner, '_inputs_unchanged', side_effect=check), \
                mock.patch.object(self.ledger.state, 'harden_database_files', side_effect=harden):
            self.owner.admit(original)
        self.assertEqual(['writer blocked'], outcome, 'a signer writer got in after the last check, before the commit')


if __name__ == '__main__':
    unittest.main()
