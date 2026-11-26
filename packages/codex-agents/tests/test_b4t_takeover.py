"""Accepted B4t Draft 2 tests, written on ce81bd96 before implementation.

Boundary tests use a deterministic operations port and real durable journal files.
Store/kernel/archive tests belong below or in the explicitly named integration modules;
the port tests alone are not evidence of a real service hand-over.
"""
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

PHASES = ('intent', 'quiesced', 'collector_stopping', 'collector_stopped', 'runs_reconciled',
          'hint_withdrawn', 'service_stopped', 'late_runs_reconciled', 'store_adopting',
          'store_adopted', 'ledger_migrating', 'ledger_migrated', 'authorized', 'account_done',
          'installed', 'complete')
A, B, C = ('a' * 64, 'b' * 64, 'c' * 64)


def run_entry(n):
    return dict(run_handle='hrn_' + format(n, '048x'), attempt=1, bridge_run_id='brg_' + format(n, '032x'))


class Cut(BaseException):
    pass


class Operations:
    """Observable external effects, each replayed with its original identity."""
    def __init__(self, root):
        self.root = root
        self.events = []
        self.refusal = None
        self.runs = [run_entry(1)]
        self.original = b'{"runtime_version":"0.8.66","runtime_digest":"' + A.encode() + b'"}\n'
        self.authorization = self.original
        self.collector = A
        self.service = A
        self.effects = set()
        self.fail = None

    def preflight(self):
        if self.refusal:
            from codex_agents.update_takeover import TakeoverError
            raise TakeoverError(self.refusal)
        return dict(project_id='prj_' + '1' * 32, root_digest='d' * 64,
                    predecessor_collector=dict(version='0.8.66', digest=A),
                    predecessor_service=dict(version='0.8.66', digest=A, entry='old/project_host.py'),
                    successor=dict(version='0.8.67', digest=B),
                    prior_authorization=self.original)

    def snapshot_runs(self, *, include_settled=False):
        self.events.append(('snapshot', include_settled))
        return copy.deepcopy(self.runs)

    def act(self, name, journal):
        # Every destructive action must have durable intent first.
        from codex_agents.takeover_journal import TakeoverJournal
        disk = TakeoverJournal(self.root).read()
        assert disk['takeover_id'] == journal['takeover_id']
        self.events.append(name)
        if self.fail == name:
            from codex_agents.update_takeover import TakeoverError
            raise TakeoverError(name + '_failed')
        self.effects.add(name)

    def quiesce(self, journal):
        self.act('quiesce', journal)
        self.authorization = b'quiesced-successor'

    def stop_collector(self, journal):
        self.act('collector_stop', journal)
        self.collector = None

    def withdraw_hint(self, journal):
        self.act('withdraw_hint', journal)

    def stop_service(self, journal):
        self.act('service_stop', journal)
        self.service = None

    def adopt_project(self, journal):
        assert self.service is None and self.collector is None
        self.act('adopt_project', journal)
        self.adopted_runs = copy.deepcopy(journal['runs'])

    def adopt_ledger(self, journal):
        assert self.collector is None
        self.act('adopt_ledger', journal)

    def authorize(self, journal):
        self.act('authorize', journal)
        self.authorization = b'successor'

    def start_collector(self, journal):
        self.act('start_collector', journal)
        self.collector = B

    def finish(self, journal):
        self.act('finish', journal)
        self.service = B

    def undo(self, journal):
        self.events.append('undo')
        self.authorization = self.original


class JournalTests(unittest.TestCase):
    __why_not_scenario__ = 'Durable intent, crash cuts and operation ordering on private disposable files.'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b4t-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ops = Operations(self.root)

    def takeover(self, checkpoint=None):
        from codex_agents.update_takeover import UpdateTakeover
        return UpdateTakeover(self.root, self.ops, checkpoint=checkpoint)

    def journal(self):
        from codex_agents.takeover_journal import TakeoverJournal
        return TakeoverJournal(self.root)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-1')
    def test_B4t_0_update_with_a_running_child_goes_ahead(self):
        with mock.patch('os.kill', side_effect=AssertionError('collector signal')), \
                mock.patch('os.killpg', side_effect=AssertionError('collector group signal')):
            result = self.takeover().run()
        self.assertEqual('account_done', result['phase'])
        self.assertEqual(B, self.ops.collector)
        self.assertIn('service_stop', self.ops.events)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_B4t_0_every_write_free_refusal_still_writes_nothing(self):
        from codex_agents.update_takeover import TakeoverError
        for reason in ('ledger_unreadable', 'update_check_unavailable', 'managed_files_uncommitted',
                       'predecessor_unverified', 'predecessor_unproven', 'takeover_unfinished', 'project_store_busy', 'update_busy', 'collector_unresponsive',
                       'records_upgrade_declined', 'install_plan_failed'):
            with self.subTest(reason=reason):
                self.ops.refusal = reason
                with self.assertRaises(TakeoverError) as caught:
                    self.takeover().run()
                self.assertEqual(reason, caught.exception.code)
                self.assertEqual([], list(self.root.iterdir()))
                self.assertEqual([], self.ops.events)
                self.assertEqual((A, A, self.ops.original),
                                 (self.ops.collector, self.ops.service, self.ops.authorization))

    @requirements('RS-B-B4t-10')
    def test_B4t_10_resumable_failures_say_what_changed(self):
        from codex_agents.update_takeover import TakeoverError
        self.ops.fail = 'collector_stop'
        with self.assertRaises(TakeoverError) as caught:
            self.takeover().run()
        self.assertTrue(caught.exception.undone)
        self.assertIn('undone', str(caught.exception))
        self.assertNotIn('nothing was changed', str(caught.exception))
        self.assertEqual(self.ops.original, self.ops.authorization)
        self.assertIsNone(self.journal().read())
        self.ops.fail = None
        self.takeover().run()

    @requirements('RS-B-B4t-8', 'RS-B-B4t-10')
    def test_a_post_stop_failure_uses_the_designs_lost_count_and_rerun_text(self):
        from codex_agents.update_takeover import TakeoverError
        self.assertEqual("The update stopped partway (service_stop_failed): this project's old service was told to stop, "
            "and 1 child run ended as lost. Run ./setup.sh --update to finish.",
            str(TakeoverError('service_stop_failed', phase='hint_withdrawn', lost=1)))

    @requirements('RS-B-B4t-9', 'RS-B-B4t-10')
    def test_native_owner_failure_keeps_the_phase_and_never_exposes_raw_error_text(self):
        from codex_agents.update_takeover import TakeoverError
        original = self.ops.adopt_project
        self.ops.adopt_project = mock.Mock(side_effect=RuntimeError('private owner detail'))
        with self.assertRaises(TakeoverError) as caught:
            self.takeover().run()
        self.assertEqual(('store_adoption_failed', 'store_adopting'), (caught.exception.code, caught.exception.phase))
        self.assertNotIn('private owner detail', str(caught.exception))
        self.assertNotIn('nothing was changed', str(caught.exception))
        self.assertEqual('store_adopting', self.journal().read()['phase'])
        self.ops.adopt_project = original
        self.assertEqual('account_done', self.takeover().run()['phase'])

    @requirements('RS-B-B4t-9', 'RS-B-B4t-10')
    def test_a_database_failure_is_redacted_and_keeps_its_resumable_phase(self):
        from codex_agents.update_takeover import TakeoverError
        self.ops.adopt_project = mock.Mock(side_effect=sqlite3.OperationalError('private database detail'))
        with self.assertRaises(TakeoverError) as caught:
            self.takeover().run()
        self.assertEqual(('store_adoption_failed', 'store_adopting'), (caught.exception.code, caught.exception.phase))
        self.assertNotIn('private database detail', str(caught.exception))
        self.assertEqual('store_adopting', self.journal().read()['phase'])

    @requirements('RS-B-B4t-10')
    def test_a_marker_write_failure_reports_the_successful_undo_and_keeps_the_prior_bytes(self):
        from codex_agents.update_takeover import TakeoverError
        self.ops.quiesce = mock.Mock(side_effect=OSError('private write detail'))
        with self.assertRaises(TakeoverError) as caught:
            self.takeover().run()
        self.assertTrue(caught.exception.undone)
        self.assertIn('undone', str(caught.exception))
        self.assertNotIn('private write detail', str(caught.exception))
        self.assertNotIn('nothing was changed', str(caught.exception))
        self.assertIsNone(self.journal().read())
        self.assertEqual(self.ops.original, self.ops.authorization)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-10')
    def test_B4t_0_no_half_installed_file_at_any_step(self):
        managed = self.root / 'managed'
        managed.write_bytes(b'old complete file')
        def check(phase, journal):
            self.assertEqual(b'old complete file', managed.read_bytes())
            self.assertEqual(phase, self.journal().read()['phase'])
            if PHASES.index(phase) >= PHASES.index('store_adopting'):
                self.assertIsNone(self.ops.service)
        self.takeover(check).run()
        self.assertNotIn('install', self.ops.effects)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-10')
    def test_published_phase_names_follow_exit_authorization_and_the_completed_run_reads(self):
        wrong = []
        def check(phase, document):
            if phase == 'collector_stopped':
                self.ops.runs.append(run_entry(2))
            expected = {
                'quiesced': self.ops.authorization == b'quiesced-successor',
                'collector_stopped': self.ops.collector is None,
                'runs_reconciled': run_entry(2) in document['runs'],
                'service_stopped': self.ops.service is None,
                'authorized': self.ops.authorization == b'successor',
                'account_done': self.ops.collector == B,
                'complete': self.ops.service == B,
            }
            if phase in expected and not expected[phase]:
                wrong.append(phase)
        command = self.takeover(check)
        command.run()
        command.installed()
        command.complete()
        self.assertEqual([], wrong)

    @requirements('RS-B-B4t-10')
    def test_B4t_10_crash_at_each_phase_then_resume(self):
        for target in PHASES[:14]:
            with self.subTest(phase=target), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root = Path(folder).resolve()
                ops = Operations(root)
                from codex_agents.update_takeover import UpdateTakeover
                def cut(phase, doc):
                    if phase == target:
                        raise Cut()
                with self.assertRaises(Cut):
                    UpdateTakeover(root, ops, checkpoint=cut).run()
                resumed = UpdateTakeover(root, ops).run()
                self.assertEqual('account_done', resumed['phase'])
                self.assertEqual(B, ops.collector)
                self.assertEqual({'quiesce', 'collector_stop', 'withdraw_hint', 'service_stop',
                                  'adopt_project', 'adopt_ledger', 'authorize', 'start_collector'}, ops.effects)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-10')
    def test_installed_and_complete_phase_cuts_resume_without_reinstalling_or_repeating_a_stop(self):
        from codex_agents.update_takeover import UpdateTakeover
        for phase in ('installed', 'complete'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root = Path(folder).resolve()
                ops = Operations(root)
                command = UpdateTakeover(root, ops)
                command.run()
                stops = ops.events.count('service_stop'), ops.events.count('collector_stop')
                managed = root / 'managed'
                managed.write_bytes(b'complete new file')  # external installer committed its own journal
                def cut(at, document):
                    if at == phase:
                        raise Cut()
                interrupted = UpdateTakeover(root, ops, checkpoint=cut)
                if phase == 'installed':
                    with self.assertRaises(Cut):
                        interrupted.installed()
                else:
                    command.installed()
                    with self.assertRaises(Cut):
                        interrupted.complete()
                resumed = UpdateTakeover(root, ops)
                resumed.run()
                resumed.complete()
                resumed.complete()
                self.assertIsNone(resumed.journal.read())
                self.assertEqual(B, ops.service)
                self.assertEqual(b'complete new file', managed.read_bytes())
                self.assertEqual(stops, (ops.events.count('service_stop'), ops.events.count('collector_stop')))

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_B4t_security_takeover_has_no_writer_of_its_own(self):
        from codex_agents import update_takeover,takeover_owner,takeover_process,takeover_journal,takeover_preflight
        for module in (update_takeover,takeover_owner,takeover_process,takeover_journal,takeover_preflight):
            source=Path(module.__file__).read_text()
            tree=ast.parse(source)
            imports=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
            imports += [alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names]
            if module is takeover_preflight:
                connects=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and getattr(node.func,'attr','')=='connect']
                sql=[node for node in connects if isinstance(node.func.value,ast.Name) and node.func.value.id=='sqlite3']
                self.assertEqual(2,len(sql))
                for node in sql:
                    self.assertIn('?mode=ro',ast.get_source_segment(source,node))
                self.assertEqual(2,source.count("PRAGMA query_only=ON"))
            else:
                self.assertFalse(any('sqlite' in name for name in imports),module.__name__)
            for node in ast.walk(tree):
                if isinstance(node,ast.Constant) and isinstance(node.value,str):
                    self.assertNotRegex(node.value.upper(),r'\b(VACUUM|INSERT INTO|UPDATE .* SET|JOURNAL_MODE)\b')

    @requirements('RS-B-B4t-10')
    def test_B4t_10_intent_is_journaled_before_any_stop(self):
        self.takeover().run()
        self.assertEqual(('snapshot', False), self.ops.events[0])
        self.assertLess(self.ops.events.index('quiesce'), self.ops.events.index('collector_stop'))
        self.assertLess(self.ops.events.index('collector_stop'), self.ops.events.index('withdraw_hint'))
        self.assertLess(self.ops.events.index('withdraw_hint'), self.ops.events.index('service_stop'))
        info = (self.root / 'update-takeover.json').stat()
        self.assertEqual(0o600, info.st_mode & 0o777)
        self.assertLessEqual(info.st_size, 16 * 1024)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8', 'RS-B-B4t-10')
    def test_B4t_4_runs_admitted_after_the_intent_are_ended_by_update(self):
        def admission(phase, journal):
            if phase == 'intent':
                self.ops.runs.append(run_entry(2))
            if phase == 'quiesced':
                self.ops.runs.append(run_entry(3))
            if phase == 'runs_reconciled':
                self.ops.runs.append(run_entry(4))
        self.takeover(admission).run()
        self.assertEqual([run_entry(i) for i in range(1, 5)], self.ops.adopted_runs)
        self.assertEqual(3, self.ops.events.count(('snapshot', False)))
        self.assertEqual(0, self.ops.events.count(('snapshot', True)))
        self.takeover().run()
        self.assertEqual([run_entry(i) for i in range(1, 5)], self.journal().read()['runs'])

    @requirements('RS-B-B4t-9', 'RS-B-B4t-10')
    def test_journal_rejects_symlink_oversize_duplicate_keys_and_wrong_project(self):
        from codex_agents.takeover_journal import JournalError
        path = self.root / 'update-takeover.json'
        path.symlink_to(self.root / 'victim')
        with self.assertRaises(JournalError):
            self.journal().read()
        path.unlink()
        for raw in (b'x' * (16 * 1024 + 1), b'{"phase":"intent","phase":"complete"}'):
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.assertRaises(JournalError):
                self.journal().read()
        path.unlink()
        self.takeover().run()
        self.ops.preflight = lambda: {**Operations.preflight(self.ops), 'project_id': 'prj_' + '2' * 32}
        before = path.read_bytes()
        from codex_agents.update_takeover import TakeoverError
        with self.assertRaises(TakeoverError) as caught:
            self.takeover().run()
        self.assertEqual('takeover_unfinished', caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    @requirements('RS-B-B4t-10')
    def test_journal_past_the_run_and_byte_limits_changes_nothing(self):
        self.ops.runs = [run_entry(i) for i in range(1000)]
        result = self.takeover().run()
        self.assertEqual(64, len(result['runs']))
        self.assertEqual(936, result['runs_unlisted'])
        self.assertLessEqual(self.journal().path.stat().st_size, 16384)
        self.assertEqual('account_done', result['phase'])

    @requirements('RS-B-B4t-9')
    def test_prior_authorization_past_the_byte_limit_changes_nothing(self):
        from codex_agents.takeover_journal import JournalError
        self.ops.preflight = lambda: {**Operations.preflight(self.ops), 'prior_authorization': b'x' * 1025}
        with self.assertRaises(JournalError):
            self.takeover().run()
        self.assertEqual([], list(self.root.iterdir()))


class LineageTests(unittest.TestCase):
    __why_not_scenario__ = 'Real SQLite lineage reads; invalid paths must perform zero writes.'

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE runtime_lineage(predecessor_digest TEXT PRIMARY KEY, predecessor_version TEXT,'
                        'successor_digest TEXT,successor_version TEXT,epoch INTEGER,takeover_id TEXT,recorded_at_ms INTEGER)')
        self.releases = {A: '0.8.65', B: '0.8.66', C: '0.8.67'}

    def edge(self, old, new):
        self.db.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)',
                        (old, self.releases[old], new, self.releases[new], '1' * 32))

    def custody(self, old=A, new=C):
        from codex_agents.runtime_lineage import successor_custody
        return successor_custody(self.db, old, new, releases=self.releases, current_version='0.8.67')

    @requirements('RS-B-B4t-5', 'RS-B-B4t-9')
    def test_B4t_5_successor_custody_is_the_only_older_runtime_path(self):
        self.assertTrue(self.custody(C))
        self.assertFalse(self.custody())
        self.edge(A, B)
        self.assertFalse(self.custody())
        self.edge(B, C)
        before = self.db.total_changes
        self.assertTrue(self.custody())
        self.assertEqual(before, self.db.total_changes)
        self.releases.pop(B)
        self.assertFalse(self.custody())

    @requirements('RS-B-B4t-9')
    def test_B4t_9_lineage_refuses_unknown_mismatched_or_newer_digests(self):
        self.edge(A, B)
        self.edge(B, C)
        for replacement in ('0.8.67', '0.8.68', '0.8.64'):
            with self.subTest(version=replacement):
                self.releases[B] = replacement
                self.assertFalse(self.custody())
        self.assertFalse(self.custody('z' * 64))

    @requirements('RS-B-B4t-5', 'RS-B-B4t-10')
    def test_B4t_5_chained_takeover_A_B_C(self):
        self.edge(A, B)
        self.edge(B, C)
        original = [tuple(row) for row in self.db.execute('SELECT * FROM runtime_lineage')]
        for _ in range(3):
            self.assertTrue(self.custody())
        self.assertEqual(original, [tuple(row) for row in self.db.execute('SELECT * FROM runtime_lineage')])
        self.db.execute('DELETE FROM runtime_lineage WHERE predecessor_digest=?', (B,))
        self.assertFalse(self.custody())

    @requirements('RS-B-B4t-9')
    def test_lineage_cycles_and_past_sixteen_links_are_refused_without_writes(self):
        self.edge(A, B)
        self.db.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)',
                        (B, '0.8.66', A, '0.8.65', '1' * 32))
        self.assertFalse(self.custody())
        self.db.execute('DELETE FROM runtime_lineage')
        digests = [format(i + 1, '064x') for i in range(18)]
        self.releases = {digest: f'0.8.{i}' for i, digest in enumerate(digests)}
        self.releases[digests[-1]] = '0.8.67'
        for old, new in zip(digests, digests[1:]):
            self.edge(old, new)
        before = self.db.total_changes
        self.assertFalse(self.custody(digests[0], digests[-1]))
        self.assertTrue(self.custody(digests[1], digests[-1]))  # exactly 16 links
        self.assertEqual(before, self.db.total_changes)

    @requirements('RS-B-B4t-9')
    def test_release_catalogue_past_the_byte_limit_is_not_trusted(self):
        from codex_agents.runtime_lineage import release_versions, MAX_RELEASE_BYTES
        with mock.patch.object(Path, 'open') as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'x' * (MAX_RELEASE_BYTES + 1)
            self.assertEqual({}, release_versions())


class StartBarrierTests(unittest.TestCase):
    __why_not_scenario__ = 'Real authorization marker read by the existing shared start gate.'

    @requirements('RS-B-B4t-3', 'RS-B-B4t-7')
    def test_B4t_3_direct_learning_serve_is_refused_at_every_phase(self):
        from codex_agents.collector_runtime import require_authorized_collector_runtime
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.collector_contracts import CollectorAuthorizationError
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            root.chmod(0o700)
            paths = CollectorPaths(root)
            for phase in PHASES[1:12]:
                paths.collector_authorization.write_text(json.dumps(dict(schema_version=1,
                    runtime_version=PACKAGE_VERSION, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                    quiesced_for_upgrade=True)))
                paths.collector_authorization.chmod(0o600)
                with self.subTest(phase=phase), self.assertRaises(CollectorAuthorizationError) as caught:
                    require_authorized_collector_runtime(paths)
                self.assertEqual('runtime_authorization_quiesced', caught.exception.code)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_login_agent_restart_is_refused_at_every_phase(self):
        from codex_agents import update_takeover
        # Login launchers share the direct start gate. A successful shutdown is
        # an ordinary exit; restart-on-failure stays configured on both systems.
        root = Path(__file__).parents[3]
        source = (root / 'scripts/setup_wizard/login_start.py').read_text()
        self.assertIn('reset-failed', source)
        self.assertIn('runtime_authorization_quiesced', update_takeover.START_REFUSALS)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_ensure_collector_project_host_and_e2c_are_refused(self):
        import inspect
        from codex_agents import collector_runtime, project_start
        from tests.support import ORCHESTRATION_ROOT
        for source in (inspect.getsource(collector_runtime.ensure_collector),
                       inspect.getsource(collector_runtime._start_collector_locked),
                       (ORCHESTRATION_ROOT / 'project_host.py').read_text(),
                       inspect.getsource(project_start)):
            self.assertIn('require_authorized_collector_runtime', source)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_a_straggler_that_passed_its_check_is_caught(self):
        from codex_agents.update_takeover import MAX_STRAGGLER_STOPS, stop_until_excluded, TakeoverError
        attempts = []
        def stop():
            attempts.append(1)
        def held():
            return len(attempts) < 2
        stop_until_excluded(stop, held)
        self.assertEqual(2, len(attempts))
        attempts.clear()
        with self.assertRaises(TakeoverError):
            stop_until_excluded(stop, lambda: True)
        self.assertEqual(MAX_STRAGGLER_STOPS, len(attempts))


class NativeProcessProofTests(unittest.TestCase):
    __why_not_scenario__ = 'Disposable native process, real kernel birth/argv/sole-holder proof and pinned signal.'

    @requirements('RS-B-B4t-3')
    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'supported native process kernels')
    def test_B4t_A1_native_candidate_pin_reads_no_other_process_and_stops_only_it(self):
        import select
        from codex_agents.takeover_process import pin_predecessor, require_free_locks
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        with tempfile.TemporaryDirectory(prefix='b4t-native-', dir='/tmp') as folder:
            root = Path(folder).resolve()
            state, project = root / 'state', root / 'project'
            state.mkdir(mode=0o700)
            project.mkdir()
            lock = state / 'history.lock'
            entry = root / 'project_host.py'
            entry.write_text('''import fcntl,os,signal,sys,time
from pathlib import Path
state=Path(sys.argv[2])
fd=os.open(state/'history.lock',os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
stopped=[]
signal.signal(signal.SIGTERM,lambda *_:stopped.append(True))
print('ready',flush=True)
while not stopped: time.sleep(.01)
os.close(fd)
''')
            child = subprocess.Popen([sys.executable, *PYTHON_ISOLATION_FLAGS, str(entry), '--state-root', str(state),
                '--project-root', str(project)], cwd='/', text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pin = None
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual('ready', child.stdout.readline().strip())
                pin = pin_predecessor(child.pid, entry=entry, state_root=state, project_root=project)
                self.assertFalse(pin.stop())
                pin = None
                child.wait(timeout=5)
                self.assertEqual(0, child.returncode)
                require_free_locks([lock])
            finally:
                if pin is not None:
                    pin.close()
                if child.poll() is None:
                    child.kill()  # exactly this disposable fixture process
                child.communicate(timeout=5)


class MigrationCutsTests(unittest.TestCase):
    __why_not_scenario__ = 'Real migration subprocesses and original before-copy recovery, named modules only.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-migration-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        top = Path(__file__).parents[3]
        packed = subprocess.run(['git', '-C', str(top), 'archive', 'afb6f610', 'packages/codex-agents', 'scripts'],
                                capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(cls.archive / 'scripts')
        done = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('b4t_fixture.py')),
                               str(cls.archive), str(cls.source), 'migrations'], env=env,
                               capture_output=True, text=True, timeout=45)
        if done.returncode:
            raise AssertionError(done.stdout + done.stderr)

    def clone(self, kind):
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / kind, root, dirs_exist_ok=True)
        return root

    def database(self, root, kind):
        return root / ('state/learning.sqlite3' if kind == 'account' else 'history/history.sqlite3')

    def snapshot(self, root, kind):
        with closing(sqlite3.connect(self.database(root, kind))) as db:
            return {name: sorted([tuple(row) for row in db.execute('SELECT * FROM ' + name)], key=repr)
                    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if name != 'runtime_takeover'}

    def kill(self, root, kind, *, old):
        import select
        import signal
        package = (self.archive / 'packages/codex-agents') if old else Path(__file__).parents[1]
        scripts = self.archive / 'scripts' if old else Path(__file__).parents[3] / 'scripts'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = os.pathsep.join((str(package), str(package / 'native-package/.codex/orchestration'), str(scripts)))
        target = 'run_slot' if kind == 'account' else 'lease'
        code = 'from tests.test_rsf_s3_resumable import worker; import sys; worker(sys.argv[1],sys.argv[2],"after",sys.argv[3],64)'
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(root), kind, target],
                                 env=env, cwd='/', text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 25)[0], 'migration did not reach its real commit')
            line = child.stdout.readline()
            self.assertTrue(line, child.stderr.read() if child.poll() is not None else 'migration ended without a boundary')
            message = json.loads(line)
            self.assertEqual('after', message['boundary'])
            with closing(sqlite3.connect(self.database(root, kind))) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual((17 if kind == 'account' else 22) if old else (18 if kind == 'account' else 23), version)
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(-signal.SIGKILL, child.returncode)
            return message
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def reopen(self, root, kind):
        from tests.test_rsf_s3_resumable import open_owner, NOW
        from codex_agents.learning_store import LearningLedger
        with mock.patch.object(LearningLedger, '_clock_ms', return_value=NOW), \
                mock.patch.object(LearningLedger, '_capacity_clock_reference', return_value=10):
            open_owner(root, kind).close()

    @requirements('RS-B-B4t-10')
    def test_B4t_10_internal_T5_crash_cuts(self):
        for kind in ('account', 'project'):
            with self.subTest(kind=kind):
                root = self.clone(kind)
                self.kill(root, kind, old=True)
                reference = Path(tempfile.mkdtemp(dir=self.root))
                shutil.copytree(root, reference, dirs_exist_ok=True)
                self.kill(root, kind, old=False)  # after the first actual stage-B batch commit
                self.reopen(root, kind)
                self.reopen(reference, kind)
                self.assertEqual(self.snapshot(reference, kind), self.snapshot(root, kind))
                self.reopen(root, kind)
                self.assertEqual(self.snapshot(reference, kind), self.snapshot(root, kind))

    @requirements('RS-B-B4t-10')
    def test_B4t_10_pending_old_target_migrations(self):
        from tests.test_rsf_s3_resumable import open_owner
        from codex_agents.history_contracts import HistoryError
        for kind in ('account', 'project'):
            with self.subTest(kind=kind):
                root = self.clone(kind)
                boundary = self.kill(root, kind, old=True)
                self.assertTrue(any(cursor == 64 and done == 0 for _, cursor, done in boundary['progress']))
                before = self.snapshot(root, kind)
                self.assertIn('hold_migration_progress', before)
                kept = {path.name: path.read_bytes() for path in (root / 'history').glob('history.sqlite3.before-22.*')}
                if kind == 'project':
                    self.assertEqual(1, len(kept))
                    missing = Path(tempfile.mkdtemp(dir=self.root))
                    shutil.copytree(root, missing, dirs_exist_ok=True)
                    for name in kept:
                        (missing / 'history' / name).unlink()
                    untouched = self.database(missing, kind).read_bytes()
                    with self.assertRaises(HistoryError):
                        open_owner(missing, kind)
                    self.assertEqual(untouched, self.database(missing, kind).read_bytes())
                self.reopen(root, kind)
                final = self.snapshot(root, kind)
                self.assertNotIn('hold_migration_progress', final)
                self.assertIn('runtime_floor', final)
                for name, raw in kept.items():
                    self.assertEqual(raw, (root / 'history' / name).read_bytes())


class ServiceStopTests(unittest.TestCase):
    __why_not_scenario__ = 'Exact enrolled operation authorization and fixed wire shape; no process signal.'

    @requirements('RS-B-B4t-8')
    def test_the_new_history_read_is_produced_by_the_real_handler_without_writes(self):
        from codex_agents.history_api import HistoryRequest, encode_history_request, decode_history_response, history_request_digest
        from codex_agents.history_service import HistoryService, HistoryAuthorization
        from codex_agents.project_history import ProjectHistory
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        project = 'prj_' + '1' * 32
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            history = ProjectHistory(Path(folder).resolve(), project)
            request = HistoryRequest(1, 'history.takeover_report', 'takeover', project, COLLECTOR_RUNTIME_DIGEST, {})
            service = HistoryService(project, COLLECTOR_RUNTIME_DIGEST, lambda: history,
                authorizer=lambda offered, context: HistoryAuthorization(project, COLLECTOR_RUNTIME_DIGEST,
                    history_request_digest(offered), 'host', frozenset({'history.takeover_report'})))
            with closing(service):
                before = history._connection.total_changes
                result = decode_history_response(service.handle(encode_history_request(request), object()), request)
                self.assertEqual([], result['records'])
                self.assertEqual(before, history._connection.total_changes)

    @requirements('RS-B-B4t-3')
    def test_stop_for_update_requires_the_owner_and_the_exact_enrolled_request(self):
        from types import SimpleNamespace
        from codex_agents.history_api import HistoryRequest, history_request_digest
        from codex_agents.hosts import OWNER_CLIENT, CODEX_HOST_CLIENT
        from codex_agents.service_host import ProjectServiceHost
        request = HistoryRequest(1, 'service.stop_for_update', 'update-stop', 'prj_' + '1' * 32, B, {})
        host = ProjectServiceHost.__new__(ProjectServiceHost)
        host._frontend = mock.Mock()
        enrolled = SimpleNamespace(role='host', host_client=OWNER_CLIENT, request_digest=history_request_digest(request))
        host._frontend.context.return_value = SimpleNamespace(kind='project', enrolled=enrolled)
        self.assertIsNotNone(host._authorize_history(request, 'token'))
        self.assertTrue(host.stopping_for_update())
        for name, value in (('host_client', CODEX_HOST_CLIENT), ('request_digest', 'f' * 64)):
            old = getattr(enrolled, name)
            setattr(enrolled, name, value)
            host._update_stop_requested = False
            self.assertIsNone(host._authorize_history(request, 'token'))
            self.assertFalse(host.stopping_for_update())
            setattr(enrolled, name, old)
        from codex_agents.history_service import HistoryService
        from codex_agents.history_api import encode_history_request, decode_history_response
        opened = mock.Mock(side_effect=AssertionError('stop operation opened a store'))
        service = HistoryService(request.project_id, request.runtime_digest, opened,
                                 authorizer=host._authorize_history)
        self.addCleanup(service.close)
        self.assertEqual({'stopping': True}, decode_history_response(
            service.handle(encode_history_request(request), 'token'), request))
        self.assertTrue(host.stopping_for_update())
        opened.assert_not_called()


class CollectorStopTests(unittest.TestCase):
    __why_not_scenario__ = 'Authenticated shutdown transport boundary and fixed bound; no collector process or store is opened.'

    def operations(self):
        from types import SimpleNamespace
        from codex_agents.takeover_owner import OwnerOperations
        state = SimpleNamespace(root=Path('/state'), socket=Path('/state/collector.sock'))
        return OwnerOperations(state, Path('/clone/cli.py'), Path('/project'), lambda: {})

    def identity(self, version='0.8.66'):
        from codex_agents.runtime_lineage import release_versions
        digest = next(digest for digest, text in release_versions().items() if text == version)
        return dict(version=version, digest=digest)

    @requirements('RS-B-B4t-0', 'RS-B-B4t-3')
    def test_drained_stop_forces_only_a_running_child_refusal_without_any_signal(self):
        from codex_agents.collector_contracts import CollectorError
        identity = self.identity()
        with mock.patch('codex_agents.takeover_owner.CollectorClient') as factory, \
                mock.patch('codex_agents.learning_store._wait_for_collector_shutdown', return_value=True), \
                mock.patch('os.kill', side_effect=AssertionError('collector signal')), \
                mock.patch('os.killpg', side_effect=AssertionError('collector signal')):
            client = factory.return_value
            client._request.return_value = dict(runtime_version=identity['version'], runtime_digest=identity['digest'])
            client.shutdown.side_effect = [CollectorError('busy', code='collector_busy'), None]
            self.operations().stop_collector(dict(predecessor_collector=identity, collector_children=False))
            self.assertEqual([mock.call(only_if_drained=True, echo_digest=identity['digest']),
                              mock.call(only_if_drained=False, echo_digest=identity['digest'])],
                             client.shutdown.call_args_list)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_collector_and_new_services_are_never_signalled(self):
        from codex_agents.orchestration_api import OrchestrationScope
        import signal
        identity = self.identity()
        old = dict(version='0.8.67',digest=A,entry='/old/project_host.py',directory='/runtime',
                   project_endpoint='/runtime/p.sock',stop_op=True,pid=42,birth='birth',
                   scope=OrchestrationScope('prj_'+'1'*32,'session',A,'instance',1).as_data())
        ops = self.operations()
        with ExitStack() as stack:
            for target in ('os.kill','os.killpg'):
                stack.enter_context(mock.patch(target,side_effect=AssertionError('forbidden signal')))
            stack.enter_context(mock.patch.object(signal,'pidfd_send_signal',create=True,
                                                 side_effect=AssertionError('forbidden pidfd signal')))
            factory=stack.enter_context(mock.patch('codex_agents.takeover_owner.CollectorClient'))
            factory.return_value._request.return_value=dict(runtime_version=identity['version'],runtime_digest=identity['digest'])
            stack.enter_context(mock.patch('codex_agents.learning_store._wait_for_collector_shutdown',return_value=True))
            ops.stop_collector(dict(predecessor_collector=identity,collector_children=True))
            factory.return_value.shutdown.assert_called_once_with(only_if_drained=False,echo_digest=identity['digest'])
            stack.enter_context(mock.patch('codex_agents.client_runtime.enroll_clients'))
            call=stack.enter_context(mock.patch('codex_agents.history_api.HistoryClient._call',return_value={'stopping':True}))
            stack.enter_context(mock.patch.object(ops,'_service_locks',return_value=[]))
            ops.stop_service(dict(predecessor_service=old))
            call.assert_called_once_with('service.stop_for_update','update-stop',{})

    @requirements('RS-B-B4t-3')
    def test_pre_identity_collectors_receive_only_the_verified_predecessors_bare_shape(self):
        for version in ('0.8.32', '0.8.55', '0.8.56', '0.8.66'):
            identity = self.identity(version)
            with self.subTest(version=version), mock.patch('codex_agents.takeover_owner.CollectorClient') as factory, \
                    mock.patch('codex_agents.learning_store._wait_for_collector_shutdown', return_value=True):
                client = factory.return_value
                client._request.return_value = dict(runtime_version=version, runtime_digest=identity['digest'])
                self.operations().stop_collector(dict(predecessor_collector=identity, collector_children=True))
                shape = {'earlier_collector': True} if version in ('0.8.32', '0.8.55') else {'echo_digest': identity['digest']}
                client.shutdown.assert_called_once_with(only_if_drained=False, **shape)

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9', 'RS-B-B4t-10')
    def test_wrong_identity_sends_nothing_and_the_stop_past_thirty_seconds_is_resumable(self):
        from codex_agents.update_takeover import TakeoverError
        identity = self.identity()
        with mock.patch('codex_agents.takeover_owner.CollectorClient') as factory, \
                mock.patch('codex_agents.learning_store._wait_for_collector_shutdown', return_value=False) as wait, \
                mock.patch('codex_agents.takeover_owner.time.monotonic', return_value=10.0), \
                mock.patch('os.kill', side_effect=AssertionError('collector signal')):
            client = factory.return_value
            client._request.return_value = dict(runtime_version=identity['version'], runtime_digest='f' * 64)
            journal = dict(predecessor_collector=identity, collector_children=True)
            with self.assertRaises(TakeoverError) as refused:
                self.operations().stop_collector(journal)
            self.assertEqual('predecessor_unverified', refused.exception.code)
            client.shutdown.assert_not_called()
            wait.assert_not_called()
            client._request.return_value['runtime_digest'] = identity['digest']
            with self.assertRaises(TakeoverError) as stopped:
                self.operations().stop_collector(journal)
            self.assertEqual('collector_stop_failed', stopped.exception.code)
            self.assertEqual(40.0, wait.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
