"""TF-1, TF-9..13, TF-17: real worker boundary tests with disposable local fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from test_test_framework import proves

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests


class WorkerTests(unittest.TestCase):
    def probe(self, body, layer, before='', bootstrap=''):
        with tempfile.TemporaryDirectory(prefix='tf-') as folder:
            root = Path(folder)
            (root / 'test_probe.py').write_text(before + '\nimport unittest\nclass Probe(unittest.TestCase):\n' +
                                               '    def test_value(self):\n' + textwrap.indent(body, '        ') + '\n')
            environment = {**os.environ, 'COCOLO_TEST_STAGE': 'unit' if layer == 'unit' else 'integration',
                           'COCOLO_TEST_LAYER': layer, 'COCOLO_TEST_MODULE': 'root:test_probe',
                           'COCOLO_TEST_RESULTS': str(root / 'results.json'), 'TMPDIR': folder}
            completed = subprocess.run([sys.executable, '-B', '-c', bootstrap + '\n' + parallel_tests._RUNNER, 'test_probe'],
                                       cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            result = json.loads((root / 'results.json').read_text()) if (root / 'results.json').exists() else None
            return completed.returncode, completed.stdout + completed.stderr, result

    @proves('TF-10')
    def test_process_apis_are_denied_in_unit_and_component_before_os_effects(self):
        attempts = ["import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'])",
                    "import subprocess; subprocess.run(['/usr/bin/git', '--version'])",
                    "import os; os.system('true')",
                    "import os, sys; os.spawnv(os.P_WAIT, sys.executable, [sys.executable, '-c', 'pass'])",
                    "import os, sys; os.execv(sys.executable, [sys.executable, '-c', 'pass'])"]
        if hasattr(os, 'posix_spawn'):
            attempts.append("import os, sys; p=os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'], os.environ); os.waitpid(p, 0)")
        if hasattr(os, 'fork'):
            attempts.append("import os; p=os.fork(); os._exit(0) if p == 0 else os.waitpid(p, 0)")
        for layer in ('unit', 'component'):
            for body in attempts:
                with self.subTest(layer=layer, body=body):
                    code, output, _ = self.probe(body, layer)
                    self.assertNotEqual(0, code, output)
                    self.assertIn(layer, output)
                    self.assertIn('root:test_probe', output)

    @proves('TF-10', 'TF-11')
    def test_import_time_and_caught_denials_still_fail_the_worker(self):
        for layer in ('unit', 'component'):
            code, output, _ = self.probe('self.assertTrue(True)', layer,
                                       "import subprocess; subprocess.run(['true'])")
            self.assertNotEqual(0, code, output)
            code, output, result = self.probe(
                'import socket\ntry:\n    socket.socket()\nexcept RuntimeError:\n    pass\nself.assertTrue(True)', layer)
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])

    @proves('TF-11')
    def test_socket_creation_pair_bind_and_resolution_are_denied(self):
        for layer in ('unit', 'component'):
            for body in ("import socket; socket.socket()", "import socket; socket.socketpair()",
                         "import socket; socket.getaddrinfo('localhost', 80)",
                         "import socket; s=socket.socket(socket.AF_UNIX); s.bind('fixture.sock')"):
                with self.subTest(layer=layer, body=body):
                    code, output, _ = self.probe(body, layer)
                    self.assertNotEqual(0, code, output)
                    self.assertIn('socket', output)


    @proves('TF-11')
    def test_inherited_socket_methods_and_asyncio_have_no_guard_exemption(self):
        bootstrap = 'import socket,builtins; builtins.TF_PAIR = socket.socketpair()'
        for method, arguments in [('listen', '1'), ('accept', ''), ('sendmsg', "[b'fixture']")]:
            if not hasattr(__import__('socket').socket, method):
                continue
            body = ("import builtins\ntry:\n    builtins.TF_PAIR[0]." + method + '(' + arguments +
                    ")\nexcept Exception:\n    pass\nfinally:\n    [s.close() for s in builtins.TF_PAIR]\nself.assertTrue(True)")
            code, output, result = self.probe(body, 'component', bootstrap=bootstrap)
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])
        code, output, _ = self.probe("import asyncio\nasyncio.run(asyncio.open_unix_connection('absent-fixture.sock'))", 'component')
        self.assertNotEqual(0, code, output)
        self.assertIn('socket', output)

    @proves('TF-12')
    def test_blocking_unit_waits_fail_but_ready_events_and_nonblocking_polls_pass(self):
        for body in ('import time; time.sleep(.01)', 'import threading; threading.Event().wait(.01)',
                     'import select; select.select([], [], [], .01)',
                     'import threading\nc=threading.Condition()\nwith c:\n    c.wait(.01)'):
            with self.subTest(body=body):
                code, output, _ = self.probe(body, 'unit')
                self.assertNotEqual(0, code, output)
        code, output, _ = self.probe(
            'import threading, select\ne=threading.Event()\ne.set()\nself.assertTrue(e.wait(10))\n'
            'self.assertEqual(([], [], []), select.select([], [], [], 0))', 'unit')
        self.assertEqual(0, code, output)

    @proves('TF-13')
    def test_real_process_git_and_local_socket_contracts_still_work_in_allowed_layers(self):
        body = ("import subprocess, socket, sys\n"
                "self.assertEqual(0, subprocess.run([sys.executable, '-c', 'pass']).returncode)\n"
                "self.assertEqual(0, subprocess.run(['git', '--version'], capture_output=True).returncode)\n"
                "a,b=socket.socketpair()\ntry:\n    a.send(b'ok'); self.assertEqual(b'ok', b.recv(2))\n"
                "finally:\n    a.close(); b.close()")
        for layer in ('crash', 'scenario', 'soak'):
            with self.subTest(layer=layer):
                code, output, _ = self.probe(body, layer)
                self.assertEqual(0, code, output)

    @proves('TF-1', 'TF-5', 'TF-9')
    def test_machine_results_distinguish_pass_skip_failure_and_import_error(self):
        for body, status in [('self.assertTrue(True)', 'passed'), ('self.skipTest("fixture")', 'skipped'),
                             ('self.assertEqual(1, 2)', 'failed')]:
            code, output, result = self.probe(body, 'component')
            self.assertIsNotNone(result, output)
            self.assertEqual(status, result['outcomes']['test_probe.Probe.test_value'])
            self.assertEqual(status != 'failed', code == 0, output)
        code, output, result = self.probe('pass', 'component', 'raise ValueError("broken import")')
        self.assertNotEqual(0, code)
        self.assertIn('error', result['outcomes'].values())

    @proves('TF-17')
    def test_a_failing_hypothesis_machine_retains_its_reproducer_and_nonzero_exit(self):
        before = ("from hypothesis import settings\nfrom hypothesis.stateful import RuleBasedStateMachine, rule\n"
                  "class Broken(RuleBasedStateMachine):\n    @rule()\n    def advance(self):\n        assert False\n"
                  "Broken.TestCase.settings=settings(max_examples=2, derandomize=True, database=None, deadline=None)\n"
                  "Generated=Broken.TestCase\n")
        code, output, result = self.probe('self.assertTrue(True)', 'component', before)
        self.assertNotEqual(0, code)
        self.assertIn('state.advance()', output)
        self.assertIn('failed', result['outcomes'].values())

    @proves('TF-12')
    def test_review_B1_thread_start_handshake_and_B2_cached_selectors(self):
        code, output, result = self.probe('import threading\nt = threading.Thread(target=lambda: None)\nt.start()\nt.join(0)\nself.assertFalse(t.is_alive())', 'unit')
        self.assertEqual(0, code, output)
        for selector in ('DefaultSelector', 'SelectSelector', 'PollSelector', 'EpollSelector'):
            if not hasattr(__import__('selectors'), selector):
                continue
            code, output, result = self.probe('import selectors\nwith selectors.' + selector + '() as selector:\n    selector.select(1)', 'unit')
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])

    @proves('TF-10', 'TF-11')
    def test_review_A9_discovery_hooks_keep_the_declared_guard(self):
        for layer in ('unit', 'component'):
            for action in ('socket.socket()', "subprocess.run(['true'])"):
                with self.subTest(layer=layer, action=action), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    (root / 'tests').mkdir()
                    (root / 'tests/test_hook.py').write_text('import unittest, socket, subprocess\ndef load_tests(loader, tests, pattern):\n    try:\n        ' + action + '\n    except Exception:\n        pass\n    return tests\n')
                    code = 'import sys,json; sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + '); from test_framework import inventory_suite; print(json.dumps(inventory_suite("root", {"root:test_hook": ' + repr(layer) + '})))'
                    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=root, capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('__import_guard__', result.stdout)


class RunnerIntegrationTests(unittest.TestCase):
    def run_fixture(self, body='self.assertEqual(2, 1 + 1)', tagged=True, timeout='600', extra_module=False, malformed=False, layer='scenario', extra_args=(), siblings=False, module_import=''):
        with tempfile.TemporaryDirectory(prefix='tf-run-') as folder:
            root = Path(folder)
            (root / 'tests').mkdir()
            (root / 'scripts').mkdir()
            tag = "@requirements('TF-1', why_not_scenario='runner boundary fixture')\n    " if tagged else ''
            (root / 'tests/test_probe.py').write_text(
                module_import + "\nimport unittest\nfrom test_framework import requirements\nclass Probe(unittest.TestCase):\n    " + tag +
                "def test_value(self):\n" + textwrap.indent(body, '        ') + '\n')
            stages = {'root:test_probe': 'integration'}
            layers = {'root:test_probe': layer}
            if extra_module:
                (root / 'tests/test_other.py').write_text('import unittest\nclass Other(unittest.TestCase):\n    def test_value(self):\n        self.fail("should not run")\n')
                stages['root:test_other'] = 'integration'
            catalogue = {'TF-1': {'statement': 'Retain isolated worker execution.', 'source': 'source.json#TF-1',
                                  'checks': ['root:test_probe.Probe.test_value']}}
            if siblings:
                (root / 'tests/test_sibling.py').write_text((root / 'tests/test_probe.py').read_text())
                stages['root:test_sibling'] = 'integration'
                layers['root:test_sibling'] = layer
                catalogue['TF-1']['checks'].append('root:test_sibling.Probe.test_value')
                extra_args = (*extra_args, '--module', 'root:test_sibling')
            (root / 'source.json').write_text(json.dumps({'requirements': [{'id': 'TF-1', 'statement': 'Retain isolated worker execution.'}]}))
            for name, data in [('test-legacy-dependencies.json', {}), ('test-stages.json', stages), ('test-layers.json', layers),
                               ('test-requirements.json', catalogue), ('test-timings.json', {'obsolete': 99999}),
                               ('test-budget-debt.json', {'baseline': 'fixture', 'modules': {}}),
                               ('test-legacy.json', {'baseline': 'fixture', 'tests': {}}),
                               ('test-legacy-ceiling.json', {'baseline': 'fixture', 'tests': {}})]:
                (root / 'scripts' / name).write_text(json.dumps(data))
            if malformed:
                (root / 'scripts/test-layers.json').write_text('{invalid json')
            driver = (f"import sys; sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                      "import parallel_tests as p\nfrom pathlib import Path\n"
                      f"p.ROOT=Path({str(root)!r}); p.SUITES={{'root':p.ROOT}}\n"
                      "p.STAGES=p.ROOT/'scripts/test-stages.json'; p.TIMINGS=p.ROOT/'scripts/test-timings.json'\n"
                      "p.legacy_versions=lambda root, current, ceiling: (ceiling, current, [])\n"
                      "p.budget_versions=lambda root, current: []\n"
                      f"raise SystemExit(p.main(['--module','root:test_probe','--save-timings',{str(root / 'timings.json')!r}]+{list(extra_args)!r}))\n")
            result = subprocess.run([sys.executable, '-B', '-c', driver], cwd=root,
                                    env={**os.environ, 'COCOLO_WORKER_TIMEOUT': timeout, 'TF_FIXTURE_SECRET': 'synthetic-secret'},
                                    capture_output=True, text=True, timeout=20)
            detail = root / 'timings.json.run.json'
            report = json.loads(detail.read_text()) if detail.exists() else None
            if report is not None:
                report['fixture_child_stopped'] = (root / 'child-stopped').exists()
                report['fixture_import_child'] = (root / 'import-child').exists()
            return result.returncode, result.stdout + result.stderr, report

    @proves('TF-1', 'TF-9')
    def test_runner_counts_structured_results_and_saves_only_current_job_seconds(self):
        code, output, report = self.run_fixture("print('Ran 99 tests in 0.1s'); self.assertTrue(True)")
        self.assertEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(1, sum(row['tests'] for row in report['workers']))
        self.assertEqual({'root:test_probe'}, set(report['modules']))
        self.assertAlmostEqual(sum(r['seconds'] for r in report['workers']), report['job_seconds'])
        self.assertEqual(max(1, (os.cpu_count() or 2) - 2), report['jobs'])

    @proves('TF-3', 'TF-4')
    def test_untagged_new_test_fails_the_actual_runner(self):
        code, output, report = self.run_fixture(tagged=False)
        self.assertNotEqual(0, code, output)
        self.assertIn('root:test_probe.Probe.test_value', output)
        self.assertTrue(report['metadata_errors'])

    @proves('TF-2')
    def test_missing_layer_in_unselected_module_is_not_hidden_by_module_filter(self):
        code, output, report = self.run_fixture(extra_module=True)
        self.assertNotEqual(0, code, output)
        self.assertIn('root:test_other: missing layer', output)
        self.assertTrue(report['metadata_errors'])

    @proves('TF-9')
    def test_worker_timeout_retains_measurements_and_names_its_module(self):
        code, output, report = self.run_fixture('import time; time.sleep(10)', timeout='1')
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(1, len(report['workers']))
        self.assertTrue(report['workers'][0]['timed_out'])
        self.assertIn('root:test_probe', output)
        self.assertGreater(report['job_seconds'], 0)

    @proves('TF-1', 'TF-5', 'TF-9')
    def test_a_worker_exiting_zero_without_a_result_is_not_a_passing_check(self):
        code, output, report = self.run_fixture('import os; os._exit(0)')
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertNotEqual(0, report['workers'][0]['returncode'])

    @proves('TF-5')
    def test_skipped_only_requirement_is_not_a_passing_run(self):
        code, output, report = self.run_fixture('self.skipTest("fixture unavailable")')
        self.assertNotEqual(0, code, output)
        self.assertEqual('failed', report['requirements']['TF-1'])

    @proves('TF-9')
    def test_preflight_failure_writes_zero_job_seconds_and_discovery_time(self):
        code, output, report = self.run_fixture(malformed=True)
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(0, report['job_seconds'])
        self.assertGreaterEqual(report['discovery_seconds'], 0)
        self.assertGreaterEqual(report['preflight_seconds'], 0)
        self.assertTrue(report['metadata_errors'])

    @proves('TF-18')
    def test_explicit_soak_module_requires_explicit_soak_profile(self):
        code, output, report = self.run_fixture(layer='soak')
        self.assertNotEqual(0, code, output)
        self.assertIn('soak', output)
        code, output, report = self.run_fixture(layer='soak', extra_args=('--profile', 'soak'))
        self.assertEqual(0, code, output)
        self.assertEqual('soak', report['profile'])

    @proves('TF-1')
    def test_worker_secrets_are_stripped_and_temporary_roots_are_isolated(self):
        body = ("import os,tempfile\nfrom pathlib import Path\n"
                "self.assertNotIn('TF_FIXTURE_SECRET', os.environ)\n"
                "path=Path(tempfile.gettempdir())/'exclusive-fixture'\n"
                "with path.open('x') as stream:\n    stream.write('private fixture')\n"
                "self.assertEqual('private fixture', path.read_text())")
        code, output, report = self.run_fixture(body, siblings=True)
        self.assertEqual(0, code, output)
        self.assertEqual(2, len(report['workers']))
        self.assertEqual(2, len({row['log'] for row in report['workers']}))

    @proves('TF-1', 'TF-9')
    def test_owned_descendants_are_reaped_when_a_worker_finishes(self):
        child = ("import signal,time\nfrom pathlib import Path\n"
                 "def stop(*args):\n    Path('child-stopped').write_text('stopped'); raise SystemExit\n"
                 "signal.signal(signal.SIGTERM, stop)\nPath('child-ready').write_text('ready')\ntime.sleep(10)")
        body = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                "end=time.monotonic()+2\nwhile not Path('child-ready').exists() and time.monotonic()<end:\n    time.sleep(.01)\n"
                "self.assertTrue(Path('child-ready').exists())")
        code, output, report = self.run_fixture(body)
        self.assertEqual(0, code, output)
        self.assertTrue(report['fixture_child_stopped'], 'worker completion must reap owned descendants')

    @proves('TF-18')
    def test_conflicting_explicit_profile_selectors_are_rejected(self):
        code, output, _ = self.run_fixture(extra_args=('--profile', 'dev', '--profile', 'ci'))
        self.assertNotEqual(0, code, output)
        self.assertIn('profile', output)

    @proves('TF-10')
    def test_discovery_cannot_run_a_restricted_module_import_side_effect(self):
        command = "from pathlib import Path; Path('import-child').write_text('ran')"
        imported = f"import subprocess,sys; subprocess.run([sys.executable,'-c',{command!r}])"
        code, output, report = self.run_fixture(layer='component', module_import=imported)
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertFalse(report['fixture_import_child'], 'preflight import must not start the prohibited child')

    @proves('TF-10')
    def test_review_B7_discovery_filters_secrets(self):
        code, output, report = self.run_fixture(module_import="import os; assert 'TF_FIXTURE_SECRET' not in os.environ")
        self.assertEqual(0, code, output)

    @proves('TF-9')
    def test_review_B8_preflight_preserves_estimates(self):
        from unittest.mock import patch
        import contextlib, io
        with tempfile.TemporaryDirectory() as folder:
            estimates = Path(folder) / 'estimates.json'
            estimates.write_text('{"retained": 2}')
            with contextlib.redirect_stdout(io.StringIO()):
                parallel_tests.preflight_failure('fixture rejection', 1, estimates, __import__('time').monotonic())
            self.assertEqual('{"retained": 2}', estimates.read_text())
            self.assertTrue(Path(str(estimates) + '.run.json').exists())

    @proves('TF-9')
    def test_review_B4_preflight_retains_default_run_record(self):
        from unittest.mock import patch
        import contextlib, io
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / 'retained.run.json'
            with patch.dict(os.environ, {'COCOLO_TEST_RUN_RECORD': str(record)}), contextlib.redirect_stdout(io.StringIO()):
                parallel_tests.preflight_failure('fixture rejection', 1, None, __import__('time').monotonic())
            self.assertTrue(record.exists())
            self.assertIn('fixture rejection', record.read_text())

    @proves('TF-9', 'TF-13')
    def test_review_B3_runner_reaps_disabled_watchdog_with_diagnostics(self):
        import time
        started = time.monotonic()
        code, output, report = self.run_fixture('import faulthandler,time\nfaulthandler.cancel_dump_traceback_later()\nprint("stuck witness", flush=True)\ntime.sleep(10)', timeout='0.3')
        self.assertLess(time.monotonic() - started, 5.)
        self.assertNotEqual(0, code, output)
        self.assertTrue(report['workers'][0]['timed_out'])
        self.assertIn('stuck witness', report['workers'][0]['log_tail'])
        self.assertTrue(Path(report['workers'][0]['log']).is_file())

    @proves('TF-5')
    def test_review_C9_executed_expected_failure_and_unexpected_success_do_not_pass_gate(self):
        for assertion in ('self.assertEqual(1, 2)', 'self.assertEqual(1, 1)'):
            before = 'import unittest\nclass Expected(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_expected(self):\n        ' + assertion
            code, output, result = WorkerTests().probe('pass', 'component', before)
            from test_framework import coverage
            statuses = result['outcomes']
            self.assertNotEqual('passed', coverage({'TF-5': {'checks': ['test_probe.Expected.test_expected']}}, statuses)['TF-5'])

    @proves('TF-1')
    def test_review_C13_unpatched_runner_fails_closed_without_trusted_base(self):
        code = 'import sys; sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + '); import parallel_tests as p; raise SystemExit(p.main(["--module", "root:test_test_framework_entrypoints"]))'
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=25)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('trusted --base', result.stdout)


if __name__ == '__main__':
    unittest.main()
