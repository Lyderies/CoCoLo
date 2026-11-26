"""S14d (decision 371): the measurement runner enforces its caps. Tiny fake scripts, low caps, seconds each."""
import ast
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements

RUNNER = ROOT / 'scripts' / 'run_measurement.py'
WHY = ('The runner is a process supervisor; its caps are proven only by real child processes, '
       'and no product service, ledger or provider is involved.')
DIRECT = re.compile(r'(python[0-9.]*|\$PY|"\$PY"|\bPY)\b[^|\n]*\bmeasure_[A-Za-z0-9_]+\.py')
SCAN_ROOTS = ('docs', 'scripts', 'packages/*/scripts')
SCAN_SUFFIXES = {'.md', '.py', '.sh', '.txt', '.yml', '.yaml'}


def measure_scripts():
    found = sorted(ROOT.glob('scripts/measure_*.py')) + sorted(ROOT.glob('packages/*/scripts/measure_*.py'))
    return [path for path in found if path.is_file()]


def declared_budget(path):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MEASUREMENT_BUDGET' for t in node.targets):
            return ast.literal_eval(node.value)
    return None


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.env = dict(os.environ, TMPDIR=str(self.dir))

    def fake(self, body, header=''):
        path = self.dir / 'measure_fake.py'
        path.write_text(header + 'import os, sys, time\nout = os.environ["MEASUREMENT_OUTPUT_DIR"]\n' + textwrap.dedent(body))
        return path

    def run_runner(self, script, *flags, timeout=60):
        started = time.monotonic()
        done = subprocess.run([sys.executable, '-B', str(RUNNER), *flags, str(script)], env=self.env,
                              capture_output=True, text=True, timeout=timeout)
        done.elapsed = time.monotonic() - started
        return done

    def leftovers(self):
        return [p.name for p in self.dir.iterdir() if p.name.startswith('measure-')]


class RunnerCapTests(RunnerCase):
    @requirements('S14D-1', why_not_scenario=WHY)
    def test_wall_clock_cap_kills_and_says_plainly(self):
        done = self.run_runner(self.fake('time.sleep(60)\n'), '--wall', '1')
        self.assertEqual(3, done.returncode, done.stderr)
        self.assertLess(done.elapsed, 15)
        line = (done.stdout + done.stderr).strip()
        self.assertEqual(1, len(line.splitlines()), line)
        for word in ('wall clock', '1 s', '--wall'):
            self.assertIn(word, line)

    @requirements('S14D-1', 'S14D-2', why_not_scenario=WHY)
    def test_data_cap_kills_and_deletes_the_data(self):
        body = ('i = 0\nwhile True:\n    open(os.path.join(out, f"f{i}"), "wb").write(b"x" * 1048576)\n'
                '    i += 1\n    time.sleep(0.05)\n')
        done = self.run_runner(self.fake(body), '--max-data-mib', '3', '--wall', '40')
        self.assertEqual(3, done.returncode, done.stderr)
        self.assertLess(done.elapsed, 15)
        line = (done.stdout + done.stderr).strip()
        self.assertEqual(1, len(line.splitlines()), line)
        for word in ('generated data', '3 MiB', '--max-data-mib'):
            self.assertIn(word, line)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_memory_cap_kills_and_says_plainly(self):
        body = 'block = bytearray(b"y" * (80 * 1048576))\ntime.sleep(60)\n'
        done = self.run_runner(self.fake(body), '--max-rss-mib', '40', '--wall', '40')
        self.assertEqual(3, done.returncode, done.stderr)
        self.assertLess(done.elapsed, 20)
        line = (done.stdout + done.stderr).strip()
        self.assertEqual(1, len(line.splitlines()), line)
        for word in ('memory', '40 MiB', '--max-rss-mib'):
            self.assertIn(word, line)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_the_whole_process_group_is_killed_and_nothing_else(self):
        marker = self.dir / 'grandchild.pid'
        body = ('import subprocess\np = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                f'open({str(marker)!r}, "w").write(str(p.pid))\ntime.sleep(60)\n')
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.terminate)
        done = self.run_runner(self.fake(body), '--wall', '2')
        self.assertEqual(3, done.returncode, done.stderr)
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail('the grandchild survived the kill')
        self.assertIsNone(bystander.poll())

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_wall_clock_hard_maximum_needs_a_decision_number(self):
        script = self.fake('pass\n')
        refused = self.run_runner(script, '--wall', '901')
        self.assertEqual(2, refused.returncode)
        self.assertIn('900', refused.stderr + refused.stdout)
        self.assertIn('--approve-decision', refused.stderr + refused.stdout)
        for bad in ('abc', '0', '-3'):
            self.assertEqual(2, self.run_runner(script, '--wall', '901', '--approve-decision', bad).returncode)
        allowed = self.run_runner(script, '--wall', '901', '--approve-decision', '371')
        self.assertEqual(0, allowed.returncode, allowed.stderr)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_a_script_declares_its_own_budget_and_the_cap_still_applies(self):
        tight = self.fake('time.sleep(60)\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 1}\n')
        done = self.run_runner(tight)
        self.assertEqual(3, done.returncode, done.stderr)
        self.assertLess(done.elapsed, 15)
        over = self.fake('pass\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 901}\n')
        refused = self.run_runner(over)
        self.assertEqual(2, refused.returncode)
        self.assertIn('decision', refused.stderr + refused.stdout)
        approved = self.fake('pass\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 901, "decision": 371}\n')
        self.assertEqual(0, self.run_runner(approved).returncode)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_the_script_is_not_imported_or_executed_to_read_its_budget(self):
        marker = self.dir / 'ran'
        script = self.fake(f'open({str(marker)!r}, "w").write("x")\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 1000}\n')
        self.assertEqual(2, self.run_runner(script).returncode)
        self.assertFalse(marker.exists())


class RunnerCleanupTests(RunnerCase):
    @requirements('S14D-2', why_not_scenario=WHY)
    def test_success_reports_bytes_and_deletes_the_data(self):
        body = 'open(os.path.join(out, "a"), "wb").write(b"x" * 2 * 1048576)\nprint("child said hi")\n'
        done = self.run_runner(self.fake(body))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn('child said hi', done.stdout)
        match = re.search(r'(\d+) bytes written', done.stdout + done.stderr)
        self.assertTrue(match, done.stdout + done.stderr)
        self.assertGreaterEqual(int(match.group(1)), 2 * 1048576)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_a_failing_script_returns_its_status_and_still_deletes_the_data(self):
        body = 'open(os.path.join(out, "a"), "wb").write(b"x" * 1048576)\nsys.exit(7)\n'
        done = self.run_runner(self.fake(body))
        self.assertEqual(7, done.returncode)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_keep_leaves_the_data_and_names_it(self):
        body = 'open(os.path.join(out, "a"), "wb").write(b"x" * 1048576)\n'
        done = self.run_runner(self.fake(body), '--keep')
        self.assertEqual(0, done.returncode, done.stderr)
        kept = self.leftovers()
        self.assertEqual(1, len(kept))
        self.assertIn(kept[0], done.stdout + done.stderr)
        self.assertTrue((self.dir / kept[0] / 'a').is_file())

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_the_child_writes_its_temporary_files_into_the_output_dir(self):
        body = 'import tempfile\nopen(os.path.join(tempfile.gettempdir(), "t"), "wb").write(b"x" * 1048576)\n' \
               'print(tempfile.gettempdir() == out)\n'
        done = self.run_runner(self.fake(body))
        self.assertIn('True', done.stdout)


class ImportTests(RunnerCase):
    @requirements('S14D-5', why_not_scenario=WHY)
    def test_importing_the_runner_does_no_io(self):
        code = ('import sys\nsys.path.insert(0, %r)\nimport run_measurement\n' % str(ROOT / 'scripts'))
        done = subprocess.run([sys.executable, '-B', '-c', code], env=self.env, capture_output=True, text=True, cwd=self.dir)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual('', done.stdout + done.stderr)
        self.assertEqual([], list(self.dir.iterdir()))


class RegistryTests(unittest.TestCase):
    @requirements('S14D-3', why_not_scenario=WHY)
    def test_every_measure_script_declares_a_valid_budget(self):
        import run_measurement
        scripts = measure_scripts()
        self.assertGreaterEqual(len(scripts), 12)
        for path in scripts:
            with self.subTest(script=path.name):
                budget = declared_budget(path)
                self.assertIsInstance(budget, dict, f'{path} must declare MEASUREMENT_BUDGET as a literal dict')
                self.assertEqual([], run_measurement.budget_errors(budget), path)
                self.assertEqual(budget, run_measurement.read_budget(path))

    @requirements('S14D-3', why_not_scenario=WHY)
    def test_the_registry_rule_rejects_bad_declarations(self):
        import run_measurement
        for bad in ({'wall_seconds': 901}, {'wall_seconds': 0}, {'data_mib': -1}, {'rss_mib': 'big'},
                    {'wall_seconds': 901, 'decision': 'x'}, {'unknown': 1}, {'wall_seconds': True}):
            self.assertTrue(run_measurement.budget_errors(bad), bad)
        for good in ({}, {'wall_seconds': 900}, {'wall_seconds': 901, 'decision': 371}, {'data_mib': 512, 'rss_mib': 1024}):
            self.assertEqual([], run_measurement.budget_errors(good), good)


class DirectInvocationTests(unittest.TestCase):
    def files(self):
        for pattern in SCAN_ROOTS:
            for base in ROOT.glob(pattern):
                for path in ([base] if base.is_file() else sorted(base.rglob('*'))):
                    if path.is_file() and path.suffix in SCAN_SUFFIXES:
                        yield path

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_no_doc_or_script_invokes_a_measure_script_directly(self):
        found = []
        for path in self.files():
            for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                if DIRECT.search(line) and 'run_measurement.py' not in line:
                    found.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}')
        self.assertEqual([], found, 'invoke measure_*.py through scripts/run_measurement.py')

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_the_direct_invocation_pattern_catches_the_known_forms(self):
        for line in ('$PY -B scripts/measure_x.py --out a', '    python -B scripts/measure_s13.py [--n 1]',
                     'TMPDIR=/tmp/x "$PY" -B scripts/measure_lockhold.py', 'PYTHONPATH=../../scripts $PY -B scripts/measure_s14_admission_day.py'):
            self.assertTrue(DIRECT.search(line), line)
        self.assertFalse(DIRECT.search('Measurement code: `packages/codex-agents/scripts/measure_s13.py` (suite scale)'))


if __name__ == '__main__':
    unittest.main()
