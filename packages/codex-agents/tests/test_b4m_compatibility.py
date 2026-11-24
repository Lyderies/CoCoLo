"""LR-1: an older local install tells its owner exactly how to update it."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, REPOSITORY_ROOT, TEMP_ROOT

import cli
from codex_agents import collector_client, host_commands, learning_store
from codex_agents.collector_contracts import CollectorNewerVersionError

ROOT = REPOSITORY_ROOT.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_framework import requirements  # noqa: E402


@requirements('RS-B-B4m-1', why_not_scenario='Real local binding and shell; transport and authorization identities are controlled, without sockets.')
class CompatibilityMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project 'with spaces; $(false)"
        self.project.mkdir()
        self.clone = self.base / "clone 'with spaces; $(false)"
        self.clone.mkdir()
        setup = self.clone / 'setup.sh'
        setup.write_text('#!/bin/sh\n[ "$#" = 1 ] || exit 9\nprintf "%s" "$1"\n')
        setup.chmod(0o700)
        self.copy = self.base / 'agent-orchestration' / host_commands.project_digest(self.project) / ('a' * 64)
        record = self.copy / host_commands.BINDING_RELATIVE
        record.parent.mkdir(parents=True)
        record.write_bytes(host_commands._binding_bytes(self.project, 'a' * 64, '0.8.55', self.clone))
        (self.copy / host_commands.MANIFEST_RELATIVE).write_text(json.dumps({
            'format': 1, 'version': '0.8.55', 'files': {str(host_commands.CLI_RELATIVE): '0' * 64}}))
        self.orchestration = record.parent
        self.command = f'cd {shlex.quote(str(self.clone))} && ./setup.sh {shlex.quote(str(self.project))}'

    def health_error(self, **extra):
        reply = {'ok': True, 'runtime_version': '0.8.55',
                 'schema_version': learning_store.LEDGER_SCHEMA_VERSION,
                 'runtime_digest': 'b' * 64, **extra}
        with mock.patch.object(collector_client, 'PACKAGE_VERSION', '0.8.54'), \
                mock.patch.object(collector_client, 'exchange_frame', return_value=json.dumps(reply).encode()):
            with self.assertRaises(CollectorNewerVersionError) as caught:
                collector_client.CollectorClient(self.base / 'unused.sock').health(self.project)
        self.assertEqual('runtime_newer', caught.exception.code)
        return str(caught.exception)

    def test_old_client_newer_collector_names_command(self):
        # Only the local protected copy lookup is controlled; its binding is really read and verified.
        with mock.patch.object(host_commands, 'runtime_copy_root', return_value=self.copy):
            message = self.health_error(clone_root='/reply/CANARY', project_root='/reply/CANARY', command='CANARY')
        self.assertIn('0.8.55', message)
        self.assertIn('holds the shared learning state', message)
        self.assertIn(self.command, message)
        self.assertIn('plain terminal', message)
        self.assertIn('Configure', message)
        self.assertNotIn('CANARY', message)
        run = subprocess.run(['/bin/sh', '-c', self.command], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(str(self.project), run.stdout)

    def test_three_sites_share_the_command(self):
        state = mock.Mock()
        state.collector_authorization.exists.return_value = True
        current = {'runtime_version': '0.8.55', 'runtime_digest': 'b' * 64}
        with mock.patch.object(host_commands, 'runtime_copy_root', return_value=self.copy):
            health = self.health_error()
            with mock.patch.object(learning_store, 'validate_collector_runtime', return_value={'runtime_version': '0.8.54'}), \
                    mock.patch.object(learning_store, '_collector_runtime_authorization', return_value=current):
                with self.assertRaises(CollectorNewerVersionError) as caught:
                    learning_store.authorize_collector_runtime(state, self.orchestration / 'cli.py')
            authorization = str(caught.exception)
            stdout = io.StringIO()
            with mock.patch.object(sys, 'argv', ['cli.py', 'learning-start-project-service', '--project-root', str(self.project)]), \
                    mock.patch.object(cli, '_require_protected_runtime'), mock.patch.object(cli, '_require_own_roots'), \
                    mock.patch.object(cli, '_start_project_host', side_effect=cli.ProjectServiceOtherVersion('0.8.55')), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(2, cli.main())
            report = json.loads(stdout.getvalue())
        self.assertEqual('project_service_other_version', report['reason_code'])
        for message in (health, authorization, report['error']):
            with self.subTest(message=message):
                self.assertIn(self.command, message)
                self.assertIn('0.8.55', message)
                self.assertIn('Configure', message)
        helper = importlib.import_module('codex_agents.compatibility_message')
        # One builder is actually called by each path, not three copied strings.
        for module in (collector_client, learning_store, cli):
            self.assertIs(helper.compatibility_message, module.compatibility_message)

    def test_missing_local_record_does_not_guess_a_clone(self):
        helper = importlib.import_module('codex_agents.compatibility_message')
        with mock.patch.object(host_commands, 'runtime_copy_root', side_effect=host_commands.HostCommandError('missing')):
            message = helper.compatibility_message('0.8.55', project_root=self.project,
                                                   orchestration_root=self.project / '.codex/orchestration')
        self.assertIn('local install record', message)
        self.assertNotIn('cd ', message)
        self.assertIn('./setup.sh', message)

    def test_registration_from_clone_keeps_the_project_in_the_command(self):
        state = mock.Mock()
        state.collector_authorization.exists.return_value = True
        with mock.patch.object(host_commands, 'runtime_copy_root', return_value=self.copy), \
                mock.patch.object(learning_store, 'validate_collector_runtime', return_value={'runtime_version': '0.8.54'}), \
                mock.patch.object(learning_store, '_collector_runtime_authorization', return_value={'runtime_version': '0.8.55'}):
            with self.assertRaises(CollectorNewerVersionError) as caught:
                learning_store.authorize_collector_runtime(
                    state, ORCHESTRATION_ROOT / 'cli.py', project_root=self.project)
        self.assertIn(self.command, str(caught.exception))

    def test_project_service_version_comes_from_local_hint_and_release_record(self):
        helper = importlib.import_module('codex_agents.compatibility_message')
        from codex_agents import project_host_client
        catalogue = self.clone / 'packages/codex-agents/runtime-releases.json'
        catalogue.parent.mkdir(parents=True)
        catalogue.write_text(json.dumps({'0.8.54': 'b' * 64, '0.8.55': 'a' * 64}))
        hint = (mock.Mock(runtime_digest='b' * 64), (self.base / 'service/socket',), True)
        with mock.patch.object(host_commands, 'runtime_copy_root', return_value=self.copy), \
                mock.patch.object(project_host_client, '_hinted_host', return_value=hint):
            self.assertEqual('0.8.54', helper.project_service_version(self.base, self.project))


@requirements('RS-B-B4m-3', why_not_scenario='Documentation contract: both published help locations must carry the old-client repair command.')
class CompatibilityDocsTests(unittest.TestCase):
    def test_old_installs_have_the_codes_and_terminal_command_in_docs(self):
        for path in (ROOT / 'docs/troubleshooting.md', ROOT / 'CHANGELOG.md'):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertIn('status_collector_unavailable', text)
                self.assertIn('runtime_newer', text)
                self.assertIn('cd /path/to/CoCoLo', text)
                self.assertIn('./setup.sh /path/to/project', text)
                self.assertIn('Configure', text)


if __name__ == '__main__':
    unittest.main()
