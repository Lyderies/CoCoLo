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



class RequirementGateProfiles(unittest.TestCase):
    """Decision 420: each check is judged in the run profile that selects its layer; planned is visible and never a release pass."""
    __why_not_scenario__ = 'Controlled run records exercise the combiner and gate CLI; no product scenario can model a missing soak run.'
    LAYERS = {'root:u': 'unit', 'root:s': 'soak', 'root:v': 'soak'}
    STAGES = {'root:u': 'unit', 'root:s': 'integration', 'root:v': 'integration'}
    CATALOGUE = {
        'R1': {'checks': ['root:u.a', 'root:s.a']},   # ci check plus soak check
        'R2': {'checks': ['root:u.b']},               # ci only: no soak link
        'R3': {'checks': ['root:v.a']},               # soak only
        'P1': {'checks': [], 'status': 'planned'},
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def record(self, profile, modules, outcomes):
        rows = [dict(suite='root', unit=module, seconds=.1, names=[module + '.x'], returncode=0,
                     outcomes={'root:' + module + '.' + case: status for case, status in cases.items()})
                for module, cases in modules.items()]
        plan = {'root:' + module: [module] for module in modules}
        return fw.measurements(rows, self.LAYERS, self.STAGES, jobs=2, elapsed=.1, profile=profile,
                               selection=['root:' + m for m in modules], catalogue=self.CATALOGUE,
                               invocation_id='gate-' + profile, expected_fragments=plan)

    def ci(self, **override):
        outcomes = {'u': {'a': 'passed', 'b': 'passed'}}
        outcomes['u'].update(override)
        return [self.record('ci', {'u': outcomes['u']}, None)]

    def soak(self, s='passed', v='passed', modules=('s', 'v')):
        return [self.record('soak', {m: {'a': {'s': s, 'v': v}[m]} for m in modules}, None)]

    @fw.requirements('TF-5')
    def test_ci_gate_passes_when_only_soak_checks_are_unrun_and_reports_them(self):
        report = fw.combine_run_records(self.ci())
        self.assertEqual(['R1', 'R3'], report['awaiting_soak'])
        self.assertEqual('awaiting_soak', report['requirements']['R1'])
        self.assertEqual('passed', report['requirements']['R2'], 'a requirement with no soak link is unaffected')
        self.assertEqual(['P1'], report['planned'])

    @fw.requirements('TF-5')
    def test_ci_gate_still_fails_on_any_unrun_or_failed_ci_check(self):
        for status in ('not_run', 'failed'):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'requirements failed/not_run.*R1'):
                fw.combine_run_records(self.ci(a=status))

    @fw.requirements('TF-5')
    def test_soak_gate_fails_if_a_soak_check_is_not_run_and_ignores_ci_only_requirements(self):
        report = fw.combine_run_records(self.soak())
        self.assertEqual({'R1': 'passed', 'R3': 'passed'}, {k: v for k, v in report['requirements'].items() if k != 'P1'})
        with self.assertRaisesRegex(ValueError, 'missing/incomplete stage evidence'):
            fw.combine_run_records(self.soak(modules=('s',)))
        for status in ('not_run', 'failed'):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'requirements failed/not_run.*R1'):
                fw.combine_run_records(self.soak(s=status))

    @fw.requirements('TF-5')
    def test_gate_cli_reports_planned_and_release_mode_fails_on_them(self):
        paths = []
        for i, record in enumerate(self.ci()):
            path = self.root / (str(i) + '.json')
            path.write_text(json.dumps(record))
            paths.append(str(path))
        self.assertEqual(0, runner.main(['--requirement-gate', *paths]))
        self.assertEqual(1, runner.main(['--requirement-gate', *paths, '--release']), 'release gate passed a planned requirement')
        for status in ('awaiting_soak', 'skipped'):  # skipped is reported, not a failure (decision 126.2), as in the combiner
            with self.subTest(status=status), mock.patch.object(fw, 'combine_run_records', return_value={'requirements': {'R': status}, 'planned': []}):
                self.assertEqual(0, runner.main(['--requirement-gate', *paths]))

    @fw.requirements('TF-5')
    def test_planned_status_is_validated_and_cannot_hide_checks(self):
        bad = {'P': {'statement': 's', 'source': 'x', 'checks': ['root:u.a'], 'status': 'planned'},
               'Q': {'statement': 's', 'source': 'x', 'checks': [], 'status': 'later'}}
        errors = fw.validate_metadata(inventory={}, stages={}, layers={}, catalogue=bad)
        self.assertTrue(any(e.startswith('P: planned requirement') for e in errors), errors)
        self.assertTrue(any(e.startswith('Q: unknown requirement status') for e in errors), errors)
