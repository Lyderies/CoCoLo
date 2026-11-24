#!/usr/bin/env python3
"""TF-27: replay owner manifests; append only fully explained legacy proofs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import test_framework as fw
from generate_test_owned_baseline import extract
from rebind_release_bookkeeping import apply_fingerprints, write_new
from test_owned_baseline import SOURCE_REVISION, checked as checked_test_owned

MANIFESTS = 'scripts/test-approved-edits'
BINDINGS = 'scripts/test-approved-edit-bindings'
APPROVALS = MANIFESTS + '/APPROVALS.json'
FIELDS = ('fingerprint', 'dependencies', 'dependency_values')


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)


def revision(root, name):
    return git(root, 'rev-parse', '--verify', name + '^{commit}').decode().strip()


def immutable(root, path):
    name = path.relative_to(root).as_posix()
    try:
        commits = git(root, 'log', '--reverse', '--format=%H', 'HEAD', '--', name).decode().split()
    except subprocess.CalledProcessError:
        commits = []
    if not commits:
        raise ValueError(name + ': immutable approved-edit document changed or uncommitted')
    try:
        introduced = git(root, 'show', commits[0] + ':' + name)
        committed = git(root, 'show', 'HEAD:' + name)
    except subprocess.CalledProcessError as error:
        raise ValueError(name + ': immutable approved-edit document missing from introduction or HEAD') from error
    if introduced != committed or path.read_bytes() != committed:
        raise ValueError(name + ': immutable approved-edit document changed or uncommitted')
    return fw.decode_json(path.read_text()), commits[0]


def read_manifests(root, *, require_committed=True):
    result = []
    for path in sorted((p for p in (root / MANIFESTS).glob('*.json') if p.name != 'APPROVALS.json'),
                       key=lambda p: tuple(map(int, p.stem.split('.')))):
        if require_committed:
            data, _ = immutable(root, path)
        else:
            data = fw.decode_json(path.read_text())
            # Preparation permits new documents, never modification of an admission.
            if git(root, 'log', '--format=%H', 'HEAD', '--', path.relative_to(root).as_posix()).strip():
                immutable(root, path)
        if set(data) != {'schema', 'decision', 'transitions'} or data['schema'] != 1 or data['decision'] != path.stem:
            raise ValueError('invalid decision manifest')
        result.append(data)
    if not result:
        raise ValueError('no approved-edit manifests')
    return result


def authorize(root, manifests, binding, *, require_committed=True):
    """This mutable policy record is protected by owner review, not by its proposer."""
    path = root / APPROVALS
    if not path.is_file():
        raise ValueError('missing owner approval record')
    if require_committed:
        try:
            committed = git(root, 'show', 'HEAD:' + APPROVALS)
        except subprocess.CalledProcessError as error:
            raise ValueError('owner approval record missing from HEAD (uncommitted)') from error
        if path.read_bytes() != committed:
            raise ValueError('owner approval record changed or uncommitted')
    record = fw.decode_json(path.read_text())
    if set(record) != {'schema', 'decisions'} or record['schema'] != 1 or not isinstance(record['decisions'], dict):
        raise ValueError('invalid owner approval record')
    for manifest in manifests:
        decision = manifest['decision']
        digest = hashlib.sha256((root / MANIFESTS / (decision + '.json')).read_bytes()).hexdigest()
        if record['decisions'].get(decision) != {'manifest_sha256': digest, 'binding': binding}:
            raise ValueError(decision + ': owner approval does not authorize manifest digest and binding name')


def trusted_file(root, name):
    # ls-tree distinguishes a genuinely absent path from an unavailable revision,
    # damaged object database, permission error, or failed blob read.
    return bool(git(root, 'ls-tree', '--full-tree', SOURCE_REVISION, '--', name).strip())


def edit_bytes(source, edits):
    """Coordinates are in the edit commit; preserve all bytes outside listed lines."""
    lines = source.decode('utf-8').splitlines(keepends=True)
    output = []; cursor = 0
    for edit in edits:
        if set(edit) != {'line', 'old', 'new'} or type(edit['line']) is not int:
            raise ValueError('invalid line edit')
        old, new = edit['old'], edit['new']
        if not all(isinstance(s, str) and len(s.splitlines(keepends=True)) <= 1 for s in (old, new)) or not (old or new):
            raise ValueError('edits must record individual exact lines')
        gap = edit['line'] - 1 - len(output)
        if gap < 0 or cursor + gap > len(lines):
            raise ValueError('stale line coordinate')
        output.extend(lines[cursor:cursor + gap]); cursor += gap
        if old:
            if cursor >= len(lines) or lines[cursor] != old:
                raise ValueError('old line differs from trusted source (stale base)')
            cursor += 1
        if new:
            output.append(new)
    output.extend(lines[cursor:])
    return ''.join(output).encode('utf-8')


def exact_current(expected, current):
    if expected != current:
        raise ValueError('unlisted change in approved-edit files')


def exact_base(expected, before):
    if expected != before:
        raise ValueError('stale base: pre-edit sources differ from admitted state')


def sources(root, manifests, *, prior=()):
    # Earlier admissions establish the trusted input, but are never included
    # in this generation's source hashes or changed-proof membership.
    _, state = sources(root, prior) if prior else ({}, {})
    original = {}
    for manifest in manifests:
        for transition in manifest['transitions']:
            if set(transition) != {'file', 'before_commit', 'edit_commit', 'edits'}:
                raise ValueError('invalid historical transition')
            name = transition['file']; path = Path(name)
            if path.is_absolute() or '..' in path.parts or not name.startswith(('tests/', 'packages/codex-agents/tests/', 'packages/claude-agents/tests/')) or path.suffix != '.py':
                raise ValueError('manifest file must be a repository test source')
            base = revision(root, transition['before_commit']); commit = revision(root, transition['edit_commit'])
            if base != transition['before_commit'] or commit != transition['edit_commit'] or revision(root, commit + '^') != base:
                raise ValueError('stale base: expected exact parent of edit commit')
            before = git(root, 'show', base + ':' + name)
            if name not in original:
                if name in state:
                    original[name] = state[name]
                elif trusted_file(root, name):
                    original[name] = git(root, 'show', SOURCE_REVISION + ':' + name)
                else:
                    # Historical verification only. build() never overlays an
                    # unanchored file into either proof snapshot.
                    original[name] = before
                state[name] = original[name]
            exact_base({name: state[name]}, {name: before})
            after = edit_bytes(before, transition['edits'])
            if after != git(root, 'show', commit + ':' + name):
                raise ValueError(name + ': manifest differs from historical edit commit')
            state[name] = after
    return original, {name: state[name] for name in original}


def changed_proofs(before, after, seed, retired=(), *, candidate):
    result = {}
    for name, expected in seed.items():
        if name not in before or name not in after or name not in candidate:
            raise ValueError(name + ': changed legacy identity')
        new = after[name]; old = before[name]
        if any(old.get(f) != expected[f] for f in FIELDS):
            raise ValueError(name + ': trusted old proof differs from admitted baseline')
        if candidate[name].get('requirements'):
            if name in retired:
                # A normal migration is checked by requirement metadata, never
                # admitted as an approved-edit legacy proof.
                continue
            raise ValueError(name + ': changed legacy identity/metadata')
        if not all(new.get(f) for f in FIELDS):
            raise ValueError(name + ': unresolved approved-edit proof')
        if any(old.get(f) != new.get(f) for f in FIELDS):
            if name in retired:
                raise ValueError(name + ': retired changed legacy proof has no requirement')
            result[name] = {f: new[f] for f in FIELDS}
    for name in after.keys() - before.keys():
        if not after[name].get('requirements'):
            raise ValueError(name + ': new untagged/restored legacy identity')
    return result


def inventory(root, snapshot, seed, *, seed_path=None):
    """Fingerprint only: no fixtures/test bodies; at most ten modules per child."""
    if seed_path is None:
        with tempfile.TemporaryDirectory(prefix='approved-edit-seed-') as folder:
            return inventory(root, snapshot, seed, seed_path=Path(folder) / 'seed.json')
    from parallel_tests import worker_environment
    result = {}
    seed_path.write_text(json.dumps(seed))
    for suite in ('root', 'codex', 'claude'):
        modules = sorted({name.split(':', 1)[1].rsplit('.', 2)[0] for name in seed if name.startswith(suite + ':')})
        package = snapshot if suite == 'root' else snapshot / 'packages' / (suite + '-agents')
        for start in range(0, len(modules), 10):
            batch = modules[start:start + 10]
            code = f"""import sys,json,unittest
from pathlib import Path
root=Path({str(snapshot)!r}); package=Path({str(package)!r})
sys.path[:0]=[str(root/'scripts'),str(package/'src'),str(package),str(root/'tests')]
import test_framework as fw
seed=json.loads(Path({str(seed_path)!r}).read_text()); out={{}}
describe=fw.describe_test
def describe_seed(test, dependencies=()):
 key={suite!r}+':'+test.id()
 return describe(test,tuple(root/p for p in seed.get(key,{{}}).get('source_dependencies',[])))
fw.describe_test=describe_seed
layers=fw.decode_json((root/'scripts/test-layers.json').read_text())
# Reuse the ordinary guarded loader, load_tests handling, duplicate-ID rejection
# and sticky violation reporting. Limit discover's names, not its safeguards.
discover=unittest.TestLoader.discover
def selected_discover(loader, *args, **kwargs):
 tests=unittest.TestSuite()
 # inventory_suite's loader sets the guard layer while importing each module.
 # Use normal discover per exact filename so _get_module_from_name is retained.
 for name in {batch!r}:
  tests.addTests(discover(loader, 'tests', pattern=name.rsplit('.',1)[-1]+'.py',
                          top_level_dir='.' if (package/'tests/__init__.py').exists() else 'tests'))
 return tests
unittest.TestLoader.discover=selected_discover
found=fw.inventory_suite({suite!r},layers)
for cases in found.values():
 for key,row in cases.items():
  if row.get('import_error'):raise ValueError('trusted snapshot import/discovery guard failed: '+key)
  if key in out:raise ValueError('duplicate discovered test: '+key)
  out[key]=row
print(json.dumps(out))
"""
            output = subprocess.check_output([sys.executable, '-B', '-c', code], cwd=package,
                                             env=worker_environment(), text=True)
            rows = fw.decode_json(output)
            if result.keys() & rows.keys():
                raise ValueError('duplicate discovered test across replay batches')
            result.update(rows)
    return result


def seed_records(root):
    data = fw.decode_json((root / 'scripts/test-legacy-test-owned-v2.json').read_text())
    seed = checked_test_owned(root, {name: row['fingerprint'] for name, row in data['tests'].items()})
    # Historical explicit file dependencies are retained; policy rows only carry hashes.
    from generate_test_owned_baseline import seed_at, read_at
    historical = seed_at(root, SOURCE_REVISION)
    original_values = read_at(root, SOURCE_REVISION, 'scripts/test-legacy-captured-values.json')
    return {name: {**row, 'source_dependencies': historical[name].get(
                'source_dependencies', original_values.get(name, {}).get('source_dependencies', []))}
            for name, row in seed.items()}


def shrinking_ids(current, previous):
    if current['baseline'] != previous['baseline'] or any(
            previous['tests'].get(name) != value for name, value in current['tests'].items()):
        raise ValueError('legacy eligibility grew, restored an ID or changed its fingerprint')
    return set(current['tests'])


def active_ids(root, at=None):
    if at is None:
        # The command has the same historical shrink checks as the gate, plus
        # a working-tree-to-HEAD check so preparation cannot restore a retired ID.
        import parallel_tests as runner
        previous_base = runner.REVIEW_BASE
        try:
            runner.REVIEW_BASE = previous_base or fw.RELEASE_BASE
            current = fw.decode_json((root / 'scripts/test-legacy.json').read_text())
            ceiling = fw.decode_json((root / 'scripts/test-legacy-ceiling.json').read_text())
            _, _, errors = runner.legacy_versions(root, current, ceiling)
            if errors:
                raise ValueError('; '.join(errors))
            release = fw.decode_json((root / 'scripts/test-legacy-0850.json').read_text())
            runner.release_versions(root, release, 'test-legacy-0850-ceiling.json', 'test-legacy-0850.json')
        finally:
            runner.REVIEW_BASE = previous_base
    documents = []
    for name in ('test-legacy.json', 'test-legacy-0850.json'):
        committed = fw.decode_json(git(root, 'show', (at or 'HEAD') + ':scripts/' + name).decode())
        current = committed if at else fw.decode_json((root / 'scripts' / name).read_text())
        documents.append(shrinking_ids(current, committed))
    # Release eligibility supersedes original eligibility even after retirement.
    from rebind_release_bookkeeping import base_bindings
    return (documents[0] - base_bindings(root).keys()) | documents[1]


def listed_ids(root, at=None):
    """Retirement requires absence from both lists, before release supersession."""
    result = set()
    for name in ('test-legacy.json', 'test-legacy-0850.json'):
        path = 'scripts/' + name
        content = git(root, 'show', at + ':' + path).decode() if at else (root / path).read_text()
        result.update(fw.decode_json(content)['tests'])
    return result


def build(root, manifests, active=None, *, at=None, prior=(), admitted=None, prior_legacy=None):
    active = active_ids(root) if active is None else active
    before_sources, after_sources = sources(root, manifests, prior=prior)
    _, prior_sources = sources(root, prior)
    seed = seed_records(root)
    seed = {name: {**row, **(admitted or {}).get(name, {})} for name, row in seed.items()}
    # Only suites owning an edited source can be affected; within them inspect
    # every historical legacy module, including transitive class/helper users.
    suites = {'codex' if name.startswith('packages/codex-agents/') else
              'claude' if name.startswith('packages/claude-agents/') else 'root' for name in after_sources}
    seed = {name: row for name, row in seed.items() if name.split(':', 1)[0] in suites}
    # Earlier introductions already placed normally migrated identities outside
    # the binding. Their replayed sources may have changed without producing an
    # admitted proof row, so the original baseline is no longer their old proof.
    # Current-generation retirements still receive the full old-proof check.
    if prior_legacy is not None:
        seed = {name: row for name, row in seed.items() if name in prior_legacy}
    with tempfile.TemporaryDirectory(prefix='approved-edit-proof-') as folder:
        snapshot = Path(folder).resolve(); extract(root, SOURCE_REVISION, snapshot)
        shutil.copyfile(root / 'scripts/test_framework.py', snapshot / 'scripts/test_framework.py')
        for name, content in prior_sources.items():
            if trusted_file(root, name):
                path = snapshot / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        before = inventory(root, snapshot, seed)
        for name, content in after_sources.items():
            if not trusted_file(root, name):
                continue
            path = snapshot / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        after = inventory(root, snapshot, seed)
    # Replay overlays only manifest files. A normally migrated definition can
    # live elsewhere, so discover its metadata in the commit being bound (or
    # the immutable introduction when verifying), never in the replay snapshot.
    with tempfile.TemporaryDirectory(prefix='approved-edit-candidate-') as folder:
        candidate_tree = Path(folder).resolve()
        candidate_revision = at or 'HEAD'
        extract(root, candidate_revision, candidate_tree)
        candidate = inventory(root, candidate_tree, seed)
    tests = changed_proofs(before, after, seed, retired=seed.keys() - listed_ids(root, candidate_revision),
                           candidate=candidate)
    tests = {name: row for name, row in tests.items() if name in active}
    return dict(schema=1, source_revision=SOURCE_REVISION,
                algorithm_sha256=hashlib.sha256((root / 'scripts/test_framework.py').read_bytes()).hexdigest(),
                manifests={m['decision']: hashlib.sha256((root / MANIFESTS / (m['decision'] + '.json')).read_bytes()).hexdigest() for m in manifests},
                before_sources={n: hashlib.sha256(v).hexdigest() for n, v in before_sources.items()},
                after_sources={n: hashlib.sha256(v).hexdigest() for n, v in after_sources.items()}, tests=tests)


def admitted_paths(root, directory):
    names = git(root, 'log', '--full-history', '--diff-filter=A', '--format=', '--name-only',
                'HEAD', '--', directory).decode().splitlines()
    historical = {root / name for name in names if name.endswith('.json')}
    paths = historical | set((root / directory).glob('*.json'))
    for path in sorted(historical):
        if not path.is_file():
            raise ValueError('missing admitted document: ' + path.relative_to(root).as_posix())
    return sorted(paths)


def verify_introduction(root, manifests, data, introduction, *, prior=()):
    for manifest in manifests:
        _, commit = immutable(root, root / MANIFESTS / (manifest['decision'] + '.json'))
        for ancestor in [commit, *(t['edit_commit'] for t in manifest['transitions'])]:
            result = subprocess.run(['git', 'merge-base', '--is-ancestor', ancestor, introduction], cwd=root)
            if result.returncode == 1:
                raise ValueError('approved-edit introduction lacks manifest/edit ancestry')
            result.check_returncode()
    _, expected = sources(root, manifests, prior=prior)
    try:
        introduced = {name: git(root, 'show', introduction + ':' + name) for name in expected}
        algorithm = git(root, 'show', introduction + ':scripts/test_framework.py')
    except subprocess.CalledProcessError as error:
        raise ValueError('approved-edit introduction lacks source/algorithm blobs') from error
    if expected != introduced or hashlib.sha256(algorithm).hexdigest() != data['algorithm_sha256']:
        raise ValueError('approved-edit introduction source/algorithm differs from replay')


def binding_state(root, manifests=None):
    """Fully verify immutable generations before comparing cumulative live bytes."""
    # Disposable runner fixtures have neither admission documents nor Git
    # metadata. Do not invoke Git there; an actual repository must still check
    # history even when every current admission document has been deleted.
    documents = (root / BINDINGS).exists() or any((root / MANIFESTS).glob('*.json'))
    repository = (any(os.path.lexists(parent / '.git') for parent in (root, *root.parents))
                  or os.environ.get('GIT_DIR') or os.environ.get('GIT_COMMON_DIR')
                  or ((root / 'HEAD').is_file() and (root / 'objects').is_dir()))
    if not documents and not repository:
        return {}, [], None
    paths = admitted_paths(root, BINDINGS)
    admitted_paths(root, MANIFESTS)
    if not paths:
        return {}, [], None
    manifests = read_manifests(root) if manifests is None else manifests
    # Binding names convey authority, not chronology. Reconstruct generations
    # in introduction order even when a later name sorts before an earlier one.
    order = {commit: index for index, commit in enumerate(
        git(root, 'rev-list', '--reverse', '--topo-order', 'HEAD').decode().split())}
    documents = [(path, *immutable(root, path)) for path in paths]
    admitted = {}; covered = set(); prior = []; prior_legacy = None
    for path, data, introduction in sorted(documents, key=lambda row: (order[row[2]], row[0])):
        selected = [m for m in manifests if m['decision'] in data['manifests']]
        if {m['decision'] for m in selected} != set(data['manifests']):
            raise ValueError('missing admitted manifest')
        if covered.intersection(data['manifests']):
            raise ValueError('duplicate approved-edit binding')
        authorize(root, selected, path.stem)
        verify_introduction(root, selected, data, introduction, prior=prior)
        # Eligibility is frozen at introduction; later shrink-only retirement
        # filters application, never changes the immutable derivation.
        introduced_active = active_ids(root, introduction)
        rebuilt = build(root, selected, introduced_active, at=introduction, prior=prior,
                        admitted=admitted, prior_legacy=prior_legacy)
        if rebuilt != data:
            raise ValueError('immutable approved-edit binding does not match replayed proofs')
        covered.update(data['manifests']); admitted.update(data['tests']); prior.extend(selected)
        introduced_legacy = listed_ids(root, introduction)
        prior_legacy = introduced_legacy if prior_legacy is None else prior_legacy & introduced_legacy
    return admitted, prior, prior_legacy


def checked_bindings(root):
    admitted, prior, _ = binding_state(root)
    if not prior:
        return {}
    _, expected = sources(root, prior)
    exact_current(expected, {name: (root / name).read_bytes() for name in expected})
    active = active_ids(root)
    check_later_retirements(root, {name: row for name, row in admitted.items() if name not in active})
    return {name: row for name, row in admitted.items() if name in active}


def check_later_retirements(root, retired):
    """Filtering an old binding requires a current, normally migrated identity."""
    if not retired:
        return
    current = inventory(root, root, retired)
    listed = listed_ids(root)
    for name in retired:
        if name not in current:
            raise ValueError(name + ': retired bound test changed legacy identity')
        if not current[name].get('requirements'):
            raise ValueError(name + ': retired bound test has no requirement')
        if name in listed:
            raise ValueError(name + ': retired bound test must leave both legacy lists')
    # Reuse ordinary metadata validation, restricted to the identities whose
    # protection would otherwise disappear. The full gate still validates all
    # other catalogue checks and their authoritative requirement sources.
    modules = {name.rsplit('.', 2)[0] for name in retired}
    discovered = {module: {n: current[n] for n in retired if n.rsplit('.', 2)[0] == module}
                  for module in modules}
    stages = fw.decode_json((root / 'scripts/test-stages.json').read_text())
    layers = fw.decode_json((root / 'scripts/test-layers.json').read_text())
    catalogue = fw.decode_json((root / 'scripts/test-requirements.json').read_text())
    requirements = {requirement for name in retired for requirement in current[name]['requirements']}
    catalogue = {requirement: {**row, 'checks': [n for n in row.get('checks', []) if n in retired]}
                 for requirement, row in catalogue.items() if requirement in requirements}
    empty = {'baseline': 'retired', 'tests': {}}
    errors = fw.validate_metadata(inventory=discovered,
        stages={m: s for m, s in stages.items() if m in modules},
        layers={m: s for m, s in layers.items() if m in modules}, catalogue=catalogue,
        legacy=empty, ceiling=empty, previous=empty)
    if errors:
        raise ValueError('retired bound test requirement metadata invalid: ' + '; '.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True, help='new immutable binding name, e.g. 107-109-114.1')
    parser.add_argument('--prepare', action='store_true', help='prepare new uncommitted manifests for review; gate still requires a commit')
    args = parser.parse_args(); root = Path(__file__).resolve().parents[1]
    if not args.binding or any(c not in '0123456789.-' for c in args.binding):
        parser.error('invalid binding name')
    path = root / BINDINGS / (args.binding + '.json')
    try:
        if path.exists():
            raise ValueError('immutable binding already exists')
        manifests = read_manifests(root, require_committed=not args.prepare)
        admitted, prior, prior_legacy = binding_state(root, manifests)
        covered = {m['decision'] for m in prior}
        manifests = [m for m in manifests if m['decision'] not in covered]
        if not manifests:
            raise ValueError('no new approved-edit manifest')
        authorize(root, manifests, args.binding, require_committed=not args.prepare)
        _, expected = sources(root, [*prior, *manifests])
        exact_current(expected, {name: (root / name).read_bytes() for name in expected})
        active = active_ids(root)
        check_later_retirements(root, {name: row for name, row in admitted.items() if name not in active})
        data = build(root, manifests, active, prior=prior, admitted=admitted, prior_legacy=prior_legacy)
        write_new(path, data)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'{path.relative_to(root)}: {len(data["tests"])} existing proofs rebound; commit manifests and binding together')


if __name__ == '__main__':
    main()
