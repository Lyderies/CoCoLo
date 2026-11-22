"""Test-only metadata, coverage and timing policy (TF-1..9, TF-16, TF-18)."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
from pathlib import Path
import platform
import re
import unittest
import uuid

BUDGETS = {'unit': 5., 'component': 30., 'crash': 60., 'scenario': 60., 'soak': 900.}
STAGES = {'unit': {'unit'}, 'component': {'integration'}, 'crash': {'integration'},
          'scenario': {'integration', 'e2e'}, 'soak': {'integration', 'e2e'}}
BASELINE = 'a1d25560c7748680387a542fb7d047c32205dad8'


def requirements(*ids, why_not_scenario=''):
    def declare(function):
        function.__requirements__ = tuple(ids)
        function.__why_not_scenario__ = why_not_scenario
        return function
    return declare


def decode_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def _source(value):
    try:
        return inspect.getsource(value).replace('\r\n', '\n')
    except (OSError, TypeError):
        code = getattr(value, '__code__', None)
        return repr((getattr(value, '__module__', ''), getattr(value, '__qualname__', ''),
                     code.co_code.hex() if code else 'generated'))


def describe_test(test):
    method = getattr(type(test), test._testMethodName)
    ids = getattr(method, '__requirements__', getattr(type(test), '__requirements__', ()))
    rationale = getattr(method, '__why_not_scenario__', getattr(type(test), '__why_not_scenario__', ''))
    # Include each defining fixture, including superclass fixtures used through super().
    sources = [_source(method)]
    for cls in type(test).__mro__:
        if cls in (unittest.TestCase, object):
            continue
        for name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass'):
            if name in cls.__dict__:
                sources.append(_source(cls.__dict__[name]))
    return {'requirements': list(ids), 'rationale': rationale,
            'fingerprint': hashlib.sha256('\n'.join(sources).encode()).hexdigest()}


def inventory_suite(suite):
    """Called only inside a suite's discovery child, honoring its load_tests hooks."""
    found = {}
    def walk(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            elif type(test).__name__ == '_FailedTest':
                name = test.id().split('.')[-1]
                found.setdefault(f'{suite}:{name}', {})[f'{suite}:{test.id()}'] = {'import_error': True}
            else:
                name = type(test).__module__
                key = f'{suite}:{test.id()}'
                module = found.setdefault(f'{suite}:{name}', {})
                if key in module:
                    raise ValueError(f'duplicate discovered test: {key}')
                module[key] = describe_test(test)
    top = '.' if Path('tests/__init__.py').exists() else 'tests'
    walk(unittest.defaultTestLoader.discover('tests', top_level_dir=top))
    return found


def validate_metadata(*, inventory, stages, layers, catalogue, legacy, ceiling, previous):
    errors = []
    tests = {key: value for module in inventory.values() for key, value in module.items()}
    for title, mapping in [('stage', stages), ('layer', layers)]:
        for key in sorted(set(inventory) - set(mapping)):
            errors.append(f'{key}: missing {title}')
        for key in sorted(set(mapping) - set(inventory)):
            errors.append(f'{key}: stale {title}')
    for module, cases in inventory.items():
        layer = layers.get(module)
        if layer not in STAGES:
            errors.append(f'{module}: unknown layer {layer!r}')
        elif stages.get(module) not in STAGES[layer]:
            errors.append(f'{module}: incompatible stage/layer {stages.get(module)}/{layer}')
        for name, record in cases.items():
            if record.get('import_error'):
                errors.append(f'{name}: test import failed')
                continue
            ids = record['requirements']
            if not ids and name not in legacy['tests']:
                errors.append(f'{name}: no requirement')
            for requirement in ids:
                if not isinstance(requirement, str) or not re.fullmatch(r'[A-Z][A-Za-z0-9_.-]*', requirement) or requirement not in catalogue:
                    errors.append(f'{name}: unknown requirement {requirement!r}')
                elif name not in catalogue[requirement]['checks']:
                    errors.append(f'{name}: {requirement} does not list its check')
            if layer != 'unit' and not record.get('rationale', '').strip() and name not in legacy['tests']:
                errors.append(f'{name}: why_not_scenario missing')
    for requirement, row in catalogue.items():
        if not row.get('statement', '').strip() or not row.get('source', '').strip() or not row.get('checks'):
            errors.append(f'{requirement}: requirement has no statement, source or check')
        if len(row.get('checks', [])) != len(set(row.get('checks', []))):
            errors.append(f'{requirement}: duplicate check')
        for name in row.get('checks', []):
            if name not in tests or requirement not in tests[name].get('requirements', []):
                errors.append(f'{requirement}: missing or unlinked check {name}')
    for name, fingerprint in legacy['tests'].items():
        if name not in tests:
            errors.append(f'{name}: stale legacy entry')
        elif tests[name].get('requirements'):
            errors.append(f'{name}: tagged test must leave legacy list')
        elif tests[name].get('fingerprint') != fingerprint:
            errors.append(f'{name}: legacy implementation changed; tag it')
        if any(version['tests'].get(name) != fingerprint for version in (ceiling, previous)):
            errors.append(f'{name}: legacy list grew or fingerprint changed')
    if legacy['baseline'] != ceiling['baseline'] or legacy['baseline'] != previous['baseline']:
        errors.append('legacy baseline changed')
    return errors


def coverage(catalogue, outcomes):
    result = {}
    for requirement, row in catalogue.items():
        states = [outcomes.get(name, 'not_run') for name in row['checks']]
        result[requirement] = ('failed' if not states or any(s not in ('passed', 'not_run') for s in states) else
                               'not_run' if 'not_run' in states else 'passed')
    return result


def select_layers(layers, profile):
    if profile not in ('dev', 'ci', 'soak'):
        raise ValueError(f'unknown test profile: {profile}')
    return {key for key, layer in layers.items() if layer != 'soak' or profile == 'soak'}


def measurements(rows, layers, stages, *, jobs, elapsed, selection, part=(1, 1), profile='ci', catalogue=None, fingerprints=None):
    modules = {}
    fragments = {}
    for row in rows:
        if not isinstance(row['seconds'], (int, float)) or not math.isfinite(row['seconds']) or row['seconds'] < 0:
            raise ValueError('worker elapsed time must be finite and nonnegative')
        module = row['suite'] + ':' + row['unit'].split('#')[0]
        modules[module] = modules.get(module, 0.) + row['seconds']
        fragments[module] = fragments.get(module, 0) + 1
    over = [{'module': name, 'seconds': seconds, 'budget': BUDGETS[layers[name]],
             'layer': layers[name], 'stage': stages[name], 'fragments': fragments[name]}
            for name, seconds in sorted(modules.items()) if seconds > BUDGETS[layers[name]]]
    plan = {'layers': layers, 'stages': stages, 'selection': sorted(selection), 'profile': profile,
            'catalogue': catalogue or {}, 'fingerprints': fingerprints or {}}
    return {'schema': 1, 'run_id': uuid.uuid4().hex, 'python': platform.python_version(),
            'platform': platform.system(), 'jobs': jobs, 'elapsed_seconds': elapsed,
            'job_seconds': sum(row['seconds'] for row in rows), 'workers': rows, 'modules': modules,
            'over_budget': over, 'partial': part != (1, 1), 'shard': list(part), 'plan': plan,
            'plan_digest': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}


def combine_measurements(reports):
    if not reports:
        raise ValueError('no shard reports')
    first = reports[0]
    count = first['shard'][1]
    if len(reports) != count or {r['shard'][0] for r in reports} != set(range(1, count + 1)):
        raise ValueError('missing or repeated shards')
    if any(r['shard'][1] != count or r['plan_digest'] != first['plan_digest'] for r in reports):
        raise ValueError('incompatible shard plans')
    rows = [row for report in reports for row in report['workers']]
    keys = [(row['suite'], row['unit']) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('overlapping shard workers')
    test_keys = [(row['suite'], name) for row in rows for name in row['names']]
    if len(test_keys) != len(set(test_keys)):
        raise ValueError('overlapping shard tests')
    result = measurements(rows, first['plan']['layers'], first['plan']['stages'],
                          selection=first['plan']['selection'], jobs=sum(r['jobs'] for r in reports),
                          elapsed=max(r['elapsed_seconds'] for r in reports), profile=first['plan']['profile'],
                          catalogue=first['plan']['catalogue'], fingerprints=first['plan']['fingerprints'])
    result['metadata_errors'] = sorted({error for report in reports for error in report.get('metadata_errors', [])})
    outcomes = {key: value for row in rows for key, value in row.get('outcomes', {}).items()}
    result['requirements'] = coverage(first['plan']['catalogue'], outcomes)
    return result


def validate_dependencies(root):
    errors = []
    text = (root / 'requirements-test.txt').read_text().replace('\\\n', ' ')
    names = set()
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z]+)==([0-9.]+)(?:\s+--hash=sha256:[a-f0-9]{64})+\s*', line)
        if not match:
            errors.append('test dependency is not exactly pinned and hashed')
        else:
            names.add(match[1])
    if names != {'hypothesis', 'sortedcontainers'}:
        errors.append('unexpected test dependency set')
    for package in (root / 'packages').iterdir():
        for file in (package / 'native-package').rglob('*.py'):
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                if any(m.split('.')[0] in names for m in modules):
                    errors.append(f'{file.relative_to(root)}: shipped test dependency import')
    return errors
