"""O10 AP5 (with U9): a lookup of one record type reads only that type (PLAN AP5-1 to AP5-4;
DESIGN-0.4.0-AP5-types.md).

Integration stage: the ledger fixture. Checked by SQLite VM steps on a read-only connection as a
project gains 20,000 events, not by timing.
"""
import ast
import sqlite3
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT
from tests.test_append_lookup import AppendLookupTests

TYPE_LOOKUP = "SELECT content FROM records WHERE project_id=? AND record_type='outcome'"
GRANT_TYPES = {'candidate', 'promotion', 'evaluation_case', 'comparison', 'canary_report'}


class TypeLookupTests(AppendLookupTests):
    def check_steps(self):
        from codex_agents.admission_evidence_schema import check_admission_evidence_coverage
        reader = sqlite3.connect(self.store.state.database.as_uri() + '?mode=ro', uri=True)
        try:
            counted = [0]

            def tick():
                counted[0] += 1
                return 0
            reader.set_progress_handler(tick, 1)
            self.assertTrue(check_admission_evidence_coverage(reader))
            return counted[0]
        finally:
            reader.close()

    def test_AP5_1_a_type_lookup_reads_only_its_type_as_events_grow(self):
        self.fill(1_000)
        small = self.steps(TYPE_LOOKUP, (self.project,))
        control_small = self.steps(TYPE_LOOKUP.replace('FROM records', 'FROM records NOT INDEXED'), (self.project,))
        self.fill(21_000)
        large = self.steps(TYPE_LOOKUP, (self.project,))
        control_large = self.steps(TYPE_LOOKUP.replace('FROM records', 'FROM records NOT INDEXED'), (self.project,))
        self.assertGreater(control_large, 5 * control_small)  # without an index it reads every record
        self.assertLess(large, 2 * small + 200, (small, large))

    def test_U9_the_start_up_check_reads_only_evidence_as_events_grow(self):
        self.fill(1_000)
        small = self.check_steps()
        self.fill(21_000)
        large = self.check_steps()
        self.assertLess(large, 2 * small + 500, (small, large))

    def test_AP5_2_the_grant_check_reads_only_the_five_types_it_uses(self):
        tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / 'learning_store.py').read_text())
        check = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                     and f.name == '_validate_granted_transition_locked')
        texts = [node.value for node in ast.walk(check) if isinstance(node, ast.Constant) and type(node.value) is str]
        wide = [text for text in texts if 'FROM records WHERE project_id=?' in text and 'record_type' not in text]
        self.assertEqual([], wide)  # no statement reads every record of the project
        named = {kind for text in texts for kind in GRANT_TYPES if f"'{kind}'" in text}
        self.assertEqual(GRANT_TYPES, named)

    def test_AP5_4_a_ledger_without_the_index_gets_it_on_open_and_nothing_else_changes(self):
        self.reopen()
        connection = self.ledger._connection
        connection.execute('DROP INDEX IF EXISTS records_type_project')

        def schema():
            return sorted(tuple(row) for row in self.ledger._connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name != 'records_type_project'"))
        before, version = schema(), connection.execute('PRAGMA user_version').fetchone()[0]
        self.reopen()
        names = {row[0] for row in self.ledger._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='records'")}
        self.assertIn('records_type_project', names)
        self.assertEqual(version, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, schema())


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited AP tests run in test_append_lookup."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(TypeLookupTests):
        if name in TypeLookupTests.__dict__:
            suite.addTest(TypeLookupTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
