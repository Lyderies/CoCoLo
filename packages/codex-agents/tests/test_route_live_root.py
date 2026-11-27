"""RT-RX on an installed CLI: the default state root is the one the route's snapshot is read from.

The installed 0.8.54 routed every task `not_checked`, with no reason, although its
account services answered: an installed CLI checks that `--state-root` is the
default one and then stores None ("the default"), and every route call site read
None as "no state root" and skipped the snapshot. The repository's tests run the
source tree, which has no install manifest, so they never took that path.

Here the CLI runs in the installed layout: the package installed into the project
with its install manifest (as tests/test_host_commands.py installs it), the
project as the CLI's own root, and the default state root resolved by the real
`default_state_root()` from the test's HOME and XDG_STATE_HOME. The account and
project services are the harness's real ones; nothing on the route, snapshot,
socket or account path is patched.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401 -- the source CLI import path
from tests import test_bridge
from tests.support import SHORT_TMP
from tests.test_bridge import ClaudePrimaryCase, cli
from tests.test_host_commands import install_package
from codex_agents.collector_paths import default_state_root
from codex_agents.host_commands import OwnRootRefused
from codex_agents.providers import headroom_record
from codex_agents.routing import route_task
from test_framework import requirements


class InstalledRootRouteTests(ClaudePrimaryCase):
    __why_not_scenario__ = ('Real CLI in the installed layout (install manifest present), real account and project '
                            'services in-process (service harness); provider clients are the harness fakes, no model '
                            'is launched.')

    def setUp(self):
        # The state lives where the real default_state_root() puts it for this HOME and XDG_STATE_HOME:
        # a short root, since the collector's socket is in the state root (macOS caps socket paths at 103 bytes).
        self.home_base = Path(tempfile.mkdtemp(dir=SHORT_TMP, prefix='rr'))
        self.addCleanup(shutil.rmtree, self.home_base, True)
        home = self.home_base / 'h'
        state_home = home / '.s' / 'x'
        state_home.mkdir(parents=True, mode=0o700)
        self.environment = {'HOME': str(home), 'XDG_STATE_HOME': str(state_home)}
        self.default_root = state_home / 'codex-agents'
        made = test_bridge.LearningState

        def at_default(root, **options):
            return made(self.default_root if Path(root).name == 'state' else root, **options)

        with mock.patch.object(test_bridge, 'LearningState', at_default):
            super().setUp()
        environment = mock.patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)
        self.assertEqual(self.default_root, default_state_root())  # the real resolver, not a patched one
        self.assertEqual(self.default_root, self.state.root)
        # Installed as the installer installs it, with the bundle's Claude agents the routes name.
        install_package(self.project)
        shutil.copytree(self.bundle / '.claude' / 'agents', self.project / '.claude' / 'agents', dirs_exist_ok=True)
        self.manifest = self.project / '.codex' / 'orchestration' / 'install-manifest.json'
        self.assertTrue(self.manifest.is_file())

    def run_cli(self, command, *arguments, installed=True, expected=0):
        """The CLI as `cli.py <command> ...`: installed, its manifest and its project are the installed ones."""
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, 'argv', ['cli.py', command, *arguments]))
            if installed:
                stack.enter_context(mock.patch.object(cli, 'INSTALL_MANIFEST', self.manifest))
                stack.enter_context(mock.patch.object(cli, 'BUNDLE_ROOT', self.project.resolve()))
            else:
                stack.enter_context(mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            code = cli.main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def exhaust_codex(self):
        fingerprint = self.admit('codex')
        stamp = self.ledger._clock_ms()
        reading = headroom_record(provider='codex', account_fingerprint=fingerprint,
            windows=[{'scope': None, 'window_minutes': 300, 'used_percent': 95, 'resets_at': None}],
            status=None, run_id=None, phase=None, observed_at_ms=stamp)
        self.ledger._store_provider_observations(self.project_id, [reading], timestamp=stamp)
        return fingerprint

    def assert_checked(self, route, fingerprint, label):
        availability = route['availability']
        self.assertEqual('checked', availability['status'], (label, availability))
        self.assertNotIn('reason_code', availability, label)
        codex = availability['providers']['codex']
        self.assertEqual((True, 'exhausted', fingerprint),
                         (codex['known'], codex['state'], codex['account_fingerprint']), label)

    @requirements('RT-RX-LIVE-ROOT-1')
    def test_installed_route_reads_the_default_state_roots_account_snapshot(self):
        fingerprint = self.exhaust_codex()
        task = self.write('task.json', self.task())
        project = ('--project-root', str(self.project))
        given = self.run_cli('learning-route', '--state-root', str(self.default_root), *project,
                             '--task', task, '--host-client', 'claude-host-v1')
        self.assert_checked(given, fingerprint, 'learning-route --state-root <default>')
        self.assertEqual('claude', given['provider'])
        omitted = self.run_cli('learning-route', *project, '--task', task, '--host-client', 'claude-host-v1')
        self.assert_checked(omitted, fingerprint, 'learning-route without --state-root')
        plain = self.run_cli('route', '--task', task, '--root', str(self.project), '--host-client', 'claude-host-v1')
        self.assert_checked(plain, fingerprint, 'route (no --state-root option)')

    @requirements('RT-RX-LIVE-ROOT-2')
    def test_a_not_checked_route_always_says_why(self):
        task = self.write('task.json', self.task())
        # The source tree with no --state-root: nothing names an account service, and it says so.
        for command, arguments in (('learning-route', ('--project-root', str(self.project))),
                                   ('route', ('--root', str(self.project)))):
            with self.subTest(command, installed=False):
                route = self.run_cli(command, *arguments, '--task', task, '--host-client', 'claude-host-v1',
                                     installed=False)
                self.assertEqual(('not_checked', 'snapshot_unavailable:no_state_root'),
                                 (route['availability']['status'], route['availability'].get('reason_code')))
        # Installed, with no default state root to resolve at all (a refused XDG_STATE_HOME).
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.home_base / 'not-hidden')}):
            for command, arguments in (('learning-route', ('--project-root', str(self.project))),
                                       ('route', ('--root', str(self.project)))):
                with self.subTest(command, installed=True):
                    route = self.run_cli(command, *arguments, '--task', task, '--host-client', 'claude-host-v1')
                    availability = route['availability']
                    self.assertEqual('not_checked', availability['status'], availability)
                    self.assertTrue(availability.get('reason_code', '').startswith('snapshot_unavailable:'),
                                    availability)

    @requirements('RT-RX-LIVE-ROOT-2')
    def test_a_route_with_no_snapshot_input_still_says_why(self):
        # Direct route_task() callers that pass neither a snapshot nor a reason.
        task = self.task()
        availability = route_task(task, self.policy)['availability']
        self.assertEqual('not_checked', availability['status'], availability)
        self.assertEqual('snapshot_unavailable:unknown', availability.get('reason_code'), availability)
        # A specific reason a caller gives is kept as it is.
        given = route_task(task, self.policy, snapshot_unavailable='snapshot_unavailable:no_state_root')
        self.assertEqual('snapshot_unavailable:no_state_root', given['availability'].get('reason_code'))

    @requirements('RT-RX-LIVE-ROOT-2')
    def test_validate_assignment_without_a_project_root_routes_with_a_reason(self):
        task = self.write('task.json', self.task())
        assigned = self.write('assignment.json', self.assignment_for(route_task(self.task(), self.policy)))
        grant = self.write('grant.json', test_bridge.PARENT_GRANT)
        routed = []
        original = cli.route_task

        def spy(*arguments, **options):
            routed.append(original(*arguments, **options))
            return routed[-1]

        with mock.patch.object(cli, 'route_task', spy):
            self.run_cli('validate-assignment', '--task', task, '--assignment', assigned, '--parent-grant', grant,
                         installed=False)
        self.assertTrue(routed)
        for made in routed:
            self.assertEqual('not_checked', made['availability']['status'])
            self.assertTrue(made['availability'].get('reason_code', '').startswith('snapshot_unavailable:'),
                            made['availability'])

    @requirements('RT-RX-LIVE-ROOT-3')
    def test_an_installed_cli_still_refuses_another_state_root(self):
        other = self.home_base / 'other-state'
        other.mkdir(mode=0o700)
        task = self.write('task.json', self.task())
        refused = []
        original = cli._require_own_roots

        def checked(args):
            try:
                return original(args)
            except OwnRootRefused as error:
                refused.append(str(error))
                raise

        with mock.patch.object(cli, '_require_own_roots', side_effect=checked):
            report = self.run_cli('learning-route', '--state-root', str(other), '--project-root', str(self.project),
                                  '--task', task, '--host-client', 'claude-host-v1', expected=2)
        self.assertFalse(report.get('ready', False), report)
        self.assertEqual(['--state-root is refused: an installed CLI uses only the default learning state'], refused)
        self.assertEqual([], sorted(path.name for path in other.iterdir()))  # nothing was written there


if __name__ == '__main__':
    unittest.main()
