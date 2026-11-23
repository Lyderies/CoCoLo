"""Test-only metadata, coverage and timing policy (TF-1..9, TF-16, TF-18)."""
from __future__ import annotations

import ast
import hashlib
from functools import lru_cache
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


@lru_cache(maxsize=None)
def _source(value):
    try:
        return inspect.getsource(value).replace('\r\n', '\n')
    except (OSError, TypeError):
        code = getattr(value, '__code__', None)
        return repr((getattr(value, '__module__', ''), getattr(value, '__qualname__', ''),
                     code.co_code.hex() if code else 'generated'))


def describe_test(test, source_dependencies=()):
    method = getattr(type(test), test._testMethodName)
    ids = getattr(method, '__requirements__', getattr(type(test), '__requirements__', ()))
    rationale = getattr(method, '__why_not_scenario__', None) or getattr(type(test), '__why_not_scenario__', '') or ''
    # Include each defining fixture, including superclass fixtures used through super().
    sources = [_source(method)]
    for cls in type(test).__mro__:
        if cls in (unittest.TestCase, object):
            continue
        for name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass'):
            if name in cls.__dict__:
                sources.append(_source(cls.__dict__[name]))
    return {'requirements': list(ids), 'rationale': rationale,
            'fingerprint': hashlib.sha256('\n'.join(sources).encode()).hexdigest(),
            'dependencies': dependency_fingerprint(test, include_values=False),
            'dependency_values': dependency_fingerprint(test, source_dependencies=source_dependencies)}


def inventory_suite(suite, layers=None):
    """Called only inside a suite's discovery child, honoring its load_tests hooks."""
    found = {}
    root = Path(__file__).resolve().parents[1]
    baseline_file = root / 'scripts/test-legacy-bound-values.json'
    if not baseline_file.exists():
        baseline_file = root / 'scripts/test-legacy-values.json'
    baseline = decode_json(baseline_file.read_text()) if baseline_file.exists() else {}
    def walk(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            elif type(test).__name__ == '_FailedTest':
                name = test._testMethodName
                found.setdefault(f'{suite}:{name}', {})[f'{suite}:{test.id()}'] = {'import_error': True}
            else:
                name = type(test).__module__
                key = f'{suite}:{test.id()}'
                module = found.setdefault(f'{suite}:{name}', {})
                if key in module:
                    raise ValueError(f'duplicate discovered test: {key}')
                module[key] = describe_test(test, tuple(root / name for name in baseline.get(key, {}).get('source_dependencies', ())))
    from test_profiles import activate
    from test_worker import Guard
    activate()
    guard = Guard('unit', 'discovery')
    guard.install()
    guard.layer = 'scenario'
    class Loader(unittest.TestLoader):
        def _get_module_from_name(self, name):
            guard.module = suite + ':' + name
            guard.layer = (layers or {}).get(guard.module, 'component')
            return super()._get_module_from_name(name)

        def loadTestsFromModule(self, module, *args, **kwargs):
            guard.module = suite + ':' + module.__name__
            guard.layer = (layers or {}).get(guard.module, 'component')
            return super().loadTestsFromModule(module, *args, **kwargs)
    top = '.' if Path('tests/__init__.py').exists() else 'tests'
    walk(Loader().discover('tests', top_level_dir=top))
    for violation in guard.violations:
        name = violation['module']
        found.setdefault(name, {})[name + '.__import_guard__'] = {'import_error': True}
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
            if layer != 'unit' and not (record.get('rationale') or '').strip() and name not in legacy['tests']:
                errors.append(f'{name}: why_not_scenario missing')
    for requirement, row in catalogue.items():
        if row.get('active') is False:
            errors.append(requirement + ': requirement cannot be marked inactive')
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



def validate_budget_debt(current, ceiling, previous):
    errors = []
    if current['baseline'] != ceiling['baseline'] or current['baseline'] != previous['baseline']:
        errors.append('budget debt baseline changed')
    for name, value in current['modules'].items():
        if any(version['modules'].get(name) != value for version in (ceiling, previous)):
            errors.append(f'{name}: budget debt grew, was restored or changed ceiling')
    return errors

def measurements(rows, layers, stages, *, jobs, elapsed, selection, part=(1, 1), profile='ci', catalogue=None, fingerprints=None,
                 budget_debt=None, discovery_seconds=0., preflight_seconds=0., invocation_id=None, expected_fragments=None):
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
    debt = (budget_debt or {}).get('modules', {})
    failures = [row for row in over if row['seconds'] > debt.get(row['module'], row['budget'])]
    warnings = [{'module': name, 'ceiling': value, 'seconds': modules.get(name),
                 'status': 'measured' if name in modules else 'not_measured'}
                for name, value in sorted(debt.items())]
    supplied_fragments = expected_fragments
    expected_fragments = expected_fragments or {}
    complete = {name for name, units in expected_fragments.items() if units and
                set(units) == {row['unit'] for row in rows if row['suite'] + ':' + row['unit'].split('#')[0] == name and row.get('returncode') == 0 and not row.get('timed_out')} }
    retirement = sorted(name for name in debt if name in complete and name in modules and modules[name] <= BUDGETS[layers[name]]) if part == (1, 1) else []
    plan = {'expected_fragments': supplied_fragments or {}, 'budget_debt': budget_debt or {}, 'layers': layers, 'stages': stages, 'selection': sorted(selection), 'profile': profile,
            'catalogue': catalogue or {}, 'fingerprints': fingerprints or {}}
    return {'schema': 1, 'run_id': uuid.uuid4().hex, 'invocation_id': invocation_id or __import__('os').environ.get('COCOLO_TEST_RUN_ID') or uuid.uuid4().hex, 'python': platform.python_version(),
            'platform': platform.system(), 'jobs': jobs, 'elapsed_seconds': elapsed,
            'job_seconds': sum(row['seconds'] for row in rows), 'workers': rows, 'modules': modules,
            'budget_failures': failures, 'budget_warnings': warnings, 'debt_retirement_required': retirement,
            'discovery_seconds': discovery_seconds, 'preflight_seconds': preflight_seconds,
            'over_budget': over, 'partial': part != (1, 1), 'shard': list(part), 'plan': plan,
            'plan_digest': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}


def combine_measurements(reports):
    if not reports:
        raise ValueError('no shard reports')
    first = reports[0]
    if not first.get('invocation_id') or any(r.get('invocation_id') != first['invocation_id'] for r in reports):
        raise ValueError('mixed or missing invocation identity')
    count = first['shard'][1]
    if len(reports) != count or {r['shard'][0] for r in reports} != set(range(1, count + 1)):
        raise ValueError('missing or repeated shards')
    if any(r['shard'][1] != count or r['plan_digest'] != first['plan_digest'] for r in reports):
        raise ValueError('incompatible shard plans')
    rows = [row for report in reports for row in report['workers']]
    keys = [(row['suite'], row['unit']) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('overlapping shard workers')
    expected = {(name.split(':', 1)[0], unit) for name, units in first['plan'].get('expected_fragments', {}).items() for unit in units}
    if expected and set(keys) != expected:
        raise ValueError('incomplete expected worker fragments')
    test_keys = [(row['suite'], name) for row in rows for name in row['names']]
    if len(test_keys) != len(set(test_keys)):
        raise ValueError('overlapping shard tests')
    result = measurements(rows, first['plan']['layers'], first['plan']['stages'],
                          selection=first['plan']['selection'], jobs=sum(r['jobs'] for r in reports),
                          elapsed=max(r['elapsed_seconds'] for r in reports), profile=first['plan']['profile'],
                          catalogue=first['plan']['catalogue'], fingerprints=first['plan']['fingerprints'],
                          budget_debt=first['plan']['budget_debt'],
                          discovery_seconds=sum(r['discovery_seconds'] for r in reports),
                          preflight_seconds=sum(r['preflight_seconds'] for r in reports), invocation_id=first['invocation_id'],
                          expected_fragments=first['plan'].get('expected_fragments'))
    result['metadata_errors'] = sorted({error for report in reports for error in report.get('metadata_errors', [])})
    outcomes = {key: value for row in rows for key, value in row.get('outcomes', {}).items()}
    result['requirements'] = coverage(first['plan']['catalogue'], outcomes)
    return result


def combine_run_records(reports):
    """Complete stage evidence for one invocation; not_run is never green."""
    if not reports:
        raise ValueError('missing run records')
    first = reports[0]
    groups = {}
    for report in reports:
        if report.get('invocation_id') != first.get('invocation_id'):
            raise ValueError('mixed invocation identity')
        if any(report['plan'][key] != first['plan'][key] for key in ('catalogue', 'profile', 'fingerprints', 'layers', 'stages', 'budget_debt')):
            raise ValueError('incompatible requirement/source/layer plan')
        groups.setdefault(report['plan_digest'], []).append(report)
    complete = [combine_measurements(group) for group in groups.values()]
    selected = {name for report in complete for name in report['plan']['selection']}
    required = {name for name, layer in first['plan']['layers'].items() if layer != 'soak'}
    if not required.issubset(selected):
        raise ValueError('missing/incomplete stage evidence: ' + repr(sorted(required - selected)))
    outcomes = {}
    for report in complete:
        if report.get('metadata_errors') or report['budget_failures'] or report['debt_retirement_required'] or any(w['returncode'] or w.get('timed_out') for w in report['workers']):
            raise ValueError('failed worker, metadata or budget evidence')
        for worker in report['workers']:
            for name, status in worker.get('outcomes', {}).items():
                if name in outcomes:
                    raise ValueError('duplicate stage check: ' + name)
                outcomes[name] = status
    for report in reports:
        for name, status in report.get('registered_outcomes', {}).items():
            if name in outcomes and outcomes[name] != status:
                raise ValueError('incompatible registered evidence: ' + name)
            outcomes[name] = status
    result = coverage(first['plan']['catalogue'], outcomes)
    failures = {name: status for name, status in result.items() if status != 'passed'}
    if failures:
        raise ValueError('requirements failed/not_run: ' + repr(failures))
    return {'invocation_id': first['invocation_id'], 'requirements': result,
            'job_seconds': sum(r['job_seconds'] for r in reports)}


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
    shipped = ('packages/codex-agents/native-package', 'packages/claude-agents/src',
               'setup.sh', 'scripts/install.py', 'scripts/setup-wizard.py', 'packages/codex-agents/scripts/install.py')
    files = set()
    for name in shipped:
        path = root / name
        if not path.exists():
            errors.append(name + ': missing shipped root')
        elif path.is_dir():
            files.update(path.rglob('*.py'))
            files.update(path.rglob('*requirements*.txt'))
            files.update(path.rglob('pyproject.toml'))
        else:
            files.add(path)
    for folder in (root, root / 'packages/claude-agents', root / 'packages/codex-agents'):
        for pattern in ('pyproject.toml', 'setup.py', 'setup.cfg', '*requirements*.txt'):
            files.update(path for path in folder.glob(pattern) if path.name != 'requirements-test.txt')
    for file in sorted(files):
        text = file.read_text()
        if file.suffix == '.py':
            tree = ast.parse(text)
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                if any(m.split('.')[0] in {'hypothesis', 'sortedcontainers'} for m in modules):
                    errors.append(f'{file.relative_to(root)}: shipped test dependency import')
        elif re.search(r'\b(hypothesis|sortedcontainers)\b', text, re.I):
            errors.append(f'{file.relative_to(root)}: shipped test dependency manifest/install reference')
    return errors


@lru_cache(maxsize=None)
def source_bindings(filename):
    try:
        tree = ast.parse(Path(filename).read_text())
    except (OSError, TypeError, SyntaxError):
        return {}
    bindings = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bindings[alias.asname or alias.name] = (node.module, alias.name)
    return bindings


def dependency_fingerprint(test, *, include_values=True, source_dependencies=()):
    """Hash local test fixtures/helpers transitively, including generated closure constants."""
    import sys
    import types
    seen, records = set(), {}
    unresolved = []
    cls = type(test)
    module = sys.modules[cls.__module__]
    explicit = tuple(getattr(cls, '__fixture_dependencies__', ())) + tuple(source_dependencies)
    explicit_files = {Path(value).resolve() for value in explicit if isinstance(value, (str, Path))}
    def covered(filename):
        return bool(filename and Path(filename).resolve() in explicit_files)
    def dependency_source(value):
        # Decorated/generated fixture methods belong to their original definition.
        original = inspect.unwrap(value)
        filename = inspect.getsourcefile(original)
        if filename and filename.startswith('<'):
            owner = sys.modules.get(original.__module__)
            return getattr(owner, '__file__', None)
        return filename
    worktree_root = Path(__file__).resolve().parents[1]
    worktree_prefix = str(worktree_root)
    def stable(value, owner_file=None):
        if isinstance(value, types.CodeType):
            return code_value(value)
        if isinstance(value, (set, frozenset)):
            return ('set', sorted((stable(v, owner_file) for v in value), key=repr))
        if isinstance(value, (tuple, list)):
            return (type(value).__name__, [stable(v, owner_file) for v in value])
        if isinstance(value, dict):
            return ('dict', sorted(((stable(k, owner_file), stable(v, owner_file)) for k, v in value.items()), key=repr))
        if isinstance(value, (str, bytes, int, float, bool, type(None))):
            if include_values and isinstance(value, str):
                if value == worktree_prefix or value.startswith(worktree_prefix + '/'):
                    return repr('<worktree>' + value[len(worktree_prefix):])
            return repr(value)
        if inspect.isfunction(value) or inspect.isclass(value):
            return (value.__module__, value.__qualname__)
        if include_values and inspect.isbuiltin(value) and isinstance(getattr(value, '__self__', None), (types.ModuleType, type(None))):
            return ('builtin', value.__module__, value.__qualname__)
        if include_values:
            if isinstance(value, Path):
                return ('path', value.relative_to(worktree_root).as_posix() if value.is_relative_to(worktree_root) else value.as_posix())
            if not covered(owner_file):
                unresolved.append(type(value).__module__ + ':' + type(value).__qualname__)
        return type(value).__module__ + ':' + type(value).__qualname__
    def global_value(name, globals_, visited=()):
        value = globals_.get(name)
        filename = globals_.get('__file__')
        binding = source_bindings(filename).get(name) if filename else None
        if isinstance(binding, tuple) and binding not in visited:
            imported = sys.modules.get(binding[0])
            if imported is not None and (binding[0].startswith(('tests.', 'test_')) or local(imported)):
                return global_value(binding[1], vars(imported), visited + (binding,))
        dynamic = binding is not None and not isinstance(binding, tuple) and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and ast.unparse(n.func).startswith(('secrets.', 'random.', 'time.', 'tempfile.', 'os.path.realpath'))
            or isinstance(n, ast.Attribute) and ast.unparse(n) == 'os.environ'
            for n in ast.walk(binding))
        if dynamic:
            if not covered(filename):
                unresolved.append(name + ': runtime fixture value needs explicit source dependency')
            return ('source-defined', ast.dump(binding, include_attributes=False))
        return stable(value, filename)

    def code_value(code):
        return (code.co_code.hex(), code.co_names, tuple(stable(v, code.co_filename) for v in code.co_consts))
    def local(value):
        owner = getattr(value, '__module__', '')
        try:
            path = getattr(value, '__file__', None) if isinstance(value, types.ModuleType) else inspect.getsourcefile(value) if inspect.isfunction(value) or inspect.isclass(value) else None
        except TypeError:
            path = None
        return owner == cls.__module__ or owner.startswith(('tests.', 'test_')) or bool(path and ('/tests/' in path or include_values and Path(path).parent == Path(getattr(module, '__file__', '')).parent))
    def walk(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (classmethod, staticmethod)):
            walk(value.__func__)
        elif inspect.ismethod(value):
            walk(value.__func__)
        elif inspect.isfunction(value) and local(value):
            key = value.__module__ + ':' + value.__qualname__
            records[key] = (_source(value), code_value(value.__code__))
            if include_values and any(name in value.__code__.co_names for name in ('getattr', '__import__', 'import_module', 'eval', 'exec')) and not covered(dependency_source(value)):
                unresolved.append(key + ': dynamic helper needs explicit source dependency')
            for name in value.__code__.co_names:
                dependency = value.__globals__.get(name)
                if inspect.isfunction(dependency) or inspect.isclass(dependency):
                    walk(dependency)
                elif isinstance(dependency, types.ModuleType) and (dependency.__name__.startswith(('tests.', 'test_')) or include_values and local(dependency)):
                    for referenced in value.__code__.co_names:
                        item = getattr(dependency, referenced, None)
                        if include_values and referenced in vars(dependency) and not callable(item) and not isinstance(item, types.ModuleType):
                            records[key + ':module:' + dependency.__name__ + ':' + referenced] = global_value(referenced, vars(dependency))
                        else:
                            walk(item)
                elif include_values and name in value.__globals__ and not isinstance(dependency, types.ModuleType):
                    records[key + ':global:' + name] = global_value(name, value.__globals__)
                for parent in cls.__mro__:
                    if parent not in (object, unittest.TestCase) and name in vars(parent):
                        item = vars(parent)[name]
                        if include_values and not callable(item) and not isinstance(item, (classmethod, staticmethod)):
                            records[key + ':class:' + parent.__qualname__ + ':' + name] = stable(item, inspect.getsourcefile(parent))
                        else:
                            walk(item)
            for index, cell in enumerate(value.__closure__ or ()):
                item = cell.cell_contents
                if inspect.isfunction(item) or inspect.isclass(item):
                    walk(item)
                elif isinstance(item, (str, bytes, int, float, bool, tuple, list, dict, type(None))):
                    records[key + ':closure:' + str(index)] = stable(item, dependency_source(value))
        elif inspect.isclass(value) and local(value):
            for item in vars(value).values():
                walk(item)
    walk(getattr(cls, test._testMethodName))
    for parent in cls.__mro__:
        if parent in (object, unittest.TestCase):
            continue
        for name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass'):
            walk(vars(parent).get(name))
    for name in ('setUpModule', 'tearDownModule'):
        walk(getattr(module, name, None))
    for value in explicit:
        if include_values and isinstance(value, (str, Path)):
            path = Path(value)
            try:
                resolved = path.resolve()
                identity = resolved.relative_to(worktree_root).as_posix() if resolved.is_relative_to(worktree_root) else resolved.as_posix()
                records['explicit:' + identity] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                unresolved.append(str(path))
        else:
            walk(value)
    if include_values and unresolved:
        return None
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def validate_legacy_values(records, legacy, baseline):
    errors = []
    for name in legacy['tests']:
        observed = records.get(name, {}).get('dependency_values')
        expected = baseline.get(name, {}).get('fingerprint')
        if observed is None or expected is None or observed != expected:
            errors.append(name + ': legacy constants/helper sources changed or unresolved; migrate its requirement proof')
    return errors


def golden_bypasses(source, *, filename=None):
    """Follow local imported path declarations without executing fixture modules."""
    def analyze(source, filename, seen):
        tree = ast.parse(source)
        golden_names = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names if 'golden' in a.name.lower()}
        if filename is not None:
            filename = Path(filename).resolve()
            seen = seen | {filename}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for alias in node.names:
                    module_name = node.module if isinstance(node, ast.ImportFrom) else alias.name
                    if not module_name:
                        continue
                    parts = module_name.split('.')
                    bases = ([filename.parents[node.level - 1]] if isinstance(node, ast.ImportFrom) and node.level
                             else [filename.parent, *filename.parents])
                    candidates = [path for base in bases for path in
                                  (base.joinpath(*parts) / '__init__.py', base.joinpath(*parts).with_suffix('.py'))]
                    dependency = next((path for path in candidates if path.is_file()), None)
                    if dependency is None or dependency in seen:
                        continue
                    exported, _ = analyze(dependency.read_text(), dependency, seen)
                    if isinstance(node, ast.ImportFrom):
                        if alias.name in exported:
                            golden_names.add(alias.asname or alias.name)
                    else:
                        local_name = alias.asname or alias.name
                        golden_names.update(local_name + '.' + name for name in exported)
        temporary_names = set()
        def references(node, names):
            return any(isinstance(n, (ast.Name, ast.Attribute)) and ast.unparse(n) in names for n in ast.walk(node))
        def temporary(node):
            return references(node, temporary_names) or any(isinstance(n, ast.Call) and
                isinstance(n.func, ast.Attribute) and n.func.attr in ('TemporaryDirectory', 'mkdtemp') for n in ast.walk(node))
        def golden(node):
            if temporary(node):
                return False
            return references(node, golden_names) or any(
                isinstance(n, ast.Constant) and isinstance(n.value, str) and 'golden' in n.value.lower()
                for n in ast.walk(node))
        assignments = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value]
        for _ in range(len(assignments) + 1):
            previous = (set(golden_names), set(temporary_names))
            for node in assignments:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {ast.unparse(target) for target in targets if isinstance(target, (ast.Name, ast.Attribute))}
                if temporary(node.value):
                    temporary_names.update(names)
                    golden_names.difference_update(names)
                elif golden(node.value):
                    golden_names.update(names)
            if previous == (golden_names, temporary_names):
                break
        errors = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in ('read_text', 'read_bytes', 'open') and golden(node.func.value):
                errors.append(f'line {node.lineno}: direct golden read bypasses shared helper')
            elif isinstance(node.func, ast.Name) and node.func.id == 'open' and any(golden(arg) for arg in node.args):
                errors.append(f'line {node.lineno}: direct golden open bypasses shared helper')
        return golden_names, errors
    return analyze(source, filename, set())[1]


def registered_outcomes(checks, evidence):
    outcomes = {}
    for name, check in checks.items():
        record = evidence.get(name)
        if record is None:
            outcomes[name] = 'not_run'
        else:
            outcomes[name] = 'failed'
            try:
                revision = check.get('revision')
                path = Path(record['record'])
                data = path.read_bytes()
                contents = decode_json(data.decode())
                if (check['kind'] == 'review' and isinstance(revision, str) and revision.strip()
                        and record.get('revision') == revision == contents.get('revision')
                        and record.get('sha256') == hashlib.sha256(data).hexdigest()
                        and record.get('outcome') == contents.get('outcome') == 'passed'):
                    outcomes[name] = 'passed'
            except (OSError, KeyError, ValueError, TypeError):
                pass
    return outcomes


def requirement_statements(text):
    """One exact, EOF-safe requirement splitter for generation and validation."""
    pattern = r'^\*\*([A-Z][A-Za-z0-9_.-]*) —.*?(?=\n\*\*[A-Z][A-Za-z0-9_.-]* —|\n## |\Z)'
    result = {}
    for match in re.finditer(pattern, text, re.M | re.S):
        if match[1] in result:
            raise ValueError('duplicate requirement: ' + match[1])
        result[match[1]] = match[0].strip()
    return result


def validate_sources(root, catalogue):
    """Compare exact text to checked-in authority, including the original 80 lifecycle statements."""
    errors = []
    lifecycle = root / 'tests/e2e/requirements.json'
    lifecycle_rows = {r['id']: r for r in decode_json(lifecycle.read_text())['requirements']} if lifecycle.exists() else {}
    authority = set(lifecycle_rows)
    for path in root.glob('REQUIREMENTS*.md'):
        authority.update(requirement_statements(path.read_text()))
    if not catalogue:
        errors.append('empty requirement catalogue')
    errors.extend(name + ': missing authoritative requirement' for name in sorted(authority - set(catalogue)))
    for name, record in catalogue.items():
        try:
            if name in lifecycle_rows:
                expected = lifecycle_rows[name]
                if (record['statement'], record['source']) != (expected['statement'], expected['source']):
                    raise ValueError('lifecycle source mismatch')
                continue
            file, separator, anchor = record['source'].partition('#')
            path = root / file
            if not separator or anchor != name or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('source must identify a local requirement anchor')
            text = path.read_text()
            if path.suffix == '.json':
                expected = next(r['statement'] for r in decode_json(text)['requirements'] if r['id'] == name)
            else:
                expected = requirement_statements(text).get(name)
            if record['statement'] != expected:
                raise ValueError('statement differs from cited source')
        except (OSError, ValueError, KeyError, StopIteration):
            errors.append(name + ': statement/source evidence mismatch')
    return errors


def discover_entrypoints(root):
    paths = set()
    for folder in ('packages/codex-agents/native-package', 'packages/claude-agents/src'):
        for path in (root / folder).rglob('*.py'):
            parsed = ast.parse(path.read_text())
            if any(isinstance(node, ast.If) and '__name__' in ast.unparse(node.test)
                   and '__main__' in ast.unparse(node.test) for node in parsed.body):
                paths.add(path.relative_to(root).as_posix())
    for name in ('setup.sh', 'scripts/install.py', 'scripts/setup-wizard.py', 'packages/codex-agents/scripts/install.py'):
        if (root / name).is_file():
            paths.add(name)
    return paths


def validate_entrypoints(root, inventory, check_layers):
    errors = []
    actual = discover_entrypoints(root)
    for name in sorted(actual ^ set(inventory)):
        errors.append(name + ': missing or stale entry-point inventory')
    for name, record in inventory.items():
        if not record.get('checks'):
            errors.append(name + ': entry point has no real check')
        for check in record.get('checks', []):
            if check_layers.get(check) not in ('crash', 'scenario', 'soak'):
                errors.append(name + ': missing real check ' + check)
    return errors
