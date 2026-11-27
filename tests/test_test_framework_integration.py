"""First full-suite integration witnesses (TF-23/24)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests as runner  # noqa: E402
from test_framework import requirements  # noqa: E402

class ReleaseIntegration(unittest.TestCase):
    __why_not_scenario__ = 'Runner integration uses disposable discovery packages and explicit policy controls.'

    @requirements('TF-24', 'TF-1')
    def test_discovery_imports_framework_and_preserves_package_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'tests').mkdir()
            (root/'tests/__init__.py').write_text('')
            (root/'tests/test_valid.py').write_text('import unittest\nclass Valid(unittest.TestCase):\n def test_ok(self): pass\n')
            (root/'tests/test_framework_user.py').write_text('from test_framework import requirements\nimport unittest\nclass Valid(unittest.TestCase):\n @requirements("TF-24")\n def test_ok(self): pass\n')
            (root/'tests/test_broken.py').write_text('raise ImportError("deliberate discovery control")')
            with mock.patch.dict(runner.SUITES, {'fixture':root}), mock.patch.object(runner, '_CURRENT_DISCOVERY', None):
                found = runner.discover('fixture')
            self.assertEqual(['tests.test_valid.Valid.test_ok'], found['tests.test_valid'])
            self.assertEqual(['tests.test_framework_user.Valid.test_ok'], found.get('tests.test_framework_user'), 'framework import failed during discovery')
            self.assertIn('tests.test_broken', found, 'failed import lost qualified module name')
            self.assertNotIn('test_broken', found)

    @requirements('TF-24', 'TF-10', 'TF-12')
    def test_existing_resource_modules_have_correct_layers_and_reasons(self):
        layers=json.loads((ROOT/'scripts/test-layers.json').read_text())
        stages=json.loads((ROOT/'scripts/test-stages.json').read_text())
        expected={'claude:tests.test_journal':'component','claude:tests.test_job_read_tool':'component',
                  'codex:tests.test_service_queue':'component','codex:tests.test_control_loop':'scenario',
                  'root:test_setup_wizard_effects':'scenario'}
        self.assertEqual('unit',layers['root:test_check_identifiers'])
        for name,layer in expected.items():
            with self.subTest(module=name):
                self.assertEqual(layer,layers[name],'existing resource module misclassified: '+name)
                self.assertEqual('integration',stages[name])
        reasons=ROOT/'scripts/test-layer-reasons.json'
        self.assertTrue(reasons.exists(),'resource reclassification lacks why_not reasons')
        reasons=json.loads(reasons.read_text())
        for name in expected:
            self.assertTrue(reasons[name]['why_not_scenario'].strip())

    @requirements('TF-24','TF-8')
    def test_exclusive_campaigns_wait_for_empty_pool_and_block_other_launches(self):
        can=getattr(runner,'can_launch',None)
        self.assertTrue(callable(can),'missing exclusive campaign scheduling')
        campaign='codex:tests.test_merge_library_contract'
        ordinary='root:test_test_stages'
        self.assertTrue(can(ordinary,[],16))
        self.assertTrue(can(ordinary,[ordinary],16))
        self.assertTrue(can(campaign,[],16))
        self.assertFalse(can(campaign,[ordinary],16))
        self.assertFalse(can(ordinary,[campaign],16))
        self.assertFalse(can(campaign,[campaign],16))
        self.assertFalse(can(ordinary,[ordinary]*16,16))
        with mock.patch.object(runner,'discover',return_value={'tests.test_merge_library_contract':['A.test_a','A.test_b']}):
            planned=runner.units('codex',{campaign:140})
        self.assertEqual(1,len(planned),'campaign fragments multiply fixture contention')
        self.assertEqual(['tests.test_merge_library_contract'],planned[0][2])




