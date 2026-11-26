"""Decision 99 A: scoped round-2 MED rejection witnesses, with positive controls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test_framework as fw  # noqa: E402
import parallel_tests as runner  # noqa: E402


class RoundTwo(unittest.TestCase):
    __why_not_scenario__ = 'Controlled corrupt metadata and dependency graphs must be rejected before product scenarios can run.'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def module(self, name, source):
        path = self.root / (name + '.py')
        path.write_text(source)
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, str(path), 'exec'), module.__dict__)
        return module



    @fw.requirements('TF-14')
    def test_M2_qualified_and_renamed_imports_retain_golden_provenance(self):
        (self.root / 'fixtures.py').write_text('from pathlib import Path\nGOLDEN_PATH = Path("goldens/result")\nEXPECTED = GOLDEN_PATH\nORDINARY = Path("input.txt")\n')
        for source in ('import fixtures\nactual = fixtures.GOLDEN_PATH.read_text()',
                       'from fixtures import EXPECTED as reference\nactual = reference.read_text()',
                       'import fixtures as f\nactual = f.EXPECTED.read_bytes()'):
            errors = fw.golden_bypasses(source, filename=self.root/'test_consumer.py')
            self.assertTrue(any('direct golden' in e for e in errors), 'imported golden read escaped: ' + source)
        self.assertEqual([], fw.golden_bypasses('from fixtures import ORDINARY\nactual = ORDINARY.read_text()', filename=self.root/'test_consumer.py'))

    @fw.requirements('TF-8', 'TF-9')
    def test_M3_no_report_or_empty_plan_cannot_authorize_retirement(self):
        row = dict(suite='root', unit='m#0', seconds=.1, names=['m.a'], returncode=0)
        options = dict(layers={'root:m':'unit'}, stages={'root:m':'unit'}, jobs=16, elapsed=.1, selection=['root:m'], budget_debt={'modules':{'root:m':10}})
        for plan in (None, {}, {'root:m':['m#0','m#1']}):
            self.assertEqual([], fw.measurements([row], expected_fragments=plan, **options)['debt_retirement_required'], 'missing authoritative fragment authorized retirement')
        self.assertEqual(['root:m'], fw.measurements([row], expected_fragments={'root:m':['m#0']}, **options)['debt_retirement_required'])

    def records(self):
        layers={'root:u':'unit','root:i':'component','root:e':'scenario'}
        stages={'root:u':'unit','root:i':'integration','root:e':'e2e'}
        checks=['root:u.a','root:i.a','root:i.b','root:e.a']
        catalogue={'TF-5':{'checks':checks}}
        records=[]
        for module, case, part in [('u','a',(1,1)),('i','a',(1,2)),('i','b',(2,2)),('e','a',(1,1))]:
            unit=module+('#'+case if module=='i' else '')
            row=dict(suite='root',unit=unit,seconds=.1,names=[module+'.'+case],returncode=0,outcomes={'root:'+module+'.'+case:'passed'})
            plan={'root:'+module: ['i#a','i#b'] if module=='i' else [module]}
            records.append(fw.measurements([row], layers, stages, jobs=16, elapsed=.1, selection=['root:'+module], catalogue=catalogue, part=part, invocation_id='r2', expected_fragments=plan))
        return records

    @fw.requirements('TF-5', 'TF-9')
    def test_M4_real_gate_rejects_not_run_failed_and_incomplete_evidence(self):
        records=self.records()
        self.assertEqual({'TF-5':'passed'}, fw.combine_run_records(records)['requirements'])
        for status in ('not_run','failed'):
            altered=copy.deepcopy(records)
            altered[0]['workers'][0]['outcomes']['root:u.a']=status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'requirements failed/not_run.*TF-5'):
                fw.combine_run_records(altered)
        with self.assertRaisesRegex(ValueError, 'missing or repeated shards'):
            fw.combine_run_records(records[:2]+records[3:])
        paths=[]
        for i,record in enumerate(records):
            path=self.root/(str(i)+'.json')
            path.write_text(json.dumps(record))
            paths.append(str(path))
        self.assertEqual(0,runner.main(['--requirement-gate',*paths]))
        altered=copy.deepcopy(records[0])
        altered['workers'][0]['outcomes'].clear()
        Path(paths[0]).write_text(json.dumps(altered))
        self.assertEqual(1,runner.main(['--requirement-gate',*paths]), 'real CLI accepted missing requirement outcome')

    @fw.requirements('TF-5', 'TF-9')
    def test_M4_caller_cannot_accept_failure_bearing_combiner_result(self):
        path=self.root/'record.json'
        path.write_text('{}')
        for status in ('not_run','failed'):
            with mock.patch.object(fw,'combine_run_records',return_value={'requirements':{'TF-5':status}}):
                self.assertEqual(1,runner.main(['--requirement-gate',str(path)]), 'caller accepted '+status+' requirement report')

    @fw.requirements('TF-14')
    def test_M2_function_local_import_keeps_golden_provenance(self):
        (self.root/'fixtures.py').write_text('from pathlib import Path\nEXPECTED = Path("goldens/result")\n')
        source='def test_output():\n    from fixtures import EXPECTED as reference\n    return reference.read_text()\n'
        self.assertTrue(fw.golden_bypasses(source,filename=self.root/'test_consumer.py'), 'M2 function-local imported golden escaped')

    @fw.requirements('TF-14')
    def test_M2_package_import_keeps_golden_provenance(self):
        (self.root/'reference_data').mkdir()
        (self.root/'reference_data/__init__.py').write_text('from pathlib import Path\nEXPECTED = Path("goldens/result")\n')
        source='from reference_data import EXPECTED as reference\nactual = reference.read_text()'
        self.assertTrue(fw.golden_bypasses(source,filename=self.root/'test_consumer.py'), 'M2 package imported golden escaped')

