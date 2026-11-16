"""Populated observation journals survive both independent restore verifiers."""
from copy import deepcopy
import json
import runpy
import sqlite3
import time
import unittest

from tests import test_account_observation as fixtures
from tests.support import REPOSITORY_ROOT
from tests.learning_support import backup_body
from codex_agents.learning_contracts import content_digest, validate_learning_record
from codex_agents.learning_store import LedgerError, _verify_project_backup_body


class AccountObservationBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountObservationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger, self.project = self.fixture.ledger, self.fixture.fixture.projects[0]
        committed = self.fixture.reserve('committed')
        self.fixture.owner.complete(committed, self.fixture.probe(committed))
        pending = self.fixture.reserve('unknown')
        self.fixture.owner.abort(pending)
        path = self.ledger.create_sanitized_backup(self.project, file_id='observations',
            expires_at_ms=time.time_ns() // 1_000_000 + 600_000)
        self.body = backup_body(path, self.project)
        self.tool = runpy.run_path(str(REPOSITORY_ROOT / 'scripts/backup_installed_project.py'))

    def standalone(self, body):
        self.tool['_verify_relational_restore'](self.ledger._connection, body,
            validate_learning_record=validate_learning_record, content_digest=content_digest)

    def test_both_verifiers_preserve_committed_and_unknown_original_requests(self):
        self.assertTrue(self.ledger.verify_sanitized_backup(self.project, file_id='observations')['verified'])
        self.standalone(self.body)
        self.assertEqual(14, self.body['ledger_schema_version'])
        self.assertEqual(2, self.body['counts']['account_observation_origins'])
        requests = self.body['tables']['account_observation_requests']
        self.assertEqual({'pending', 'committed'}, {row['state'] for row in requests})
        self.assertEqual(self.body['columns']['account_observation_requests'],
                         list(self.tool['_expected_columns'](13, 'account_observation_requests')))
        for row in requests:
            if row['state'] == 'committed':
                self.assertTrue(all(set(value) == {'record_id', 'content_digest'}
                                    for value in json.loads(row['references_json'])))

    def test_both_verifiers_refuse_missing_or_mismatched_original_companions(self):
        for table in ('account_observation_origins', 'account_observation_requests'):
            broken = deepcopy(self.body)
            broken['tables'][table].pop()
            broken['counts'][table] -= 1
            with self.subTest(table=table), self.assertRaises(LedgerError):
                _verify_project_backup_body(broken)
            with self.subTest(table=table), self.assertRaises((RuntimeError, sqlite3.IntegrityError)):
                self.standalone(broken)
        broken = deepcopy(self.body)
        broken['tables']['account_observation_origins'][0]['parameters_digest'] = 'f' * 64
        with self.assertRaises(LedgerError):
            _verify_project_backup_body(broken)
        with self.assertRaisesRegex(RuntimeError, 'observation coverage'):
            self.standalone(broken)
