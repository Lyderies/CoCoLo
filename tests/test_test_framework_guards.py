"""TF-1, TF-9..13, TF-17: real worker boundary tests with disposable local fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests
from test_framework import requirements


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
            if result is not None:
                result['markers'] = {name: (root / name).exists() for name in ('child', 'reached', 'fixture.sock')}
            return completed.returncode, completed.stdout + completed.stderr, result

    @requirements('TF-10', why_not_scenario='Verify process apis are denied in unit and component before os effects using isolated framework fixtures.')
    def test_process_apis_are_denied_in_unit_and_component_before_os_effects(self):
        child = "from pathlib import Path; Path('child').write_text('started')"
        args = '[sys.executable, "-c", ' + repr(child) + ']'
        attempts = ['import subprocess,sys; subprocess.run(' + args + ')',
                    'import subprocess,sys; subprocess.Popen(' + args + ').wait()',
                    "import subprocess; subprocess.run(['/usr/bin/git', '--version'])",
                    "import os; os.system(" + repr('"' + sys.executable + '" -c "from pathlib import Path; Path(\'child\').write_text(\'started\')"') + ')']
        for name in ('spawnv', 'spawnve', 'spawnvp', 'spawnvpe'):
            if hasattr(os, name):
                attempts.append('import os,sys; os.' + name + '(os.P_WAIT, sys.executable, ' + args + (', os.environ' if name.endswith('e') else '') + ')')
        for name in ('execv', 'execve', 'execvp', 'execvpe'):
            if hasattr(os, name):
                attempts.append('import os,sys; os.' + name + '(sys.executable, ' + args + (', os.environ' if name.endswith('e') else '') + ')')
        for family in ('spawn', 'exec'):
            for suffix in ('l', 'le', 'lp', 'lpe'):
                name = family + suffix
                if hasattr(os, name):
                    prefix = 'os.P_WAIT, ' if family == 'spawn' else ''
                    attempts.append('import os,sys; os.' + name + '(' + prefix + 'sys.executable, sys.executable, "-c", ' + repr(child) + (', os.environ' if suffix.endswith('e') else '') + ')')
        for name in ('posix_spawn', 'posix_spawnp'):
            if hasattr(os, name):
                attempts.append('import os,sys; p=os.' + name + '(sys.executable,' + args + ',os.environ); os.waitpid(p,0)')
        for name in ('fork', 'forkpty'):
            if hasattr(os, name):
                attempts.append('import os\nfrom pathlib import Path\np = os.' + name + '()' + ('[0]' if name == 'forkpty' else '') + "\nif p == 0:\n    Path('child').write_text('started'); os._exit(0)\nelse:\n    os.waitpid(p,0)")
        for layer in ('unit', 'component'):
            for body in attempts:
                with self.subTest(layer=layer, body=body):
                    code, output, result = self.probe(body, layer)
                    self.assertIsNotNone(result, output)
                    self.assertFalse(result['markers']['child'], 'prohibited child actually started')
                    self.assertNotEqual(0, code, output)
                    self.assertIn(layer, output)
                    self.assertIn('root:test_probe', output)

    @requirements('TF-10', 'TF-11', why_not_scenario='Verify import time and caught denials still fail the worker using isolated framework fixtures.')
    def test_import_time_and_caught_denials_still_fail_the_worker(self):
        for layer in ('unit', 'component'):
            code, output, _ = self.probe('self.assertTrue(True)', layer,
                                       "import subprocess; subprocess.run(['true'])")
            self.assertNotEqual(0, code, output)
            code, output, result = self.probe(
                'import socket\ntry:\n    socket.socket()\nexcept Exception:\n    pass\nfrom pathlib import Path\nPath("reached").write_text("yes")\nself.assertTrue(True)', layer)
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])
            self.assertTrue(result['markers']['reached'])
            code, output, result = self.probe(
                "import subprocess\ntry:\n    subprocess.run(['true'])\nexcept Exception:\n    pass\nfrom pathlib import Path\nPath('reached').write_text('yes')", layer)
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])
            self.assertTrue(result['markers']['reached'])

    @requirements('TF-11', why_not_scenario='Verify socket creation pair bind and resolution are denied using isolated framework fixtures.')
    def test_socket_creation_pair_bind_and_resolution_are_denied(self):
        for layer in ('unit', 'component'):
            for body in ("import socket; socket.socket()", "import socket; socket.socketpair()",
                         "import socket; socket.getaddrinfo('localhost', 80)",
                         "import socket; s=socket.socket(socket.AF_UNIX); s.bind('fixture.sock')"):
                with self.subTest(layer=layer, body=body):
                    code, output, _ = self.probe(body, layer)
                    self.assertNotEqual(0, code, output)
                    self.assertIn('socket', output)


    @requirements('TF-11', why_not_scenario='Verify inherited socket methods and asyncio have no guard exemption using isolated framework fixtures.')
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

    @requirements('TF-12', why_not_scenario='Verify blocking unit waits fail but ready events and nonblocking polls pass using isolated framework fixtures.')
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

    @requirements('TF-13', why_not_scenario='Verify real process git and local socket contracts still work in allowed layers using isolated framework fixtures.')
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

    @requirements('TF-1', 'TF-5', 'TF-9', why_not_scenario='Verify machine results distinguish pass skip failure and import error using isolated framework fixtures.')
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

    @requirements('TF-17', why_not_scenario='Verify a failing hypothesis machine retains its reproducer and nonzero exit using isolated framework fixtures.')
    def test_a_failing_hypothesis_machine_retains_its_reproducer_and_nonzero_exit(self):
        before = ("from hypothesis import settings\nfrom hypothesis.stateful import RuleBasedStateMachine, rule\n"
                  "class Broken(RuleBasedStateMachine):\n    @rule()\n    def advance(self):\n        assert False\n"
                  "print('loaded profile', settings().max_examples, settings().derandomize, settings().deadline)\n"
                  "Generated=Broken.TestCase\n")
        code, output, result = self.probe('self.assertTrue(True)', 'component', before)
        self.assertNotEqual(0, code)
        self.assertIn('state.advance()', output)
        self.assertIn('loaded profile 100 True None', output)
        self.assertIn('failed', result['outcomes'].values())

    @requirements('TF-12', why_not_scenario='Verify review B1 thread start handshake and B2 cached selectors using isolated framework fixtures.')
    def test_review_B1_thread_start_handshake_and_B2_cached_selectors(self):
        code, output, result = self.probe('import threading\nt = threading.Thread(target=lambda: None)\nt.start()\nt.join(0)\nself.assertFalse(t.is_alive())', 'unit')
        self.assertEqual(0, code, output)
        for selector in ('DefaultSelector', 'SelectSelector', 'PollSelector', 'EpollSelector'):
            if not hasattr(__import__('selectors'), selector):
                continue
            code, output, result = self.probe('import selectors\nwith selectors.' + selector + '() as selector:\n    selector.select(1)', 'unit')
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])

    @requirements('TF-10', 'TF-11', why_not_scenario='Verify review A9 discovery hooks keep the declared guard using isolated framework fixtures.')
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

    @requirements('TF-11', why_not_scenario='Verify review C5 inherited and convenience socket apis in both layers using isolated framework fixtures.')
    def test_review_C5_inherited_and_convenience_socket_apis_in_both_layers(self):
        bootstrap = 'import socket,builtins; builtins.TF_SOCKET=socket.socket(socket.AF_UNIX); builtins.TF_UDP=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)'
        bodies = ["builtins.TF_SOCKET.bind('fixture.sock')", "builtins.TF_SOCKET.connect('absent.sock')",
                  "builtins.TF_SOCKET.connect_ex('absent.sock')", "builtins.TF_UDP.sendto(b'x',('127.0.0.1',9))",
                  "socket.create_connection(('127.0.0.1',9))", "socket.create_server(('127.0.0.1',0))",
                  "socket.fromfd(builtins.TF_SOCKET.fileno(),socket.AF_UNIX,socket.SOCK_STREAM)",
                  "socket.gethostbyname('localhost')", "socket.gethostbyaddr('127.0.0.1')",
                  "socket.getnameinfo(('127.0.0.1',9),0)",
                  "asyncio.run(asyncio.open_connection('127.0.0.1',9))",
                  "asyncio.run(asyncio.start_server(lambda a,b: None,'127.0.0.1',0))"]
        for layer in ('unit', 'component'):
            for body in bodies:
                with self.subTest(layer=layer, body=body):
                    code, output, result = self.probe('import socket,builtins,asyncio\ntry:\n    ' + body + '\nexcept Exception:\n    pass\nfinally:\n    builtins.TF_SOCKET.close(); builtins.TF_UDP.close()', layer, bootstrap=bootstrap)
                    self.assertNotEqual(0, code, output)
                    self.assertTrue(result['violations'])
                    self.assertFalse(result['markers']['fixture.sock'])
            code, output, result = self.probe('import io\nfake=io.BytesIO()\nfake.write(b"ok")\nself.assertEqual(b"ok", fake.getvalue())', layer)
            self.assertEqual(0, code, output)
            code, output, result = self.probe('pass', layer, before='import socket; socket.socket()')
            self.assertNotEqual(0, code, output)
            self.assertTrue(result['violations'])

    @requirements('TF-1', why_not_scenario='Discovery must preserve inherited, generated and filtered cases before worker sharding.')
    def test_review_C8_discovery_and_shards_have_a_stable_disjoint_union(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'tests').mkdir(); (root / 'tests/__init__.py').write_text('')
            (root / 'tests/test_inventory.py').write_text("import unittest\nclass Parent(unittest.TestCase):\n    def test_inherited(self): pass\nclass Child(Parent): pass\nclass Generated(unittest.TestCase): pass\nsetattr(Generated,'test_generated',lambda self: None)\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Child('test_inherited'),Generated('test_generated')])\n")
            (root / 'tests/test_broken.py').write_text('raise ImportError("full module witness")')
            code = 'import sys,json; sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + '); from test_framework import inventory_suite; print(json.dumps(inventory_suite("root", {"root:tests.test_inventory":"unit", "root:tests.test_broken":"unit"})))'
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            found = json.loads(result.stdout)
            self.assertIn('root:tests.test_broken', found)
            self.assertTrue(next(iter(found['root:tests.test_broken'].values()))['import_error'])
            names = set(found['root:tests.test_inventory'])
            self.assertEqual({'root:tests.test_inventory.Child.test_inherited', 'root:tests.test_inventory.Generated.test_generated'}, names)
            queue = [(1, 'root', name, [name]) for name in sorted(names)]
            first = [parallel_tests.shard(queue, index, 2) for index in (1, 2)]
            self.assertEqual(first, [parallel_tests.shard(list(reversed(queue)), index, 2) for index in (1, 2)])
            observed = [item[2] for part in first for item in part]
            self.assertEqual(names, set(observed)); self.assertEqual(len(names), len(observed))
            (root / 'tests/test_inventory.py').write_text("import unittest\nclass Case(unittest.TestCase):\n    def test_duplicate(self): pass\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Case('test_duplicate'),Case('test_duplicate')])\n")
            duplicate = subprocess.run([sys.executable, '-B', '-c', code], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, duplicate.returncode)
            self.assertIn('duplicate discovered test: root:tests.test_inventory.Case.test_duplicate', duplicate.stderr)

    @requirements('TF-12', why_not_scenario='Nonblocking selector controls must exercise cached backend functions inside the unit guard.')
    def test_review_B2_nonblocking_cached_selectors_are_allowed(self):
        for name in ('SelectSelector', 'DefaultSelector', 'PollSelector', 'EpollSelector'):
            if not hasattr(__import__('selectors'), name):
                print('not_run: ' + sys.platform + ' lacks selectors.' + name)
                continue
            code, output, _ = self.probe('import selectors\nwith selectors.' + name + '() as selector:\n    self.assertEqual([],selector.select(0))', 'unit')
            self.assertEqual(0, code, output)


class RunnerIntegrationTests(unittest.TestCase):
    def run_fixture(self, body='self.assertEqual(2, 1 + 1)', tagged=True, timeout='600', extra_module=False, malformed=False, layer='scenario', extra_args=(), siblings=False, module_import='', interrupt=False, trusted=True):
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
            interruption = ("real_sleep=p.time.sleep\ninterrupted=False\ndef pause(seconds):\n    global interrupted\n    if not interrupted and (p.ROOT/'child-ready').exists():\n        interrupted=True\n        raise KeyboardInterrupt('owned interrupt fixture')\n    return real_sleep(seconds)\np.time.sleep=pause\n") if interrupt else ''
            driver = (f"import sys; sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                      "import parallel_tests as p\nfrom pathlib import Path\n"
                      f"p.ROOT=Path({str(root)!r}); p.SUITES={{'root':p.ROOT}}\n"
                      "p.STAGES=p.ROOT/'scripts/test-stages.json'; p.TIMINGS=p.ROOT/'scripts/test-timings.json'\n"
                      ("p.legacy_versions=lambda root, current, ceiling: (ceiling, current, [])\n"
                       "p.budget_versions=lambda root, current: []\n" if trusted else "")
                      + interruption + f"raise SystemExit(p.main(['--module','root:test_probe','--save-timings',{str(root / 'timings.json')!r}]+{list(extra_args)!r}))\n")
            result = subprocess.run([sys.executable, '-B', '-c', driver], cwd=root,
                                    env={**os.environ, 'COCOLO_WORKER_TIMEOUT': timeout, 'TF_FIXTURE_SECRET': 'synthetic-secret'},
                                    capture_output=True, text=True, timeout=20)
            detail = root / 'timings.json.run.json'
            report = json.loads(detail.read_text()) if detail.exists() else None
            if report is not None:
                report['fixture_child_stopped'] = (root / 'child-stopped').exists()
                report['fixture_import_child'] = (root / 'import-child').exists()
            return result.returncode, result.stdout + result.stderr, report

    @requirements('TF-1', 'TF-9', why_not_scenario='Verify runner counts structured results and saves only current job seconds using isolated framework fixtures.')
    def test_runner_counts_structured_results_and_saves_only_current_job_seconds(self):
        code, output, report = self.run_fixture("print('Ran 99 tests in 0.1s'); self.assertTrue(True)")
        self.assertEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(1, sum(row['tests'] for row in report['workers']))
        self.assertEqual({'root:test_probe'}, set(report['modules']))
        self.assertAlmostEqual(sum(r['seconds'] for r in report['workers']), report['job_seconds'])
        self.assertEqual(max(1, (os.cpu_count() or 2) - 2), report['jobs'])

    @requirements('TF-3', 'TF-4', why_not_scenario='Verify untagged new test fails the actual runner using isolated framework fixtures.')
    def test_untagged_new_test_fails_the_actual_runner(self):
        code, output, report = self.run_fixture(tagged=False)
        self.assertNotEqual(0, code, output)
        self.assertIn('root:test_probe.Probe.test_value', output)
        self.assertTrue(report['metadata_errors'])

    @requirements('TF-2', why_not_scenario='Verify missing layer in unselected module is not hidden by module filter using isolated framework fixtures.')
    def test_missing_layer_in_unselected_module_is_not_hidden_by_module_filter(self):
        code, output, report = self.run_fixture(extra_module=True)
        self.assertNotEqual(0, code, output)
        self.assertIn('root:test_other: missing layer', output)
        self.assertTrue(report['metadata_errors'])

    @requirements('TF-9', why_not_scenario='Verify worker timeout retains measurements and names its module using isolated framework fixtures.')
    def test_worker_timeout_retains_measurements_and_names_its_module(self):
        code, output, report = self.run_fixture('import time; time.sleep(10)', timeout='1')
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(1, len(report['workers']))
        self.assertTrue(report['workers'][0]['timed_out'])
        self.assertIn('root:test_probe', output)
        self.assertGreater(report['job_seconds'], 0)

    @requirements('TF-1', 'TF-5', 'TF-9', why_not_scenario='Verify a worker exiting zero without a result is not a passing check using isolated framework fixtures.')
    def test_a_worker_exiting_zero_without_a_result_is_not_a_passing_check(self):
        code, output, report = self.run_fixture('import os; os._exit(0)')
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertNotEqual(0, report['workers'][0]['returncode'])

    @requirements('TF-5', why_not_scenario='Verify skipped only requirement is not a passing run using isolated framework fixtures.')
    def test_skipped_only_requirement_is_not_a_passing_run(self):
        code, output, report = self.run_fixture('self.skipTest("fixture unavailable")')
        self.assertNotEqual(0, code, output)
        self.assertEqual('failed', report['requirements']['TF-1'])

    @requirements('TF-9', why_not_scenario='Verify preflight failure writes zero job seconds and discovery time using isolated framework fixtures.')
    def test_preflight_failure_writes_zero_job_seconds_and_discovery_time(self):
        code, output, report = self.run_fixture(malformed=True)
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertEqual(0, report['job_seconds'])
        self.assertGreaterEqual(report['discovery_seconds'], 0)
        self.assertGreaterEqual(report['preflight_seconds'], 0)
        self.assertTrue(report['metadata_errors'])

    @requirements('TF-18', why_not_scenario='Verify explicit soak module requires explicit soak profile using isolated framework fixtures.')
    def test_explicit_soak_module_requires_explicit_soak_profile(self):
        code, output, report = self.run_fixture(layer='soak')
        self.assertNotEqual(0, code, output)
        self.assertIn('soak', output)
        code, output, report = self.run_fixture(layer='soak', extra_args=('--profile', 'soak'))
        self.assertEqual(0, code, output)
        self.assertEqual('soak', report['profile'])

    @requirements('TF-1', why_not_scenario='Verify worker secrets are stripped and temporary roots are isolated using isolated framework fixtures.')
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

    @requirements('TF-1', 'TF-9', why_not_scenario='Verify owned descendants are reaped when a worker finishes using isolated framework fixtures.')
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
        code, output, report = self.run_fixture(body + '\nimport time; time.sleep(10)', interrupt=True)
        self.assertNotEqual(0, code, output)
        self.assertTrue(report['fixture_child_stopped'], 'runner interrupt must reap owned descendants')
        self.assertIn('KeyboardInterrupt', ' '.join(report['metadata_errors']))

    @requirements('TF-18', why_not_scenario='Verify conflicting explicit profile selectors are rejected using isolated framework fixtures.')
    def test_conflicting_explicit_profile_selectors_are_rejected(self):
        code, output, _ = self.run_fixture(extra_args=('--profile', 'dev', '--profile', 'ci'))
        self.assertNotEqual(0, code, output)
        self.assertIn('profile', output)

    @requirements('TF-10', why_not_scenario='Verify discovery cannot run a restricted module import side effect using isolated framework fixtures.')
    def test_discovery_cannot_run_a_restricted_module_import_side_effect(self):
        command = "from pathlib import Path; Path('import-child').write_text('ran')"
        imported = f"import subprocess,sys; subprocess.run([sys.executable,'-c',{command!r}])"
        code, output, report = self.run_fixture(layer='component', module_import=imported)
        self.assertNotEqual(0, code, output)
        self.assertIsNotNone(report)
        self.assertFalse(report['fixture_import_child'], 'preflight import must not start the prohibited child')

    @requirements('TF-10', why_not_scenario='Verify review B7 discovery filters secrets using isolated framework fixtures.')
    def test_review_B7_discovery_filters_secrets(self):
        code, output, report = self.run_fixture(module_import="import os; assert 'TF_FIXTURE_SECRET' not in os.environ")
        self.assertEqual(0, code, output)

    @requirements('TF-9', why_not_scenario='Verify review B8 preflight preserves estimates using isolated framework fixtures.')
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

    @requirements('TF-9', why_not_scenario='Verify review B4 preflight retains default run record using isolated framework fixtures.')
    def test_review_B4_preflight_retains_default_run_record(self):
        from unittest.mock import patch
        import contextlib, io
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / 'retained.run.json'
            with patch.dict(os.environ, {'COCOLO_TEST_RUN_RECORD': str(record)}), contextlib.redirect_stdout(io.StringIO()):
                parallel_tests.preflight_failure('fixture rejection', 1, None, __import__('time').monotonic())
            self.assertTrue(record.exists())
            self.assertIn('fixture rejection', record.read_text())

    @requirements('TF-9', 'TF-13', why_not_scenario='Verify review B3 runner reaps disabled watchdog with diagnostics using isolated framework fixtures.')
    def test_review_B3_runner_reaps_disabled_watchdog_with_diagnostics(self):
        import time
        started = time.monotonic()
        code, output, report = self.run_fixture('import faulthandler,time\nfaulthandler.cancel_dump_traceback_later()\nprint("stuck witness", flush=True)\ntime.sleep(10)', timeout='0.3')
        self.assertLess(time.monotonic() - started, 5.)
        self.assertNotEqual(0, code, output)
        self.assertTrue(report['workers'][0]['timed_out'])
        self.assertIn('stuck witness', report['workers'][0]['log_tail'])
        self.assertTrue(Path(report['workers'][0]['log']).is_file())

    @requirements('TF-5', why_not_scenario='Verify review C9 executed expected failure and unexpected success do not pass gate using isolated framework fixtures.')
    def test_review_C9_executed_expected_failure_and_unexpected_success_do_not_pass_gate(self):
        for assertion in ('self.assertEqual(1, 2)', 'self.assertEqual(1, 1)'):
            before = 'import unittest\nclass Expected(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_expected(self):\n        ' + assertion
            code, output, result = WorkerTests().probe('pass', 'component', before)
            from test_framework import coverage
            statuses = result['outcomes']
            self.assertNotEqual('passed', coverage({'TF-5': {'checks': ['test_probe.Expected.test_expected']}}, statuses)['TF-5'])

    @requirements('TF-1', why_not_scenario='Verify review C13 unpatched runner fails closed without trusted base using isolated framework fixtures.')
    def test_review_C13_unpatched_runner_fails_closed_without_trusted_base(self):
        code, output, report = self.run_fixture(trusted=False)
        self.assertNotEqual(0, code)
        self.assertIn('trusted --base', output)
        self.assertIsNotNone(report)

    @requirements('TF-1', why_not_scenario='Execute the actual CI base-selection shell against diverged local git history.')
    def test_B5_missing_base_fails_and_advanced_PR_base_uses_merge_base(self):
        from test_ci_stages import jobs
        section = jobs((ROOT / '.github/workflows/ci.yml').read_text())['unit']
        shell = textwrap.dedent(section.split('run: |', 1)[1].split('      - name:', 1)[0])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-b', 'main'); git('config', 'user.name', 'Review fixture'); git('config', 'user.email', 'review@example.invalid')
            git('commit', '--allow-empty', '-m', 'base'); common = git('rev-parse', 'HEAD')
            git('checkout', '-b', 'feature'); git('commit', '--allow-empty', '-m', 'feature')
            git('checkout', 'main'); git('commit', '--allow-empty', '-m', 'advanced base'); advanced = git('rev-parse', 'HEAD')
            git('checkout', 'feature')
            env = {**os.environ, 'PR_BASE': '', 'PUSH_BASE': '', 'GITHUB_ENV': str(root / 'environment')}
            missing = subprocess.run(['bash', '-e', '-c', shell], cwd=root, env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(0, missing.returncode)
            self.assertFalse((root / 'environment').exists())
            advanced_run = subprocess.run(['bash', '-e', '-c', shell], cwd=root, env={**env, 'PR_BASE': advanced}, capture_output=True, text=True, timeout=5)
            self.assertEqual(0, advanced_run.returncode, advanced_run.stderr)
            self.assertEqual('COCOLO_REVIEW_BASE=' + common + '\n', (root / 'environment').read_text())

    @requirements('TF-17', 'TF-18', why_not_scenario='An unknown inherited profile must fail before discovery or execution.')
    def test_unknown_profile_environment_is_rejected_by_runner_discovery(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'COCOLO_TEST_PROFILE': 'unknown-review-profile'}):
            code, output, report = self.run_fixture()
        self.assertNotEqual(0, code)
        self.assertIn('unknown test profile', output)

    @requirements('TF-12', why_not_scenario='Ready-only waits and explicit injected clocks must pass while live joins and polls fail immediately.')
    def test_C13_ready_join_fake_clock_poll_and_elapsed_bound(self):
        body = 'import builtins, selectors, os\nbuiltins.DONE.join(1)\nr,w=os.pipe()\nos.write(w,b"ready")\ntry:\n    with selectors.SelectSelector() as selector:\n        selector.register(r,selectors.EVENT_READ)\n        self.assertTrue(selector.select(1))\nfinally:\n    os.close(r); os.close(w)\nclass FakeClock:\n    now=0\n    def sleep(self,seconds): self.now += seconds\nclock=FakeClock(); clock.sleep(20); self.assertEqual(20,clock.now)'
        bootstrap = 'import threading,builtins; builtins.DONE=threading.Thread(target=lambda: None); builtins.DONE.start(); builtins.DONE.join()'
        code, output, _ = WorkerTests().probe(body, 'unit', bootstrap=bootstrap)
        self.assertEqual(0, code, output)
        bootstrap = 'import threading,time,builtins; real_sleep=time.sleep; builtins.LIVE=threading.Thread(target=lambda: real_sleep(10),daemon=True); builtins.LIVE.start()'
        code, output, result = WorkerTests().probe('import builtins,time\nstarted=time.monotonic()\ntry:\n    builtins.LIVE.join(1)\nexcept Exception:\n    pass\nself.assertLess(time.monotonic()-started,.5)', 'unit', bootstrap=bootstrap)
        self.assertNotEqual(0, code, output); self.assertTrue(result['violations'])
        if hasattr(__import__('select'), 'poll'):
            code, output, result = WorkerTests().probe('import select,time\nstarted=time.monotonic()\ntry:\n    select.poll().poll(1000)\nexcept Exception:\n    pass\nself.assertLess(time.monotonic()-started,.5)', 'unit')
            self.assertNotEqual(0, code, output); self.assertTrue(result['violations'])
        else:
            print('not_run: ' + sys.platform + ' lacks select.poll')

    @requirements('TF-9', 'TF-13', why_not_scenario='Two concurrent workers must retain independent failure and timeout diagnostics.')
    def test_C13_failing_and_timed_out_workers_keep_both_logs(self):
        body = "import faulthandler,time\nfaulthandler.cancel_dump_traceback_later()\nif __name__ == 'test_probe':\n    self.fail('intentional failure')\nelse:\n    time.sleep(30)"
        code, output, report = self.run_fixture(body, siblings=True, timeout='.3')
        self.assertNotEqual(0, code)
        self.assertEqual(2, len(report['workers']))
        self.assertEqual(1, sum(w['timed_out'] for w in report['workers']))
        for worker in report['workers']:
            self.assertNotEqual(0, worker['returncode'])
            self.assertTrue(Path(worker['log']).exists())
            self.assertTrue(worker['log_tail'])


if __name__ == '__main__':
    unittest.main()
