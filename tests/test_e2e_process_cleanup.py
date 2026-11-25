"""E2ELEAK: real detached World-like processes, without an installer or live account."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements

# The stand-in builds no product files and uses only the test's writable scratch root.
FLOW = r'''
import os, sys, time, subprocess, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import e2e_support
def launch(world):
    root = Path(sys.argv[2])
    service = world.base / 'collector_host.py'
    native = ("import os,signal,time; from pathlib import Path; "
              "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              "Path(%r).write_text(str(os.getpid())); time.sleep(90)" % str(root / 'service.pid'))
    service.write_text("import signal,time,sys,subprocess\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + "child=subprocess.Popen([%r,'-c',%r],start_new_session=True)\n" % (world.python, native)
        + "while not Path(%r).exists(): time.sleep(.01)\n" % str(root / 'service.pid')
        + "Path(sys.argv[1]).write_text(str(__import__('os').getpid()))\ntime.sleep(90)\n")
    return subprocess.Popen([str(world.shim), str(service), str(root / 'child.pid')],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=world.env, start_new_session=True)
class FixtureWorld(e2e_support.World):
    def _build(self):
        self.base = Path(sys.argv[2]) / 'e2e-fixture'
        self.base.mkdir()
        self.home = self.base / 'h'
        (self.home / '.e2e-bin').mkdir(parents=True)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.python = sys.executable
        self.passphrase = 'fixture'
        self.env = dict(os.environ)
        self._stand_in_python()
        if sys.argv[3] == 'partial':
            launch(self)
            while not Path(sys.argv[2], 'child.pid').exists(): time.sleep(.01)
            raise RuntimeError('partial construction')
world = FixtureWorld().__enter__()
child = launch(world)
Path(sys.argv[2], 'owner-ready').write_text(str(child.pid))
while not Path(sys.argv[2], 'child.pid').exists(): time.sleep(.01)
mode = sys.argv[3]
if mode == 'pass': world.__exit__(None, None, None)
elif mode == 'exception': raise RuntimeError('mid-flow')
elif mode == 'assertion': assert False, 'mid-flow'
elif mode == 'timeout':
    try: subprocess.run([str(world.shim), '-c', 'import time; time.sleep(90)'], timeout=.2, env=world.env)
    finally: world.__exit__(None, None, None)
elif mode == 'exit': pass
elif mode == 'discard':
    del world
    __import__('gc').collect()
    e2e_support.close_worlds()
    assert child.poll() is not None, 'discarded World was forgotten'
    Path(sys.argv[2], 'discard-cleaned').touch()
else:
    Path(sys.argv[2], 'waiting').touch()
    time.sleep(90)
'''


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate(): return True
        time.sleep(.02)
    return bool(predicate())


class WorldCleanupTests(unittest.TestCase):
    __why_not_scenario__ = "Scenario witnesses for the harness itself use detached disposable hosts without product installation."
    def flow(self, mode, kill=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = root / 'worker'
            registry.mkdir()
            env = {**os.environ, 'COCOLO_E2E_REGISTRY': str(registry)}
            log = (root / 'owner.log').open('wb')
            process = subprocess.Popen([sys.executable, '-B', '-c', FLOW, str(ROOT / 'scripts'), folder, mode],
                                       env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
            try:
                self.assertTrue(wait_for(lambda: (root / 'child.pid').exists()), 'fixture never started')
                pid = int((root / 'child.pid').read_text())
                if kill:
                    self.assertTrue(wait_for(lambda: (root / 'waiting').exists()))
                    process.send_signal(kill)
                process.wait(timeout=10)
                if mode == 'discard': self.assertTrue((root / 'discard-cleaned').exists(), (root / 'owner.log').read_text())
                self.assertTrue(wait_for(lambda: not alive(pid)), 'World collector survived flow end')
                service_pid = int((root / 'service.pid').read_text())
                self.assertTrue(wait_for(lambda: not alive(service_pid)), 'detached native project service survived')
                self.assertTrue(wait_for(lambda: not (root / 'e2e-fixture').exists()), 'World directory survived')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                for name in ('child.pid', 'service.pid'):
                    if (root / name).exists():
                        candidate = int((root / name).read_text())
                        if alive(candidate): os.kill(candidate, signal.SIGKILL)

    @requirements('E2ELEAK-1', 'E2ELEAK-3')
    def test_failure_mid_flow_leaves_no_collector_or_world(self):
        for mode in ('assertion', 'exception', 'partial'):
            with self.subTest(mode=mode): self.flow(mode)

    @requirements('E2ELEAK-1', 'E2ELEAK-3')
    def test_pass_exit_and_timeout_leave_no_survivor(self):
        for mode in ('pass', 'exit', 'timeout'):
            with self.subTest(mode=mode): self.flow(mode)

    @requirements('E2ELEAK-1', 'E2ELEAK-4')
    def test_sigterm_and_hard_owner_death_remove_detached_collector(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig): self.flow('wait', sig)

    @requirements('E2ELEAK-2', 'E2ELEAK-4')
    def test_reaper_uses_world_marker_and_start_time_across_sessions(self):
        import e2e_processes as owned
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = root / 'registry'
            registry.mkdir()
            base = root / 'e2e-owned'
            base.mkdir()
            marker = owned.initialize(base, registry=registry)
            children = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],
                                        start_new_session=True) for _ in range(4)]
            try:
                records = [owned.register(base, p.pid) for p in children]
                # A stale pid start time and a foreign World marker never authorize a kill.
                records[1]['start'] = 'stale'
                records[2]['marker'] = 'another-world'
                for i in (1, 2):
                    (base / '.e2e-processes' / f'{children[i].pid}.json').write_text(json.dumps(records[i]))
                (base / '.e2e-processes' / f'{children[3].pid}.json').unlink()  # unrecorded lookalike
                (base / '.e2e-processes' / 'bad.json').write_text('{')
                owned.reap_worlds(registry)
                self.assertIsNotNone(children[0].poll(), f'recorded detached process survived reaper: record={records[0]}, current={owned.identity(children[0].pid)}')
                for child in children[1:]: self.assertIsNone(child.poll(), 'reaper killed a nonmatching process')
                self.assertFalse(base.exists())
                self.assertTrue(marker)
            finally:
                for child in children:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=3)

    @requirements('E2ELEAK-3')
    def test_drained_stop_precedes_term_and_kill_and_closed_world_refuses_spawn(self):
        from unittest import mock
        import e2e_processes as owned
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            owned.initialize(base)
            child = subprocess.Popen([sys.executable, '-c',
                                      'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(90)'])
            try:
                owned.register(base, child.pid)
                events = []
                real_kill = owned.os.kill
                def kill(pid, sig):
                    events.append(sig)
                    return real_kill(pid, sig)
                with mock.patch.object(owned, 'drained_stop', side_effect=lambda base: events.append('drain')), \
                     mock.patch.object(owned.os, 'kill', side_effect=kill):
                    owned.cleanup(base, remove=False)
                child.wait(timeout=3)
                self.assertEqual('drain', events[0])
                self.assertIn(signal.SIGTERM, events)
                self.assertIn(signal.SIGKILL, events)
                self.assertLess(events.index(signal.SIGTERM), events.index(signal.SIGKILL))
                with self.assertRaises(RuntimeError): owned.register(base, os.getpid())
                owned.cleanup(base, remove=False)  # idempotent
            finally:
                if child.poll() is None: child.kill()
                child.wait(timeout=3)

    @requirements('E2ELEAK-4')
    def test_runner_reaps_detached_registered_hosts_on_all_exit_paths(self):
        from tests.test_test_framework_guards import RunnerIntegrationTests
        child_source = ("import signal,time\nfrom pathlib import Path\n"
                        "def stop(*args):\n    Path('child-stopped').touch(); raise SystemExit\n"
                        "signal.signal(signal.SIGTERM,stop)\nPath('child-ready').touch()\ntime.sleep(90)")
        body = ("import os,subprocess,sys,time,signal\nfrom pathlib import Path\nimport e2e_processes as owned\n"
                "base=Path.cwd()/'e2e-detached'\nbase.mkdir()\n"
                "owned.initialize(base, registry=os.environ['COCOLO_E2E_REGISTRY'])\n"
                f"child=subprocess.Popen([sys.executable,'-c',{child_source!r}],start_new_session=True)\n"
                "owned.register(base,child.pid)\n"
                "end=time.monotonic()+3\nwhile not Path('child-ready').exists() and time.monotonic()<end: time.sleep(.01)\n"
                "self.assertTrue(Path('child-ready').exists())\n")
        helper = RunnerIntegrationTests()
        for mode in ('pass', 'failure', 'timeout', 'sigterm', 'interrupt'):
            with self.subTest(mode=mode):
                tail = {'pass': '', 'failure': "self.fail('mid-flow')", 'timeout': 'time.sleep(90)',
                        'sigterm': 'os.kill(os.getppid(),signal.SIGTERM); time.sleep(90)',
                        'interrupt': 'time.sleep(90)'}[mode]
                code, output, report = helper.run_fixture(body + tail, timeout='1' if mode == 'timeout' else '600',
                                                         interrupt=mode == 'interrupt')
                self.assertIsNotNone(report, output)
                self.assertEqual(0 if mode == 'pass' else 1, int(code != 0), output)
                self.assertTrue(report['fixture_child_stopped'], output)
                if mode in ('pass', 'failure', 'timeout'): self.assertEqual([], report['metadata_errors'])

    @requirements('E2ELEAK-1', 'E2ELEAK-4')
    def test_lifecycle_parent_sigterm_stops_workers_and_their_worlds(self):
        # Exercise the real lifecycle scheduler, replacing only the worker's product flow.
        worker_flow = FLOW.replace('import e2e_support',
            "import e2e_support\nimport e2e_processes\ne2e_processes.register(Path(os.environ['FIXTURE_OWNER']),os.getpid())")
        driver = ("import sys,subprocess,os\nfrom pathlib import Path\n"
                  f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\nimport e2e_lifecycle as life\n"
                  "original=subprocess.run\n"
                  "def worker(argv,**kw):\n"
                  "    if '--execute' not in argv: return original(argv,**kw)\n"
                  "    owner=argv[argv.index('--owner-world')+1]\n"
                  "    kw['env']['FIXTURE_OWNER']=owner\n"
                  f"    return original([sys.executable,'-c',{worker_flow!r},{str(ROOT / 'scripts')!r},sys.argv[1],'wait'],**kw)\n"
                  "life.subprocess.run=worker\nlife._execute_in_processes(['fixture_check'],2)\n")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = subprocess.Popen([sys.executable, '-B', '-c', driver, folder],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                self.assertTrue(wait_for(lambda: (root / 'waiting').exists()))
                pid = int((root / 'child.pid').read_text())
                process.terminate()
                process.wait(timeout=10)
                self.assertTrue(wait_for(lambda: not alive(pid)), 'lifecycle worker left a World collector')
                self.assertFalse((root / 'e2e-fixture').exists())
                self.assertFalse(alive(int((root / 'service.pid').read_text())))
            finally:
                if process.poll() is None: process.kill()
                process.wait(timeout=3)
                for name in ('child.pid', 'service.pid'):
                    if (root / name).exists():
                        pid = int((root / name).read_text())
                        if alive(pid): os.kill(pid, signal.SIGKILL)

    @requirements('E2ELEAK-1')
    def test_discarded_world_is_still_owned_until_cleanup(self):
        self.flow('discard')

    @requirements('E2ELEAK-3')
    def test_drained_stop_uses_world_installed_client_without_starting_collector(self):
        import e2e_processes as owned
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            base = root / 'e2e-drain'
            base.mkdir()
            owned.initialize(base)
            endpoint = base / 'h/.local/state/codex-agents/collector.sock'
            endpoint.parent.mkdir(parents=True)
            endpoint.touch()
            package = base / 'project/.codex/orchestration/codex_agents'
            package.mkdir(parents=True)
            (package / '__init__.py').touch()
            result = root / 'drain.json'
            (package / 'collector_client.py').write_text(
                "import json\nfrom pathlib import Path\nclass CollectorClient:\n"
                "    def __init__(self,socket_path,timeout): self.socket=socket_path\n"
                "    def shutdown(self,*,only_if_drained):\n"
                f"        Path({str(result)!r}).write_text(json.dumps([str(self.socket),only_if_drained]))\n")
            owned.cleanup(base, remove=False)
            self.assertEqual([str(endpoint), True], json.loads(result.read_text()))
            self.assertEqual([], list((base / '.e2e-processes').glob('*.json')))

    @requirements('E2ELEAK-1', 'E2ELEAK-2')
    def test_late_boot_without_ownership_helper_never_runs_product_code(self):
        import e2e_support
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'deleted-world/h'
            boot = root / 'late-boot.py'
            boot.write_text(e2e_support.BOOT.replace('%HOME%', repr(str(home)))
                            .replace('%SHIM%', repr('/unused'))
                            .replace('TRACK_WORLD = False', 'TRACK_WORLD = True'))
            sentinel = root / 'product-started'
            product = root / 'product.py'
            product.write_text(f"from pathlib import Path; Path({str(sentinel)!r}).touch()")
            done = subprocess.run([sys.executable, '-B', str(boot), str(product)], capture_output=True, text=True)
            self.assertNotEqual(0, done.returncode)
            self.assertFalse(sentinel.exists(), 'late host escaped registration after its World was removed')

    @requirements('E2ELEAK-2', 'E2ELEAK-4')
    def test_verified_exiting_child_is_not_gone_until_waitpid_releases_it(self):
        from unittest import mock
        import e2e_processes as owned
        record = {'pid': 99999999, 'start': 'verified-birth', 'base': '/unused', 'marker': 'world'}
        # Model the kernel transition: birth identity is gone, but our direct child is
        # not waitable until the next polling pass. No real pid is queried or signalled.
        with mock.patch.object(owned, 'survivors', return_value=[]), \
             mock.patch.object(owned, 'identity', return_value=None), \
             mock.patch.object(owned.os, 'waitpid', side_effect=[(0, 0), (record['pid'], 0)]) as wait, \
             mock.patch.object(owned.time, 'sleep'):
            remaining = owned._wait(Path('/unused'), {}, 1, [record])
        self.assertEqual([], remaining)
        self.assertEqual(2, wait.call_count, 'cleanup returned while its verified child was not yet reaped')
