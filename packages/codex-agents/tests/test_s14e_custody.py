"""S14e (decision 380): live_ledger_custody looks run holds up by the full primary key and so grows linearly."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tests.support  # noqa: F401
from test_framework import requirements
from tests import test_learning_store as legacy

from codex_agents import runtime_lineage

ROOT = Path(__file__).resolve().parents[3]
WHY = 'The plan and the growth are properties of the real SQLite ledger schema and the real measurement runner.'
# Kinds whose identity is a launch handle digest: the run_slot hold (active_run_capacity.handle_digest) and the
# binding hold (host_run_bindings.handle_digest); a probe hold's identity is the probe request key.
HANDLE_KINDS = ('run_slot', 'binding')


class Recorder:
    """A connection stand-in that remembers every statement live_ledger_custody runs."""

    def __init__(self, db):
        self.db, self.calls = db, []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        return self.db.execute(sql, params)


class CustodyQueryTests(unittest.TestCase):
    @requirements('S14E-2', why_not_scenario=WHY)
    def test_S14E_2_every_custody_query_searches_run_holds_by_the_full_key(self):
        fixture = legacy.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        db = fixture.ledger._connection
        recorder = Recorder(db)
        runtime_lineage.live_ledger_custody(recorder, 2_000_000_000_000)
        self.assertGreaterEqual(len(recorder.calls), 2)
        seen = 0
        for sql, params in recorder.calls:
            steps = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, params)]
            for step in steps:
                if 'run_holds' not in step:
                    continue
                seen += 1
                self.assertNotIn('SCAN', step, step)
                self.assertIn('project_id=? AND kind=? AND identity=?', step, step)
        self.assertGreaterEqual(seen, 1)

    @requirements('S14E-1', why_not_scenario=WHY)
    def test_S14E_1_the_custody_hold_lookup_names_exactly_the_handle_kinds(self):
        source = Path(runtime_lineage.__file__).read_text()
        body = source[source.index('def live_ledger_custody'):source.index('def lineage_status')]
        self.assertIn("r.kind IN ('run_slot','binding')", body)
        self.assertNotIn('probe', body)

    @requirements('S14E-3', why_not_scenario=WHY)
    def test_S14E_3_custody_time_grows_about_linearly_on_small_ledgers(self):
        best = {}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as scratch:
            for size in (16, 128):
                out = Path(scratch) / f'size-{size}.json'
                done = subprocess.run(
                    [sys.executable, '-B', str(ROOT / 'scripts/run_measurement.py'), str(ROOT / 'scripts/measure_s14c.py'),
                     '--', '--size-mib', str(size), '--no-upgrade', '--output', str(out)],
                    cwd=ROOT, capture_output=True, text=True, timeout=240)
                self.assertEqual(done.returncode, 0, done.stdout[-500:] + done.stderr[-500:])
                report = json.loads(out.read_text())
                self.assertGreater(report['runs'], 100)
                best[size] = (report['runs'], report['custody']['live_ledger_custody']['best_of_3_seconds'])
        (runs_small, small), (runs_large, large) = best[16], best[128]
        run_ratio = runs_large / runs_small
        # Linear is about run_ratio (~3.7); the full scan before S14e was quadratic (~run_ratio squared, ~14).
        # A floor keeps a noise-sized denominator from deciding the answer.
        self.assertLess(large / max(small, 0.003), run_ratio ** 1.5, best)


if __name__ == '__main__':
    unittest.main()
