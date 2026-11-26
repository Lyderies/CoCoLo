"""GEN-1..3: the requirement catalogue generator is a no-op on the committed tree."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements

COMMITTED = ROOT / 'scripts/test-requirements.json'


def generate(directory, name):
    out = Path(directory) / name
    subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/generate_test_metadata.py'), '--catalogue', '--output', str(out)],
                   cwd=ROOT / 'scripts', check=True, capture_output=True, timeout=120)
    return out.read_bytes()


class GeneratorTests(unittest.TestCase):
    __why_not_scenario__ = 'The generator is exercised as a subprocess against the committed catalogue; no product flow is involved.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.first, cls.second = generate(cls.temp.name, 'a.json'), generate(cls.temp.name, 'b.json')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @requirements('GEN-1')
    def test_regenerating_is_a_noop_and_keeps_hand_registered_checks(self):
        self.assertEqual(COMMITTED.read_bytes(), self.first)
        catalogue = json.loads(self.first)
        for name in ('L14.1', 'L15.1'):
            self.assertTrue(any(c.startswith('codex:') and '_' + name.replace('.', '_') + '_' in c for c in catalogue[name]['checks']), name)

    @requirements('GEN-1')
    def test_L8_3_hand_registered_check_is_preserved(self):
        catalogue = json.loads(self.first)
        self.assertIn('codex:tests.test_refusal_steps.RefusalStepTests.test_L8_3_an_unfinished_orchestration_close_names_the_runs_status_not_a_service_restart',
                      catalogue['L8.3']['checks'])

    @requirements('GEN-2')
    def test_output_is_deterministic_and_sorted(self):
        self.assertEqual(self.first, self.second)
        catalogue = json.loads(self.first)
        self.assertEqual(sorted(catalogue), list(catalogue))
        for row in catalogue.values():
            self.assertEqual(sorted(row['checks']), row['checks'])
        self.assertEqual(json.dumps(catalogue, indent=1, sort_keys=True) + '\n', self.first.decode())

    @requirements('GEN-3')
    def test_requirement_json_sources_are_included(self):
        catalogue = json.loads(self.first)
        seen = 0
        for path in sorted((ROOT / 'tests/requirements').glob('*.json')):
            for row in json.loads(path.read_text())['requirements']:
                seen += 1
                self.assertEqual({'statement': row['statement'], 'source': 'tests/requirements/' + path.name + '#' + row['id']},
                                 {k: catalogue[row['id']][k] for k in ('statement', 'source')})
        self.assertGreater(seen, 50)
        for name in ('AP3', 'S15-B0-2'):
            self.assertIn(name, catalogue)


if __name__ == '__main__':
    unittest.main()
