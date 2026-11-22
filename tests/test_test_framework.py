"""TF-1..9, TF-14..18: requirement-derived framework checks; no product fixtures."""
import copy
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


from test_framework import requirements


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.fw = importlib.import_module('test_framework')
        self.module = 'root:test_sample'
        self.test = self.module + '.Sample.test_value'
        self.inventory = {self.module: {self.test: {'requirements': ['TF-2'], 'rationale': 'metadata rejection', 'fingerprint': 'a' * 64}}}
        self.catalogue = {'TF-2': {'statement': 'Every module declares one valid layer.', 'source': 'requirements:TF-2', 'checks': [self.test]}}
        self.stages = {self.module: 'integration'}
        self.layers = {self.module: 'component'}
        self.legacy = {'baseline': 'baseline', 'tests': {}}

    def errors(self, **changes):
        options = dict(inventory=self.inventory, stages=self.stages, layers=self.layers,
                       catalogue=self.catalogue, legacy=self.legacy, ceiling=self.legacy, previous=self.legacy)
        options.update(changes)
        return self.fw.validate_metadata(**options)

    @requirements('TF-2', why_not_scenario='Verify layer is required for every module even outside selection using isolated framework fixtures.')
    def test_layer_is_required_for_every_module_even_outside_selection(self):
        self.assertEqual([], self.errors())
        for layers in ({}, {self.module: 'typo'}, {self.module: 'component', 'root:stale': 'unit'}):
            with self.subTest(layers=layers):
                self.assertTrue(self.errors(layers=layers))
        self.assertTrue(self.errors(stages={self.module: 'unit'}))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.fw.decode_json('{"root:m":"unit","root:m":"scenario"}')

    @requirements('TF-3', 'TF-6', why_not_scenario='Verify unknown empty and untagged proofs and missing rationale are rejected using isolated framework fixtures.')
    def test_unknown_empty_and_untagged_proofs_and_missing_rationale_are_rejected(self):
        for ids in ([], ['UNKNOWN'], ['']):
            self.inventory[self.module][self.test]['requirements'] = ids
            with self.subTest(ids=ids):
                self.assertTrue(any(self.test in error for error in self.errors()))
        self.inventory[self.module][self.test]['requirements'] = ['TF-2']
        self.inventory[self.module][self.test]['rationale'] = ''
        self.assertTrue(any(self.test in error for error in self.errors()))

    @requirements('TF-3', why_not_scenario='Verify decorator inheritance and override follow the actual implementation using isolated framework fixtures.')
    def test_decorator_inheritance_and_override_follow_the_actual_implementation(self):
        fw = self.fw
        class Parent(unittest.TestCase):
            @fw.requirements('TF-3')
            def test_value(self):
                self.assertEqual(1, 1)
        class Inherited(Parent):
            pass
        class Overridden(Parent):
            def test_value(self):
                self.assertEqual(2, 2)
        self.assertEqual(['TF-3'], fw.describe_test(Inherited('test_value'))['requirements'])
        self.assertEqual([], fw.describe_test(Overridden('test_value'))['requirements'])
        self.assertNotEqual(fw.describe_test(Inherited('test_value'))['fingerprint'],
                            fw.describe_test(Overridden('test_value'))['fingerprint'])

    @requirements('TF-4', why_not_scenario='Verify requirement and check links are checked in both directions using isolated framework fixtures.')
    def test_requirement_and_check_links_are_checked_in_both_directions(self):
        for field, value in [('checks', []), ('checks', ['root:missing.test']), ('statement', ''), ('source', '')]:
            catalogue = copy.deepcopy(self.catalogue)
            catalogue['TF-2'][field] = value
            with self.subTest(field=field, value=value):
                self.assertTrue(self.errors(catalogue=catalogue))
        catalogue = copy.deepcopy(self.catalogue)
        catalogue['TF-9'] = {**catalogue['TF-2'], 'checks': [self.test]}
        self.assertTrue(self.errors(catalogue=catalogue))

    @requirements('TF-7', why_not_scenario='Verify legacy list cannot grow restore entries change fingerprints or rename tests using isolated framework fixtures.')
    def test_legacy_list_cannot_grow_restore_entries_change_fingerprints_or_rename_tests(self):
        self.inventory[self.module][self.test]['requirements'] = []
        self.inventory[self.module][self.test]['rationale'] = ''
        allowed = {'baseline': 'baseline', 'tests': {self.test: 'a' * 64}}
        opts = dict(catalogue={}, legacy=allowed, ceiling=allowed, previous=allowed)
        self.assertEqual([], self.errors(**opts))
        for changes in (
            {'previous': self.legacy}, {'ceiling': self.legacy},
            {'legacy': {'baseline': 'changed', 'tests': allowed['tests']}},
            {'legacy': {'baseline': 'baseline', 'tests': {self.test: 'b' * 64}}},
        ):
            with self.subTest(changes=changes):
                self.assertTrue(self.errors(**(opts | changes)))
        renamed = self.test + '_renamed'
        self.inventory[self.module][renamed] = self.inventory[self.module].pop(self.test)
        self.assertTrue(self.errors(**opts))

    @requirements('TF-7', why_not_scenario='Verify migration removes legacy entry and new tagged proof is required using isolated framework fixtures.')
    def test_migration_removes_legacy_entry_and_new_tagged_proof_is_required(self):
        old = {'baseline': 'baseline', 'tests': {self.test: 'a' * 64}}
        self.assertEqual([], self.errors(ceiling=old, previous=old))
        self.assertTrue(self.errors(legacy=old, ceiling=old, previous=old))

    @requirements('TF-5', why_not_scenario='Verify a requirement passes only when all checks passed and actually ran using isolated framework fixtures.')
    def test_a_requirement_passes_only_when_all_checks_passed_and_actually_ran(self):
        doc = {'TF-5': {'checks': ['one', 'two']}}
        for status in ('failed', 'skipped', 'error', 'expected_failure', 'unexpected_success', None):
            outcomes = {'one': 'passed'}
            if status is not None:
                outcomes['two'] = status
            with self.subTest(status=status):
                self.assertNotEqual('passed', self.fw.coverage(doc, outcomes)['TF-5'])
        self.assertEqual('passed', self.fw.coverage(doc, {'one': 'passed', 'two': 'passed'})['TF-5'])
        self.assertEqual('not_run', self.fw.coverage(doc, {})['TF-5'])

    @requirements('TF-8', 'TF-9', why_not_scenario='Verify module budgets sum fragments and name every offender using isolated framework fixtures.')
    def test_module_budgets_sum_fragments_and_name_every_offender(self):
        rows = [dict(suite='root', unit='first#0', seconds=20., names=['a'], returncode=0),
                dict(suite='root', unit='first#1', seconds=20., names=['b'], returncode=0),
                dict(suite='root', unit='second', seconds=30.01, names=['second'], returncode=0),
                dict(suite='root', unit='equal', seconds=30., names=['equal'], returncode=0)]
        layers = {f'root:{n}': 'component' for n in ('first', 'second', 'equal')}
        stages = {key: 'integration' for key in layers}
        report = self.fw.measurements(rows, layers, stages, jobs=16, elapsed=50., selection=['a', 'b'], part=(1, 1))
        self.assertAlmostEqual(100.01, report['job_seconds'])
        self.assertEqual(['root:first', 'root:second'], [r['module'] for r in report['over_budget']])
        self.assertEqual(2, report['over_budget'][0]['fragments'])
        self.assertEqual(40., report['modules']['root:first'])
        self.assertEqual(16, report['jobs'])
        self.assertEqual(rows, report['workers'])
        self.assertIn('python', report)
        self.assertIn('platform', report)
        partial = self.fw.measurements(rows, layers, stages, jobs=16, elapsed=50., selection=['a'], part=(1, 2))
        self.assertTrue(partial['partial'])

    @requirements('TF-8', why_not_scenario='Verify shards require complete disjoint evidence before aggregate budget can pass using isolated framework fixtures.')
    def test_shards_require_complete_disjoint_evidence_before_aggregate_budget_can_pass(self):
        row = dict(suite='root', unit='m#0', seconds=20., names=['m.A.test_a'], returncode=0)
        opts = dict(layers={'root:m': 'component'}, stages={'root:m': 'integration'}, jobs=1, elapsed=20., selection=['root:m'], invocation_id='fixture-shards')
        a = self.fw.measurements([row], part=(1, 2), **opts)
        b = self.fw.measurements([{**row, 'unit': 'm#1', 'names': ['m.A.test_b']}], part=(2, 2), **opts)
        with self.assertRaises(ValueError):
            self.fw.combine_measurements([a])
        with self.assertRaises(ValueError):
            self.fw.combine_measurements([a, a])
        combined = self.fw.combine_measurements([a, b])
        self.assertFalse(combined['partial'])
        self.assertEqual(40., combined['modules']['root:m'])
        self.assertEqual('root:m', combined['over_budget'][0]['module'])

    @requirements('TF-5', 'TF-8', 'TF-9', why_not_scenario='Verify shards preserve metadata failures and reject duplicate tests and invalid time using isolated framework fixtures.')
    def test_shards_preserve_metadata_failures_and_reject_duplicate_tests_and_invalid_time(self):
        row = dict(suite='root', unit='m#0', seconds=2., names=['m.A.test_a'], returncode=0)
        opts = dict(layers={'root:m': 'component'}, stages={'root:m': 'integration'}, jobs=1, elapsed=2., selection=['root:m'], invocation_id='fixture-shards')
        a = self.fw.measurements([row], part=(1, 2), **opts)
        b = self.fw.measurements([{**row, 'unit': 'm#1', 'names': ['m.A.test_b']}], part=(2, 2), **opts)
        a['metadata_errors'] = ['missing requirement']
        self.assertEqual(['missing requirement'], self.fw.combine_measurements([a, b])['metadata_errors'])
        b['workers'][0]['names'] = ['m.A.test_a']
        with self.assertRaises(ValueError):
            self.fw.combine_measurements([a, b])
        for invalid in (-1., float('nan'), float('inf')):
            with self.subTest(seconds=invalid), self.assertRaises(ValueError):
                self.fw.measurements([{**row, 'seconds': invalid}], **opts)

    @requirements('TF-18', why_not_scenario='Verify complete sweep is explicit soak only and profiles are validated using isolated framework fixtures.')
    def test_complete_sweep_is_explicit_soak_only_and_profiles_are_validated(self):
        layers = {'root:fast': 'crash', 'root:full': 'soak'}
        self.assertEqual({'root:fast'}, self.fw.select_layers(layers, 'ci'))
        self.assertEqual({'root:fast'}, self.fw.select_layers(layers, 'dev'))
        self.assertEqual(set(layers), self.fw.select_layers(layers, 'soak'))
        with self.assertRaises(ValueError):
            self.fw.select_layers(layers, 'typo')


class GoldenTests(unittest.TestCase):
    def setUp(self):
        self.golden = importlib.import_module('test_golden')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'goldens'
        self.root.mkdir()

    def compare(self, value='landed\n', **changes):
        args = dict(root=self.root, name='state.txt', actual=value, requirement='TF-14',
                    declared={'TF-14'}, known={'TF-14'}, environ={})
        args.update(changes)
        return self.golden.compare(**args)

    @requirements('TF-14', why_not_scenario='Verify expected output is exact and missing or different fails using isolated framework fixtures.')
    def test_expected_output_is_exact_and_missing_or_different_fails(self):
        with self.assertRaises(AssertionError):
            self.compare()
        (self.root / 'state.txt').write_text('landed\n')
        self.compare()
        with self.assertRaisesRegex(AssertionError, 'reverted'):
            self.compare('reverted\n')
        self.assertEqual('landed\n', (self.root / 'state.txt').read_text())

    @requirements('TF-15', why_not_scenario='Verify update requires exact environment and a known declared requirement using isolated framework fixtures.')
    def test_update_requires_exact_environment_and_a_known_declared_requirement(self):
        for env in ({}, {'UPDATE_GOLDEN': '0'}, {'UPDATE_GOLDEN': 'true'}):
            with self.subTest(env=env), self.assertRaises(AssertionError):
                self.compare(environ=env)
        for changes in ({'requirement': ''}, {'requirement': 'UNKNOWN'}, {'declared': set()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.compare(environ={'UPDATE_GOLDEN': '1'}, **changes)
        self.assertFalse((self.root / 'state.txt').exists())
        with self.assertRaisesRegex(self.golden.GoldenUpdated, 'TF-14'):
            self.compare(environ={'UPDATE_GOLDEN': '1'})
        self.assertEqual('TF-14\n', (self.root / 'state.txt.requirement').read_text())
        self.compare()

    @requirements('TF-15', why_not_scenario='Verify update cannot escape through dotdot absolute path or symlink using isolated framework fixtures.')
    def test_update_cannot_escape_through_dotdot_absolute_path_or_symlink(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('owner data')
        (self.root / 'link').symlink_to(outside)
        for name in ('../outside', str(outside), 'link'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.compare(name=name, environ={'UPDATE_GOLDEN': '1'})
        self.assertEqual('owner data', outside.read_text())

    @requirements('TF-14', 'TF-15', why_not_scenario='Verify equal update mode is not green and newlines are not silently normalized using isolated framework fixtures.')
    def test_equal_update_mode_is_not_green_and_newlines_are_not_silently_normalized(self):
        (self.root / 'state.txt').write_bytes(b'landed\n')
        with self.assertRaises(self.golden.GoldenUpdated):
            self.compare(environ={'UPDATE_GOLDEN': '1'})
        (self.root / 'state.txt').write_bytes(b'landed\r\n')
        with self.assertRaises(AssertionError):
            self.compare('landed\n')

    @requirements('TF-15', why_not_scenario='Verify provenance symlink cannot write outside the golden root using isolated framework fixtures.')
    def test_provenance_symlink_cannot_write_outside_the_golden_root(self):
        outside = Path(self.temp.name) / 'owner'
        outside.write_text('unchanged')
        (self.root / 'state.txt').write_text('original golden')
        sidecar = self.root / 'state.txt.requirement'
        sidecar.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.compare(environ={'UPDATE_GOLDEN': '1'})
        self.assertEqual('unchanged', outside.read_text())
        self.assertEqual('original golden', (self.root / 'state.txt').read_text())
        self.assertTrue(sidecar.is_symlink())
        self.assertEqual(outside, sidecar.readlink())


class DependencyTests(unittest.TestCase):
    @requirements('TF-16', why_not_scenario='Verify dependencies are exactly pinned hashed and excluded from shipped imports using isolated framework fixtures.')
    def test_dependencies_are_exactly_pinned_hashed_and_excluded_from_shipped_imports(self):
        fw = importlib.import_module('test_framework')
        self.assertEqual([], fw.validate_dependencies(ROOT))
        text = (ROOT / 'requirements-test.txt').read_text()
        self.assertIn('hypothesis==6.170.17', text)
        self.assertIn('sortedcontainers==2.4.0', text)
        self.assertIn('--hash=sha256:', text)

    @requirements('TF-17', why_not_scenario='Verify hypothesis profiles are bounded and io deadline is disabled using isolated framework fixtures.')
    def test_hypothesis_profiles_are_bounded_and_io_deadline_is_disabled(self):
        profiles = importlib.import_module('test_profiles')
        for name, examples in [('dev', 20), ('ci', 100), ('soak', 1000)]:
            with self.subTest(profile=name):
                settings = profiles.settings_for(name)
                (self.assertGreaterEqual if name == 'soak' else self.assertEqual)(settings.max_examples, examples)
                self.assertIsNone(settings.deadline)
        self.assertTrue(profiles.settings_for('ci').derandomize)
        from unittest.mock import patch
        with patch.dict(__import__('os').environ, {}, clear=True):
            profiles.activate()
            active = __import__('hypothesis').settings()
            self.assertEqual(profiles.settings_for('ci').max_examples, active.max_examples)
            self.assertTrue(active.derandomize)
            self.assertIsNone(active.deadline)
        with self.assertRaises(ValueError):
            profiles.settings_for('unknown')


class BudgetDebtTests(unittest.TestCase):
    def setUp(self):
        self.fw = importlib.import_module('test_framework')
        self.debt = {'baseline': 'baseline', 'modules': {'root:old': 80., 'root:unselected': 90.}}
        self.layers = {'root:old': 'scenario', 'root:unselected': 'scenario', 'root:new': 'component'}
        self.stages = {key: 'integration' for key in self.layers}

    def measure(self, seconds, **kw):
        rows = [dict(suite='root', unit='old', seconds=seconds, names=['test'], returncode=0)]
        return self.fw.measurements(rows, self.layers, self.stages, jobs=16, elapsed=seconds,
                                    selection=['root:old'], budget_debt=self.debt, **kw)

    @requirements('TF-8', why_not_scenario='Verify baseline debt warns even unselected and only regression fails using isolated framework fixtures.')
    def test_baseline_debt_warns_even_unselected_and_only_regression_fails(self):
        report = self.measure(80.)
        self.assertEqual([], report['budget_failures'])
        self.assertEqual(['root:old', 'root:unselected'], [r['module'] for r in report['budget_warnings']])
        self.assertIsNone(report['budget_warnings'][1]['seconds'])
        self.assertEqual('not_measured', report['budget_warnings'][1]['status'])
        self.assertEqual(['root:old'], [r['module'] for r in self.measure(80.001)['budget_failures']])
        self.assertEqual(['root:old'], self.measure(60.)['debt_retirement_required'])
        self.assertEqual([], self.measure(60., part=(1, 2))['debt_retirement_required'])

    @requirements('TF-8', why_not_scenario='Verify debt can only shrink and does not exempt new modules using isolated framework fixtures.')
    def test_debt_can_only_shrink_and_does_not_exempt_new_modules(self):
        self.assertEqual([], self.fw.validate_budget_debt(self.debt, self.debt, self.debt))
        for changed in ({'baseline': 'wrong', 'modules': self.debt['modules']},
                        {'baseline': 'baseline', 'modules': {**self.debt['modules'], 'root:new': 100.}},
                        {'baseline': 'baseline', 'modules': {**self.debt['modules'], 'root:old': 81.}}):
            with self.subTest(changed=changed):
                self.assertTrue(self.fw.validate_budget_debt(changed, self.debt, self.debt))
        removed = {'baseline': 'baseline', 'modules': {'root:unselected': 90.}}
        self.assertTrue(self.fw.validate_budget_debt(self.debt, self.debt, removed))
        rows = [dict(suite='root', unit='new', seconds=30.001, names=['test'], returncode=0)]
        report = self.fw.measurements(rows, self.layers, self.stages, jobs=16, elapsed=30.001,
                                      selection=['root:new'], budget_debt=self.debt)
        self.assertEqual('root:new', report['budget_failures'][0]['module'])

    @requirements('TF-8', 'TF-9', why_not_scenario='Verify shards use combined debt ceiling and preserve preflight time using isolated framework fixtures.')
    def test_shards_use_combined_debt_ceiling_and_preserve_preflight_time(self):
        options = dict(layers=self.layers, stages=self.stages, jobs=1, elapsed=40., selection=['root:old'],
                       budget_debt=self.debt, discovery_seconds=2., preflight_seconds=3., invocation_id='fixture-shards')
        row = dict(suite='root', unit='old#0', seconds=40., names=['a'], returncode=0)
        a = self.fw.measurements([row], part=(1, 2), **options)
        b = self.fw.measurements([{**row, 'unit': 'old#1', 'names': ['b']}], part=(2, 2), **options)
        result = self.fw.combine_measurements([a, b])
        self.assertEqual([], result['budget_failures'])
        self.assertEqual(4., result['discovery_seconds'])
        self.assertEqual(6., result['preflight_seconds'])
        b['workers'][0]['seconds'] += .001
        self.assertEqual('root:old', self.fw.combine_measurements([a,b])['budget_failures'][0]['module'])


class WorkflowTests(unittest.TestCase):
    @requirements('TF-7', 'TF-8', 'TF-16', why_not_scenario='Verify ci prepares hashed dependencies and combines complete shards using isolated framework fixtures.')
    def test_ci_prepares_hashed_dependencies_and_combines_complete_shards(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        from test_ci_stages import jobs
        sections = jobs(text)
        for name in ('unit', 'integration', 'full'):
            with self.subTest(job=name):
                self.assertIn('pip install --require-hashes -r requirements-test.txt', sections[name])
                self.assertIn('fetch-depth: 0', sections[name])
        self.assertIn('python-version: "3.11"', sections['unit'])
        self.assertIn('--save-timings', sections['integration'])
        self.assertIn('if: always()', sections['integration'])
        self.assertIn('--combine-timings', sections['integration-budget'])
        self.assertIn('needs: integration', sections['integration-budget'])


class ReviewCoreTests(unittest.TestCase):
    __requirements__ = ('TF-4', 'TF-5', 'TF-7', 'TF-8', 'TF-9', 'TF-14', 'TF-16')
    __why_not_scenario__ = 'Negative framework fixtures verify metadata and evidence rejection without a product lifecycle.'

    def setUp(self):
        self.fw = importlib.import_module('test_framework')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def module(self, source):
        import types
        path = self.root / 'test_values.py'
        path.write_text(source)
        module = types.ModuleType('test_values')
        module.__file__ = str(path)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        exec(compile(source, str(path), 'exec'), module.__dict__)
        return module

    def test_A1_C3_constant_fingerprints_have_equality_and_independent_change_controls(self):
        source = "import unittest\nVALUE = 3\nclass Case(unittest.TestCase):\n    EXPECTED = 3\n    def helper(self):\n        return VALUE\n    def setUp(self):\n        self.actual = self.helper()\n    def test_value(self):\n        self.assertEqual(self.EXPECTED, self.actual)\n"
        def fingerprint(text):
            return self.fw.dependency_fingerprint(self.module(text).Case('test_value'))
        baseline = fingerprint(source)
        self.assertEqual(baseline, fingerprint(source))
        for changed in (source.replace('VALUE = 3', 'VALUE = 4'), source.replace('EXPECTED = 3', 'EXPECTED = 4'),
                        source.replace('return VALUE', 'return VALUE + 1'), source.replace('self.actual = self.helper()', 'self.actual = 4')):
            with self.subTest(source=changed):
                self.assertNotEqual(baseline, fingerprint(changed))

    def test_A3_dynamic_helpers_need_explicit_source_dependencies(self):
        source = "import unittest\nclass Case(unittest.TestCase):\n    def helper(self):\n        return 1\n    def test_value(self):\n        self.assertEqual(1, getattr(self, 'helper')())\n"
        module = self.module(source)
        self.assertIsNone(self.fw.dependency_fingerprint(module.Case('test_value')))
        module.Case.__fixture_dependencies__ = (self.root / 'test_values.py',)
        self.assertIsNotNone(self.fw.dependency_fingerprint(module.Case('test_value')))

    def test_A4_review_records_bind_digest_and_nonempty_revision(self):
        import hashlib, json
        record = self.root / 'review.json'
        record.write_text(json.dumps({'revision': 'reviewed', 'outcome': 'passed'}))
        checks = {'review:x': {'kind': 'review', 'revision': 'reviewed'}}
        valid = {'revision': 'reviewed', 'record': str(record), 'sha256': hashlib.sha256(record.read_bytes()).hexdigest(), 'outcome': 'passed'}
        self.assertEqual({'review:x': 'passed'}, self.fw.registered_outcomes(checks, {'review:x': valid}))
        for changed in ({**valid, 'record': str(self.root / 'absent')}, {**valid, 'sha256': '0' * 64},
                        {**valid, 'revision': None}, {**valid, 'outcome': 'failed'}):
            with self.subTest(record=changed):
                self.assertEqual('failed', self.fw.registered_outcomes(checks, {'review:x': changed})['review:x'])
        self.assertEqual('failed', self.fw.registered_outcomes({'review:x': {'kind': 'review'}}, {'review:x': {**valid, 'revision': None}})['review:x'])

    def test_A5_A12_authority_ids_and_EOF_splitter(self):
        text = '**TF-1 — First.** first statement.\n\n**TF-2 — Second.** final statement.'
        (self.root / 'REQUIREMENTS-test-framework.md').write_text(text)
        parsed = self.fw.requirement_statements(text)
        self.assertEqual({'TF-1', 'TF-2'}, set(parsed))
        catalogue = {key: {'statement': value, 'source': 'REQUIREMENTS-test-framework.md#' + key} for key, value in parsed.items()}
        self.assertEqual([], self.fw.validate_sources(self.root, catalogue))
        self.assertTrue(self.fw.validate_sources(self.root, {}))
        del catalogue['TF-2']
        self.assertTrue(any('TF-2' in e for e in self.fw.validate_sources(self.root, catalogue)))

    def test_A6_imported_golden_path_cannot_bypass_helper(self):
        source = 'from fixtures import GOLDEN_PATH\nself.assertEqual(actual, GOLDEN_PATH.read_text())'
        self.assertTrue(self.fw.golden_bypasses(source))

    def test_A7_mixed_invocations_cannot_combine(self):
        row = dict(suite='root', unit='m#0', seconds=1., names=['m.a'], returncode=0)
        options = dict(layers={'root:m': 'unit'}, stages={'root:m': 'unit'}, jobs=1, elapsed=1., selection=['root:m'], invocation_id='fixture-shards')
        a = self.fw.measurements([row], part=(1, 2), **options)
        b = self.fw.measurements([{**row, 'unit': 'm#1', 'names': ['m.b']}], part=(2, 2), **options)
        a['invocation_id'], b['invocation_id'] = 'one', 'two'
        with self.assertRaisesRegex(ValueError, 'invocation'):
            self.fw.combine_measurements([a, b])

    def test_A8_incomplete_and_timed_out_modules_do_not_retire_debt(self):
        row = dict(suite='root', unit='m#0', seconds=1., names=['m.a'], returncode=0, timed_out=True)
        options = dict(layers={'root:m': 'unit'}, stages={'root:m': 'unit'}, jobs=1, elapsed=1., selection=['root:m'],
                       budget_debt={'modules': {'root:m': 10.}})
        self.assertEqual([], self.fw.measurements([row], **options)['debt_retirement_required'])
        row.update(timed_out=False, returncode=1)
        self.assertEqual([], self.fw.measurements([row], **options)['debt_retirement_required'])

    def test_A11_empty_method_rationale_falls_back_to_class(self):
        fw = self.fw
        class Case(unittest.TestCase):
            __why_not_scenario__ = 'class fixture rationale'
            @fw.requirements('TF-1')
            def test_value(self):
                pass
        self.assertEqual(Case.__why_not_scenario__, fw.describe_test(Case('test_value'))['rationale'])
        Case.test_value.__why_not_scenario__ = None
        self.assertEqual(Case.__why_not_scenario__, fw.describe_test(Case('test_value'))['rationale'])

    def test_A2_C4_dependency_scan_covers_shipped_roots_and_bad_pins(self):
        for folder in ('packages/codex-agents/native-package', 'packages/claude-agents/src', 'scripts', 'packages/codex-agents/scripts'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        for file in ('setup.sh', 'scripts/install.py', 'scripts/setup-wizard.py', 'packages/codex-agents/scripts/install.py'):
            (self.root / file).write_text('')
        requirements = self.root / 'requirements-test.txt'
        valid = 'hypothesis==6.170.17 --hash=sha256:' + 'a' * 64 + '\nsortedcontainers==2.4.0 --hash=sha256:' + 'b' * 64 + '\n'
        requirements.write_text(valid)
        self.assertEqual([], self.fw.validate_dependencies(self.root))
        for file in ('packages/claude-agents/src/planted.py', 'scripts/install.py'):
            path = self.root / file
            path.write_text('import hypothesis\n')
            self.assertTrue(any(file in e for e in self.fw.validate_dependencies(self.root)))
            path.write_text('')
        (self.root / 'packages/claude-agents/src/planted.py').unlink()
        (self.root / 'packages/claude-agents/src').rmdir()
        self.assertTrue(self.fw.validate_dependencies(self.root))
        for invalid in ('hypothesis==6.170.17', 'hypothesis>=6', valid.replace(' --hash=sha256:' + 'b' * 64, '')):
            requirements.write_text(invalid)
            self.assertTrue(self.fw.validate_dependencies(self.root))


class ReviewRunnerPolicyTests(unittest.TestCase):
    __requirements__ = ('TF-1', 'TF-5', 'TF-9')
    __why_not_scenario__ = 'The aggregation gate must reject fabricated partial evidence independently of product checks.'

    def test_review_B6_full_gate_rejects_not_run_and_incomplete_stage_evidence(self):
        import test_framework as fw
        catalogue = {'TF-5': {'checks': ['root:m.a', 'root:n.b']}}
        row = {'suite': 'root', 'unit': 'm', 'seconds': 1., 'names': ['m.a'], 'returncode': 0, 'outcomes': {'root:m.a': 'passed'}}
        report = fw.measurements([row], {'root:m': 'unit', 'root:n': 'scenario'}, {'root:m': 'unit', 'root:n': 'e2e'},
                                 jobs=1, elapsed=1., selection=['root:m'], catalogue=catalogue)
        report['metadata_errors'] = []
        with self.assertRaisesRegex(ValueError, 'not_run|missing|incomplete'):
            fw.combine_run_records([report])

    def test_review_B5_workflow_has_validated_merge_base_and_no_parent_fallback(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertNotIn('git rev-parse HEAD^', text)
        self.assertIn('git merge-base "$PR_BASE" HEAD', text)
        self.assertIn('exit 1', text)
        self.assertIn('--requirement-gate', text)
        self.assertIn('needs: [unit, integration, full, pr-e2e]', text)


class ReviewEvidenceTests(unittest.TestCase):
    __requirements__ = ('TF-1', 'TF-3', 'TF-4', 'TF-5', 'TF-7', 'TF-9', 'TF-14')
    __why_not_scenario__ = 'Direct negative controls prove framework discovery, source integrity and evidence enforcement.'

    def test_B_note_stored_constant_hash_and_unresolved_dependencies_are_enforced(self):
        import test_framework as fw
        name = 'root:fixture.Case.test_value'
        legacy = {'tests': {name: 'source'}}
        baseline = {name: {'fingerprint': 'old'}}
        self.assertEqual([], fw.validate_legacy_values({name: {'dependency_values': 'old'}}, legacy, baseline))
        for observed in ('edited', None):
            self.assertIn(name, ' '.join(fw.validate_legacy_values({name: {'dependency_values': observed}}, legacy, baseline)))
        self.assertTrue(fw.validate_legacy_values({name: {'dependency_values': 'old'}}, legacy, {}))

    def test_C6_overridden_and_generated_cases_fail_preflight_by_full_id(self):
        from hypothesis.stateful import RuleBasedStateMachine, rule
        import test_framework as fw
        fixture = MetadataTests(); fixture.setUp()
        class Parent(unittest.TestCase):
            @fw.requirements('TF-3')
            def test_value(self):
                pass
        class Overridden(Parent):
            def test_value(self):
                pass
        class Machine(RuleBasedStateMachine):
            @rule()
            def advance(self):
                pass
        for case in (Overridden('test_value'), Machine.TestCase('runTest')):
            name = 'root:' + case.id()
            record = fw.describe_test(case)
            fixture.inventory = {fixture.module: {name: record}}
            self.assertIn(name + ': no requirement', fixture.errors(catalogue={}))
        Machine.TestCase.__requirements__ = ('TF-3',)
        Machine.TestCase.__why_not_scenario__ = 'Generated metadata fixture.'
        case = Machine.TestCase('runTest'); name = 'root:' + case.id()
        fixture.inventory = {fixture.module: {name: fw.describe_test(case)}}
        self.assertEqual([], fixture.errors(catalogue={'TF-3': {'statement': 'generated requirement', 'source': 'fixture', 'checks': [name]}}))

    def test_C13_inactive_and_duplicate_requirements_are_rejected(self):
        fixture = MetadataTests(); fixture.setUp()
        fixture.catalogue['TF-2']['active'] = False
        self.assertIn('inactive', ' '.join(fixture.errors()))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            fixture.fw.requirement_statements('**TF-1 — A.** first\n**TF-1 — B.** repeated')

    def test_B9_finished_test_cannot_own_module_teardown_violation(self):
        import io
        from test_worker import Guard, Result
        guard = Guard('component', 'root:fixture')
        result = Result(io.StringIO(), True, 1, guard=guard)
        test = unittest.FunctionTestCase(lambda: None)
        result.startTest(test); result.stopTest(test)
        self.assertIsNone(guard.test)
        with self.assertRaises(RuntimeError):
            guard.deny('teardown operation')
        self.assertIsNone(guard.violations[-1]['test'])

    def test_C13_reason_codes_and_golden_diff_are_exact_and_bounded(self):
        from test_golden import compare
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'reason').write_text('owner_changed\n' * 1000)
            with self.assertRaises(AssertionError) as raised:
                compare(root=root, name='reason', actual='io_error\n' * 1000, requirement='TF-14', declared={'TF-14'}, known={'TF-14'}, environ={})
            self.assertIn('owner_changed', str(raised.exception))
            self.assertLessEqual(len(str(raised.exception)), 4020)


class ReviewLifecycleTests(unittest.TestCase):
    __requirements__ = ('TF-5', 'TF-7', 'TF-9')
    __why_not_scenario__ = 'Execute the lifecycle assertion counter and cross-module dependency controls in isolated fixtures.'

    def test_C9_assertion_free_lifecycle_check_fails_the_real_gate(self):
        import e2e_lifecycle as lifecycle
        import test_framework as fw
        from unittest.mock import patch
        def assertion_free(phase):
            pass
        doc = {'requirements': [{'id': 'TF-5', 'flow': 'fixture', 'checks': ['assertion_free']}]}
        with patch.dict(lifecycle.CHECKS, {'assertion_free': (('TF-5',), assertion_free)}, clear=True), patch.object(lifecycle, 'requirements', return_value=doc):
            report = lifecycle.run_checks({'fixture'})
        self.assertEqual('check_asserted_nothing', report['checks'][0]['failure'])
        self.assertFalse(report['requirements']['TF-5']['passed'])
        outcomes = {'lifecycle:' + row['check']: 'passed' if row['passed'] else 'failed' for row in report['checks']}
        self.assertEqual('failed', fw.coverage({'TF-5': {'checks': ['lifecycle:assertion_free']}}, outcomes)['TF-5'])

    def test_C3_C13_cross_module_helper_is_found_by_source_path(self):
        import types
        import test_framework as fw
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def loaded(name, source):
                path = root / (name + '.py'); path.write_text(source)
                module = types.ModuleType(name); module.__file__ = str(path)
                sys.modules[name] = module; self.addCleanup(sys.modules.pop, name, None)
                exec(compile(source, str(path), 'exec'), module.__dict__)
                return module
            source = 'import unittest, outside_helper\nclass Case(unittest.TestCase):\n    def test_value(self): self.assertEqual(1,outside_helper.expected())\n'
            def fingerprint(value):
                loaded('outside_helper', 'def expected():\n    return ' + str(value) + '\n')
                return fw.dependency_fingerprint(loaded('test_cross_module', source).Case('test_value'))
            before = fingerprint(1)
            self.assertEqual(before, fingerprint(1))
            self.assertNotEqual(before, fingerprint(2))

    def test_B6_complete_shard_labels_cannot_hide_a_missing_worker(self):
        import test_framework as fw
        row = {'suite': 'root', 'unit': 'm', 'seconds': 1., 'names': ['m.a'], 'returncode': 0, 'outcomes': {'root:m.a': 'passed'}}
        report = fw.measurements([row], {'root:m': 'unit'}, {'root:m': 'unit'}, jobs=1, elapsed=1., selection=['root:m'],
            expected_fragments={'root:m': ['m', 'm#Case']})
        with self.assertRaisesRegex(ValueError, 'incomplete.*worker'):
            fw.combine_measurements([report])

    def test_B6_stage_records_must_share_source_and_layer_plans(self):
        import test_framework as fw
        import copy
        layers = {'root:m': 'unit', 'root:n': 'scenario'}
        stages = {'root:m': 'unit', 'root:n': 'e2e'}
        catalogue = {'TF-5': {'checks': ['root:m.a', 'root:n.b']}}
        records = []
        for module, case in [('m', 'a'), ('n', 'b')]:
            row = {'suite': 'root', 'unit': module, 'seconds': 1., 'names': [module+'.'+case], 'returncode': 0, 'outcomes': {'root:'+module+'.'+case: 'passed'}}
            records.append(fw.measurements([row], layers, stages, jobs=1, elapsed=1., selection=['root:'+module], catalogue=catalogue, fingerprints={'test':'same'}, invocation_id='one'))
        self.assertEqual({'TF-5':'passed'}, fw.combine_run_records(records)['requirements'])
        for key, value in [('fingerprints', {'test':'changed'}), ('layers', {'root:m':'soak','root:n':'scenario'}), ('stages', {}), ('budget_debt', {'modules':{'root:m':1000}})]:
            changed = copy.deepcopy(records)
            changed[1]['plan'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'incompatible'):
                fw.combine_run_records(changed)

    def test_A12_unknown_registry_id_is_a_diagnostic_not_a_key_error(self):
        from unittest import mock
        import generate_test_metadata as generator
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'scripts').mkdir(); (root/'tests/e2e').mkdir(parents=True)
            (root/'REQUIREMENTS-test-framework.md').write_text('**TF-1 — Fixture.** exact.\n')
            (root/'tests/e2e/requirements.json').write_text('{"requirements":[]}')
            (root/'scripts/test-checks.json').write_text('{"bad":{"requirements":["TF-999"]}}')
            with mock.patch.object(generator, 'ROOT', root), mock.patch.object(generator.parallel_tests, 'inventory', return_value={}), mock.patch.object(sys, 'argv', ['generator','--catalogue']):
                with self.assertRaisesRegex(SystemExit, 'bad: unknown registered requirement TF-999'):
                    generator.main()


    def test_A1_relocated_worktrees_keep_equal_fixture_hashes_without_hiding_constant_edits(self):
        import types
        import test_framework as fw
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            def fingerprint(label, expected):
                root = Path(folder) / label
                (root / 'tests').mkdir(parents=True, exist_ok=True)
                path = root / 'tests/test_relocation.py'
                source = 'import unittest\nfrom pathlib import Path\nFILE = __file__\nROOT = Path(__file__).parent\nEXPECTED = ' + repr(expected) + '\nclass Case(unittest.TestCase):\n    def test_value(self):\n        self.assertTrue(FILE.endswith("test_relocation.py"))\n        self.assertTrue(ROOT.is_dir())\n        self.assertEqual(EXPECTED, 1)\n'
                path.write_text(source)
                module = types.ModuleType('test_relocation'); module.__file__ = str(path)
                sys.modules[module.__name__] = module
                self.addCleanup(sys.modules.pop, module.__name__, None)
                exec(compile(source, str(path), 'exec'), module.__dict__)
                with patch.object(fw, '__file__', str(root / 'scripts/test_framework.py')):
                    return fw.dependency_fingerprint(module.Case('test_value'))
            first = fingerprint('first', 1)
            self.assertEqual(first, fingerprint('second', 1))
            self.assertNotEqual(first, fingerprint('second', 2))


if __name__ == '__main__':
    unittest.main()
