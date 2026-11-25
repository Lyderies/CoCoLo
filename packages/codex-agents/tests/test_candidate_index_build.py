"""AP3: fast first-open and actual candidate-query plan checks.

The large disposable-ledger measurement lives in scripts/measure_candidate_index.py.
"""
import unittest

from test_framework import requirements
from tests.test_candidate_evidence_lookup import CandidateEvidenceLookupTests


class CandidateIndexBuildTests(CandidateEvidenceLookupTests):
    @requirements('AP3', why_not_scenario='Small durable ledger fixture; first open and actual SQLite query plans.')
    def test_AP3_first_open_builds_index_used_by_candidate_lookups(self):
        connection = self.ledger._connection
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='records_project_digest'").fetchone())
        self.assertEqual(['project_id', 'content_digest'], [row['name'] for row in
                         connection.execute('PRAGMA index_info(records_project_digest)')])
        proposed, _, _ = self.mixed()
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            self.ledger._validate_evidence_links_locked(self.project, proposed)
        finally:
            connection.set_trace_callback(None)
        # Explain the actual executed SQL, including the invalidation outcome
        # query; a separately written exemplar could hide a production scan.
        lookups = [sql for sql in statements if 'content_digest IN (' in sql]
        self.assertEqual(2, len(lookups), statements)
        for sql in lookups:
            with self.subTest(sql=sql):
                plan = [row['detail'] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql)]
                self.assertTrue(any('SEARCH records USING INDEX records_project_digest' in detail
                                    and 'project_id=? AND content_digest=?' in detail for detail in plan), plan)
                self.assertFalse(any('SCAN records' in detail for detail in plan), plan)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([CandidateIndexBuildTests(
        'test_AP3_first_open_builds_index_used_by_candidate_lookups')])


if __name__ == '__main__':
    unittest.main()
