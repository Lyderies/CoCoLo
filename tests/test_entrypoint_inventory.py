"""TF-13/16: keep a real executable boundary for every shipped entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements


@requirements('TF-13', 'TF-16', why_not_scenario='Cold interpreter/argument boundaries complement product scenarios and verify shipped programs start without test dependencies.')
class EntryPointTests(unittest.TestCase):
    def test_inventory_rejects_missing_entry_points_and_missing_real_checks(self):
        import test_framework as fw
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / 'packages/codex-agents/native-package/program.py'
            program.parent.mkdir(parents=True)
            program.write_text("if __name__ == '__main__':\n    print('entry')\n")
            name = program.relative_to(root).as_posix()
            check = 'root:test_boundary.Case.test_real'
            inventory = {name: {'checks': [check]}}
            self.assertTrue(fw.validate_entrypoints(root, {}, {check: 'scenario'}))
            self.assertTrue(fw.validate_entrypoints(root, inventory, {}))
            self.assertTrue(fw.validate_entrypoints(root, inventory, {check: 'unit'}))
            self.assertEqual([], fw.validate_entrypoints(root, inventory, {check: 'scenario'}))

    def test_argparse_entrypoints_start_in_real_stdlib_only_processes(self):
        manifest = json.loads((ROOT / 'scripts/test-entrypoints.json').read_text())
        orchestration = ROOT / 'packages/codex-agents/native-package/.codex/orchestration'
        with tempfile.TemporaryDirectory() as directory:
            for name, record in manifest.items():
                if record.get('probe') != 'help':
                    continue
                with self.subTest(entry=name):
                    path = ROOT / name
                    command = [sys.executable, '-B', '-S']
                    command += ['-m', record['module']] if record.get('module') else [str(path)]
                    result = subprocess.run(command + ['--help'], cwd=directory, timeout=5,
                                            env={**os.environ, 'PYTHONPATH': str(orchestration)}, capture_output=True)
                    self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
                    self.assertIn(b'usage:', result.stdout.lower())

    def test_job_reader_process_refuses_unbound_invocation_without_output(self):
        script = ROOT / 'packages/claude-agents/src/claude_agents/job_read_tool.py'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(script)], cwd=directory,
                                    input=b'', capture_output=True, timeout=5)
        self.assertEqual(2, result.returncode)
        self.assertEqual(b'', result.stdout)
        self.assertEqual(b'job_reader_denied\n', result.stderr)
