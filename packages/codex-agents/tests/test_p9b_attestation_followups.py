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
from codex_agents import account_service, attestation_capture, learning_engine
from codex_agents.account_service import AccountOwnerViews
from codex_agents.admission_inputs import AdmissionCaptureLimits, AdmissionInputPolicy
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.providers import parse_codex_rate_limits
from codex_agents.signature_inputs import SignatureCaptureLimits
from codex_agents.sql_read_budget import SQLReadLimits


# Exact lines observed with real OpenSSH 10.5p1, not substring fragments.
DETERMINISTIC_SIGNATURE_DIAGNOSTICS = (
    b"Couldn't parse signature: missing footer",
    b"Couldn't parse signature: missing header",
    b'sig_verify: sshsig_armor: invalid format',
    b'sshsig_dearmor: decode base64: invalid format',
    b"Couldn't verify signature: invalid format",
    b'sshsig_peek_hashalg: parse signature object: incomplete message',
    b"Couldn't verify signature: namespace does not match",
    b'Signature verification failed: incorrect signature',
)


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

    def assert_invalid_cached_once(self):
        with mock.patch.object(learning_engine.subprocess, 'run', wraps=subprocess.run) as run:
            self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
            self.assertEqual(1, run.call_count)
            self.assertEqual((0, 1), tuple(self.ledger._connection.execute(
                'SELECT valid,current FROM temp.attestation_checks WHERE record_id=?',
                (self.record['record_id'],)).fetchone()))
            self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
            self.assertEqual(1, run.call_count)
        self.assertNotIn(self.record['record_id'], self.capture_ids())

    @requirements('P9b-1', why_not_scenario='Rotate a real owner key and verify the previous key signature with real ssh-keygen.')
    def test_previous_owner_key_signature_is_cached_invalid_without_reverification(self):
        replacement = self.base / 'replacement'
        replacement.mkdir()
        _, public = generate_test_owner_key(replacement)
        self.ledger.replace_owner_key(public, now_ms=42)
        self.assert_invalid_cached_once()

    @requirements('P9b-1', why_not_scenario='Truncate a real SSH signature and exercise the actual parser and maintenance cache.')
    def test_truncated_signature_is_cached_invalid_without_reverification(self):
        connection = self.ledger._connection
        signature = connection.execute('SELECT signature FROM owner_attestations WHERE subject_record_id=?',
                                       (self.record['record_id'],)).fetchone()[0]
        connection.execute('UPDATE owner_attestations SET signature=? WHERE subject_record_id=?',
                           (signature[:len(signature) // 2], self.record['record_id']))
        self.assert_invalid_cached_once()

    @requirements('P9b-1', why_not_scenario='Sign the real record in the wrong namespace and exercise the maintenance cache.')
    def test_wrong_namespace_signature_is_cached_invalid_without_reverification(self):
        payload = OwnerGrantAuthority.owner_record_payload(self.project, self.record)
        completed = subprocess.run(
            [str(learning_engine.SSH_KEYGEN_PATH), '-Y', 'sign', '-f', str(self.key),
             '-n', 'wrong-namespace'], input=payload, capture_output=True, check=True)
        self.ledger._connection.execute(
            'UPDATE owner_attestations SET signature=? WHERE subject_record_id=?',
            (completed.stdout, self.record['record_id']))
        self.assert_invalid_cached_once()

    @requirements('P9b-1', why_not_scenario='Pin exact observed diagnostics independently of the installed OpenSSH version.')
    def test_deterministic_malformed_signature_diagnostics_are_definitive(self):
        authority = OwnerGrantAuthority(self.state.allowed_signers_path, raise_verifier_failures=True)
        for diagnostic in DETERMINISTIC_SIGNATURE_DIAGNOSTICS:
            for terminator in (b'', b'\n', b'\r\n'):
                with self.subTest(diagnostic=diagnostic, terminator=terminator):
                    completed = subprocess.CompletedProcess(['ssh-keygen'], 255,
                        b'Could not verify signature.' + terminator, diagnostic + terminator)
                    with mock.patch.object(learning_engine.subprocess, 'run', return_value=completed):
                        self.assertFalse(authority.verify_owner_record(self.project, self.record, b'bad-signature'))
        completed = subprocess.CompletedProcess(['ssh-keygen'], 255, b'Could not verify signature.\n',
            b'\r\n'.join(DETERMINISTIC_SIGNATURE_DIAGNOSTICS) + b'\r\n')
        with mock.patch.object(learning_engine.subprocess, 'run', return_value=completed):
            self.assertFalse(authority.verify_owner_record(self.project, self.record, b'bad-signature'))

    @requirements('P9b-1', why_not_scenario='Exact stdout and every stderr line are required for a definitive invalid verdict.')
    def test_rejection_requires_exact_stdout_and_only_allowlisted_stderr_lines(self):
        authority = OwnerGrantAuthority(self.state.allowed_signers_path, raise_verifier_failures=True)
        rejection = b'Could not verify signature.\n'
        outcomes = [(stdout, diagnostic + b'\n')
                    for diagnostic in DETERMINISTIC_SIGNATURE_DIAGNOSTICS
                    for stdout in (b'', b' Could not verify signature.\n', rejection + b'extra\n',
                                   rejection + b'\n')]
        outcomes += [(rejection, diagnostic)
                     for diagnostic in (b"Couldn't parse signature: invalid format\n",
                         b'invalid format\n', b"Couldn't verify signature: invalid format extra\n",
                         b"couldn't verify signature: invalid format\n",
                         b" Couldn't verify signature: invalid format\n",
                         b"Couldn't verify signature: invalid format\n\n")]
        for stdout, stderr in outcomes:
            with self.subTest(stdout=stdout, stderr=stderr):
                completed = subprocess.CompletedProcess(['ssh-keygen'], 255, stdout, stderr)
                with mock.patch.object(learning_engine.subprocess, 'run', return_value=completed):
                    with self.assertRaises(OSError):
                        authority.verify_owner_record(self.project, self.record, b'bad-signature')

    @requirements('P9b-1', why_not_scenario='Transient and unknown stderr must leave real signed rows unchecked until an unmocked retry.')
    def test_unallowlisted_stderr_is_not_cached_and_recovers_without_restart(self):
        diagnostics = (b'memory allocation failed', b'libcrypto error: x', b'error in libcrypto',
                       b'Unable to open allowed keys file F: Too many open files',
                       b'Unable to open allowed keys file F: Input/output error',
                       b"Couldn't read signature file: Input/output error", b'unknown extra line')
        for number, diagnostic in enumerate(diagnostics):
            for mixed in (False, True):
                with self.subTest(diagnostic=diagnostic, mixed=mixed):
                    record = self.attest(account='0123456789bcdef'[2 * number + int(mixed)])
                    stderr = ((DETERMINISTIC_SIGNATURE_DIAGNOSTICS[0] + b'\n') if mixed else b'')
                    completed = subprocess.CompletedProcess(['ssh-keygen'], 255,
                        b'Could not verify signature.\n', stderr + diagnostic + b'\n')
                    with mock.patch.object(learning_engine.subprocess, 'run', return_value=completed):
                        self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
                        self.assertIsNone(self.ledger._connection.execute(
                            'SELECT valid FROM temp.attestation_checks WHERE record_id=?',
                            (record['record_id'],)).fetchone())
                    self.assert_unchecked_then_recovered(record)

    @requirements('P9b-1', why_not_scenario='Infrastructure diagnostics override rejection text; ambiguous results remain unchecked and retryable.')
    def test_infrastructure_and_ambiguous_results_raise_and_leave_rows_unchecked(self):
        authority = OwnerGrantAuthority(self.state.allowed_signers_path, raise_verifier_failures=True)
        rejection = b'Could not verify signature.\n'
        infrastructure = [b'Cannot open signer', b'No such file or directory', b'Permission denied',
                          b'Cannot allocate memory', b'Out of memory', b'Internal error', b'Killed by signal 9']
        outcomes = [subprocess.CompletedProcess(['ssh-keygen'], 255, rejection,
                    b"Couldn't parse signature: invalid format\n" + diagnostic) for diagnostic in infrastructure]
        outcomes += [subprocess.CompletedProcess(['ssh-keygen'], code, rejection, b'invalid format')
                     for code in (-15, -9, 1, 2, 137)]
        outcomes += [subprocess.CompletedProcess(['ssh-keygen'], 255, stdout, b'unknown diagnostic')
                     for stdout in (b'', b' Could not verify signature.\n', rejection + b'extra\n')]
        outcomes.append(subprocess.TimeoutExpired(['ssh-keygen'], 5))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                patch = ({'side_effect': outcome} if isinstance(outcome, Exception)
                         else {'return_value': outcome})
                with mock.patch.object(learning_engine.subprocess, 'run', **patch):
                    with self.assertRaises((OSError, subprocess.SubprocessError)):
                        authority.verify_owner_record(self.project, self.record, b'bad-signature')
                    self.assertEqual(0, self.ledger.sweep_superseded_attestations(batch=100))
                self.assertIsNone(self.ledger._connection.execute(
                    'SELECT valid FROM temp.attestation_checks WHERE record_id=?',
                    (self.record['record_id'],)).fetchone())
                self.assertIn(self.record['record_id'], self.capture_ids())
        self.assert_unchecked_then_recovered(self.record)

    @requirements('P9b-2', why_not_scenario='Inspect the actual admission union plan and count rows entering its final sort on overflowing history.')
    def test_admission_union_uses_indexes_and_bounds_rows_before_final_sort(self):
        connection = self.ledger._connection
        original = dict(connection.execute('SELECT * FROM records WHERE record_id=?',
                                           (self.record['record_id'],)).fetchone())
        # Interleave both union branches with unrelated records so the bounded
        # result must retain the exact rowid prefix across branch boundaries.
        connection.executemany('INSERT INTO records(' + ','.join(original) + ') VALUES(' +
                               ','.join('?' for _ in original) + ')',
            [tuple(dict(original, record_id='union-' + str(i),
                        record_type=('billing_attestation', 'provider_identity', 'event')[i % 3],
                        source_sequence=i + 2).values()) for i in range(3000)])
        scope = {'p': self.project, 'provider': 'codex', 'a': 1, 'limit': 3}
        # An always-true predicate counts every union row reaching the final
        # joins/sort. A SELECT expression would only count top-N insertions.
        query = 'SELECT r.rowid,a.signature' + account_service._ADMISSION_INPUT_JOIN.replace(
            'WHERE r.rowid=c.record_rowid',
            'WHERE r.rowid=c.record_rowid AND p9b_sort_input(c.record_rowid)')
        sort_inputs = []
        connection.create_function('p9b_sort_input', 1, lambda rowid: sort_inputs.append(rowid) or rowid)
        self.addCleanup(lambda: connection.create_function('p9b_sort_input', 1, None))
        plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + query, scope)]
        self.assertTrue(any(detail.startswith('SEARCH ') and 'capture_candidates USING' in detail
                            for detail in plan), plan)
        # U3: the non-attestation evidence branch seeks its windows through the
        # record type/time index (was the record type/project index).
        self.assertTrue(any(detail.startswith('SEARCH records USING') and 'records_type_observed' in detail
                            for detail in plan), plan)
        self.assertFalse(any(detail.startswith('SCAN r') or detail.startswith('SCAN records')
                             for detail in plan), plan)
        # Count predicate evaluations before SQLite's final ORDER BY/LIMIT
        # discards overflow.
        rows = connection.execute(query, scope).fetchall()
        expected = connection.execute("SELECT rowid FROM records WHERE project_id=? AND record_type IN "
            "('billing_attestation','provider_identity') ORDER BY rowid LIMIT ?",
            (self.project, scope['limit'])).fetchall()
        self.assertEqual([row[0] for row in expected], [row[0] for row in rows])
        self.assertGreaterEqual(len(sort_inputs), scope['limit'])
        self.assertLessEqual(len(sort_inputs), 2 * scope['limit'])

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
