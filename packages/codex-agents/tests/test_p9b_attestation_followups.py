from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import attest_provider, generate_test_owner_key, provider_fixture
from test_framework import requirements
from codex_agents import attestation_capture, learning_engine
from codex_agents.account_service import AccountOwnerViews
from codex_agents.admission_inputs import AdmissionCaptureLimits, AdmissionInputPolicy
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.providers import parse_codex_rate_limits
from codex_agents.signature_inputs import SignatureCaptureLimits
from codex_agents.sql_read_budget import SQLReadLimits


class P9bAttestationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        self.key, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(lambda: self.ledger.close())
        root = self.base / 'project'
        root.mkdir()
        self.project = self.ledger.register_project(root, now_ms=1)
        self.record = self.attest()

    def attest(self, *, issued_at=1, account='a', provider='codex'):
        return attest_provider(self.ledger, self.project, self.key, provider, account * 64,
                               issued_at=issued_at)

    def capture_ids(self, provider='codex'):
        captured = AccountOwnerViews(self.ledger).capture_signature_inputs(
            self.project, provider, SignatureCaptureLimits(64, 1_048_576),
            SQLReadLimits(100_000, 1000, 1_048_576))
        return {row[0] for row in captured.rows}

    def assert_unchecked_then_recovered(self, record):
        connection = self.ledger._connection
        self.assertIsNone(connection.execute('SELECT valid FROM temp.attestation_checks WHERE record_id=?',
                                             (record['record_id'],)).fetchone())
        self.assertIn(record['record_id'], self.capture_ids())
        self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
        self.assertEqual((1, 1), tuple(connection.execute(
            'SELECT valid,current FROM temp.attestation_checks WHERE record_id=?',
            (record['record_id'],)).fetchone()))
        self.assertIn(record['record_id'], self.capture_ids())

    @requirements('P9b-1', why_not_scenario='Real signed rows are swept with unsafe verifier metadata, then retried.')
    def test_unsafe_verifier_leaves_genuine_rows_unchecked_until_repaired(self):
        executable = learning_engine.SSH_KEYGEN_PATH
        lstat, is_symlink = Path.lstat, Path.is_symlink
        cases = [('executable-writable', executable, 'mode'),
                 ('executable-owner', executable, 'owner'),
                 ('executable-symlink', executable, 'symlink'),
                 ('parent-writable', executable.parent, 'mode'),
                 ('parent-owner', executable.parent, 'owner'),
                 ('parent-symlink', executable.parent, 'symlink')]
        for number, (name, target, problem) in enumerate(cases):
            with self.subTest(case=name):
                record = self.attest(account='012345'[number])
                def unsafe_stat(path, *args, **kwargs):
                    result = lstat(path, *args, **kwargs)
                    fields = list(result)
                    if path == target:
                        if problem == 'mode':
                            fields[0] |= stat.S_IWGRP | stat.S_IWOTH
                        elif problem == 'owner':
                            fields[4] = 12345
                    return os.stat_result(fields)
                def unsafe_symlink(path):
                    return (path == target and problem == 'symlink') or is_symlink(path)
                with mock.patch.object(Path, 'lstat', unsafe_stat), \
                        mock.patch.object(Path, 'is_symlink', unsafe_symlink):
                    self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
                self.assert_unchecked_then_recovered(record)

    @requirements('P9b-1', why_not_scenario='Exercise nonzero subprocess outcomes at the real maintenance boundary.')
    def test_failed_verifier_process_is_retryable_without_restart(self):
        outcomes = [(-9, b''), (137, b''), (1, b'internal error'),
                    (255, b'Cannot allocate memory\n'),
                    (255, b'Signature verification failed: incorrect signature\ninternal error\n')]
        for number, (returncode, stderr) in enumerate(outcomes):
            with self.subTest(returncode=returncode, stderr=stderr):
                record = self.attest(account='bcdef'[number])
                completed = subprocess.CompletedProcess(['ssh-keygen'], returncode, b'', stderr)
                with mock.patch.object(learning_engine.subprocess, 'run', return_value=completed):
                    self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
                self.assert_unchecked_then_recovered(record)

    @requirements('P9b-1', why_not_scenario='A real cryptographic mismatch must still cache a definitive negative.')
    def test_clean_signature_invalid_is_cached(self):
        other = self.attest(account='b')
        connection = self.ledger._connection
        connection.execute('UPDATE owner_attestations SET signature=(SELECT signature FROM owner_attestations '
                           'WHERE subject_record_id=?) WHERE subject_record_id=?',
                           (other['record_id'], self.record['record_id']))
        with mock.patch.object(learning_engine.subprocess, 'run', wraps=subprocess.run) as run:
            self.ledger.sweep_superseded_attestations(batch=100)
            calls = run.call_count
            self.assertEqual(2, calls)
            self.ledger.sweep_superseded_attestations(batch=100)
            self.assertEqual(calls, run.call_count)
        self.assertEqual((0, 1), tuple(connection.execute(
            'SELECT valid,current FROM temp.attestation_checks WHERE record_id=?',
            (self.record['record_id'],)).fetchone()))
        self.assertEqual({other['record_id']}, self.capture_ids())

    @requirements('P9b-2', why_not_scenario='Measure the actual per-open TEMP rebuild at SQLite VM instruction granularity.')
    def test_candidate_count_and_open_rebuild_ignore_non_attestation_history(self):
        def reopen():
            self.ledger.close()
            steps = []
            initialize = attestation_capture.initialize
            def measured(connection):
                connection.set_progress_handler(lambda: steps.append(1) or 0, 1)
                try:
                    initialize(connection)
                finally:
                    connection.set_progress_handler(None, 0)
            with mock.patch.object(attestation_capture, 'initialize', side_effect=measured):
                self.ledger = LearningLedger(self.state)
            count = self.ledger._connection.execute('SELECT COUNT(*) FROM temp.capture_candidates').fetchone()[0]
            self.assertEqual(2, count)
            return len(steps)
        before = reopen()
        connection = self.ledger._connection
        original = dict(connection.execute('SELECT * FROM records WHERE record_id=?',
                                           (self.record['record_id'],)).fetchone())
        connection.executemany('INSERT INTO records(' + ','.join(original) + ') VALUES(' +
                               ','.join('?' for _ in original) + ')',
                               [tuple(dict(original, record_id='history-' + str(i),
                                           record_type=('event' if i % 2 else 'candidate'),
                                           source_sequence=i + 2).values()) for i in range(10000)])
        inserted_count = connection.execute('SELECT COUNT(*) FROM temp.capture_candidates').fetchone()[0]
        after = reopen()
        self.assertEqual(2, inserted_count)
        self.assertLessEqual(abs(after - before), 100, (before, after))
        self.assertEqual({self.record['record_id']}, self.capture_ids())

    @requirements('P9b-2', why_not_scenario='Record-type transitions must maintain attestation-only membership atomically.')
    def test_candidate_membership_tracks_record_type_changes(self):
        connection = self.ledger._connection
        for checked in (False, True):
            with self.subTest(checked=checked):
                if checked:
                    self.ledger.sweep_superseded_attestations(batch=100)
                connection.execute("UPDATE records SET record_type='event'")
                self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM temp.capture_candidates').fetchone()[0])
                connection.execute("UPDATE records SET record_type='billing_attestation'")
                self.assertIn(self.record['record_id'], self.capture_ids())

    @requirements('P9b-2', why_not_scenario='Public admission retains non-attestation evidence while excluding unrelated events.')
    def test_admission_keeps_other_evidence_outside_candidate_table(self):
        now = 10_000_000_000
        observations = parse_codex_rate_limits(json.loads(provider_fixture('codex-rate-limits.json')),
            digest_key=self.state.digest_key(), observed_at_ms=now,
            client=('/usr/bin/fixture-codex', 'f' * 64), client_version='0.164.0')
        self.ledger._store_provider_observations(self.project, observations, timestamp=now)
        with mock.patch.object(self.ledger, '_clock_ms', return_value=now):
            captured = AccountOwnerViews(self.ledger).capture_admission_inputs(self.project, 'codex',
                AdmissionInputPolicy('0.164.0', '0.164.1', 60), AdmissionCaptureLimits(64, 1_048_576),
                SQLReadLimits(100_000, 1000, 1_048_576))
        self.assertEqual({'billing_attestation', 'provider_identity', 'provider_billing', 'provider_headroom'},
                         {row[4] for row in captured.rows})
        self.assertEqual({'billing_attestation'}, {row[0] for row in self.ledger._connection.execute(
            'SELECT DISTINCT record_type FROM temp.capture_candidates')})

    @requirements('P9b-3', why_not_scenario='Both SQL deletion and public expiry restore the surviving account/provider predecessor.')
    def test_deleting_newest_restores_predecessor_for_every_record_delete_path(self):
        for number, path in enumerate(('sql', 'sql-without-foreign-keys', 'expiry', 'expiry-report')):
            with self.subTest(path=path):
                older = self.attest(account='bcde'[number])
                newest = self.attest(account='bcde'[number], issued_at=2)
                connection = self.ledger._connection
                # Retain the predecessor without linking its lifetime to newest.
                connection.execute('INSERT INTO stored_files VALUES(?,?,?,?,?,?,?)',
                    (self.project, 'keep-' + str(number), 'artifact', 'keep-' + str(number),
                     'd', older['record_id'], None))
                self.ledger.sweep_superseded_attestations(batch=100)
                self.assertNotIn(older['record_id'], self.capture_ids())
                self.assertIn(newest['record_id'], self.capture_ids())
                with mock.patch.object(OwnerGrantAuthority, 'verify_owner_record',
                                       side_effect=AssertionError('deletion must reuse exact checks')):
                    if path.startswith('sql'):
                        if path == 'sql-without-foreign-keys':
                            connection.execute('PRAGMA foreign_keys=OFF')
                        try:
                            connection.execute('DELETE FROM records WHERE record_id=?', (newest['record_id'],))
                        finally:
                            connection.execute('PRAGMA foreign_keys=ON')
                    else:
                        connection.execute('UPDATE records SET expires_at_ms=2 WHERE record_id=?',
                                           (newest['record_id'],))
                        removed = (self.ledger.expire(self.project, now_ms=3) if path == 'expiry' else
                                   self.ledger.expire_report(self.project, now_ms=3)['removed'])
                        self.assertEqual(1, removed)
                    self.assertIn(older['record_id'], self.capture_ids())
                    self.assertNotIn(newest['record_id'], self.capture_ids())
                    self.assertEqual({self.record['record_id']} | {
                        row[0] for row in connection.execute(
                            "SELECT record_id FROM records WHERE record_type='billing_attestation'")},
                        self.capture_ids())
