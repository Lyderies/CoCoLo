"""B4t takeover tests, part owner (CI round 6 split of test_b4t_takeover, test alignment 126; classes moved verbatim; the shared journal harness stays in test_b4t_takeover)."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
import subprocess
import sys
import shutil
from contextlib import closing, ExitStack, contextmanager

import tests.support  # noqa: F401
from test_framework import requirements
from tests.test_b4t_takeover import PHASES, A, B, C, run_entry, Operations


class OwnerIntegrationTests(unittest.TestCase):
    __why_not_scenario__ = 'Read-only owner command boundaries on disposable state and mocked transport.'

    @requirements('RS-B-B4t-7', 'RS-B-B4t-8')
    def test_owner_report_reads_other_projects_after_account_handover_without_a_project_service(self):
        import cli
        from types import SimpleNamespace
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        intent = dict(predecessor_service=dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST),
                      predecessor_collector=dict(version='0.8.66', digest=A))
        document = dict(phase='account_done', runs=[], outcomes=dict(lost=0, lost_held=1, finished_kept=2,
                                                                  refused_unverifiable=0))
        account = dict(other_projects=1, other_runs=1, updates=[], recorded_at_ms=0, next_offset=None)
        args = SimpleNamespace(state_root=Path('/state'), project_root=Path('/project'), action='run', upgrade_ledger=False)
        with mock.patch.object(cli.os, 'lstat'), \
                mock.patch.object(cli, 'LearningState', return_value=SimpleNamespace(root=Path('/state'))), \
                mock.patch.object(cli, 'validate_collector_runtime'), \
                mock.patch('codex_agents.takeover_preflight.read_plan', return_value=dict(
                    needed=True, intent=intent, report=dict(records_upgrade=False))), \
                mock.patch('codex_agents.takeover_owner.run_owner_takeover', return_value=document), \
                mock.patch('codex_agents.client_runtime.account_takeover_report_client', return_value=account) as read:
            report = cli._run_update_takeover(args)
        self.assertTrue(report['ready'])
        self.assertEqual('0.8.66', report['predecessor_version'])
        self.assertEqual(account, report['other_projects'])
        read.assert_called_once()
        self.assertEqual((1, 2), (report['lost'], report['finished_kept']))

    @requirements('RS-B-B4t-8')
    def test_post_start_account_diagnostics_wait_for_the_existing_bounded_service_start(self):
        from codex_agents.client_runtime import account_takeover_report_client
        state = object()
        with mock.patch('codex_agents.client_runtime.CollectorPaths', return_value=state), \
                mock.patch('codex_agents.client_runtime._account_target',
                           return_value=('prj_' + '1' * 32, Path('/account.sock'))) as target, \
                mock.patch('codex_agents.client_runtime.enroll_clients'), \
                mock.patch('codex_agents.client_runtime.AccountClient') as client:
            client.return_value.takeover_report.return_value = dict(recorded_at_ms=None, other_projects=0,
                other_runs=0, updates=[], next_offset=None)
            self.assertIsNotNone(account_takeover_report_client('/state', Path('/project'), host_client='owner-cli-v1',
                                                               await_start=True))
        target.assert_called_once_with(state, Path('/project'), await_start=True)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_a_project_straggler_is_stopped_again_before_an_owner_open(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.history_contracts import HistoryError
        from types import SimpleNamespace
        old = dict(digest=A, directory='/old-service')
        state = SimpleNamespace(root=Path('/state'))
        ops = OwnerOperations(state, Path('/cli'), Path('/project'), mock.Mock())
        ops._service_locks = mock.Mock(return_value=[])
        ops._stop_project_straggler = mock.Mock()
        owner = mock.Mock()
        with mock.patch('codex_agents.takeover_owner.require_free_locks'), \
                mock.patch('codex_agents.project_takeover.takeover_report', return_value=dict(
                    records=[], lost=0, lost_held=0, finished_kept=0, refused_unverifiable=0)), \
                mock.patch('codex_agents.takeover_owner.TakeoverJournal'), \
                mock.patch('codex_agents.project_service.open_offline_history',
                           side_effect=[HistoryError('history_writer_busy'), owner]) as opened:
            ops.adopt_project(dict(predecessor_service=old, project_id='prj_' + '1' * 32, runs=[]))
        self.assertEqual(2, opened.call_count)
        self.assertEqual(2, ops._stop_project_straggler.call_count)
        owner.close.assert_called_once()

    @requirements('RS-B-B4t-0', 'RS-B-B4t-7', 'RS-B-B4t-8')
    def test_another_projects_unfinished_takeover_names_its_verified_B4m_command(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.takeover_journal import TakeoverJournal
        from codex_agents.learning_store import LearningState
        from codex_agents.update_takeover import TakeoverError
        from codex_agents.learning_contracts import keyed_digest
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            project, other = root / 'project', root / 'other'
            project.mkdir()
            other.mkdir()
            state = LearningState(root / 'state')
            intent = Operations(state.root).preflight()
            intent['root_digest'] = keyed_digest(state.digest_key(), str(other))
            TakeoverJournal(state.root).create(intent, [])
            before = {p.name: p.read_bytes() for p in state.root.iterdir() if p.is_file()}
            foreign = dict(project_id=intent['project_id'], canonical_root=str(other))
            with mock.patch('codex_agents.takeover_preflight._registry', side_effect=[(None, 18, {}), (foreign, 18, {})]), \
                    mock.patch('codex_agents.compatibility_message.compatibility_message',
                               return_value='verified other-project update command') as builder:
                with self.assertRaises(TakeoverError) as caught:
                    read_plan(state, project)
            self.assertEqual('takeover_unfinished', caught.exception.code)
            self.assertIn('Finish it first: verified other-project update command', str(caught.exception))
            builder.assert_called_once_with(None, project_root=other, state_name='project service')
            self.assertEqual(before, {p.name: p.read_bytes() for p in state.root.iterdir() if p.is_file()})

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_a_new_service_that_ignores_its_stop_operation_has_one_thirty_second_bound(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.update_takeover import TakeoverError
        from types import SimpleNamespace
        from codex_agents.orchestration_api import OrchestrationScope
        scope = OrchestrationScope('prj_' + '1' * 32, 'primary', A, 'supervisor', 1)
        old = dict(digest=A, directory='/old-service', stop_op=True, scope=scope.as_data(),
                   project_endpoint='/old-service/project.sock', pid=123, birth=1)
        ops = OwnerOperations(SimpleNamespace(root=Path('/state')), Path('/cli'), Path('/project'), mock.Mock())
        ops._service_locks = mock.Mock(return_value=[])
        with mock.patch('codex_agents.client_runtime.enroll_clients'), \
                mock.patch('codex_agents.history_api.HistoryClient') as client, \
                mock.patch('codex_agents.takeover_owner.require_free_locks',
                           side_effect=TakeoverError('predecessor_lock_inherited')), \
                mock.patch('codex_agents.process_tree.process_gone', return_value=False), \
                mock.patch('codex_agents.process_tree.process_record', return_value=(0, 0, 1)), \
                mock.patch('codex_agents.takeover_owner.time.monotonic', side_effect=[0, 30]), \
                mock.patch('codex_agents.takeover_owner.time.sleep') as sleep, \
                mock.patch('os.kill', side_effect=AssertionError('new service signalled')):
            with self.assertRaises(TakeoverError) as caught:
                ops.stop_service(dict(predecessor_service=old))
        self.assertEqual('service_stop_failed', caught.exception.code)
        sleep.assert_not_called()
        client.return_value._call.assert_called_once()

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_owner_command_does_not_report_an_uninspectable_ledger_as_absent(self):
        import cli
        from types import SimpleNamespace
        args = SimpleNamespace(state_root=Path('/uninspectable-state'), project_root=Path('/project'),
                               action='run', upgrade_ledger=False)
        with mock.patch.object(cli.os, 'lstat', side_effect=PermissionError('private detail')), \
                mock.patch.object(cli.os, 'open', side_effect=AssertionError('opened before T0')):
            report = cli._run_update_takeover(args)
        self.assertIs(False, report['ready'])
        self.assertEqual('ledger_unreadable', report['reason_code'])
        self.assertIn('nothing was changed', report['error'])
        self.assertNotIn('private detail', report['error'])

    @requirements('RS-B-B4t-7', 'RS-B-B4t-8')
    def test_recovery_handover_names_the_B4m_update_command(self):
        from codex_agents.project_start import recovery_line
        with mock.patch('codex_agents.compatibility_message.compatibility_message', return_value='B4m update command') as builder:
            line = recovery_line(dict(outcome='handover'), Path('/project'), 0)
        self.assertEqual('recovery: needs update hand-over: B4m update command', line)
        builder.assert_called_once_with(None, project_root=Path('/project'), state_name='project service')

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_a_newer_ledger_floor_is_refused_by_T0_before_intent_or_epoch_writes(self):
        from codex_agents.learning_store import LearningState, LearningLedger
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.update_takeover import TakeoverError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            project = root / 'project'
            project.mkdir()
            state = LearningState(root / 'state')
            ledger = LearningLedger(state)
            ledger.register_project(project)
            with ledger._account_transaction():
                ledger._connection.execute("UPDATE runtime_floor SET floor_version='999.0.0'")
            ledger.close()
            before = state.database.read_bytes()
            with self.assertRaises(TakeoverError):
                read_plan(state, project)
            self.assertEqual(before, state.database.read_bytes())
            self.assertFalse((state.root / 'update-takeover.json').exists())

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_a_ledger_straggler_is_stopped_again_before_the_owner_open(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.learning_store import LedgerError
        from types import SimpleNamespace
        state = SimpleNamespace(root=Path('/state'), lock=Path('/state/collector.lock'))
        ops = OwnerOperations(state, Path('/cli.py'), Path('/project'), mock.Mock())
        ops.stop_collector = mock.Mock()
        owner = mock.Mock()
        with mock.patch('codex_agents.takeover_preflight._lock_held', side_effect=[False, True, False]), \
                mock.patch('codex_agents.learning_store.ledger_schema_behind', return_value=None), \
                mock.patch('codex_agents.learning_store.LearningLedger', side_effect=[LedgerError('writer raced'), owner]) as opened:
            ops.adopt_ledger({})
        self.assertEqual(2, opened.call_count)
        self.assertEqual(2, ops.stop_collector.call_count)
        owner.close.assert_called_once()

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_a_straggler_past_three_rounds_never_reaches_an_owner_open(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.update_takeover import TakeoverError
        from types import SimpleNamespace
        state = SimpleNamespace(root=Path('/state'), lock=Path('/state/collector.lock'))
        ops = OwnerOperations(state, Path('/cli.py'), Path('/project'), mock.Mock())
        ops.stop_collector = mock.Mock()
        with mock.patch('codex_agents.takeover_preflight._lock_held', return_value=True), \
                mock.patch('codex_agents.learning_store.ledger_schema_behind', return_value=None), \
                mock.patch('codex_agents.learning_store.LearningLedger') as opened:
            with self.assertRaises(TakeoverError) as caught:
                ops.adopt_ledger({})
        self.assertEqual('predecessor_lock_inherited', caught.exception.code)
        self.assertEqual(3, ops.stop_collector.call_count)
        opened.assert_not_called()

    @requirements('RS-B-B4t-0', 'RS-B-B4t-1')
    def test_update_check_replaces_only_the_running_child_refusal(self):
        import cli
        busy = cli._update_check_result(True, True, unreadable=False)
        self.assertIs(False, busy['refused'])
        self.assertIsNone(busy['reason'])
        refused = cli._update_check_result(True, True, unreadable=True)
        self.assertIs(True, refused['refused'])
        self.assertEqual('ledger_unreadable', refused['reason'])
        self.assertIn('nothing was changed', refused['message'])

    @requirements('RS-B-B4t-0', 'RS-B-B4t-1', 'RS-B-B4t-10')
    def test_wizard_runs_the_takeover_after_approval_and_before_installer_apply(self):
        from setup_wizard.flow import Wizard
        wizard = Wizard.__new__(Wizard)
        wizard.project, wizard.t = '/project', mock.Mock()
        wizard.takeover_plan = dict(takeover='planned', children=1, service_version='0.8.66', records_upgrade=True)
        wizard.ledger_upgrade_approved, wizard.takeover_active = False, False
        wizard.approve_ledger_upgrade = mock.Mock(return_value=True)
        wizard.last_reply = dict(ready=True, takeover=True, phase='account_done', predecessor_version='0.8.66',
                                  successor_version='0.8.67', runs=1, lost=1, finished_kept=2, killed=False,
                                  other_projects=dict(updates=[dict(version='0.8.66', children=1,
                                                                  update_message='Q B4m update command')]))
        wizard.run_owner_cli = mock.Mock(return_value=0)
        wizard.forget_refused = mock.Mock()
        wizard.begin_step = mock.Mock()
        self.assertTrue(wizard.prepare_update_takeover())
        wizard.approve_ledger_upgrade.assert_called_once()
        self.assertTrue(wizard.takeover_active)
        self.assertIn(mock.call('ok', 'Handed over from CoCoLo 0.8.66 to 0.8.67. '
            '1 child run ended (lost, ended by the update). 2 finished results kept.'), wizard.t.list_add.call_args_list)
        self.assertIn(mock.call('warn', '1 other project still runs CoCoLo 0.8.66 and has 1 child run. '
            'It keeps running. To record it, update that project: Q B4m update command'), wizard.t.list_add.call_args_list)
        self.assertIn('--upgrade-ledger', wizard.run_owner_cli.call_args.args)
        self.assertIn('--action', wizard.run_owner_cli.call_args.args)
        source = (Path(__file__).parents[3] / 'scripts/setup_wizard/flow.py').read_text()
        tree = ast.parse(source)
        install = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'step_install')
        calls = [(node.lineno, node.func.attr) for node in ast.walk(install)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertLess(next(line for line, name in calls if name == 'update_refusal'),
                        next(line for line, name in calls if name == 'prepare_update_takeover'))
        self.assertLess(next(line for line, name in calls if name == 'prepare_update_takeover'),
                        next(line for line, name in calls if name == 'run_installer'))

    @requirements('RS-B-B4t-0')
    def test_records_upgrade_approval_keeps_the_other_projects_warning_literal(self):
        from setup_wizard.flow import Wizard
        wizard=Wizard.__new__(Wizard)
        wizard.t=mock.Mock()
        wizard.choice_needed=mock.Mock(return_value=0)
        wizard.begin_step=mock.Mock()
        self.assertTrue(wizard.approve_ledger_upgrade())
        self.assertIn(mock.call('Every project on this machine shares these records. The update hands over this project\'s service. Other projects keep running. Their results cannot be recorded until you run this setup for each of them.'),wizard.t.list_sub.call_args_list)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-10')
    def test_declined_upgrade_or_failed_takeover_does_not_start_an_install(self):
        from setup_wizard.flow import Wizard
        wizard = Wizard.__new__(Wizard)
        wizard.project, wizard.t = '/project', mock.Mock()
        wizard.takeover_plan = dict(takeover='planned', children=1, service_version='0.8.66', records_upgrade=True)
        wizard.ledger_upgrade_approved, wizard.takeover_active = False, False
        wizard.approve_ledger_upgrade = mock.Mock(return_value=False)
        wizard.run_owner_cli = mock.Mock(return_value=2)
        wizard.forget_refused, wizard.begin_step = mock.Mock(), mock.Mock()
        self.assertFalse(wizard.prepare_update_takeover())
        wizard.run_owner_cli.assert_not_called()
        wizard.approve_ledger_upgrade.return_value = True
        wizard.last_reply = dict(ready=False, takeover=True, phase='hint_withdrawn', error='Run ./setup.sh --update to finish.')
        self.assertFalse(wizard.prepare_update_takeover())
        self.assertFalse(wizard.takeover_active)

    @requirements('RS-B-B4t-9')
    def test_preflight_requires_both_catalogue_and_matching_authorized_loaded_identity(self):
        from codex_agents.takeover_preflight import checked_collector_identity
        from codex_agents.update_takeover import TakeoverError
        old = dict(version='0.8.66', digest=A)
        health = dict(runtime_version='0.8.66', runtime_digest=A)
        auth = dict(schema_version=1, **health)
        self.assertEqual(old, checked_collector_identity(health, auth, releases={A: '0.8.66'}))
        for changed, authorized, releases in (
            (dict(health, runtime_digest=C), auth, {A: '0.8.66'}),
            (health, dict(auth, runtime_digest=C), {A: '0.8.66'}),
            (health, auth, {}),
            (dict(health, runtime_version='999.0.0'), auth, {A: '999.0.0'}),
        ):
            with self.subTest(health=changed, authorized=authorized):
                with self.assertRaises(TakeoverError) as caught:
                    checked_collector_identity(changed, authorized, releases=releases)
                self.assertEqual('predecessor_unverified', caught.exception.code)

    @requirements('RS-B-B4t-3')
    def test_legacy_exit_does_not_add_another_thirty_second_wait_for_an_inherited_lock(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.update_takeover import TakeoverError
        from types import SimpleNamespace
        old = dict(version='0.8.66', digest=A, directory='/old-host', entry='/old/project_host.py', pid=42, birth=17)
        ops = OwnerOperations(SimpleNamespace(root=Path('/state')), Path('/cli.py'), Path('/project'), mock.Mock())
        pin = mock.Mock(identity=dict(birth=17))
        pin.stop.return_value = False
        with mock.patch('codex_agents.process_tree.process_gone', return_value=False), \
                mock.patch('codex_agents.takeover_owner.pin_predecessor', return_value=pin), \
                mock.patch.object(ops, '_service_locks', return_value=[]), \
                mock.patch('codex_agents.takeover_owner.require_free_locks',
                           side_effect=TakeoverError('predecessor_lock_inherited', phase='service_stopped')) as locks, \
                mock.patch('codex_agents.takeover_owner.time.sleep', side_effect=AssertionError('extra stop wait')):
            with self.assertRaises(TakeoverError) as caught:
                ops.stop_service(dict(project_id='prj_' + '1' * 32, predecessor_service=old))
        self.assertEqual('predecessor_lock_inherited', caught.exception.code)
        locks.assert_called_once()

    @requirements('RS-B-B4t-3')
    def test_linux_below_pidfd_support_uses_the_accepted_birth_recheck_fallback(self):
        import errno
        from codex_agents.takeover_process import Kernel
        kernel = Kernel()
        with mock.patch('codex_agents.takeover_process.sys.platform', 'linux'), \
                mock.patch('codex_agents.takeover_process.os.pidfd_open', side_effect=OSError(errno.ENOSYS, 'old kernel'), create=True), \
                mock.patch.object(kernel, 'identity', return_value=dict(uid=os.getuid(), birth=17, argv=['python'])):
            pin = kernel.pin(42)
            self.assertFalse(kernel.exited(pin))
            with mock.patch.object(kernel, 'identity', return_value=dict(uid=os.getuid(), birth=18, argv=['python'])):
                self.assertTrue(kernel.exited(pin))
            kernel.close(pin)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_additive_17_to_18_does_not_ask_for_records_upgrade_consent(self):
        from codex_agents.learning_store import _schema_behind
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            path = Path(folder).resolve() / 'ledger.sqlite3'
            for version in (15, 16, 17, 18):
                with closing(sqlite3.connect(path)) as db:
                    db.execute('PRAGMA user_version=' + str(version))
                self.assertEqual(15 if version == 15 else None, _schema_behind(path))

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_preflight_prior_authorization_past_the_existing_limit_is_read_only(self):
        from codex_agents.learning_store import LearningState, LearningLedger
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.update_takeover import TakeoverError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            project = root / 'project'
            project.mkdir()
            state = LearningState(root / 'state')
            ledger = LearningLedger(state)
            ledger.register_project(project)
            ledger.close()
            authorization = dict(schema_version=1, runtime_version=PACKAGE_VERSION, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
            state.collector_authorization.write_bytes(json.dumps(authorization).encode() + b' ' * 1025)
            state.collector_authorization.chmod(0o600)
            before = state.database.read_bytes(), state.collector_authorization.read_bytes()
            # The file grew between the first validation and the retained-byte
            # read: the second bounded read must independently reject it too.
            with mock.patch('codex_agents.collector_runtime._collector_runtime_authorization', return_value=authorization):
                with self.assertRaises(TakeoverError):
                    read_plan(state, project)
            self.assertEqual(before, (state.database.read_bytes(), state.collector_authorization.read_bytes()))
            self.assertFalse((state.root / 'update-takeover.json').exists())

    @requirements('RS-B-B4t-9')
    def test_historical_package_identity_is_hashed_without_loading_its_code(self):
        import hashlib
        from codex_agents.takeover_preflight import trusted_package
        from codex_agents.update_takeover import TakeoverError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            orchestration = root / '.codex/orchestration'
            (orchestration / 'codex_agents').mkdir(parents=True)
            files = {'cli.py': b'raise AssertionError("must not execute predecessor code")\n',
                     'project_host.py': b'old service entry\n',
                     'codex_agents/runtime_files.py': b'RUNTIME_FILES = ("cli.py", "project_host.py", "codex_agents/runtime_files.py")\n'}
            hasher = hashlib.sha256()
            for name, value in files.items():
                (orchestration / name).write_bytes(value)
                hasher.update(len(name.encode()).to_bytes(4, 'big') + name.encode())
                hasher.update(len(value).to_bytes(8, 'big') + value)
            digest = hasher.hexdigest()
            manifest = dict(format=1, version='0.8.66', files={'.codex/orchestration/' + name:
                            hashlib.sha256(value).hexdigest() for name, value in files.items()})
            (orchestration / 'install-manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(dict(version='0.8.66', digest=digest),
                             trusted_package(root, releases={digest: '0.8.66'}))
            (orchestration / 'project_host.py').write_bytes(b'changed service entry\n')
            with self.assertRaises(TakeoverError):
                trusted_package(root, releases={digest: '0.8.66'})

    @requirements('RS-B-B4t-0', 'RS-B-B4t-10')
    def test_owner_command_exposes_run_installed_and_complete_without_a_second_install(self):
        import cli
        parser = cli.build_parser()
        for action in ('run', 'installed', 'complete'):
            args = parser.parse_args(['learning-update-takeover', '--project-root', '/project', '--action', action])
            self.assertEqual(action, args.action)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_J6a_excludes_both_old_and_late_settled_entries_after_restart(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.takeover_journal import TakeoverJournal
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            intent = Operations(root).preflight()
            launches = root / 'projects' / intent['project_id'] / 'launches'
            launches.mkdir(parents=True)
            entries = {}
            def add(n, state):
                value = dict(run_entry(n), state=state)
                path = launches / (value['bridge_run_id'] + '.json')
                path.touch()
                entries[path] = value
            add(1, 'settled')
            add(2, 'claimed')
            journal = TakeoverJournal(root)
            document = journal.create(intent, [run_entry(2)])
            state = SimpleNamespace(root=root)
            ops = OwnerOperations(state, root / 'cli.py', root / 'project', lambda: intent)
            with mock.patch('codex_agents.launch_journal._read_entry', side_effect=lambda path: entries[path]):
                document = ops.reconcile_runs(document, late=False)
                journal.write(document)
                add(3, 'settled')  # the final admission reply arrived after J4a, then the service closed
                restarted = OwnerOperations(state, root / 'cli.py', root / 'project', lambda: intent)
                document = restarted.reconcile_runs(journal.read(), late=True)
                self.assertEqual([run_entry(2)], restarted.reconcile_runs(document, late=True)['runs'])
            self.assertEqual([run_entry(2)], document['runs'])

    @requirements('RS-B-B4t-8')
    def test_takeover_report_is_a_new_closed_read_without_changing_existing_shapes(self):
        from codex_agents.history_api import HistoryRequest, encode_history_request, decode_history_request
        from codex_agents.reply_shapes import REPLY_FIELDS
        request = HistoryRequest(1, 'history.takeover_report', 'takeover', 'prj_' + '1' * 32, A, {})
        self.assertEqual(request, decode_history_request(encode_history_request(request)))
        self.assertEqual({'records', 'lost', 'lost_held', 'finished_kept', 'refused_unverifiable'},
                         set(REPLY_FIELDS['history.takeover_report']['required']))
        self.assertEqual({'project_holds', 'recovery_pending', 'result_custody', 'result_retirement_repairs'},
                         set(REPLY_FIELDS['history.holds_report']['required']))


class RetentionA1Tests(unittest.TestCase):
    __why_not_scenario__ = 'Real owner retention transactions on disposable SQLite connections; no transport.'

    def project(self):
        from codex_agents.takeover_schema import PROJECT_SCHEMA
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(PROJECT_SCHEMA + "CREATE TABLE history_identity(project_id TEXT PRIMARY KEY);"
                        "INSERT INTO history_identity VALUES('project');"
                        "CREATE TABLE project_holds(project_id TEXT DEFAULT 'project',kind,identity,state,evidence,"
                        "PRIMARY KEY(project_id,kind,identity));"
                        "CREATE TABLE result_leases(run_handle,attempt);")
        return db

    def records(self, db, count, *, runs=False):
        for i in range(count):
            identity = format(i+1, '032x')
            db.execute("INSERT INTO takeover_record VALUES(?,?,?,?,?,'complete',?,?,?)",
                       (identity, format(i+1,'064x'), '0.8.1', format(i+2,'064x'), '0.8.2', 1, int(runs), 0))
            if runs:
                run = run_entry(i+1)
                db.execute('INSERT INTO takeover_runs VALUES(?,?,?,?)',
                           (identity, run['bridge_run_id'], run['run_handle'], run['attempt']))
        db.commit()

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_B4t_A1_takeover_tables_have_retention_and_caps(self):
        from codex_agents.project_takeover import purge_takeover
        from codex_agents.project_history import SETTLEMENT_REPLAY_WINDOW_MS
        db = self.project()
        self.records(db, 50, runs=True)
        purge_takeover(db, 1)
        self.assertEqual(50, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])
        purge_takeover(db, SETTLEMENT_REPLAY_WINDOW_MS+2)
        self.assertEqual(0, db.execute('SELECT COUNT(*) FROM takeover_runs').fetchone()[0])
        self.assertEqual(17, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])
        self.assertEqual(list(range(34,51)), [int(r[0],16) for r in db.execute('SELECT takeover_id FROM takeover_record ORDER BY rowid')])
        purge_takeover(db, SETTLEMENT_REPLAY_WINDOW_MS+3)
        self.assertEqual(17, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])

    @requirements('RS-B-B4t-9', 'RS-B-B4t-10')
    def test_retention_keeps_refused_obligations_and_rolls_back_a_failed_pass(self):
        from codex_agents.project_takeover import purge_takeover
        from codex_agents.project_history import SETTLEMENT_REPLAY_WINDOW_MS
        db = self.project()
        self.records(db, 50, runs=True)
        db.execute("INSERT INTO project_holds(kind,identity,state,evidence) "
                   "VALUES('reservation','held','releasing','refused_unverifiable')")
        db.execute('INSERT INTO takeover_obligations VALUES(?,?,?,?,?,?)',
                   ('reservation','held',format(1,'032x'),'original','a'*64,'b'*64))
        db.commit()
        def cut():
            raise RuntimeError('crash before retention commit')
        with self.assertRaises(RuntimeError):
            purge_takeover(db, SETTLEMENT_REPLAY_WINDOW_MS+2, checkpoint=cut)
        self.assertEqual(50, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])
        self.assertEqual(50, db.execute('SELECT COUNT(*) FROM takeover_runs').fetchone()[0])
        purge_takeover(db, SETTLEMENT_REPLAY_WINDOW_MS+2)
        self.assertEqual(18, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])
        self.assertEqual(1, db.execute('SELECT COUNT(*) FROM takeover_obligations').fetchone()[0])
        db.execute("DELETE FROM project_holds")
        db.commit()
        purge_takeover(db, SETTLEMENT_REPLAY_WINDOW_MS+2)
        self.assertEqual(0, db.execute('SELECT COUNT(*) FROM takeover_obligations').fetchone()[0])
        self.assertEqual(17, db.execute('SELECT COUNT(*) FROM takeover_record').fetchone()[0])

    @requirements('RS-B-B4t-5', 'RS-B-B4t-9')
    def test_lineage_retention_keeps_65_live_branches_and_18_successive_links(self):
        from codex_agents.runtime_lineage import prune_lineage, successor_custody
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        from codex_agents.takeover_schema import LEDGER_SCHEMA
        db.executescript(LEDGER_SCHEMA)
        current = 'f'*64
        releases = {format(i,'064x'): '0.8.1' for i in range(1,66)}
        for digest in releases:
            db.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)', (digest,'0.8.1',current,'0.8.67','a'*32))
        db.commit()
        self.assertEqual(0, prune_lineage(db, set(releases), current))
        self.assertEqual(65, db.execute('SELECT COUNT(*) FROM runtime_lineage').fetchone()[0])
        from codex_agents.runtime_lineage import lineage_status
        self.assertEqual('Update records: 65 links to earlier CoCoLo versions are kept because unfinished runs still need them. They clear by themselves as those runs settle. Nothing needs doing.',lineage_status(db))
        for digest in releases:
            self.assertTrue(successor_custody(db,digest,current,releases=releases,current_version='0.8.67'))
        self.assertEqual(65, prune_lineage(db,set(),current))
        digests = [format(i+100,'064x') for i in range(19)]
        for i,(old,new) in enumerate(zip(digests,digests[1:])):
            db.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)',(old,f'0.8.{i}',new,f'0.8.{i+1}','a'*32))
        db.commit()
        self.assertEqual(0, prune_lineage(db,{digests[0]},digests[-1]))
        self.assertEqual(18, db.execute('SELECT COUNT(*) FROM runtime_lineage').fetchone()[0])
        self.assertEqual(18, prune_lineage(db,set(),digests[-1]))


class NativeExecA1Tests(unittest.TestCase):
    __why_not_scenario__ = 'Real pinned subprocesses and flocks, pipes only; no sockets or account service.'

    @requirements('RS-B-B4t-3','RS-B-B4t-9')
    def test_native_exec_before_and_after_I4_reaches_only_the_same_process(self):
        # A1.7 item 7 / TL: record the accepted exec-after-I4 residual. Neither
        # a pidfd nor a kqueue attests the program that runs after the last read.
        import select
        from codex_agents.takeover_process import pin_predecessor,require_free_locks,Kernel
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        from codex_agents.update_takeover import TakeoverError
        replacement="import os,sys,signal,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));print('new '+str(os.getpid()),flush=True);time.sleep(60)"
        for before in (False,True):
            with self.subTest(exec_before_last_check=before),tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root=Path(folder).resolve()
                state=root/'state'
                state.mkdir(mode=0o700)
                project=root/'project'
                project.mkdir()
                entry=root/'project_host.py'
                lock=state/'history.lock'
                entry.write_text("import os,sys,fcntl,time\nfrom pathlib import Path\n"
                    "fd=os.open(Path(sys.argv[2])/'history.lock',os.O_RDWR|os.O_CREAT,0o600)\n"
                    "fcntl.flock(fd,fcntl.LOCK_EX);os.set_inheritable(fd,True)\n"
                    "print('old '+str(os.getpid()),flush=True)\nsys.stdin.readline()\n"
                    + 'os.execv(sys.executable,[sys.executable,"-I","-B","-S","-c",'+repr(replacement)+'])\n')
                child=subprocess.Popen([sys.executable,*PYTHON_ISOLATION_FLAGS,str(entry),'--state-root',str(state),
                    '--project-root',str(project)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                pin=None
                try:
                    self.assertTrue(select.select([child.stdout],[],[],10)[0])
                    self.assertEqual('old '+str(child.pid),child.stdout.readline().strip())
                    kernel=Kernel()
                    pin=pin_predecessor(child.pid,entry=entry,state_root=state,project_root=project,kernel=kernel)
                    with self.assertRaises(TakeoverError):
                        require_free_locks([lock])
                    def execute():
                        child.stdin.write('exec\n')
                        child.stdin.flush()
                        self.assertTrue(select.select([child.stdout],[],[],10)[0])
                        self.assertEqual('new '+str(child.pid),child.stdout.readline().strip())
                        with self.assertRaises(TakeoverError):
                            require_free_locks([lock])
                    sent=[]
                    real_send=kernel.send
                    if before:
                        execute()
                        with mock.patch.object(kernel,'send') as send:
                            with self.assertRaises(TakeoverError):
                                pin.stop()
                            send.assert_not_called()
                        pin=None
                    else:
                        def send(handle,pid,number):
                            execute()  # hook strictly after I4, before signal
                            sent.append(pid)
                            real_send(handle,pid,number)
                        with mock.patch.object(kernel,'send',side_effect=send):
                            pin.stop()
                        pin=None
                        child.wait(timeout=5)
                        self.assertEqual(0,child.returncode)
                        self.assertEqual([child.pid],sent)
                        require_free_locks([lock])
                finally:
                    if pin is not None:
                        pin.close()
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

    @requirements('RS-B-B4t-3','RS-B-B4t-9')
    def test_archived_service_exec_sites_are_beyond_explicit_child_process_boundaries(self):
        # A1.7 item 7 / TL. Import closure is deliberately NOT the oracle:
        # signature_process is imported by the service but its main is a new
        # process entry, launched by signature_execution's bootstrap.
        top=Path(__file__).parents[3]
        for revision in ('afb6f610','v0.3.4'):
            with self.subTest(revision=revision),tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root=Path(folder)
                archive=subprocess.run(['git','-C',str(top),'archive',revision,
                    'packages/codex-agents/native-package/.codex/orchestration'],capture_output=True,check=True)
                subprocess.run(['tar','-x','-C',str(root)],input=archive.stdout,check=True)
                runtime=root/'packages/codex-agents/native-package/.codex/orchestration'
                sites=set()
                for path in runtime.rglob('*.py'):
                    source=path.read_bytes()
                    # Every site this checks names one of these words in the source, so a file without them has none.
                    if not re.search(rb'signature_process|\.\s*exec|_verify',source):
                        continue
                    tree=ast.parse(source)
                    parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
                    for node in ast.walk(tree):
                        if isinstance(node,ast.ImportFrom) and (node.module or '').endswith('signature_process'):
                            self.assertFalse({alias.name for alias in node.names}&{'main','_verify'},str(path))
                        if not isinstance(node,ast.Call):
                            continue
                        if isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='os' and node.func.attr.startswith('exec'):
                            parent=node
                            while parent in parents and not isinstance(parent,ast.FunctionDef):
                                parent=parents[parent]
                            sites.add((path.name,getattr(parent,'name','<module>')))
                        if path.name=='signature_process.py' and isinstance(node.func,ast.Name) and node.func.id=='_verify':
                            parent=node
                            while parent in parents and not isinstance(parent,ast.FunctionDef):
                                parent=parents[parent]
                            self.assertEqual('main',parent.name)
                self.assertEqual({('cli.py','main'),('signature_process.py','_verify')},sites)
                execution=(runtime/'codex_agents/signature_execution.py').read_text()
                self.assertIn('from codex_agents.signature_process import main',execution)
                self.assertIn('_BOOTSTRAP',execution)
                self.assertIn('create_child(' if revision=='afb6f610' else 'subprocess.Popen(',execution)


class AccountTakeoverReadTests(unittest.TestCase):
    __why_not_scenario__ = 'Real account producer and owner connection; private diagnostic rows, no service start.'

    def setUp(self):
        from tests.test_learning_store import LearningStoreTests
        from codex_agents.account_service import AccountService, AccountOwnerViews, AccountAuthorization
        from codex_agents.account_api import account_request_digest
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        self.fixture = LearningStoreTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.token = object()
        def authorize(request, context):
            if context is self.token:
                return AccountAuthorization(self.project, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                            'host', frozenset({'account.takeover_report'}))
        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(self.service.close)

    @requirements('RS-B-B4t-8')
    def test_lineage_over_cap_notice_is_produced_by_the_real_account_handler(self):
        # Diagnostic row-shape fixture; protected-path retention/settlements
        # have separate witnesses and are not inferred from this injected set.
        with self.ledger._account_transaction():
            for i in range(65):
                self.ledger._connection.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)',
                    (format(i+1,'064x'),'0.8.1',self.ledger._fence.digest,'0.8.67','a'*32))
        before=self.ledger._connection.total_changes
        reply=self.read()
        self.assertEqual('Update records: 65 links to earlier CoCoLo versions are kept because unfinished runs still need them. They clear by themselves as those runs settle. Nothing needs doing.',reply['lineage_message'])
        self.assertEqual(before,self.ledger._connection.total_changes)

    def read(self, offset=0, runtime=None):
        from codex_agents.account_api import AccountRequest, encode_account_request, decode_account_response
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        request = AccountRequest(1, 'account.takeover_report', 'takeover-' + str(offset), self.project,
                                 COLLECTOR_RUNTIME_DIGEST if runtime is None else runtime,
                                 dict(offset=offset, predecessor_version='0.8.66'))
        return decode_account_response(self.service.handle(encode_account_request(request), self.token), request)

    def children(self, count):
        import time
        now, mono = time.time_ns() // 1_000_000, time.monotonic_ns()
        for n in range(count):
            root = Path(self.fixture.temporary.name).resolve() / ('other-' + str(n))
            root.mkdir()
            project = self.ledger.register_project(root)
            digest, bridge = f'{n+1:064x}', 'brg_' + f'{n+1:032x}'
            with self.ledger._account_transaction():
                self.ledger._connection.execute("INSERT INTO launch_claims VALUES(?,?,?,'claimed',?,?)",
                                                (project, digest, 'run-' + str(n), now, bridge))
                self.ledger._connection.execute('INSERT INTO active_run_capacity(project_id,handle_digest,run_id,'
                    'bridge_run_id,kind,provider,account_fingerprint,runtime_digest,policy_digest,claimed_at_ms,'
                    'deadline_at_ms,clock_reference_ns,execution_deadline_ns,clock_owner_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (project, digest, 'run-' + str(n), bridge, 'claimed', 'codex', '7' * 64, A, '8' * 64,
                     now, now + 3600000, mono, mono + 3600000000000, '9' * 64))

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_more_than_one_page_of_other_projects_is_read_without_releasing_any_slot(self):
        self.children(9)
        before = self.ledger._connection.total_changes
        first, second = self.read(), self.read(8)
        self.assertEqual((9, 9, 8, 8), (first['other_projects'], first['other_runs'], len(first['updates']), first['next_offset']))
        self.assertEqual((1, None), (len(second['updates']), second['next_offset']))
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(9, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0])

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_past_the_reply_byte_budget_continues_on_the_next_page_and_old_senders_stay_refused(self):
        from codex_agents.account_api import AccountError
        self.children(9)
        before = self.ledger._connection.total_changes
        with mock.patch('codex_agents.compatibility_message.compatibility_message', return_value='x' * 9000):
            first = self.read()
            self.assertEqual((1, 1), (len(first['updates']), first['next_offset']))
            self.assertEqual(1, len(self.read(1)['updates']))
        with self.assertRaises(AccountError):
            self.read(runtime=A)
        self.assertEqual(before, self.ledger._connection.total_changes)


class ProcessProofTests(unittest.TestCase):
    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_a_kernel_argument_limit_past_four_MiB_is_unproven_before_reading_arguments(self):
        import ctypes
        from codex_agents import takeover_process as process, process_tree
        queries = []
        def sysctl(name, length, output, size, *_):
            queries.append(length)
            if length == 2:
                ctypes.cast(output, ctypes.POINTER(ctypes.c_int)).contents.value = process.FD_SCAN_BYTES + 1
                return 0
            return -1
        with mock.patch.object(process_tree, '_SYSCTL', [sysctl]):
            with self.assertRaises(OSError):
                process._darwin_argv(42)
        self.assertEqual([4, 2], queries)

    __why_not_scenario__ = 'Kernel boundary injection proves identity checks precede the sole pinned signal.'

    def kernel(self):
        kernel = mock.Mock()
        kernel.pin.return_value = 91
        kernel.identity.return_value = dict(uid=os.getuid(), birth='birth-1',
            argv=['python3', '-I', '-B', '-S', '/trusted/project_host.py',
                  '--state-root', '/state', '--project-root', '/project'])
        kernel.sole_holder.return_value = True
        kernel.exited.return_value = False
        kernel.changed.return_value = False
        kernel.wait.return_value = True
        return kernel

    def pin(self, kernel):
        from codex_agents.takeover_process import pin_predecessor
        return pin_predecessor(1234, entry=Path('/trusted/project_host.py'), state_root=Path('/state'),
                               project_root=Path('/project'), kernel=kernel)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_B4t_3_signals_only_the_proven_process(self):
        from codex_agents.update_takeover import TakeoverError
        for bad in ('uid', 'entry', 'birth', 'exited'):
            with self.subTest(proof=bad):
                kernel = self.kernel()
                good = kernel.identity.return_value
                if bad == 'uid':
                    kernel.identity.return_value = {**good, 'uid': os.getuid() + 1}
                elif bad == 'entry':
                    kernel.identity.return_value = {**good, 'argv': ['python3', '/forged/project_host.py']}
                elif bad == 'birth':
                    kernel.identity.side_effect = [good, {**good, 'birth': 'birth-2'}]
                else:
                    kernel.changed.return_value = True
                with self.assertRaises(TakeoverError):
                    self.pin(kernel)
                kernel.send.assert_not_called()
                kernel.close.assert_called_once_with(91)
        kernel = self.kernel()
        pinned = self.pin(kernel)
        pinned.stop()
        import signal
        kernel.send.assert_called_once_with(91, 1234, signal.SIGTERM)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_predecessor_entry_accepts_the_actual_launcher_isolation_flags_only(self):
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        from codex_agents.update_takeover import TakeoverError
        kernel = self.kernel()
        kernel.identity.return_value['argv'] = ['python3', *PYTHON_ISOLATION_FLAGS,
            '/trusted/project_host.py', '--state-root', '/state', '--project-root', '/project']
        pinned = self.pin(kernel)
        pinned.close()
        kernel.send.assert_not_called()
        kernel = self.kernel()
        kernel.identity.return_value['argv'] = ['python3', '-X', 'presite=forged',
            '/trusted/project_host.py', '--state-root', '/state', '--project-root', '/project']
        with self.assertRaises(TakeoverError):
            self.pin(kernel)
        kernel.send.assert_not_called()

    @requirements('RS-B-B4t-3', 'RS-B-B4t-4')
    def test_B4t_3_bounded_stop_then_kill_the_same_process(self):
        import signal
        kernel = self.kernel()
        kernel.wait.side_effect = [False, True]
        pinned = self.pin(kernel)
        self.assertTrue(pinned.stop())
        self.assertEqual([mock.call(91, 1234, signal.SIGTERM), mock.call(91, 1234, signal.SIGKILL)],
                         kernel.send.call_args_list)
        self.assertEqual([mock.call(91, 30.0), mock.call(91, 5.0)], kernel.wait.call_args_list)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_inherited_lock_blocks_adoption(self):
        from codex_agents.update_takeover import TakeoverError
        from codex_agents.takeover_process import require_free_locks
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            import fcntl
            path = Path(folder) / 'history.lock'
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(TakeoverError) as caught:
                require_free_locks([path])
            self.assertEqual('predecessor_lock_inherited', caught.exception.code)
            fcntl.flock(fd, fcntl.LOCK_UN)
            require_free_locks([path])

    @requirements('RS-B-B4t-9')
    def test_linux_foreign_flock_holder_is_busy_without_descriptor_reads(self):
        from codex_agents import takeover_process as process
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            path = Path(folder) / 'history.lock'
            path.touch()
            info = path.stat()
            table = f'1: FLOCK ADVISORY WRITE 99 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n'
            with mock.patch.object(process.sys, 'platform', 'linux'), \
                    mock.patch.object(Path, 'open', mock.mock_open(read_data=table)):
                self.assertTrue(process.foreign_lock_holder(42, [path]))
                self.assertFalse(process.foreign_lock_holder(99, [path]))

    @requirements('RS-B-B4t-9')
    def test_process_arguments_past_byte_limit_are_unproven(self):
        from codex_agents import takeover_process as process
        with mock.patch.object(process.sys, 'platform', 'linux'), \
                mock.patch('codex_agents.process_tree.process_record', return_value=(1, 1, 'birth', False)), \
                mock.patch.object(process, 'Path') as path:
            path.return_value.__truediv__.return_value.stat.return_value.st_uid = os.getuid()
            stream = path.return_value.__truediv__.return_value.__truediv__.return_value.open
            stream.return_value.__enter__.return_value.read.return_value = b'x' * (process.FD_SCAN_BYTES + 1)
            with self.assertRaises(OSError):
                process.Kernel().identity(1234)

    @requirements('RS-B-B4t-3','RS-B-B4t-9')
    def test_unrelated_native_history_and_host_lock_holders_never_receive_a_signal(self):
        from codex_agents.takeover_process import require_free_locks
        from codex_agents.update_takeover import TakeoverError
        import select
        for name in ('history.lock','host.lock'):
            with self.subTest(lock=name),tempfile.TemporaryDirectory(dir='/tmp') as folder:
                path=Path(folder)/name
                code="import os,sys,fcntl;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX);print('ready',flush=True);sys.stdin.read();os.close(fd)"
                child=subprocess.Popen([sys.executable,'-I','-B','-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
                try:
                    self.assertTrue(select.select([child.stdout],[],[],10)[0])
                    self.assertEqual('ready',child.stdout.readline().strip())
                    with mock.patch('os.kill',side_effect=AssertionError('unrelated holder signalled')):
                        with self.assertRaises(TakeoverError) as caught:
                            require_free_locks([path])
                        self.assertEqual('predecessor_lock_inherited',caught.exception.code)
                    child.stdin.close()
                    child.stdin=None
                    child.communicate(timeout=5)
                    self.assertEqual(0,child.returncode)
                    require_free_locks([path])
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)


class AmendmentA1Tests(unittest.TestCase):
    __why_not_scenario__ = 'Product planning/owner operations with transport faults injected; private files only.'

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_B4t_A1_no_other_process_is_inspected(self):
        from codex_agents import takeover_process as process
        source = Path(process.__file__).read_text()
        for forbidden in ('proc_listpids', 'proc_pidfdinfo', 'PROC_UID_ONLY', "Path('/proc').iterdir"):
            self.assertFalse(forbidden in source, forbidden)
        kernel = ProcessProofTests().kernel()
        kernel.sole_holder.side_effect = AssertionError('descriptor census')
        from codex_agents.takeover_process import pin_predecessor
        pin = pin_predecessor(1234, entry=Path('/trusted/project_host.py'), state_root=Path('/state'),
                              project_root=Path('/project'), kernel=kernel)
        pin.close()
        kernel.sole_holder.assert_not_called()

    @contextmanager
    def plan_case(self, *, busy=False, proof_error=True, held=False):
        from codex_agents import takeover_preflight as preflight
        from codex_agents.learning_store import LearningState
        from codex_agents.control_api import ControlScope
        from codex_agents.collector_contracts import CollectorError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, ExitStack() as stack:
            root = Path(folder).resolve()
            project = root / 'project'
            project.mkdir()
            state = LearningState(root / 'state')
            directory = root / 'runtime'
            directory.mkdir(mode=0o700)
            scope = ControlScope(project_id='prj_' + '1'*32, primary_session_id='session', runtime_digest=A,
                                 supervisor_instance_id='instance', supervisor_generation=1)
            row = dict(project_id=scope.project_id, canonical_root=str(project))
            stack.enter_context(mock.patch.object(preflight, '_registry', return_value=(row, 17,
                                {scope.project_id: 1} if busy else {})))
            stack.enter_context(mock.patch.object(preflight, '_project_owner', return_value=A))
            stack.enter_context(mock.patch.object(preflight, 'trusted_package', return_value=dict(version='0.8.66', digest=A)))
            stack.enter_context(mock.patch.object(preflight, 'release_versions', return_value={A:'0.8.66'}))
            stack.enter_context(mock.patch('codex_agents.project_host_client._hinted_host', return_value=(scope,
                              (directory/'dispatch.sock', directory/'result.sock'), True)))
            stack.enter_context(mock.patch.object(preflight, '_peer', return_value=42))
            stack.enter_context(mock.patch.object(preflight, '_lock_held', return_value=held))
            stack.enter_context(mock.patch.object(preflight.CollectorClient, '_request', side_effect=CollectorError('unavailable')))
            pin = stack.enter_context(mock.patch('codex_agents.takeover_process.pin_predecessor'))
            if proof_error:
                from codex_agents.update_takeover import TakeoverError
                pin.side_effect = TakeoverError('predecessor_unproven')
            else:
                pin.return_value.identity = dict(birth='birth')
            # I1 directory computation is independent of a hint's supplied path.
            stack.enter_context(mock.patch('codex_agents.client_runtime.runtime_directory', return_value=directory))
            stack.enter_context(mock.patch('codex_agents.takeover_owner.OwnerOperations._service_answers',return_value=False))
            yield preflight, state, project

    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_one_peer_identity_is_captured_per_planning_pass(self):
        # Review 0.8.69 LOW: an endpoint owner changing between tree attempts must not get a second pid inspected.
        from codex_agents.update_takeover import TakeoverError
        with self.plan_case() as (preflight, state, project):
            second = project.parent / 'copy'
            second.mkdir()
            row = dict(project_id='prj_' + '1'*32, canonical_root=str(project), launch_copy=str(second), launch_digest=A)
            with mock.patch.object(preflight, '_registry', return_value=(row, 17, {})), \
                    mock.patch.object(preflight, '_peer', side_effect=[42, 43]) as peer:
                try:
                    preflight.read_plan(state, project)
                except TakeoverError:
                    pass
            self.assertEqual(1, peer.call_count)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_B4t_A1_idle_predecessor_updates_without_process_proof(self):
        # Reproduces 146955fc's unconditional idle refusal with the same EPERM result.
        from codex_agents.takeover_owner import OwnerOperations
        with self.plan_case() as (preflight, state, project):
            plan = preflight.read_plan(state, project)
            self.assertEqual('hint', plan['report']['service_stop'])
            self.assertTrue(plan['needed'])
            _old = plan['intent']['predecessor_service']
            ops = OwnerOperations(state, Path('/cli'), project, lambda: plan['intent'])
            with mock.patch('codex_agents.takeover_owner.require_free_locks'), \
                    mock.patch('codex_agents.project_host_client._hinted_host', return_value=None), \
                    mock.patch('os.kill', side_effect=AssertionError('idle signal')), \
                    mock.patch('os.killpg', side_effect=AssertionError('idle group signal')):
                ops.stop_service(plan['intent'])

    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_idle_predecessor_with_readable_identity_also_receives_no_signal(self):
        from codex_agents.takeover_owner import OwnerOperations
        with self.plan_case(proof_error=False) as (preflight,state,project):
            plan=preflight.read_plan(state,project)
            self.assertEqual('signal',plan['report']['service_stop'])
            ops=OwnerOperations(state,Path('/cli'),project,lambda:plan['intent'])
            with mock.patch('codex_agents.process_tree.process_gone',return_value=False), \
                    mock.patch('codex_agents.takeover_owner.pin_predecessor',side_effect=AssertionError('idle signal path')), \
                    mock.patch('codex_agents.takeover_owner.require_free_locks'), \
                    mock.patch('codex_agents.project_host_client._hinted_host',return_value=None):
                ops.stop_service(plan['intent'])

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_B4t_A1_busy_predecessor_without_identity_refuses_write_free(self):
        from codex_agents.update_takeover import TakeoverError
        with self.plan_case(busy=True) as (preflight, state, project):
            before = sorted((str(p.relative_to(state.root)), p.read_bytes()) for p in state.root.rglob('*') if p.is_file())
            with self.assertRaises(TakeoverError) as caught:
                preflight.read_plan(state, project)
            self.assertEqual('predecessor_unproven', caught.exception.code)
            self.assertIn('When that run ends', str(caught.exception))
            self.assertEqual(before, sorted((str(p.relative_to(state.root)), p.read_bytes()) for p in state.root.rglob('*') if p.is_file()))

    @requirements('RS-B-B4t-3')
    def test_candidate_gone_at_last_read_counts_as_exited(self):
        from codex_agents.takeover_process import PinnedPredecessor
        kernel = ProcessProofTests().kernel()
        kernel.identity.side_effect = ProcessLookupError()
        kernel.exited.return_value = True
        self.assertFalse(PinnedPredecessor(kernel, 91, 42, {}).stop())
        kernel.send.assert_not_called()

    @requirements('RS-B-B4t-3')
    def test_exit_event_between_identity_and_last_event_read_counts_as_exited(self):
        from codex_agents.takeover_process import PinnedPredecessor
        kernel=ProcessProofTests().kernel()
        kernel.exited.side_effect=[False,True]
        kernel.changed.return_value=True
        self.assertFalse(PinnedPredecessor(kernel,91,42,kernel.identity.return_value).stop())
        kernel.send.assert_not_called()

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_registration_without_owner_consent_never_enables_records_upgrade(self):
        import cli
        from types import SimpleNamespace
        args = SimpleNamespace(state_root=Path('/state'), project_root=Path('/project'),
                               upgrade_ledger=False, move_legacy_state=None)
        with mock.patch.object(cli, 'LearningState'), \
                mock.patch.object(cli, 'ledger_schema_behind', return_value=None), \
                mock.patch.object(cli, '_compatible_collector', return_value=None), \
                mock.patch.object(cli, 'CollectorMaintenance'), \
                mock.patch.object(cli, 'LearningLedger') as ledger, \
                mock.patch.object(cli, 'authorize_collector_runtime'), \
                mock.patch.object(cli, 'ensure_collector'), \
                mock.patch.object(cli, 'copy_ledger_before_upgrade') as copy:
            cli._register_project(args, None, mock.Mock(), {'copy':None})
        self.assertIs(False, ledger.call_args.kwargs['allow_schema_upgrade'])
        copy.assert_not_called()

    @requirements('RS-B-B4t-8')
    def test_account_report_page_offsets_and_page_count_are_bounded(self):
        from codex_agents import client_runtime as client
        for offsets in ([0], [-1], list(range(1, 12))):
            with self.subTest(offsets=offsets), ExitStack() as stack:
                stack.enter_context(mock.patch.object(client, 'CollectorPaths'))
                stack.enter_context(mock.patch.object(client, '_account_target', return_value=('prj_'+'1'*32, Path('/a'))))
                stack.enter_context(mock.patch.object(client, 'enroll_clients'))
                account = stack.enter_context(mock.patch.object(client, 'AccountClient')).return_value
                account.takeover_report.side_effect = [dict(updates=[], next_offset=n) for n in offsets] + [dict(updates=[], next_offset=None)]
                self.assertIsNone(client.account_takeover_report_client('/state', Path('/project'), host_client='owner-cli-v1'))
                self.assertLessEqual(account.takeover_report.call_count, 9)

    @requirements('RS-B-B4t-8', 'RS-B-B4t-10')
    def test_B4t_A1_journal_stays_bounded_on_a_busy_project(self):
        from codex_agents.takeover_journal import TakeoverJournal
        from codex_agents.takeover_owner import OwnerOperations
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            journal = TakeoverJournal(root)
            intent = Operations(root).preflight()
            intent['predecessor_service']['entry'] = '/' + 'é' * 1814
            document = journal.create(intent, [])
            entries = [('brg_' + format(i, '032x') + '.json', dict(run_entry(i), state='settled' if i < 1000 else 'running')) for i in range(1070)]
            ops = OwnerOperations(SimpleNamespace(root=root), Path('/cli'), Path('/project'), lambda:intent)
            with mock.patch.object(ops, '_launch_snapshot', return_value=entries):
                for late in (False, False, True, True):
                    document = ops.reconcile_runs(document, late=late)
                    self.assertEqual(64, len(document['runs']))
                    self.assertEqual(6, document['runs_unlisted'])
                    self.assertNotIn('reconciled_entries', document)
                    self.assertLessEqual(journal.path.stat().st_size, 16384)
            for phase in PHASES:
                document = journal.phase(document, phase)
                self.assertLessEqual(journal.path.stat().st_size, 16384)


    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_B4t_A1_held_store_lock_without_a_service(self):
        from codex_agents.update_takeover import TakeoverError
        with self.plan_case() as (preflight,state,project):
            with mock.patch('codex_agents.project_host_client._hinted_host',return_value=None), \
                    mock.patch.object(preflight,'_lock_held',side_effect=lambda path:path.name=='history.lock'), \
                    mock.patch.object(preflight.time,'monotonic',side_effect=[0,0,6]), \
                    mock.patch.object(preflight.time,'sleep') as sleep:
                with self.assertRaises(TakeoverError) as caught:
                    preflight.read_plan(state,project)
                self.assertEqual('project_store_busy',caught.exception.code)
                sleep.assert_not_called()

    @requirements('RS-B-B4t-3','RS-B-B4t-10')
    def test_withdrawn_hint_does_not_hide_an_endpoint_that_still_answers(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.update_takeover import TakeoverError
        with self.plan_case() as (preflight,state,project):
            intent=preflight.read_plan(state,project)['intent']
            ops=OwnerOperations(state,Path('/cli'),project,lambda:intent)
            ops._service_answers=mock.Mock(return_value=True)
            with mock.patch('codex_agents.takeover_owner.require_free_locks'), \
                    mock.patch('codex_agents.project_host_client._hinted_host',return_value=None), \
                    mock.patch('codex_agents.takeover_owner.time.monotonic',side_effect=[0,31]):
                with self.assertRaises(TakeoverError) as caught:
                    ops.stop_service(intent)
                self.assertEqual('service_still_finishing',caught.exception.code)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_B4t_A1_withdrawn_service_that_still_holds_work(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.update_takeover import TakeoverError
        with self.plan_case() as (preflight,state,project):
            intent=preflight.read_plan(state,project)['intent']
            ops=OwnerOperations(state,Path('/cli'),project,lambda:intent)
            with mock.patch.object(ops,'_service_answers',return_value=True), \
                    mock.patch('codex_agents.takeover_owner.require_free_locks',side_effect=TakeoverError('predecessor_lock_inherited')), \
                    mock.patch('codex_agents.takeover_owner.time.monotonic',side_effect=[0,31]), \
                    mock.patch('os.kill',side_effect=AssertionError('hint must never signal')):
                with self.assertRaises(TakeoverError) as caught:
                    ops.stop_service(intent)
                self.assertEqual('service_still_finishing',caught.exception.code)
            with mock.patch('codex_agents.takeover_owner.require_free_locks'), \
                    mock.patch('codex_agents.project_host_client._hinted_host',return_value=None):
                ops.stop_service(intent)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-10')
    def test_withdraw_hint_past_five_seconds_and_owner_lock_past_twenty_seconds(self):
        from codex_agents.takeover_owner import OwnerOperations,run_owner_takeover
        from codex_agents.update_takeover import TakeoverError
        import fcntl
        with self.plan_case() as (preflight,state,project):
            intent=preflight.read_plan(state,project)['intent']
            directory=Path(intent['predecessor_service']['directory'])
            lock=directory/'hint.lock'
            lock.touch(mode=0o600)
            fd=os.open(lock,os.O_RDWR)
            self.addCleanup(os.close,fd)
            fcntl.flock(fd,fcntl.LOCK_EX)
            ops=OwnerOperations(state,Path('/cli'),project,lambda:intent)
            with mock.patch('codex_agents.takeover_owner.time.monotonic',side_effect=[0,6]):
                with self.assertRaises(TakeoverError) as caught:
                    ops.withdraw_hint(intent)
                self.assertEqual('service_stop_failed',caught.exception.code)
            state.runtime_lock.touch(mode=0o600,exist_ok=True)
            owner_fd=os.open(state.runtime_lock,os.O_RDWR)
            self.addCleanup(os.close,owner_fd)
            fcntl.flock(owner_fd,fcntl.LOCK_EX)
            with mock.patch('codex_agents.takeover_owner.time.monotonic',side_effect=[0,21]):
                with self.assertRaises(TakeoverError) as caught:
                    run_owner_takeover(ops)
                self.assertEqual('update_busy',caught.exception.code)
            self.assertFalse((state.root/'update-takeover.json').exists())

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_B4t_A1_exec_after_last_check_is_recorded(self):
        # A1.7 item 7 / TL: this records the accepted same-process exec gap,
        # not an assertion that a pidfd attests the program it will signal.
        from codex_agents.takeover_process import PinnedPredecessor
        import signal
        kernel=ProcessProofTests().kernel()
        identity=kernel.identity.return_value
        state={'program':'old'}
        def last_read(*_):
            state['program']='new'  # the test hook at I4's final kernel read
            return False
        kernel.changed.side_effect=last_read
        sent=[]
        kernel.send.side_effect=lambda pin,pid,number:sent.append((pid,state['program'],number))
        PinnedPredecessor(kernel,91,1234,identity).stop()
        self.assertEqual([(1234,'new',signal.SIGTERM)],sent)
        kernel=ProcessProofTests().kernel()
        kernel.changed.return_value=True  # exec before I4 (NOTE_EXEC pending)
        with self.assertRaises(Exception):
            PinnedPredecessor(kernel,91,1234,kernel.identity.return_value).stop()
        kernel.send.assert_not_called()


    @requirements('RS-B-B4t-0','RS-B-B4t-9')
    def test_released_0859_owner_upgrade_preserves_project_local_enumeration(self):
        from codex_agents.learning_store import LearningLedger,LearningState,LOCAL_TABLE_COLUMNS,OTHER_TABLE_COLUMNS
        from codex_agents.collector_client import PACKAGE_VERSION
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root=Path(folder).resolve()
            old=root/'old'
            old.mkdir()
            project=root/'project'
            project.mkdir()
            packed=subprocess.run(['git','-C',str(Path(__file__).parents[3]),'archive','227b0c5e',
                                   'packages/codex-agents/native-package'],capture_output=True)
            if packed.returncode:
                self.skipTest('Released 0.8.59 object unavailable for this offline fixture; primary must run E2a_1 with its released artifact')
            subprocess.run(['tar','-x','-C',str(old)],input=packed.stdout,check=True)
            runtime=old/'packages/codex-agents/native-package/.codex/orchestration'
            state=root/'state'
            code='import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from codex_agents.learning_store import LearningLedger,LearningState;owner=LearningLedger(LearningState(Path(sys.argv[2])));owner.register_project(Path(sys.argv[3]));owner.close()'
            made=subprocess.run([sys.executable,'-B','-c',code,str(runtime),str(state),str(project)],capture_output=True,text=True,timeout=20)
            self.assertEqual(0,made.returncode,made.stderr)
            with closing(LearningLedger(LearningState(state), allow_schema_upgrade=True)) as owner:  # 352: 17 -> 18 is consented
                self.assertEqual(18,owner._connection.execute('PRAGMA user_version').fetchone()[0])
                self.assertEqual({'project_wake','project_wake_instances','project_starts'},set(LOCAL_TABLE_COLUMNS))
                for table in LOCAL_TABLE_COLUMNS:
                    self.assertIn('project_id',[r[1] for r in owner._connection.execute('PRAGMA table_info('+table+')')])
                self.assertEqual(('singleton','floor_version'),OTHER_TABLE_COLUMNS['runtime_floor'])
                self.assertEqual([(1,PACKAGE_VERSION)],[tuple(r) for r in owner._connection.execute('SELECT * FROM runtime_floor')])


    @requirements('RS-B-B4t-8','RS-B-B4t-10')
    def test_maximum_path_bytes_and_all_later_fields_fit_the_journal(self):
        import base64
        from codex_agents.takeover_journal import TakeoverJournal,MAX_INTEGER,PHASES,JournalError
        for raw in (b'/' + ('é'*2047).encode(), b'/' + ('😀'*1023).encode()+b'xx',
                    b'/' + b'\x01'*4094, b'/' + b'\xff'*4094):
            with self.subTest(path_bytes=len(raw)), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root=Path(folder).resolve()
                journal=TakeoverJournal(root)
                intent=Operations(root).preflight()
                intent['prior_authorization']=b'x'*1024
                intent['predecessor_service'].update(entry=os.fsdecode(raw),directory=str(root),
                    project_endpoint=str(root/'p.sock'),pid=MAX_INTEGER,birth=str(MAX_INTEGER)+'.999999',stop_op=False,service_stop='signal',
                    scope=dict(project_id=intent['project_id'],runtime_digest=A,primary_session_id='a'*160,
                               supervisor_instance_id='b'*160,supervisor_generation=MAX_INTEGER))
                runs=[dict(run_entry(i),attempt=MAX_INTEGER) for i in range(100)]
                document=journal.create(intent,runs)
                self.assertGreaterEqual(len(document['runs']),54)
                self.assertLessEqual(len(document['runs']),64)
                self.assertEqual(100,len(document['runs'])+document['runs_unlisted'])
                self.assertEqual(raw,base64.b64decode(document['predecessor_service']['entry_b64']))
                document.update(stopped_children=100,outcomes={k:MAX_INTEGER for k in ('lost','lost_held','finished_kept','refused_unverifiable')})
                for phase in PHASES:
                    document=journal.phase(document,phase)
                    self.assertLessEqual(journal.path.stat().st_size,16384)
                self.assertEqual(raw,base64.b64decode(journal.read()['predecessor_service']['entry_b64']))
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root=Path(folder).resolve()
            intent=Operations(root).preflight()
            intent['predecessor_service']['entry']='/'+'x'*4095
            with self.assertRaises(JournalError):
                TakeoverJournal(root).create(intent,[])
            self.assertFalse((root/'update-takeover.json').exists())


    @requirements('RS-B-B4t-9','RS-B-B4t-10')
    def test_unbounded_identity_fields_refuse_before_journal_creation(self):
        from codex_agents.takeover_journal import TakeoverJournal,JournalError
        for change in (dict(unknown='x'),dict(pid=2**63),dict(birth='x'*33),dict(birth='\x01'*32),dict(version='١.١.١'),dict(service_generation=2**63)):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root=Path(folder).resolve()
                intent=Operations(root).preflight()
                intent['predecessor_service'].update(change)
                with self.assertRaises(JournalError):
                    TakeoverJournal(root).create(intent,[])
                self.assertFalse((root/'update-takeover.json').exists())


    @requirements('RS-B-B4t-9','RS-B-B4t-10')
    def test_later_stop_generation_and_stopped_count_have_fixed_integer_widths(self):
        from codex_agents.takeover_journal import TakeoverJournal,MAX_INTEGER,JournalError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root=Path(folder).resolve()
            journal=TakeoverJournal(root)
            intent=Operations(root).preflight()
            intent['predecessor_service']['stop_op']=True
            document=journal.create(intent,[run_entry(i) for i in range(64)])
            with self.subTest(field='future generation'):
                self.assertEqual(MAX_INTEGER,json.loads(journal.widest(document))['predecessor_service']['service_generation'])
            with self.subTest(field='stopped count'):
                document.update(runs_unlisted=MAX_INTEGER,stopped_children=MAX_INTEGER+1)
                with self.assertRaises(JournalError):
                    journal.write(document)
