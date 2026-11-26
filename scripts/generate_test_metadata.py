#!/usr/bin/env python3
"""Generate the requirement catalogue: python -B scripts/generate_test_metadata.py --catalogue."""
import argparse
import json
from pathlib import Path

import parallel_tests
from test_framework import decode_json, requirement_statements

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', action='store_true')
    parser.add_argument('--output', help='write here instead of scripts/test-requirements.json (the committed file is still read)')
    args = parser.parse_args()
    found = parallel_tests.inventory()
    tests = {key: record for cases in found.values() for key, record in cases.items()}
    if args.catalogue:
        text = (ROOT / 'REQUIREMENTS-test-framework.md').read_text()
        catalogue = {}
        for requirement, statement in requirement_statements(text).items():
            catalogue[requirement] = {'statement': statement, 'source': 'REQUIREMENTS-test-framework.md#' + requirement,
                                     'checks': sorted(key for key, row in tests.items() if requirement in row.get('requirements', []))}
        # Lifecycle checks stay in tests/e2e/requirements.json, validated apart from this catalogue (parallel_tests.metadata).
        for row in decode_json((ROOT / 'tests/e2e/requirements.json').read_text())['requirements']:
            if row['id'] in catalogue:
                raise SystemExit('duplicate requirement: ' + row['id'])
            catalogue[row['id']] = {'statement': row['statement'], 'source': row['source'], 'checks': []}
        # GEN-3: every tests/requirements/<name>.json is an authority; its source is the file and the row id.
        for path in sorted((ROOT / 'tests/requirements').glob('*.json')):
            for row in decode_json(path.read_text())['requirements']:
                if row['id'] in catalogue:
                    raise SystemExit('duplicate requirement: ' + row['id'])
                catalogue[row['id']] = {'statement': row['statement'], 'source': 'tests/requirements/' + path.name + '#' + row['id'],
                                        'checks': sorted(key for key, rec in tests.items() if row['id'] in rec.get('requirements', []))}
        target = ROOT / 'scripts/test-requirements.json'
        # GEN-1: keep every hand-registered requirement and check already in the committed catalogue.
        if target.exists():
            for requirement, row in decode_json(target.read_text()).items():
                if requirement not in catalogue:
                    catalogue[requirement] = {**row, 'checks': []}
                for field in row.keys() - {'statement', 'source', 'checks'}:
                    catalogue[requirement][field] = row[field]
                catalogue[requirement]['checks'] = sorted(set(catalogue[requirement]['checks']) | set(row['checks']))
                for key, rec in tests.items():
                    if requirement in rec.get('requirements', []) and key not in catalogue[requirement]['checks']:
                        catalogue[requirement]['checks'].append(key)
                catalogue[requirement]['checks'].sort()
        registry = ROOT / 'scripts/test-checks.json'
        if registry.exists():
            for name, check in decode_json(registry.read_text()).items():
                for requirement in check['requirements']:
                    if requirement not in catalogue:
                        raise SystemExit(name + ': unknown registered requirement ' + requirement)
                    if name not in catalogue[requirement]['checks']:
                        catalogue[requirement]['checks'].append(name)
                    catalogue[requirement]['checks'].sort()
        Path(args.output or target).write_text(json.dumps(catalogue, indent=1, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
