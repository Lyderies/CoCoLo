"""The owner's installed 0.8.69 (final build b63b6f5c) must be taken over by this build's update (0.8.73).

The installed service runs from its protected runtime copy (`~/.codex/agent-orchestration/<project>/<runtime>/`),
the launch target setup records and the collector's starter launches. The S5 native witnesses launched the archived
service from the project's own tree, so the copy was never the candidate entry. This module runs the unmodified
0.8.69 package (`tests/fixtures/b4t/0.8.69-native-package.tar.gz`, `git archive b63b6f5c`), with its own collector
starting its own service from that copy, exactly as on the owner's machine. Run outside the builder sandbox.
COCOLO_TEST_B4T_SKIP_SOCKETS=1 is the builder's explicit no-socket gate only.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.test_project_start import StarterBase, wait_for
from tests.test_host_commands import install_package
from test_framework import requirements

ARTIFACT = Path(__file__).resolve().parents[3] / 'tests/fixtures/b4t/0.8.69-native-package.tar.gz'
ARTIFACT_SHA256 = '674deabb79f6cce82c01687c51a2655fd6d6a9a9c9143eed737c23fb88982294'
P69_DIGEST = '60569d309ef2688b93694926c6a1e3e8df6ed0537b5e65f815828aeba63ca7e3'

SETUP = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents import host_commands
home=Path(sys.argv[4])
host_commands.account_home=lambda:home
host_commands.temp_roots=lambda:set()
from codex_agents.learning_store import LearningState,LearningLedger,authorize_collector_runtime
state=LearningState(Path(sys.argv[2]),create=False)
project=Path(sys.argv[3])
owner=LearningLedger(state)
try:
    project_id=owner.register_project(project)
    copy,_=host_commands.install_runtime_copy(project,{'HOME':str(home)})
    owner.record_launch(project_id,str(copy),copy.name)
finally:owner.close()
authorize_collector_runtime(state,Path(sys.argv[1])/'cli.py',project_root=project)
print(json.dumps([project_id,str(copy)]))
'''


@unittest.skipIf(os.environ.get('COCOLO_TEST_B4T_SKIP_SOCKETS') == '1',
                 'Primary must rerun: this builder is forbidden to run socket tests')
class RealP69LaunchCopyTests(StarterBase):
    def setUp(self):
        StarterBase.setUp(self)
        raw = ARTIFACT.read_bytes()
        self.assertEqual(ARTIFACT_SHA256, hashlib.sha256(raw).hexdigest(), 'vendored 0.8.69 artifact changed')
        archive = self.base / 'p69'
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as packed:
            packed.extractall(archive, filter='data')
        package = archive / 'packages/codex-agents/native-package'
        self.assertEqual('0.8.69', (package / 'VERSION').read_text().strip())
        with mock.patch('tests.test_host_commands.BUNDLE_ROOT', package), \
                mock.patch('tests.test_host_commands.PACKAGE_VERSION', '0.8.69'):
            self.project = install_package(self.base / 'P').resolve()
        (self.project / '.git').mkdir()
        self.older = self.project / '.codex/orchestration'
        self.state.collector_authorization.unlink()
        made = subprocess.run([sys.executable, '-I', '-B', '-c', SETUP, str(self.older), str(self.state.root),
                               str(self.project), str(self.home)],
                              env={'PATH': os.defpath, 'TMPDIR': str(self.base)}, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(0, made.returncode, made.stderr)
        self.project_id, copy = json.loads(made.stdout)
        self.copy = Path(copy)
        self.assertEqual(P69_DIGEST, self.copy.name)
        # 0.8.69's own collector; its starter launches the recorded copy (the installed launch line).
        self.launch(orchestration=self.older)
        from codex_agents.project_host_client import _hinted_host
        self.hint = wait_for(self, lambda: (lambda found: found if found is not None and found[2] is True else None)(
            _hinted_host(self.paths, self.project, any_runtime=True)), 45,
            lambda: 'the 0.8.69 starter never served: ' + (self.base / 'collector.err').read_text()[-2000:])

    def relaunch(self):
        if self.collector is not None:
            self.held.append(self.collector)
        self.launch()

    def service_pid(self):
        from codex_agents.takeover_preflight import _peer
        return _peer(self.hint[1][1])

    def run_update(self, plan):
        from codex_agents.takeover_owner import OwnerOperations, run_owner_takeover
        from tests.support import ORCHESTRATION_ROOT
        ops = OwnerOperations(self.state, ORCHESTRATION_ROOT / 'cli.py', self.project, lambda: plan['intent'])
        ops.start_collector = lambda document: self.relaunch()
        with mock.patch('os.kill', side_effect=AssertionError('idle service signalled')), \
                mock.patch('os.killpg', side_effect=AssertionError('idle group signalled')), \
                mock.patch.object(signal, 'pidfd_send_signal', create=True,
                                  side_effect=AssertionError('idle pidfd signal')):
            return run_owner_takeover(ops)

    def store_path(self):
        return self.state.root / 'projects' / self.project_id / 'history.sqlite3'

    @requirements('RS-B-B4t-1', 'RS-B-B4t-3',
                  why_not_scenario='Real 0.8.69 collector and service from its launch copy; native peer, argv and flocks.')
    def test_B4t_0871_idle_real_P69_service_is_taken_over_without_a_signal_and_the_floor_rises(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.process_tree import process_gone
        from codex_agents.collector_client import CollectorClient, COLLECTOR_RUNTIME_DIGEST
        from codex_agents.takeover_schema import check_floor
        pid = self.service_pid()
        plan = read_plan(self.state, self.project)
        self.assertTrue(plan['needed'])
        self.assertEqual('stop_op', plan['report']['service_stop'])
        self.assertEqual('idle', plan['report']['service'])
        self.assertEqual('0.8.69', plan['report']['service_version'])
        self.assertEqual(0, plan['report']['children'])
        done = self.run_update(plan)
        self.assertEqual('account_done', done['phase'])
        wait_for(self, lambda: process_gone(pid), 10, 'the 0.8.69 service did not stop')
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, CollectorClient(self.state.socket).health()['runtime_digest'])
        from contextlib import closing
        import sqlite3
        for database in (self.state.root / 'learning.sqlite3', self.store_path()):
            if not database.exists():
                continue
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                self.assertEqual('0.8.73', db.execute('SELECT floor_version FROM runtime_floor').fetchone()[0], database)
                check_floor(db, '0.8.73', RuntimeError)
                with self.assertRaises(RuntimeError):
                    check_floor(db, '0.8.69', RuntimeError)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9',
                  why_not_scenario='Real 0.8.69 service whose launch line cannot be matched while one child run is counted: plain write-free refusal.')
    def test_B4t_0871_busy_service_that_cannot_be_proven_refuses_write_free_with_the_busy_wording(self):
        from codex_agents import takeover_preflight
        from codex_agents.takeover_process import Kernel
        from codex_agents.update_takeover import TakeoverError
        from codex_agents.process_tree import process_gone
        pid = self.service_pid()
        real_identity, real_registry = Kernel.identity, takeover_preflight._registry
        def other_argv(kernel, target):
            found = real_identity(kernel, target)
            return dict(found, argv=[*found['argv'][:-1], '/elsewhere'])
        def one_child(state, root_digest):
            row, schema, children = real_registry(state, root_digest)
            return row, schema, {row['project_id']: 1}  # the account census counts one active child run
        def snapshot():
            store = self.store_path()
            return (store.read_bytes() if store.exists() else None, self.state.collector_authorization.read_bytes())
        before = snapshot()
        with mock.patch.object(Kernel, 'identity', other_argv), \
                mock.patch.object(takeover_preflight, '_registry', one_child), \
                self.assertRaises(TakeoverError) as caught:
            takeover_preflight.read_plan(self.state, self.project)
        self.assertEqual('predecessor_unproven', caught.exception.code)
        text = str(caught.exception)
        self.assertIn('running 1 child run', text)
        self.assertIn('When that run ends, the old service stops by itself', text)
        self.assertIn('Nothing was changed', text)
        self.assertIn('./setup.sh --update', text)
        self.assertFalse(process_gone(pid))
        self.assertEqual(before, snapshot())  # no store byte and no authorization byte changed
        from codex_agents.takeover_journal import TakeoverJournal
        self.assertIsNone(TakeoverJournal(self.state.root).read())


    @requirements('RS-B-B4t-3',
                  why_not_scenario='Real released 0.8.69 runtime against the store the 0.8.73 update adopted.')
    def test_B4t_0871_after_adoption_released_P69_cannot_reopen_the_store(self):
        from codex_agents.takeover_preflight import read_plan
        self.assertEqual('account_done', self.run_update(read_plan(self.state, self.project))['phase'])
        store = self.store_path()
        self.assertTrue(store.exists())
        old_runtime = self.project / '.codex/orchestration'  # still the unmodified 0.8.69 package tree
        code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from codex_agents.project_history import ProjectHistory;"
                "ProjectHistory(Path(sys.argv[2]),sys.argv[3])")
        self.stop_hosts()  # this test's own successor service and collector, so the store's writer lock is free
        self.stop_collector()
        before = store.read_bytes()
        done = subprocess.run([sys.executable, '-B', '-c', code, str(old_runtime), str(store.parent),
                               self.project_id], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, done.returncode)
        # The floor (0.8.73) refuses the released 0.8.69: the fence refusal (`history_wrong_owner`), with its update step.
        self.assertIn('This store owner was replaced or lost its writer lock. Run: ./setup.sh --update', done.stderr)
        self.assertEqual(before, store.read_bytes())


if __name__ == '__main__':
    unittest.main()
