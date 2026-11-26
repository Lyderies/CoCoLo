"""S14d (decision 371): the measurement runner enforces its caps. Tiny fake scripts, low caps, seconds each."""
import ast
import contextlib
import io
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements  # noqa: E402

RUNNER = ROOT / 'scripts' / 'run_measurement.py'
WHY = ('The runner is a process supervisor; its caps are proven only by real child processes, '
       'and no product service, ledger or provider is involved.')
INTERP = r'(?:python[0-9.]*|\$PY|"\$PY"|\bPY)'
MEASURE = r'measure_[A-Za-z0-9_]+'
DIRECT_FORMS = (
    re.compile(INTERP + r'\b[^|\n]*\b' + MEASURE + r'\.py'),
    re.compile(INTERP + r'\b[^|\n]*\s-m\s+[\w.]*' + MEASURE + r'\b'),
    re.compile(r'(?:^|[\s`"\'(])\./[\w./-]*' + MEASURE + r'\.py'),
    re.compile(r'\[[^\]\n]*' + MEASURE + r'\.py'),               # subprocess argument lists
    re.compile(r'\bsubprocess\.\w+\([^\n]*' + MEASURE + r'\b'),
)
RUNNER_AS_PROGRAM = (
    re.compile(INTERP + r'(?:\s+-\S+)*\s+[\w./$"-]*run_measurement\.py\b'),
    re.compile(r'\[[^\]\n]*run_measurement\.py[^\]\n]*' + MEASURE),
)
SCAN_ROOTS = ('docs', 'scripts', 'packages/*/scripts')
SCAN_SUFFIXES = {'.md', '.py', '.sh', '.txt', '.yml', '.yaml'}


def invokes_directly(text):
    """True when text runs a measure script other than as the argument of scripts/run_measurement.py."""
    text = re.split(r'(?:^|\s)#', text)[0]   # a comment is not a command
    for part in re.split(r'&&|\|\||;|\|', text):
        if any(form.search(part) for form in DIRECT_FORMS) and not any(form.search(part) for form in RUNNER_AS_PROGRAM):
            return True
    return False


def command_texts(path):
    """The lines to test: every line of code files; only fenced blocks and `inline code` of Markdown prose."""
    fenced = False
    for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
        if path.suffix != '.md':
            yield number, line
        elif line.strip().startswith('```'):
            fenced = not fenced
        elif fenced:
            yield number, line
        else:
            for span in re.findall(r'`([^`]+)`', line):
                yield number, span


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
        selfish = self.fake('pass\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 901, "decision": 371}\n')
        self.assertEqual(2, self.run_runner(selfish).returncode)
        self.assertEqual(0, self.run_runner(selfish, '--approve-decision', '371').returncode)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_the_script_is_not_imported_or_executed_to_read_its_budget(self):
        marker = self.dir / 'ran'
        script = self.fake(f'open({str(marker)!r}, "w").write("x")\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 1000}\n')
        self.assertEqual(2, self.run_runner(script).returncode)
        self.assertFalse(marker.exists())


class RunnerReviewTests(RunnerCase):
    def grandchild_body(self, marker, parent_sleep):
        return ('import subprocess\np = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)\n'
                f'open({str(marker)!r}, "w").write(str(p.pid))\ntime.sleep({parent_sleep})\n')

    def assertDies(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail('a descendant survived the runner')

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_data_written_outside_the_data_dir_is_a_volume_breach(self):
        outside = self.dir / 'elsewhere.bin'
        body = (f'f = open({str(outside)!r}, "wb")\nf.write(b"x" * (40 * 1048576))\nf.flush()\nos.fsync(f.fileno())\ntime.sleep(20)\n')
        done = self.run_runner(self.fake(body), '--max-data-mib', '3', '--wall', '60')
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertLess(done.elapsed, 20)
        self.assertIn('outside the data directory', done.stdout)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_a_setsid_grandchild_is_found_killed_after_the_child_exits(self):
        marker = self.dir / 'gc.pid'
        done = self.run_runner(self.fake(self.grandchild_body(marker, 1.5)), '--wall', '30')
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertDies(int(marker.read_text()))

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_a_setsid_grandchild_is_killed_on_a_breach(self):
        marker = self.dir / 'gc.pid'
        done = self.run_runner(self.fake(self.grandchild_body(marker, 60)), '--wall', '2')
        self.assertEqual(3, done.returncode)
        self.assertDies(int(marker.read_text()))

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_data_written_and_gone_between_polls_is_still_a_breach(self):
        body = 'open(os.path.join(out, "a"), "wb").write(b"x" * (6 * 1048576))\n'
        done = self.run_runner(self.fake(body), '--max-data-mib', '3')
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn('generated data', done.stdout)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_a_short_memory_spike_is_a_breach_after_exit(self):
        body = 'block = bytearray(b"y" * (120 * 1048576))\n'
        done = self.run_runner(self.fake(body), '--max-rss-mib', '60')
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn('memory', done.stdout)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_a_ps_failure_is_an_error_not_zero_memory(self):
        empty = self.dir / 'nobin'
        empty.mkdir()
        self.env['PATH'] = str(empty)
        done = self.run_runner(self.fake('time.sleep(5)\n'))
        self.assertEqual(4, done.returncode, done.stdout + done.stderr)
        self.assertIn('ps', done.stderr)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_hard_maxima_and_odd_numbers(self):
        script = self.fake('pass\n')
        for flags in (('--max-data-mib', '4097'), ('--max-rss-mib', '6145'), ('--wall', 'inf'), ('--wall', 'nan'),
                      ('--max-data-mib', '-5'), ('--max-rss-mib', '0')):
            self.assertEqual(2, self.run_runner(script, *flags).returncode, flags)
        self.assertEqual(0, self.run_runner(script, '--max-data-mib', '4097', '--approve-decision', '371').returncode)
        for key in ('data_mib', 'rss_mib'):
            over = self.fake('pass\n', header='MEASUREMENT_BUDGET = {"%s": 99999, "decision": 1}\n' % key)
            self.assertEqual(2, self.run_runner(over).returncode)
        weird = self.fake('pass\n', header='MEASUREMENT_BUDGET = {"wall_seconds": 1e999}\n')
        self.assertEqual(2, self.run_runner(weird).returncode)

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_an_unreadable_subdirectory_is_still_deleted(self):
        body = 'd = os.path.join(out, "locked")\nos.mkdir(d)\nopen(os.path.join(d, "f"), "w").write("x")\nos.chmod(d, 0)\n'
        done = self.run_runner(self.fake(body))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.leftovers())

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_launch_failure_and_deletion_failure_are_reported_and_clean_up(self):
        import run_measurement
        script = self.fake('pass\n')
        with mock.patch.object(tempfile, 'tempdir', str(self.dir)), \
                mock.patch.object(run_measurement.subprocess, 'Popen', side_effect=OSError('no exec')):
            self.assertEqual(4, run_measurement.main([str(script)]))
        self.assertEqual([], self.leftovers())
        with mock.patch.object(tempfile, 'tempdir', str(self.dir)), \
                mock.patch.object(run_measurement, 'delete_tree', return_value=False):
            self.assertEqual(4, run_measurement.main([str(script)]))
        for name in self.leftovers():
            __import__('shutil').rmtree(self.dir / name)

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_the_directory_walk_is_bounded(self):
        import run_measurement
        for number in range(300):
            (self.dir / f'f{number}').write_text('x')
        total, complete = run_measurement.tree_bytes(self.dir, 0)
        self.assertFalse(complete)
        self.assertTrue(run_measurement.tree_bytes(self.dir, 30)[1])

    @requirements('S14D-1', why_not_scenario=WHY)
    def test_the_directory_walk_is_bounded_for_empty_directories_too(self):
        import run_measurement
        for number in range(300):
            (self.dir / f'd{number}' / 'e').mkdir(parents=True)
        self.assertFalse(run_measurement.tree_bytes(self.dir, 0)[1])
        self.assertTrue(run_measurement.tree_bytes(self.dir, 30)[1])

    @requirements('S14D-2', why_not_scenario=WHY)
    def test_a_breach_that_cannot_delete_its_data_says_so_and_exits_with_an_error(self):
        import run_measurement
        script = self.fake('time.sleep(30)\n')
        out = io.StringIO()
        with mock.patch.object(tempfile, 'tempdir', str(self.dir)), \
                mock.patch.object(run_measurement, 'delete_tree', return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = run_measurement.main(['--wall', '1', str(script)])
        self.assertEqual(4, code)
        text = out.getvalue()
        self.assertIn('wall clock', text)
        self.assertIn('could not delete', text)
        self.assertNotIn('was deleted', text)
        for name in self.leftovers():
            __import__('shutil').rmtree(self.dir / name)


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
                    {'wall_seconds': 901, 'decision': 371}, {'data_mib': 4097}, {'rss_mib': 6145, 'decision': 1},
                    {'wall_seconds': float('inf')}, {'data_mib': 1.5}, {'unknown': 1}, {'wall_seconds': True}):
            self.assertTrue(run_measurement.budget_errors(bad), bad)
        for good in ({}, {'wall_seconds': 900}, {'wall_seconds': 900, 'decision': 371}, {'data_mib': 4096, 'rss_mib': 1024}):
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
            for number, text in command_texts(path):
                if invokes_directly(text):
                    found.append(f'{path.relative_to(ROOT)}:{number}: {text.strip()[:120]}')
        self.assertEqual([], found, 'invoke measure_*.py through scripts/run_measurement.py')

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_the_scanner_catches_every_known_direct_form(self):
        for line in ('$PY -B scripts/measure_x.py --out a', '    python -B scripts/measure_s13.py [--n 1]',
                     'TMPDIR=/tmp/x "$PY" -B scripts/measure_lockhold.py', 'PYTHONPATH=../../scripts $PY -B scripts/measure_s14_admission_day.py',
                     'python -m scripts.measure_x --out a', '$PY -B -m packages.measure_y',
                     'subprocess.run([sys.executable, "scripts/measure_x.py"])', 'cmd = [sys.executable, "-B", "x/measure_x.py"]',
                     './scripts/measure_x.py --out a', 'subprocess.run(cmd + ["measure_x"])',
                     '$PY scripts/run_measurement.py && $PY -B scripts/measure_x.py'):
            self.assertTrue(invokes_directly(line), line)

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_only_a_real_runner_invocation_is_exempt(self):
        for line in ('$PY -B scripts/run_measurement.py scripts/measure_x.py --out a',
                     'python -B scripts/run_measurement.py --wall 60 packages/x/scripts/measure_x.py --',
                     'subprocess.run([sys.executable, "-B", "scripts/run_measurement.py", "scripts/measure_x.py"])'):
            self.assertFalse(invokes_directly(line), line)
        self.assertTrue(invokes_directly('see run_measurement.py. $PY -B scripts/measure_x.py'))
        self.assertTrue(invokes_directly('$PY scripts/measure_x.py # python scripts/run_measurement.py'))
        self.assertTrue(invokes_directly('$PY -B scripts/measure_x.py scripts/run_measurement.py'))

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_prose_only_counts_inside_code(self):
        path = Path(tempfile.mkdtemp()) / 'a.md'
        self.addCleanup(lambda: path.parent.exists() and __import__('shutil').rmtree(path.parent))
        path.write_text('We ran python scripts to measure_x.py results.\n`$PY -B scripts/measure_x.py`\n```\n$PY -B scripts/measure_y.py\n```\n')
        hits = [number for number, text in command_texts(path) if invokes_directly(text)]
        self.assertEqual([2, 4], hits)

    @requirements('S14D-4', why_not_scenario=WHY)
    def test_a_measure_script_never_writes_outside_the_runner_dir_by_name(self):
        for path in measure_scripts():
            text = path.read_text()
            self.assertNotRegex(text, r'["\'](/private)?/tmp[/"\']', f'{path.name} names /tmp; use TMPDIR or MEASUREMENT_OUTPUT_DIR')


if __name__ == '__main__':
    unittest.main()
