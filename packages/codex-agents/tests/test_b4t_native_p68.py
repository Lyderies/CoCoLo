"""Decision 372: the owner's 0.8.69 install was refused (`predecessor_unproven`) against a real, idle 0.8.68 service.

The installed service runs from its protected runtime copy (`~/.codex/agent-orchestration/<project>/<runtime>/`),
the launch target setup records and the collector's starter launches. The S5 native witnesses launched the archived
service from the project's own tree, so the copy was never the candidate entry. This module runs the unmodified
0.8.68 package (`tests/fixtures/b4t/0.8.68-native-package.tar.gz`, `git archive e46759a5`), with its own collector
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

ARTIFACT = Path(__file__).resolve().parents[3] / 'tests/fixtures/b4t/0.8.68-native-package.tar.gz'
ARTIFACT_SHA256 = 'e05a68602ff65455dc8cdbb4efd0c696b4a61fe9a20665c34d4c622547e9b765'
P68_DIGEST = '28435b174d88830fbf06bf1fc1b07ec832177a17045455a06c3ada86673d9ed0'

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
class RealP68LaunchCopyTests(StarterBase):
    def setUp(self):
        StarterBase.setUp(self)
        raw = ARTIFACT.read_bytes()
        self.assertEqual(ARTIFACT_SHA256, hashlib.sha256(raw).hexdigest(), 'vendored 0.8.68 artifact changed')
        archive = self.base / 'p68'
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as packed:
            packed.extractall(archive, filter='data')
        package = archive / 'packages/codex-agents/native-package'
        self.assertEqual('0.8.68', (package / 'VERSION').read_text().strip())
        with mock.patch('tests.test_host_commands.BUNDLE_ROOT', package), \
                mock.patch('tests.test_host_commands.PACKAGE_VERSION', '0.8.68'):
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
        self.assertEqual(P68_DIGEST, self.copy.name)
        # 0.8.68's own collector; its starter launches the recorded copy (the installed launch line).
        self.launch(orchestration=self.older)
        from codex_agents.project_host_client import _hinted_host
        self.hint = wait_for(self, lambda: (lambda found: found if found is not None and found[2] is True else None)(
            _hinted_host(self.paths, self.project, any_runtime=True)), 45,
            lambda: 'the 0.8.68 starter never served: ' + (self.base / 'collector.err').read_text()[-2000:])

    def relaunch(self):
        if self.collector is not None:
            self.held.append(self.collector)
        self.launch()

    def service_pid(self):
        from codex_agents.takeover_preflight import _peer
        return _peer(self.hint[1][1])

    @requirements('RS-B-B4t-1', 'RS-B-B4t-3',
                  why_not_scenario='Real 0.8.68 collector and service from its launch copy; native peer, argv and flocks.')
    def test_B4t_0869_idle_real_P68_service_from_its_launch_copy_is_stopped_by_its_stop_op(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.takeover_owner import OwnerOperations, run_owner_takeover
        from codex_agents.takeover_process import Kernel
        from codex_agents.process_tree import process_gone
        from codex_agents.collector_client import CollectorClient, COLLECTOR_RUNTIME_DIGEST
        from tests.support import ORCHESTRATION_ROOT
        pid = self.service_pid()
        argv = Kernel().identity(pid)['argv']
        entry = self.copy / '.codex/orchestration/project_host.py'
        # The installed launch line: the copy's entry, this state root and project root.
        self.assertEqual(['-I', '-B', '-S', '-X', 'pycache_prefix=/dev/null', str(entry), '--state-root',
                          str(self.state.root), '--project-root', str(self.project)], argv[1:])
        plan = read_plan(self.state, self.project)  # 0.8.69 candidate: predecessor_unproven, 0 child runs
        self.assertTrue(plan['needed'])
        self.assertEqual('stop_op', plan['report']['service_stop'])
        self.assertEqual('idle', plan['report']['service'])
        self.assertEqual('0.8.68', plan['report']['service_version'])
        old = plan['intent']['predecessor_service']
        self.assertEqual(str(entry), old['entry'])
        self.assertEqual(pid, old['pid'])
        ops = OwnerOperations(self.state, ORCHESTRATION_ROOT / 'cli.py', self.project, lambda: plan['intent'])
        ops.start_collector = lambda document: self.relaunch()
        with mock.patch('os.kill', side_effect=AssertionError('idle service signalled')), \
                mock.patch('os.killpg', side_effect=AssertionError('idle group signalled')), \
                mock.patch.object(signal, 'pidfd_send_signal', create=True,
                                  side_effect=AssertionError('idle pidfd signal')):
            done = run_owner_takeover(ops)
        self.assertEqual('account_done', done['phase'])
        wait_for(self, lambda: process_gone(pid), 10, 'the 0.8.68 service did not stop')
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, CollectorClient(self.state.socket).health()['runtime_digest'])

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9',
                  why_not_scenario='Real idle 0.8.68 service whose launch line cannot be matched: plain write-free refusal.')
    def test_B4t_0869_idle_service_that_cannot_be_proven_names_a_working_step(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.takeover_process import Kernel
        from codex_agents.update_takeover import TakeoverError
        pid = self.service_pid()
        real = Kernel.identity
        def other_argv(kernel, target):
            found = real(kernel, target)
            return dict(found, argv=[*found['argv'][:-1], '/elsewhere'])
        from codex_agents.takeover_journal import TakeoverJournal
        authorization = self.state.collector_authorization.read_bytes()
        with mock.patch.object(Kernel, 'identity', other_argv), self.assertRaises(TakeoverError) as caught:
            read_plan(self.state, self.project)
        self.assertEqual('predecessor_unproven', caught.exception.code)
        text = str(caught.exception)
        self.assertNotIn('When that run ends', text)
        self.assertNotIn('0 child runs', text)
        self.assertIn('no child run', text)
        self.assertIn(f'ps -o pid,lstart,command -p {pid}', text)
        self.assertIn(f'kill {pid}', text)
        self.assertIn('Nothing was changed', text)
        self.assertIn('./setup.sh --update', text)
        from codex_agents.process_tree import process_gone
        self.assertFalse(process_gone(pid))  # nothing was stopped
        self.assertIsNone(TakeoverJournal(self.state.root).read())
        self.assertEqual(authorization, self.state.collector_authorization.read_bytes())


if __name__ == '__main__':
    unittest.main()
