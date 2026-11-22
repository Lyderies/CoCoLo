#!/usr/bin/env python3
"""Generate the requirement catalogue; initialize legacy IDs once or remove migrated entries.

Run from the repository root using the prepared test interpreter:
  python -B scripts/generate_test_metadata.py --catalogue
  python -B scripts/generate_test_metadata.py --shrink-legacy
The one-time initialization uses the committed a1d25560 discovery inventory and refuses changed sources.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

import parallel_tests
from test_framework import BASELINE, decode_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', action='store_true')
    parser.add_argument('--initialize-legacy', action='store_true')
    parser.add_argument('--shrink-legacy', action='store_true')
    args = parser.parse_args()
    found = parallel_tests.inventory()
    tests = {key: record for cases in found.values() for key, record in cases.items()}
    if args.initialize_legacy:
        paths = [ROOT / 'scripts' / name for name in ('test-legacy.json', 'test-legacy-ceiling.json')]
        if any(path.exists() for path in paths):
            raise SystemExit('legacy baseline already exists; it must not be regenerated')
        baseline = decode_json((ROOT / 'measurements/test-framework/baseline-tests.json').read_text())
        if baseline['baseline_commit'] != BASELINE:
            raise SystemExit('wrong baseline inventory')
        legacy = {}
        for suite, modules in baseline['modules'].items():
            prefix = ROOT / 'tests' if suite == 'root' else ROOT / f'packages/{suite}-agents'
            for module, names in modules.items():
                file = prefix / (module.replace('.', '/') + '.py')
                source = subprocess.check_output(['git', 'show', BASELINE + ':' + str(file.relative_to(ROOT))], cwd=ROOT)
                if source != file.read_bytes():
                    raise SystemExit('baseline module changed: ' + module)
                for name in names:
                    legacy[suite + ':' + name] = tests[suite + ':' + name]['fingerprint']
        doc = {'baseline': BASELINE, 'tests': dict(sorted(legacy.items()))}
        for path in paths:
            path.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
    if args.shrink_legacy:
        path = ROOT / 'scripts/test-legacy.json'
        doc = decode_json(path.read_text())
        retained = {}
        for name, fingerprint in doc['tests'].items():
            if name in tests and not tests[name]['requirements']:
                if tests[name]['fingerprint'] != fingerprint:
                    raise SystemExit('changed legacy test must be tagged: ' + name)
                retained[name] = fingerprint
        doc['tests'] = retained
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
    if args.catalogue:
        text = (ROOT / 'REQUIREMENTS-test-framework.md').read_text()
        matches = list(re.finditer(r'^\*\*(TF-\d+) —', text, re.M))
        catalogue = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else text.index('\n## ', match.end())
            requirement = match[1]
            catalogue[requirement] = {'statement': text[match.start():end].strip(),
                                     'source': 'REQUIREMENTS-test-framework.md#' + requirement,
                                     'checks': sorted(key for key, row in tests.items() if requirement in row.get('requirements', []))}
        for row in decode_json((ROOT / 'tests/e2e/requirements.json').read_text())['requirements']:
            if row['id'] in catalogue:
                raise SystemExit('duplicate requirement: ' + row['id'])
            catalogue[row['id']] = {'statement': row['statement'], 'source': row['source'],
                                    'checks': ['lifecycle:' + name for name in row['checks']]}
        target = ROOT / 'scripts/test-requirements.json'
        # Preserve independently imported requirements, e.g. ML, with their reviewed statements.
        if target.exists():
            for requirement, row in decode_json(target.read_text()).items():
                if requirement not in catalogue:
                    catalogue[requirement] = {**row, 'checks': sorted(key for key, rec in tests.items() if requirement in rec.get('requirements', []))}
        target.write_text(json.dumps(catalogue, indent=1, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
