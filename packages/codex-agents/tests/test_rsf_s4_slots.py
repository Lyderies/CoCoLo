"""RSF-S4 store regressions: retained evidence, deadlines and signed owner confirmation."""
import time
import unittest
from unittest import mock
from tests import test_learning_store as legacy
from tests.test_account_launch_claims import downgrade_ledger_to_six
from tests.learning_support import sign_owner_payload
from codex_agents import learning_store, launch_journal
from codex_agents.active_run_capacity import CRASHED_CLAIM_GRACE_MS, crashed_claim
from codex_agents.learning_contracts import content_digest, keyed_digest, SUPPORTED_MODELS, provider_for_model
from codex_agents.learning_engine import OwnerGrantAuthority
HOUR = 3_600_000

class LegacySlotEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.project_a_id
        self.now = time.time_ns() // 1_000_000
        self.handles = ['hrn_' + c * 48 for c in ('a', 'b')]
        self.digests = [keyed_digest(self.fixture.state.digest_key(), h) for h in self.handles]
        self.bridges = ['brg_' + c * 32 for c in ('a', 'b')]
    @property
    def ledger(self):
        return self.fixture.ledger
    def seed(self, *, intent=False, prune=False, native=False, journal=None, live=False, two=False):
        ledger = self.ledger
        for i in range(2 if two else 1):
            ledger._connection.execute(
                'INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,'
                'route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (self.project, self.digests[i], 'legacy-run-' + str(i), 'b'*64, 'c'*64, 'd'*64,
                 'e'*64, 'claude-host-v1' if native else 'bridge-v1', self.now - 30*24*HOUR,
                 self.now + 90*24*HOUR, 'pending'))
        if intent:
            record = {'schema_version': 1, 'record_type': 'dispatch_intent', 'run_id': 'legacy-run-0',
                      'bridge_run_id': self.bridges[0], 'provider': 'codex',
                      'model': next(m for m in sorted(SUPPORTED_MODELS) if provider_for_model(m) == 'codex'),
                      'effort': 'high', 'deadline_at_ms': self.now + HOUR, 'observed_at_ms': self.now}
            self.intent = {**record, 'record_id': 'dispatch_intent:' + content_digest(record)}
            with ledger._write_unit():
                ledger._append_checked_locked(self.project, self.intent, producer='lifecycle', timestamp=self.now)
                if prune:
                    ledger._connection.execute("DELETE FROM records WHERE record_type='dispatch_intent'")
        if journal:
            directory = self.fixture.state.root / 'projects' / self.project / 'launches'
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.journal = launch_journal.LaunchJournal(directory)
            self.addCleanup(self.journal.release)
            self.journal.claimed(self.bridges[0], run_handle=self.handles[0], attempt=1,
                                 host_client='claude-host-v1' if native else 'codex-host-v1', lease={},
                                 identity_digest='c'*64, claim_request_id='s4-claim', storage_evidence=b'proof',
                                 workspace=None)
            if journal == 'spawning':
                self.journal.spawning(self.bridges[0])
            if journal == 'watched':
                value = self.journal.entry(self.bridges[0])
                value.pop('marker_path')
                value.update(state='watched', child={'pid': 2001, 'birth': 'child'},
                             watchdog={'pid': 2002, 'birth': 'watchdog'}, run_tag='a'*32)
                self.assertIsNotNone(launch_journal.checked_entry(value))
                self.journal._write(self.bridges[0], value)
            if not live:
                self.journal.release()
        downgrade_ledger_to_six(ledger)
        ledger.close()
        self.fixture.ledger = learning_store.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients,
            allow_schema_upgrade=True, backup_before_upgrade=True)
    def reopen(self):
        self.ledger.close()
        self.fixture.ledger = learning_store.LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
    def count(self):
        sql, params = crashed_claim('a', self.ledger._clock_ms(), self.ledger._capacity_clock_reference())
        return self.ledger._connection.execute(
            'SELECT COUNT(*) FROM active_run_capacity a WHERE released_at_ms IS NULL AND NOT ' + sql, params).fetchone()[0]
    def slot(self):
        return self.ledger.account_holds_status(self.project)['run_slot']
    def test_pruned_intent_stays_held_fenced_through_adoption_sweep_restart(self):
        self.seed(intent=True, prune=True)
        self.assertEqual(1, self.count())
        self.assertFalse(self.ledger.drained())
        self.ledger.release_due_account_holds()
        self.reopen()
        self.assertEqual(1, self.count())
        self.assertEqual('legacy_fenced', self.ledger._connection.execute('SELECT kind FROM launch_claims').fetchone()[0])
        self.assertIsNone(self.slot()['next_eligible'])
        self.assertIn('dispatch evidence is missing', self.slot()['reasons'])
    def test_claimed_journal_is_positive_unsent_proof_released_once_at_adoption(self):
        self.seed(journal='claimed')
        self.assertEqual(0, self.count())
        row = tuple(self.ledger._connection.execute("SELECT release_kind,receipt,evidence FROM run_holds WHERE kind='run_slot'").fetchone())
        self.assertEqual('unsent', row[0])
        self.assertEqual(0, self.ledger.release_due_account_holds())
        self.reopen()
        self.assertEqual(row, tuple(self.ledger._connection.execute("SELECT release_kind,receipt,evidence FROM run_holds WHERE kind='run_slot'").fetchone()))
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_completions').fetchone()[0])
    def test_live_claimed_and_spawning_journals_are_not_unsent_proof(self):
        self.seed(journal='claimed', live=True)
        self.assertEqual(1, self.count())
        self.journal.spawning(self.bridges[0])
        self.journal.release()
        self.ledger.release_due_account_holds()
        self.assertEqual(1, self.count())
    def test_retained_bridge_deadline_strict_grace_boundary_and_durable_release(self):
        self.seed(intent=True)
        boundary = self.intent['deadline_at_ms'] + CRASHED_CLAIM_GRACE_MS
        for instant in (boundary - 1, boundary):
            self.ledger._clock_ms = lambda: instant
            self.assertEqual(1, self.count())
            self.assertFalse(self.ledger.account_holds_due())
            self.assertEqual(boundary + 1, self.slot()['next_eligible'])
            self.assertIn('waiting for the recorded deadline and one-hour grace', self.slot()['reasons'])
            self.assertEqual(0, self.ledger.release_due_account_holds())
        self.ledger._clock_ms = lambda: boundary + 1
        self.assertEqual(0, self.count())
        self.assertTrue(self.ledger.account_holds_due())
        self.assertEqual(1, self.ledger.release_due_account_holds())
        receipt = self.ledger._connection.execute('SELECT release_digest FROM active_run_capacity').fetchone()[0]
        self.reopen()
        self.assertEqual(receipt, self.ledger._connection.execute('SELECT release_digest FROM active_run_capacity').fetchone()[0])
    def test_native_alive_past_a_day_held_and_status_never_writes(self):
        with mock.patch.object(launch_journal, 'tree_state', return_value='alive') as observed:
            self.seed(native=True, journal='watched')
            self.assertEqual(1, self.count())
            self.ledger.release_due_account_holds()
            self.reopen()
            before = self.ledger._connection.total_changes
            status = self.slot()
            self.assertEqual(1, self.count())
            self.assertIsNone(status['next_eligible'])
            self.assertIn('waiting for process evidence or owner confirmation', status['reasons'])
            self.assertEqual(before, self.ledger._connection.total_changes)
            self.assertTrue(observed.called)
    def test_mixed_holds_have_no_finite_finish_promise_or_private_reason(self):
        self.seed(intent=True, two=True)
        self.assertEqual({'holding': 2, 'finished_by_ms': None}, self.ledger.earlier_version_runs())
        self.assertIsNone(self.slot()['next_eligible'])
        for reason in self.slot()['reasons']:
            for private in [self.project, *self.digests, *self.bridges, 'legacy-run', str(self.fixture.state.root)]:
                self.assertNotIn(private, reason)
    def test_owner_confirmation_is_signed_scoped_atomic_and_idempotent(self):
        self.seed(native=True, two=True)
        command = self.slot()['owner_confirmations'][0]['command']
        self.assertIn('learning-confirm-run-slot', command)
        self.assertIn('--run-id legacy-run-0', command)
        record = {'schema_version': 1, 'record_type': 'run_slot_confirmation',
                  'record_id': 'run_slot_confirmation:' + content_digest({'run_id': 'legacy-run-0'}),
                  'run_id': 'legacy-run-0'}
        signature = sign_owner_payload(self.fixture.owner_private_key,
            OwnerGrantAuthority.owner_record_payload(self.project, record), 's4-confirm').read_bytes()
        before = self.ledger._connection.total_changes
        with self.assertRaises(learning_store.LedgerError):
            self.ledger.commit_owner_record(self.project, record=record, signature=b'forged')
        self.assertEqual(before, self.ledger._connection.total_changes)
        with self.assertRaises(learning_store.LedgerError):
            self.ledger.commit_owner_record(self.fixture.project_b_id, record=record, signature=signature)
        self.ledger.commit_owner_record(self.project, record=record, signature=signature)
        self.assertIsNone(self.ledger._connection.execute(
            "SELECT expires_at_ms FROM records WHERE record_id=?", (record['record_id'],)).fetchone()[0])
        self.assertEqual(1, self.count())
        receipt = self.ledger._connection.execute("SELECT release_digest FROM active_run_capacity WHERE run_id='legacy-run-0'").fetchone()[0]
        self.reopen()
        self.ledger.commit_owner_record(self.project, record=record, signature=signature)
        self.assertEqual(1, self.count())
        self.assertEqual(receipt, self.ledger._connection.execute("SELECT release_digest FROM active_run_capacity WHERE run_id='legacy-run-0'").fetchone()[0])
        self.assertEqual(1, self.ledger._connection.execute("SELECT COUNT(*) FROM owner_attestations WHERE subject_record_id=?", (record['record_id'],)).fetchone()[0])

    def test_collector_confirmation_op_has_runtime_gate_and_owner_signature_gate(self):
        from codex_agents.collector_contracts import COLLECTOR_MUTATING_OPERATIONS
        self.seed(native=True)
        self.assertIn('confirm_legacy_slot', COLLECTOR_MUTATING_OPERATIONS)
        server = learning_store.CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        request = {'protocol': 1, 'op': 'confirm_legacy_slot', 'project_root': str(self.fixture.project_a),
                   'record': {}, 'signature_b64': ''}
        before = self.ledger._connection.total_changes
        for runtime in (None, '0'*64):
            fields = {} if runtime is None else {'runtime_digest': runtime}
            reply = server._dispatch({**request, **fields})
            self.assertEqual('runtime_upgrade_required', reply['code'])
        reply = server._dispatch({**request, 'runtime_digest': learning_store.COLLECTOR_RUNTIME_DIGEST})
        self.assertFalse(reply['ok'])
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(1, self.count())
        import base64
        from codex_agents.learning_contracts import run_slot_confirmation
        record = run_slot_confirmation('legacy-run-0')
        signature = sign_owner_payload(self.fixture.owner_private_key,
            OwnerGrantAuthority.owner_record_payload(self.project, record), 's4-dispatch').read_bytes()
        reply = server._dispatch({**request, 'runtime_digest': learning_store.COLLECTOR_RUNTIME_DIGEST,
            'record': record, 'signature_b64': base64.b64encode(signature).decode('ascii')})
        self.assertEqual({'ok', 'committed'}, set(reply))
        self.assertTrue(reply['ok'])
        self.assertTrue(reply['committed']['stored'])
        self.assertEqual(0, self.count())

    def test_status_renderer_shows_plain_reason_and_exact_owner_command(self):
        from codex_agents.client_runtime import _status_account_holds
        self.seed(native=True)
        view = _status_account_holds(self.ledger.account_capacity(self.project))
        self.assertIsNotNone(view)
        self.assertIn('waiting for process evidence or owner confirmation', view['summary'])
        self.assertIn(self.slot()['owner_confirmations'][0]['command'], view['summary'])

    def test_process_gone_releases_without_terminal_or_cleanup_evidence(self):
        with mock.patch.object(launch_journal, 'tree_state', return_value='gone'):
            self.seed(native=True, journal='watched')
        self.assertEqual(0, self.count())
        release = self.ledger._connection.execute("SELECT release_kind,evidence FROM run_holds WHERE kind='run_slot'").fetchone()
        self.assertEqual('process_gone', release['release_kind'])
        import json
        self.assertIn('journal', json.loads(release['evidence'])['proof'])
        self.reopen()
        self.assertEqual(0, self.count())
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_completions').fetchone()[0])

    def test_owner_record_failure_rolls_back_the_release_and_attestation(self):
        from codex_agents.learning_contracts import run_slot_confirmation
        self.seed(native=True)
        record = run_slot_confirmation('legacy-run-0')
        signature = sign_owner_payload(self.fixture.owner_private_key,
            OwnerGrantAuthority.owner_record_payload(self.project, record), 's4-rollback').read_bytes()
        with mock.patch.object(self.ledger, '_append_checked_locked', side_effect=learning_store.LedgerError('failed')):
            with self.assertRaises(learning_store.LedgerError):
                self.ledger.commit_owner_record(self.project, record=record, signature=signature)
        self.assertEqual(1, self.count())
        self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM owner_attestations').fetchone()[0])

    def test_unreadable_instance_lock_cannot_establish_positive_unsent(self):
        import os
        self.seed(journal='claimed', live=True)
        original = launch_journal.os.open
        def unreadable(path, *args, **kwargs):
            if str(path).endswith('.lock'):
                raise PermissionError('unreadable')
            return original(path, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=unreadable):
            self.ledger.release_due_account_holds()
            self.assertEqual(1, self.count())


class LegacySlotCorrectionTests(LegacySlotEvidenceTests):
    """Security review round: every evidence-poor hold has a runnable owner command; no process work in counts."""

    def commands(self):
        return self.slot()['owner_confirmations']

    def run_command(self, command):
        """The command as shown: the unsigned step prints the payload, the signed step is the same command + file."""
        import shlex
        steps = command.split(' ; then ')
        self.assertEqual(2, len(steps))
        unsigned = shlex.split(steps[0])
        self.assertNotIn('--signature', unsigned)
        signed = shlex.split(steps[1].replace('with the signed payload: ', ''))
        self.assertIn('--signature', signed)
        return unsigned, signed

    def argument(self, words, name):
        return words[words.index(name) + 1]

    def record_for(self, words):
        from codex_agents.learning_contracts import run_slot_confirmation
        return run_slot_confirmation(self.argument(words, '--run-id'), self.argument(words, '--handle-digest'))

    def release_by(self, command, label):
        _, signed = self.run_command(command)
        record = self.record_for(signed)
        signature = sign_owner_payload(self.fixture.owner_private_key,
            OwnerGrantAuthority.owner_record_payload(self.project, record), label).read_bytes()
        self.ledger.commit_owner_record(self.project, record=record, signature=signature)

    def test_every_evidence_poor_slot_shows_a_runnable_command_that_releases_exactly_it(self):
        self.seed(native=True, two=True)
        # One of each: native (claude-host-v1), a codex-host-v1 binding with no evidence, and no binding row at all.
        digests = self.digests
        self.ledger._connection.execute("UPDATE host_run_bindings SET client_version='codex-host-v1' WHERE handle_digest=?", (digests[1],))
        self.assertEqual(2, len(self.commands()))
        self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?", (digests[1],))
        commands = self.commands()
        self.assertEqual(2, len(commands))
        for item in commands:
            self.assertIn(item['reason'], ('waiting for process evidence or owner confirmation', 'dispatch evidence is missing'))
        for index in (0, 1):
            self.assertEqual(2 - index, self.count())
            shown = {self.argument(self.run_command(c['command'])[1], '--run-id'): c['command'] for c in self.commands()}
            self.release_by(shown['legacy-run-' + str(index)], 's4-poor-%d' % index)
            self.assertEqual(1 - index, self.count())
            released = [r[0] for r in self.ledger._connection.execute(
                "SELECT run_id FROM active_run_capacity WHERE released_at_ms IS NOT NULL")]
            self.assertEqual(['legacy-run-%d' % i for i in range(index + 1)], sorted(released))

    def test_codex_host_and_bridge_with_pruned_intent_get_commands(self):
        self.seed(intent=True, prune=True)
        self.assertEqual(1, len(self.commands()))
        self.assertEqual('dispatch evidence is missing', self.commands()[0]['reason'])

    def test_a_run_id_appearing_twice_releases_each_slot_by_handle(self):
        self.seed(native=True, two=True)
        # A run id kept twice by an earlier version (its binding rows are separate rows per handle).
        self.ledger._connection.execute('PRAGMA foreign_keys=OFF')
        self.ledger._connection.execute("UPDATE active_run_capacity SET run_id='legacy-run-0' WHERE handle_digest=?", (self.digests[1],))
        self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?", (self.digests[1],))
        self.ledger._connection.execute('PRAGMA foreign_keys=ON')
        self.assertEqual(2, self.count())
        for expected in (1, 0):
            command = self.commands()[0]['command']
            self.release_by(command, 's4-dup-%d' % expected)
            self.assertEqual(expected, self.count())

    def test_command_has_unsigned_step_then_signed_step_and_shows_paths(self):
        self.seed(native=True)
        unsigned, signed = self.run_command(self.commands()[0]['command'])
        self.assertEqual(signed[:len(unsigned)], unsigned)
        self.assertEqual('owner-signature.bin', self.argument(signed, '--signature'))
        self.assertEqual(str(self.fixture.state.root), self.argument(unsigned, '--state-root'))
        self.assertEqual('legacy-run-0', self.argument(unsigned, '--run-id'))

    def test_capacity_counts_do_no_journal_or_process_work(self):
        from codex_agents import legacy_slots
        self.seed(native=True, two=True)
        with mock.patch.object(legacy_slots, 'process_proof') as proof, \
                mock.patch.object(launch_journal, 'tree_state') as tree, \
                mock.patch.object(launch_journal, '_read_entry_or_none') as read, \
                mock.patch('pathlib.Path.glob') as glob:
            self.assertEqual(2, self.count())
            self.slot()
            self.ledger.account_holds_due()
            self.ledger.earlier_version_runs()
            for spy in (proof, tree, read, glob):
                self.assertFalse(spy.called)

    def test_process_gone_after_adoption_is_released_by_the_sweep_only(self):
        with mock.patch.object(launch_journal, 'tree_state', return_value='alive'):
            self.seed(native=True, journal='watched')
        self.assertEqual(1, self.count())
        with mock.patch.object(launch_journal, 'tree_state', return_value='gone'):
            self.assertEqual(1, self.count())
            self.assertEqual(1, self.ledger.release_due_account_holds())
        self.assertEqual(0, self.count())
