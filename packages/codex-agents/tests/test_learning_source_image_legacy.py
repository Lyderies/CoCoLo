"""Actual signed source rows bind inert staging; no native or cutover authority."""
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from tests import test_learning_store as legacy
from tests.learning_support import event_record
from codex_agents import learning_store
from codex_agents import project_learning_corpus as corpus
from codex_agents import project_learning_qa as qa
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity
from codex_agents.sql_read_budget import SQLReadLimits


class LearningSourceImageLegacyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.test_operational_qa_supersedes_unverified_run_and_surfaces_no_owner_review()
        self.fixture.ledger.close()
        self.fixture.ledger = learning_store.LearningLedger(
            self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.source = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.key = self.fixture.state.digest_key()
        directory = Path(self.fixture.temporary.name) / 'image-target-history'
        directory.mkdir(mode=0o700)
        self.target = ProjectHistory(directory, self.project, result_key=self.key,
            result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000, 120_000),
            allow_schema_upgrade=True)
        self.addCleanup(self.target.close)
        self.domain = self.target.learning_key_domain_digest()
        self.backup = self.source.create_sanitized_backup(
            self.project, file_id='source_image', expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        self.body = json.loads(self.backup.read_bytes())['body']

    def capture(self, *, file_id='source_image', domain=None):
        return self.source._capture_learning_source_image(
            self.project, file_id,
            expected_domain_digest=self.domain if domain is None else domain,
            work_limits=SQLReadLimits(100_000, 1000, 2 * 1_048_576))

    def test_complete_original_image_matches_existing_owner_and_stays_read_only(self):
        before = learning_store._snapshot_project_partition(
            self.source._connection, self.project, ledger_schema_version=13)
        changes = self.source._connection.total_changes
        raw_backup = self.backup.read_bytes()
        key_bytes = self.fixture.state.key_path.read_bytes()
        read_key = self.fixture.state.digest_key

        def outside_snapshot():
            self.assertFalse(self.source._connection.in_transaction, 'key I/O under a source transaction')
            return read_key()

        with mock.patch.object(learning_store, '_snapshot_project_partition',
                               side_effect=AssertionError('unbounded current snapshot')), \
                mock.patch.object(self.fixture.state, 'digest_key', side_effect=outside_snapshot):
            image = self.capture()
            self.assertEqual(image, self.capture())
        self.assertEqual(30, len(image['counts']))
        self.assertEqual((23, 2, 1), tuple(dict(image['counts'])[n] for n in
                                         ('records', 'owner_attestations', 'operational_qa')))
        self.assertEqual(canonical_json(self.body), image['body'])
        self.assertEqual(self.domain, image['key_domain_digest'])
        metadata = {n: image[n] for n in ('project_id', 'ledger_schema_version',
                                        'backup_digest', 'manifest_digest', 'key_domain_digest')}
        self.assertEqual(keyed_digest(self.key, b'learning-source-image-v1\0' + canonical_json(metadata)),
                         image['source_identity_digest'])
        self.assertEqual(hashlib.sha256(raw_backup).hexdigest(), image['backup_digest'])
        self.assertEqual(content_digest(self.body), image['manifest_digest'])
        self.assertIs(False, image['learning_ready'])
        self.assertEqual(changes, self.source._connection.total_changes)
        self.assertFalse(self.source._connection.in_transaction)
        self.assertEqual(before, learning_store._snapshot_project_partition(
            self.source._connection, self.project, ledger_schema_version=13))
        self.assertEqual(raw_backup, self.backup.read_bytes())
        self.assertEqual(key_bytes, self.fixture.state.key_path.read_bytes())

    def test_central_original_custody_change_refuses_despite_valid_backup_and_same_staged_subsets(self):
        self.source._connection.execute('UPDATE run_provenance SET created_at_ms=created_at_ms+1 '
                                        'WHERE project_id=?', (self.project,))
        self.assertTrue(self.source.verify_sanitized_backup(self.project, file_id='source_image')['verified'])
        current = learning_store._snapshot_project_partition(
            self.source._connection, self.project, ledger_schema_version=13)
        for table in ('records', 'owner_attestations', 'operational_qa'):
            self.assertEqual(self.body['tables'][table], current['tables'][table])
        changes = self.source._connection.total_changes
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.capture()
        self.assertEqual(changes, self.source._connection.total_changes)
        self.assertFalse(self.source._connection.in_transaction)
        self.assertEqual(0, self.target._connection.execute(
            'SELECT COUNT(*) FROM project_learning_imports').fetchone()[0])

    def test_other_project_history_changes_do_not_change_this_source_identity(self):
        first = self.capture()
        self.source.append(self.fixture.project_b_id, event_record('other-project-image', observed_at_ms=1),
                           producer='hook', now_ms=1)
        self.assertEqual(first, self.capture())

    def test_wrong_existing_target_key_domain_refuses_without_staging_or_key_changes(self):
        changes = self.source._connection.total_changes
        original = self.fixture.state.key_path.read_bytes()
        wrong = keyed_digest(b'x' * 32, b'project-learning-key-domain-v1\0' + self.project.encode('ascii'))
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.capture(domain=wrong)
        self.assertEqual(changes, self.source._connection.total_changes)
        self.assertEqual(original, self.fixture.state.key_path.read_bytes())

    def test_only_the_exact_self_registration_is_excluded(self):
        second = self.source.create_sanitized_backup(
            self.project, file_id='source_image_second',
            expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        with self.assertRaisesRegex(HistoryError, '^history_conflict$'):
            self.capture()
        image = self.capture(file_id='source_image_second')
        self.assertEqual(canonical_json(json.loads(second.read_bytes())['body']), image['body'])
        self.assertTrue(any(row['file_id'] == 'source_image'
                            for row in json.loads(image['body'])['tables']['stored_files']))
        self.assertTrue(self.backup.exists())

    def test_existing_unicode_backup_identifiers_keep_their_original_file_and_body(self):
        identifier = 'source_image_\u00e9'
        path = self.source.create_sanitized_backup(
            self.project, file_id=identifier, expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        raw = path.read_bytes()
        image = self.capture(file_id=identifier)
        self.assertEqual(canonical_json(json.loads(raw)['body']), image['body'])
        self.assertEqual(raw, path.read_bytes())
        self.assertIs(False, image['learning_ready'])

    def test_real_complete_source_identity_binds_record_signature_and_qa_staging(self):
        image = self.capture()
        body = json.loads(image['body'])
        decode = learning_store._backup_decode_value
        records = [{n: decode(row[n]) for n in corpus._ENTRY_FIELDS - {'late'}} | {'late': bool(row['late'])}
                   for row in sorted(body['tables']['records'], key=lambda r: r['source_sequence'])]
        signatures = [{'subject_record_id': row['subject_record_id'], 'signature': decode(row['signature']),
                       'observed_at_ms': row['attested_at_ms']}
                      for row in sorted(body['tables']['owner_attestations'],
                                        key=lambda r: r['subject_record_id'], reverse=True)]
        self.assertEqual((23, 2), (len(records), len(signatures)))
        self.assertTrue(all(json.loads(row['content'])['record_type'] in corpus._RECORD_TYPES for row in records))
        digest = corpus.chunk_digest(records, signatures)
        manifest = {'schema_version': 1, 'scope': 'learning-record-staging-v1', 'project_id': self.project,
                    'source_identity_digest': image['source_identity_digest'], 'key_domain_digest': self.domain,
                    'record_count': len(records), 'attestation_count': len(signatures),
                    'total_bytes': sum(len(r['content']) for r in records) + sum(len(r['signature']) for r in signatures),
                    'chain_digest': corpus.next_chain_digest(corpus.EMPTY_CHAIN_DIGEST, digest)}
        self.target.begin_learning_import('image', manifest, now_ms=1000, before_commit=lambda: None)
        original = self.target.stage_learning_import_chunk('image', 0, records, signatures,
                                                           now_ms=1001, before_commit=lambda: None)
        self.assertIs(False, self.target.finish_learning_import(
            'image', now_ms=1002, before_commit=lambda: None)['learning_ready'])
        rows = [{n: decode(row[n]) for n in ('qa_id', 'qa_digest', 'host_outcome_digest',
                 'verifier_host_outcome_digest', 'accepted', 'observed_at_ms', 'content')}
                for row in sorted(body['tables']['operational_qa'], key=lambda r: r['qa_id'])]
        self.assertEqual(1, len(rows))
        qa_manifest = {'schema_version': 1, 'scope': 'learning-operational-qa-staging-v1',
                       'project_id': self.project, 'records_manifest_digest': content_digest(manifest),
                       'source_identity_digest': image['source_identity_digest'], 'key_domain_digest': self.domain,
                       'qa_count': len(rows), 'total_bytes': sum(len(r['content']) for r in rows),
                       'chain_digest': qa.qa_chain_digest(qa.ZERO, qa.qa_chunk_digest(rows))}
        self.target.begin_learning_qa_import('image', qa_manifest, now_ms=1100, before_commit=lambda: None)
        self.target.stage_learning_qa_import_chunk('image', 0, rows, now_ms=1101, before_commit=lambda: None)
        self.assertIs(False, self.target.finish_learning_qa_import(
            'image', now_ms=1102, before_commit=lambda: None)['learning_ready'])
        restored = self.target.learning_import_chunk('image', 0)
        self.assertEqual((tuple(records), tuple(signatures), original),
                         (restored['records'], restored['attestations'], restored['receipt']))
        self.assertEqual(tuple(rows), self.target.learning_import_qa('image'))
        feedback = next(json.loads(r['content']) for r in records
                        if json.loads(r['content'])['record_type'] == 'feedback')
        signature = next(r['signature'] for r in signatures if r['subject_record_id'] == feedback['record_id'])
        authority = OwnerGrantAuthority(self.fixture.state.owner_allowed_signers())
        self.assertTrue(authority.verify_owner_record(self.project, feedback, signature))
        self.assertEqual(image, self.capture())
        self.assertIs(False, restored['receipt']['learning_ready'])
        self.assertEqual(0, self.target._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        with self.assertRaisesRegex(HistoryError, '^history_not_found$'):
            self.target.record(feedback['record_id'])
