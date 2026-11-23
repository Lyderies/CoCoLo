"""Decision 99 A: scoped round-2 MED rejection witnesses, with positive controls."""
import copy
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
import test_framework as fw
import parallel_tests as runner


class RoundTwo(unittest.TestCase):
    __requirements__ = ('TF-4', 'TF-5', 'TF-7', 'TF-8', 'TF-9', 'TF-14')
    __why_not_scenario__ = 'Controlled corrupt metadata and dependency graphs must be rejected before product scenarios can run.'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def module(self, name, source):
        path = self.root / (name + '.py'); path.write_text(source)
        module = types.ModuleType(name); module.__file__ = str(path)
        sys.modules[name] = module; self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, str(path), 'exec'), module.__dict__)
        return module

    def test_M1_unrelated_source_cannot_resolve_a_specific_dependency(self):
        module = self.module('test_binding', 'import unittest\nOPAQUE = object()\nclass Case(unittest.TestCase):\n    def helper(self): return 1\n    def test_value(self): self.assertTrue(OPAQUE); self.assertEqual(1, getattr(self,"helper")())\n')
        unrelated = self.root / 'unrelated.py'; unrelated.write_text('VALUE = 1\n')
        case = module.Case('test_value')
        self.assertIsNone(fw.dependency_fingerprint(case))
        self.assertIsNone(fw.dependency_fingerprint(case, source_dependencies=(unrelated,)), 'unrelated source restored unresolved dependency eligibility')
        self.assertIsNotNone(fw.dependency_fingerprint(case, source_dependencies=(module.__file__,)))

    def test_N1_same_basename_dependencies_both_contribute(self):
        module = self.module('test_files', 'import unittest\nclass Case(unittest.TestCase):\n    def test_value(self): self.assertEqual(1,1)\n')
        paths = [self.root / part / 'fixture.py' for part in ('a','b')]
        for path in paths: path.parent.mkdir(); path.write_text('VALUE = 1\n')
        def fingerprint(): return fw.dependency_fingerprint(module.Case('test_value'), source_dependencies=paths)
        initial = fingerprint(); self.assertEqual(initial, fingerprint())
        for path in paths:
            path.write_text('VALUE = 2\n')
            self.assertNotEqual(initial, fingerprint(), 'explicit dependency overwritten: ' + path.parent.name)
            path.write_text('VALUE = 1\n')
            self.assertEqual(initial, fingerprint())

    def test_M2_qualified_and_renamed_imports_retain_golden_provenance(self):
        (self.root / 'fixtures.py').write_text('from pathlib import Path\nGOLDEN_PATH = Path("goldens/result")\nEXPECTED = GOLDEN_PATH\nORDINARY = Path("input.txt")\n')
        for source in ('import fixtures\nactual = fixtures.GOLDEN_PATH.read_text()',
                       'from fixtures import EXPECTED as reference\nactual = reference.read_text()',
                       'import fixtures as f\nactual = f.EXPECTED.read_bytes()'):
            errors = fw.golden_bypasses(source, filename=self.root/'test_consumer.py')
            self.assertTrue(any('direct golden' in e for e in errors), 'imported golden read escaped: ' + source)
        self.assertEqual([], fw.golden_bypasses('from fixtures import ORDINARY\nactual = ORDINARY.read_text()', filename=self.root/'test_consumer.py'))

    def test_M3_no_report_or_empty_plan_cannot_authorize_retirement(self):
        row = dict(suite='root', unit='m#0', seconds=.1, names=['m.a'], returncode=0)
        options = dict(layers={'root:m':'unit'}, stages={'root:m':'unit'}, jobs=16, elapsed=.1, selection=['root:m'], budget_debt={'modules':{'root:m':10}})
        for plan in (None, {}, {'root:m':['m#0','m#1']}):
            self.assertEqual([], fw.measurements([row], expected_fragments=plan, **options)['debt_retirement_required'], 'missing authoritative fragment authorized retirement')
        self.assertEqual(['root:m'], fw.measurements([row], expected_fragments={'root:m':['m#0']}, **options)['debt_retirement_required'])

    def records(self):
        layers={'root:u':'unit','root:i':'component','root:e':'scenario'}
        stages={'root:u':'unit','root:i':'integration','root:e':'e2e'}
        checks=['root:u.a','root:i.a','root:i.b','root:e.a']; catalogue={'TF-5':{'checks':checks}}
        records=[]
        for module, case, part in [('u','a',(1,1)),('i','a',(1,2)),('i','b',(2,2)),('e','a',(1,1))]:
            unit=module+('#'+case if module=='i' else '')
            row=dict(suite='root',unit=unit,seconds=.1,names=[module+'.'+case],returncode=0,outcomes={'root:'+module+'.'+case:'passed'})
            plan={'root:'+module: ['i#a','i#b'] if module=='i' else [module]}
            records.append(fw.measurements([row], layers, stages, jobs=16, elapsed=.1, selection=['root:'+module], catalogue=catalogue, part=part, invocation_id='r2', expected_fragments=plan))
        return records

    def test_M4_real_gate_rejects_not_run_failed_and_incomplete_evidence(self):
        records=self.records()
        self.assertEqual({'TF-5':'passed'}, fw.combine_run_records(records)['requirements'])
        for status in ('not_run','failed'):
            altered=copy.deepcopy(records); altered[0]['workers'][0]['outcomes']['root:u.a']=status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'requirements failed/not_run.*TF-5'):
                fw.combine_run_records(altered)
        with self.assertRaisesRegex(ValueError, 'missing or repeated shards'):
            fw.combine_run_records(records[:2]+records[3:])
        paths=[]
        for i,record in enumerate(records):
            path=self.root/(str(i)+'.json'); path.write_text(json.dumps(record)); paths.append(str(path))
        self.assertEqual(0,runner.main(['--requirement-gate',*paths]))
        altered=copy.deepcopy(records[0]); altered['workers'][0]['outcomes'].clear(); Path(paths[0]).write_text(json.dumps(altered))
        self.assertEqual(1,runner.main(['--requirement-gate',*paths]), 'real CLI accepted missing requirement outcome')

    def test_M4_caller_cannot_accept_failure_bearing_combiner_result(self):
        path=self.root/'record.json'; path.write_text('{}')
        for status in ('not_run','failed'):
            with mock.patch.object(fw,'combine_run_records',return_value={'requirements':{'TF-5':status}}):
                self.assertEqual(1,runner.main(['--requirement-gate',str(path)]), 'caller accepted '+status+' requirement report')
