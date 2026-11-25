"""S13e executable-bytecode negative controls (no product change)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT
from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
from codex_agents.runtime_files import PACKAGE_DATA_FILES, RUNTIME_FILES

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements

SCENARIO = 'Fresh product-isolated interpreters; executable bytecode canaries need no sockets.'


class ProjectHostStartCostTests(unittest.TestCase):
    def run_python(self, root, program):
        return subprocess.run(
            [sys.executable, *PYTHON_ISOLATION_FLAGS, '-c', program, str(root)],
            env={'PATH': os.defpath}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=20)

    @requirements('S13E-2', why_not_scenario=SCENARIO)
    def test_host_and_deferred_imports_ignore_planted_and_stale_bytecode(self):
        self.assertEqual(('-I', '-B', '-S', '-X', 'pycache_prefix=/dev/null'), PYTHON_ISOLATION_FLAGS)
        modules = ('history_service', 'project_qa_checks', 'project_qa_commit', 'bridge', 'learning_engine')
        for mode in ('timestamp', 'stale', 'checked_hash', 'unchecked_hash', 'sourceless'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / '.codex' / 'orchestration'
                shutil.copytree(ORCHESTRATION_ROOT, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                shutil.copyfile(BUNDLE_ROOT / 'VERSION', Path(temporary) / 'VERSION')
                self.assertEqual(set(RUNTIME_FILES) | set(PACKAGE_DATA_FILES), {
                    p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()})
                marker = Path(temporary) / 'executed'
                for name in modules:
                    source = root / 'codex_agents' / (name + '.py')
                    original, info = source.read_bytes(), source.stat()
                    canary = ('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ran")\n').encode()
                    # Match source size/mtime: an ordinary timestamp loader would accept this planted code.
                    source.write_bytes(canary + b'#' * (len(original) - len(canary)))
                    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
                    invalidation = (py_compile.PycInvalidationMode.CHECKED_HASH if mode == 'checked_hash'
                                    else py_compile.PycInvalidationMode.UNCHECKED_HASH if mode == 'unchecked_hash'
                                    else py_compile.PycInvalidationMode.TIMESTAMP)
                    cache = source.with_suffix('.pyc') if mode == 'sourceless' else None
                    py_compile.compile(str(source), cfile=str(cache) if cache else None, doraise=True,
                                       invalidation_mode=invalidation)
                    source.write_bytes(original)
                    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + (2_000_000_000 if mode == 'stale' else 0)))
                # A source-less module is also never loaded from the package directory.
                absent = root / 'codex_agents' / 's13e_bytecode_only.py'
                absent.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ran")\n')
                py_compile.compile(str(absent), cfile=str(absent.with_suffix('.pyc')), doraise=True)
                absent.unlink()
                before = sorted(str(p.relative_to(root)) for p in root.rglob('*.pyc'))
                if mode in ('stale', 'checked_hash'):
                    # Positive control: the same planted bytes execute once the cache matches its source, so the
                    # rejection above is the validation (stale mtime / source hash), not a broken canary.
                    source = root / 'codex_agents' / 'bridge.py'
                    cache = Path(importlib.util.cache_from_source(str(source)))
                    planted, info = cache.read_bytes(), source.stat()
                    if mode == 'stale':
                        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns - 2_000_000_000))
                    else:
                        cache.write_bytes(planted[:8] + importlib.util.source_hash(source.read_bytes()) + planted[16:])
                    control = subprocess.run(
                        [sys.executable, '-I', '-B', '-S', '-c',
                         'import sys; sys.path.insert(0, sys.argv[1]); import codex_agents.bridge', str(root)],
                        env={'PATH': os.defpath}, capture_output=True, text=True, timeout=20)
                    self.assertEqual(0, control.returncode, control.stderr)
                    self.assertTrue(marker.exists(), 'negative control did not execute the planted pyc')
                    marker.unlink()
                    cache.write_bytes(planted)
                    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
                if mode in ('timestamp', 'unchecked_hash'):
                    control = subprocess.run(
                        [sys.executable, '-I', '-B', '-S', '-c',
                         'import sys; sys.path.insert(0, sys.argv[1]); import codex_agents.bridge', str(root)],
                        env={'PATH': os.defpath}, capture_output=True, text=True, timeout=20)
                    self.assertEqual(0, control.returncode, control.stderr)
                    self.assertTrue(marker.exists(), 'negative control did not execute the planted pyc')
                    marker.unlink()
                result = self.run_python(root, '''
import importlib, runpy, sys
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[1] + '/project_host.py', '--help']
try:
    host = runpy.run_path(sys.argv[0], run_name='s13e_host')
    host['main'](['--help'])
except SystemExit as error:
    assert error.code == 0
for name in ('bridge', 'learning_engine', 'project_qa_checks', 'project_qa_commit'):
    importlib.import_module('codex_agents.' + name)
''')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(marker.exists(), 'unverified bytecode executed')
                self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob('*.pyc')))
                # The product refuses source-less extras through its pre-import exact-file gate.
                (root / 'runtime-binding.json').write_text('{}')
                (root / 'install-manifest.json').write_text(json.dumps({
                    'files': {'.codex/orchestration/' + name: ''
                              for name in (*RUNTIME_FILES, *PACKAGE_DATA_FILES)}}))
                checked = self.run_python(root, '''
import runpy, sys
sys.argv = [sys.argv[1] + '/cli.py', '--help']
runpy.run_path(sys.argv[0], run_name='__main__')
''')
                self.assertEqual(2, checked.returncode, checked.stderr)
                self.assertIn('protected runtime copy holds files it should not', checked.stdout)
                self.assertFalse(marker.exists(), 'source-less bytecode executed before inventory refusal')

