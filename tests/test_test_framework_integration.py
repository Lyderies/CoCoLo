"""First full-suite integration witnesses (TF-23/24)."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests as runner
import test_framework as fw
from test_framework import requirements

class ReleaseIntegration(unittest.TestCase):
    __why_not_scenario__ = 'Runner integration uses disposable discovery packages and explicit policy controls.'

    @requirements('TF-24', 'TF-1')
    def test_discovery_imports_framework_and_preserves_package_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'tests').mkdir(); (root/'tests/__init__.py').write_text('')
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
        for name in expected:self.assertTrue(reasons[name]['why_not_scenario'].strip())

    @requirements('TF-24','TF-8')
    def test_exclusive_campaigns_wait_for_empty_pool_and_block_other_launches(self):
        can=getattr(runner,'can_launch',None)
        self.assertTrue(callable(can),'missing exclusive campaign scheduling')
        campaign='codex:tests.test_merge_library_contract'
        ordinary='root:test_test_stages'
        self.assertTrue(can(ordinary,[],16));self.assertTrue(can(ordinary,[ordinary],16))
        self.assertTrue(can(campaign,[],16))
        self.assertFalse(can(campaign,[ordinary],16))
        self.assertFalse(can(ordinary,[campaign],16))
        self.assertFalse(can(campaign,[campaign],16))
        self.assertFalse(can(ordinary,[ordinary]*16,16))
        with mock.patch.object(runner,'discover',return_value={'tests.test_merge_library_contract':['A.test_a','A.test_b']}):
            planned=runner.units('codex',{campaign:140})
        self.assertEqual(1,len(planned),'campaign fragments multiply fixture contention')
        self.assertEqual(['tests.test_merge_library_contract'],planned[0][2])

    @requirements('TF-24','TF-7')
    def test_release_overlay_is_immutable_and_candidate_changes_are_rejected(self):
        apply=getattr(fw,'release_legacy',None)
        self.assertTrue(callable(apply),'missing reviewed release baseline version')
        old={'baseline':'old','tests':{'old.test':'before'}}
        release={'source_revision':'reviewed','tests':{'old.test':{'fingerprint':'after'},'new.test':{'fingerprint':'new'}}}
        current,ceiling,previous=apply(old,old,old,release)
        self.assertEqual({'old.test':'after','new.test':'new'},current['tests'])
        self.assertEqual({'old.test':'before'},old['tests'])
        records={k:dict(v,requirements=[],rationale='') for k,v in release['tests'].items()}
        args=dict(inventory={'x':records},stages={'x':'unit'},layers={'x':'unit'},catalogue={},legacy=current,ceiling=ceiling,previous=previous)
        self.assertEqual([],fw.validate_metadata(**args))
        records['new.test']['fingerprint']='candidate edit'
        self.assertIn('new.test: legacy implementation changed; tag it',fw.validate_metadata(**args))
        records['untagged.test']=dict(fingerprint='added',requirements=[],rationale='')
        self.assertIn('untagged.test: no requirement',fw.validate_metadata(**args))

    @requirements('TF-24','TF-8')
    def test_versioned_debt_has_measured_ceilings_and_cannot_grow(self):
        path=ROOT/'scripts/test-budget-debt-0850.json'
        self.assertTrue(path.exists(),'missing measured release debt version')
        debt=json.loads(path.read_text())
        for name in ('billing_gate','billing_rejudge','ledger_startup','session_guard'):
            self.assertGreater(debt['modules']['codex:tests.test_'+name],fw.BUDGETS['unit' if name in ('ledger_startup','session_guard') else 'scenario'])
        self.assertFalse(any('merge_library' in name for name in debt['modules']))
        self.assertNotIn('codex:tests.test_cli_billing_runtime',debt['modules'])
        self.assertNotIn('codex:tests.test_run_network',debt['modules'])
        self.assertEqual([],fw.validate_budget_debt(debt,debt,debt))
        bad=copy.deepcopy(debt);bad['modules']['new']=1
        self.assertTrue(fw.validate_budget_debt(bad,debt,debt))
        bad=copy.deepcopy(debt);name=next(iter(bad['modules']));bad['modules'][name]+=1
        self.assertTrue(fw.validate_budget_debt(bad,debt,debt))

    @requirements('TF-24','TF-7')
    def test_explicit_base_is_documented_and_missing_base_stops_before_discovery(self):
        base='b531844030fa619c1f62ddd6c092ebc2ea4fca38'
        self.assertIn('--base '+base,(ROOT/'TESTING-framework.md').read_text(),'local full-run base is unspecified')
        bases=[line.split('TRUSTED_BASE: ',1)[1] for line in (ROOT/'.github/workflows/ci.yml').read_text().splitlines() if 'TRUSTED_BASE: ' in line]
        self.assertTrue(bases)
        self.assertTrue(all(value == base for value in bases),'CI release base is stale')
        with mock.patch.object(runner,'metadata',side_effect=AssertionError('discovery ran without trusted base')), mock.patch.object(runner,'persist_record'):
            self.assertEqual(1,runner.main([]))
