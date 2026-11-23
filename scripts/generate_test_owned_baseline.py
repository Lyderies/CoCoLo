#!/usr/bin/env python3
"""Generate TF-26's one-time immutable translation from the fixed reviewed tree."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import test_framework as fw
from test_owned_baseline import NAME, SOURCE_REVISION, validate_document


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)


def read_at(root, revision, name):
    return fw.decode_json(git(root, 'show', revision + ':' + name).decode())


def seed_at(root, revision):
    original = read_at(root, revision, 'scripts/test-legacy.json')['tests']
    dependencies = read_at(root, revision, 'scripts/test-legacy-dependencies.json')
    values = read_at(root, revision, 'scripts/test-legacy-captured-values.json')
    records = {name: {'fingerprint': fingerprint, 'dependencies': dependencies[name],
                      'dependency_values': values[name]['fingerprint']}
               for name, fingerprint in original.items()}
    release = {}
    for filename in ('test-legacy-release-0850.json', 'test-legacy-release-0850-bindings.json', 'test-legacy-release-0850-runtime.json'):
        release.update(read_at(root, revision, 'scripts/' + filename)['tests'])
    retained = read_at(root, revision, 'scripts/test-legacy-0850.json')['tests']
    for name in release:
        records.pop(name, None)
    records.update({name: release[name] for name in retained})
    names = git(root, 'ls-tree', '-r', '--name-only', revision, 'scripts/test-release-bindings').decode().splitlines()
    for name in sorted(names):
        for key, record in read_at(root, revision, name)['tests'].items():
            if key in records:
                records[key] = record
    return records


def admitted_at(root, revision):
    return {name: row['fingerprint'] for name, row in seed_at(root, revision).items()}


def extract(root, revision, target):
    # Only our trusted tree, regular files/directories; no external worktree edits.
    archive = git(root, 'archive', revision)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            if member.name.startswith(('measurements/', 'reviews/')):
                continue
            path = target / member.name
            if not path.resolve().is_relative_to(target.resolve()) or not (member.isdir() or member.isfile()):
                raise ValueError('unsafe trusted snapshot entry')
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(stream.extractfile(member).read())


def inventory(snapshot):
    from parallel_tests import worker_environment
    code = "import sys,json;sys.path.insert(0,'scripts');import parallel_tests as p;print(json.dumps(p.inventory()))"
    result = subprocess.check_output([sys.executable, '-B', '-c', code], cwd=snapshot,
                                     env=worker_environment(), text=True)
    return {name: row for cases in fw.decode_json(result).values() for name, row in cases.items()}


def translate(before, after, seed):
    result = {}
    for name, expected in seed.items():
        fields = ('fingerprint', 'dependencies', 'dependency_values')
        if name not in before or any(before[name].get(field) != expected[field] for field in fields):
            raise ValueError(name + ': trusted old proof differs from admitted baseline')
        if name not in after or after[name]['fingerprint'] != expected['fingerprint']:
            raise ValueError(name + ': migration changed test identity or implementation')
        if not after[name]['dependencies'] or not after[name]['dependency_values']:
            raise ValueError(name + ': unresolved translated dependency')
        result[name] = {field: after[name][field] for field in fields}
    return result


def generate(root, algorithm_revision):
    revision = git(root, 'rev-parse', '--verify', algorithm_revision + '^{commit}').decode().strip()
    algorithm = git(root, 'show', revision + ':scripts/test_framework.py')
    if algorithm != (root / 'scripts/test_framework.py').read_bytes():
        raise ValueError('algorithm must be committed before generating its baseline')
    seed = seed_at(root, SOURCE_REVISION)
    with tempfile.TemporaryDirectory(prefix='tf26-proof-') as folder:
        snapshot = Path(folder).resolve()
        extract(root, SOURCE_REVISION, snapshot)
        before = inventory(snapshot)
        (snapshot / 'scripts/test_framework.py').write_bytes(algorithm)
        after = inventory(snapshot)
    document = dict(schema=1, policy='test-owned-v1', source_revision=SOURCE_REVISION,
                    source_tree=git(root, 'rev-parse', SOURCE_REVISION + '^{tree}').decode().strip(),
                    algorithm_revision=revision, algorithm_sha256=hashlib.sha256(algorithm).hexdigest(),
                    generator_revision=git(root, 'rev-parse', 'HEAD').decode().strip(),
                    admitted={name: row['fingerprint'] for name, row in seed.items()},
                    tests=translate(before, after, seed))
    errors = validate_document(document, document['admitted'])
    if errors:
        raise ValueError('; '.join(errors))
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--algorithm', required=True, help='committed TF-26 algorithm revision')
    args = parser.parse_args(); root = Path(__file__).resolve().parents[1]; path = root / NAME
    if path.exists():
        parser.error('immutable test-owned baseline already exists')
    document = generate(root, args.algorithm)
    with path.open('x') as output:
        output.write(json.dumps(document, indent=1, sort_keys=True) + '\n')
    print(f'{NAME}: {len(document["tests"])} unchanged legacy proofs translated')


if __name__ == '__main__':
    main()
