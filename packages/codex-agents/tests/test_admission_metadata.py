from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.support import ORCHESTRATION_ROOT
from codex_agents import admission_metadata, providers


FIXTURE = Path(__file__).resolve().parent / 'fixtures/learning/admission-metadata-vectors.json'


def calculate(module, case):
    arguments = json.loads(json.dumps(case['arguments']))
    if case['function'] == 'evaluate_admission':
        arguments['prior_claude_admissions'] = set(arguments['prior_claude_admissions'])
        if arguments.get('cited_digests') is not None:
            arguments['cited_digests'] = set(arguments['cited_digests'])
    functions = {name: getattr(module, name) for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms')}
    return json.loads(json.dumps(functions[case['function']](**arguments)))


class AdmissionMetadataTests(unittest.TestCase):
    def test_legacy_calculations_and_import_entrypoints_are_preserved(self):
        for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms'):
            self.assertIs(getattr(admission_metadata, name), getattr(providers, name))
        vectors = json.loads(FIXTURE.read_text())
        self.assertEqual(35, len(vectors['cases']))
        for case in vectors['cases']:
            with self.subTest(name=case['name']):
                self.assertEqual(case['expected'], calculate(admission_metadata, case))

    def test_cold_import_excludes_provider_persistence_and_all_calculation_io(self):
        program = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
with open(sys.argv[2]) as source:
    vectors = json.load(source)
allowed = {'codex_agents', 'codex_agents.contracts', 'codex_agents.learning_contracts',
           'codex_agents.admission_metadata'}
class ImportBoundary:
    def find_spec(self, name, path=None, target=None):
        if name.partition('.')[0] in {'sqlite3', '_sqlite3'} or (name.startswith('codex_agents.') and name not in allowed):
            raise AssertionError('metadata dependency boundary crossed')
sys.meta_path.insert(0, ImportBoundary())
from codex_agents import admission_metadata as metadata
loaded = {name for name in sys.modules if name == 'codex_agents' or name.startswith('codex_agents.')}
assert loaded <= allowed
assert 'sqlite3' not in sys.modules and '_sqlite3' not in sys.modules
def audit(event, arguments):
    if event == 'open' or event.startswith(('socket.', 'subprocess.', 'sqlite3.')) or event in {
        'os.system', 'os.fork', 'os.posix_spawn', 'os.chdir', 'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir'
    }:
        raise AssertionError('metadata calculation attempted external work')
sys.addaudithook(audit)
functions = {name: getattr(metadata, name) for name in ('evaluate_admission', 'evaluate_launch', 'window_start_ms')}
for case in vectors['cases']:
    arguments = case['arguments']
    if case['function'] == 'evaluate_admission':
        arguments['prior_claude_admissions'] = set(arguments['prior_claude_admissions'])
        if arguments.get('cited_digests') is not None:
            arguments['cited_digests'] = set(arguments['cited_digests'])
    actual = json.loads(json.dumps(functions[case['function']](**arguments)))
    assert actual == case['expected'], case['name']
print(json.dumps({'cases': len(vectors['cases']), 'application_modules': sorted(loaded)}))
'''
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-S', '-c', program, str(ORCHESTRATION_ROOT), str(FIXTURE)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
            env={'PATH': os.defpath, 'LC_ALL': 'C'}, close_fds=True,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        report = json.loads(result.stdout)
        self.assertEqual(35, report['cases'])
        self.assertEqual({'codex_agents', 'codex_agents.contracts', 'codex_agents.learning_contracts',
                          'codex_agents.admission_metadata'}, set(report['application_modules']))
