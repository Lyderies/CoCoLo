"""Step-1 review r1 witnesses; requirements precede these rejection fixtures."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements


@requirements(why_not_scenario='Metadata rejection and helper-change controls cannot be established by product lifecycle scenarios.')
class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.fw = importlib.import_module('test_framework')

    @requirements('TF-14', why_not_scenario='Framework rejection witness independent of product scenarios.')
    def test_direct_golden_read_and_assert_bypass_is_rejected(self):
        source = '''from pathlib import Path
GOLDEN_ROOT = Path('tests/goldens')
def test_output(self):
    expected = (GOLDEN_ROOT / 'output.txt').read_text()
    self.assertEqual('wrong reason', expected)
'''
        self.assertTrue(self.fw.golden_bypasses(source))
        approved = "def test_output(self):\n    compare(root, 'output.txt', output, requirement='TF-14', declared={'TF-14'}, known={'TF-14'})\n"
        self.assertEqual([], self.fw.golden_bypasses(approved))
        self.assertEqual([], self.fw.golden_bypasses('def test_value(self):\n    self.assertEqual(1, 1)\n'))

    @requirements('TF-14', why_not_scenario='Helper contract fixtures must inspect temporary writes independently.')
    def test_temporary_helper_output_and_unrelated_owner_files_are_not_repository_goldens(self):
        source = """import tempfile
from pathlib import Path
class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'goldens'
    def test_update(self):
        self.assertEqual('TF-14', (self.root / 'value.requirement').read_text())
        outside = Path(self.temp.name) / 'owner'
        self.assertEqual('unchanged', outside.read_text())
"""
        self.assertEqual([], self.fw.golden_bypasses(source))

    @requirements('TF-4', why_not_scenario='Framework rejection witness independent of product scenarios.')
    def test_catalogue_statement_must_equal_its_exact_cited_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.json').write_text(json.dumps({'requirements': [{'id': 'TF-4', 'statement': 'Exact reason code.'}]}))
            catalogue = {'TF-4': {'statement': 'Exact reason code.', 'source': 'source.json#TF-4', 'checks': ['one']}}
            self.assertEqual([], self.fw.validate_sources(root, catalogue))
            catalogue['TF-4']['statement'] = 'Approximate reason code.'
            self.assertTrue(self.fw.validate_sources(root, catalogue))
            catalogue['TF-4']['source'] = 'missing.json#TF-4'
            self.assertTrue(self.fw.validate_sources(root, catalogue))





if __name__ == '__main__':
    unittest.main()
