"""Replay TF-27 r1 witnesses with the reviewed validator or the working fix.

Use the task's pinned interpreter with -B. This does not modify the checkout.
The old numeric manifest reader predates APPROVALS.json; its glob alone is
filtered to decision manifests so red measures missing authorization rather
than failure to parse the newly introduced policy filename.
"""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import test_approved_edits as tests

BASE = '5d4ab8607767ca67330afc32e47bd792bf7e562f'
mode = sys.argv[1]
if mode not in ('red', 'green'):
    raise SystemExit('red or green required')
folder = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as temporary:
    if mode == 'red':
        path = Path(temporary) / 'reviewed_validator.py'
        path.write_bytes(subprocess.check_output(['git', 'show', BASE + ':scripts/rebind_approved_edits.py'], cwd=ROOT))
        original_glob = Path.glob
        def numeric_glob(directory, pattern):
            return (p for p in original_glob(directory, pattern) if p.name != 'APPROVALS.json')
        def reviewed_policy(self):
            spec = importlib.util.spec_from_file_location('reviewed_validator', path)
            policy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(policy)
            read = policy.read_manifests
            def decisions_only(*args, **kwargs):
                with mock.patch.object(Path, 'glob', numeric_glob):
                    return read(*args, **kwargs)
            policy.read_manifests = decisions_only
            return policy
        tests.ApprovedEditsR1.policy = reviewed_policy
    log = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(tests.ApprovedEditsR1)
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (folder / (mode + '.log')).write_text(log.getvalue())
    print(log.getvalue())
    if mode == 'red':
        failed = {case.id().split(' (')[0] for case, _ in result.failures}
        if result.errors or len(failed) != 7:
            raise SystemExit('expected seven intended witness failures and no errors')
    elif not result.wasSuccessful():
        raise SystemExit(1)
