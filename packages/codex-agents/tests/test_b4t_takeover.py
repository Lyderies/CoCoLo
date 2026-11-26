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
        from codex_agents.takeover_journal import JournalError
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


class StoreTests(unittest.TestCase):
    __why_not_scenario__ = 'Archived 0.8.66 business methods create authentic custody; disposable stores only.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-store-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        here = Path(__file__).parent
        packed = subprocess.run(['git', '-C', str(here.parents[2]), 'archive', 'afb6f610',
                                 'packages/codex-agents', 'scripts'], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(cls.archive / 'scripts')
        made = subprocess.run([sys.executable, '-B', str(here / 'b4t_fixture.py'), str(cls.archive),
                               str(cls.source), getattr(cls, 'fixture_mode', 'build')],
                              env=env, capture_output=True, text=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        cls.data = json.loads((cls.source / 'manifest.json').read_text())

    def setUp(self):
        from codex_agents.history_contracts import AttemptBinding
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', self.case / 'history')
        self.finished = AttemptBinding(**self.data['finished'])
        self.running = AttemptBinding(**self.data['running'])
        self.scope = ResultLeaseBinding(**self.data['running_scope'])
        self.key = bytes.fromhex(self.data['key'])
        self.store = None
        self.addCleanup(lambda: self.store.close() if self.store else None)
        self.intent = dict(takeover_id='1' * 32, project_id=self.data['project'], root_digest='d' * 64,
            predecessor_service=dict(version='0.8.66', digest=self.finished.runtime_digest,
                                     entry='old/project_host.py'),
            predecessor_collector=dict(version='0.8.66', digest=self.finished.runtime_digest),
            successor=dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST),
            runs=[dict(run_handle=self.running.run_handle, attempt=self.running.attempt,
                       bridge_run_id=self.running.claim_id)], phase='store_adopting', recorded_at_ms=self.data['now'])

    def open(self, *, takeover=True):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        if self.store:
            self.store.close()
        self.store = ProjectHistory(self.case / 'history', self.data['project'], result_key=self.key,
            result_capacity=ResultCapacity(256, 256 * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * 86_400_000),
            result_clock_ms=lambda: self.data['now'], allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True, takeover=self.intent if takeover else None)
        return self.store

    def rows(self, name):
        return [tuple(row) for row in self.store._connection.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]

    def primary_record(self, *, corrupt=False):
        from codex_agents.hosts import CODEX_HOST_CLIENT
        from codex_agents.host_result_contracts import HostResultReference
        from codex_agents.service_record import ServiceRecordAuthority
        reservation = self.store.result_reservation(self.finished)
        terminal = self.store.record(reservation.terminal_record_id)
        reference = HostResultReference(self.finished, terminal.record_id,
                                        'f' * 64 if corrupt else terminal.content_digest, reservation.result_digest)
        original = self.store.stored_result_context(self.finished, host_class=CODEX_HOST_CLIENT)
        return self.store.admit_host_result(reference, assignment=original.assignment, task=original.task,
            route=original.route, client_capability=original.client_capability, before_commit=lambda: None,
            final_check=ServiceRecordAuthority(self.store._fence.digest, False))

    def predecessor_records(self, *, classify_only=False):
        (self.case / 'manifest.json').write_text(json.dumps(self.data))
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.project_history import ProjectHistory
from codex_agents.history_contracts import AttemptBinding
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
from codex_agents.service_record import ServiceRecordAuthority
root=Path(sys.argv[2]);data=json.loads((root/'manifest.json').read_text())
owner=ProjectHistory(root/'history',data['project'],result_key=bytes.fromhex(data['key']),
 result_capacity=ResultCapacity(256,256*MAX_RESULT_BODY_BYTES,1800000,7*86400000),
 result_clock_ms=lambda:data['now'],allow_legacy_orchestration_reference=True)
try:
 if sys.argv[3]=='classify':
  with owner._transaction(): owner._classify_legacy_custody(None)
 else:
  result=owner.admit_service_result(AttemptBinding(**data['finished']),
   final_check=ServiceRecordAuthority(owner._fence.digest,True))
  assert result['admission']['host_outcome_digest']
finally: owner.close()
'''
        done = subprocess.run([sys.executable, '-B', '-c', code, str(runtime), str(self.case),
                               'classify' if classify_only else 'record'],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(0, done.returncode, done.stderr)

    @requirements('RS-B-B4t-2')
    def test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store(self):
        # The archived fixture is first captured before publish; its old writer
        # commits a real result at the service-stop boundary, before adoption.
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.project_takeover import stopped_children
        from codex_agents.history_contracts import AttemptBinding
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        made = subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('b4t_fixture.py')),
            str(self.archive),str(self.case),'stop-result'],env={**os.environ,'PYTHONPATH':str(self.archive/'scripts')},
            capture_output=True,text=True,timeout=30)
        self.assertEqual(0,made.returncode,made.stderr)
        self.data=json.loads((self.case/'manifest.json').read_bytes())
        self.running=AttemptBinding(**self.data['running']);self.finished=self.running
        self.key=bytes.fromhex(self.data['key'])
        self.intent.update(project_id=self.data['project'],recorded_at_ms=self.data['now'],
                           runs=[dict(run_handle=self.running.run_handle,attempt=self.running.attempt,bridge_run_id=self.running.claim_id)])
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        with closing(sqlite3.connect(self.case/'history/history.sqlite3')) as db:
            self.assertIsNone(db.execute('SELECT committed_at_ms FROM result_reservations WHERE claim_id=?',
                                        (self.running.claim_id,)).fetchone()[0])
        # A real old writer publishes, through the same product methods as its
        # service. It is closed before the successor can take the lifetime lock.
        code = """import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.project_history import ProjectHistory
from codex_agents.history_contracts import AttemptBinding
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
from codex_agents.result_contracts import ResultObservation
root=Path(sys.argv[2]); data=json.loads((root/'manifest.json').read_text())
binding=AttemptBinding(**data['running'])
owner=ProjectHistory(root/'history',data['project'],result_key=bytes.fromhex(data['key']),
 result_capacity=ResultCapacity(256,256*MAX_RESULT_BODY_BYTES,1800000,7*86400000),
 result_clock_ms=lambda:data['now'],allow_legacy_orchestration_reference=True)
body=data['body'].encode()
try:
 owner.publish_result(binding,data['assignment'],body,ResultObservation(binding.claim_id,data['now'],False,True,None))
finally:owner.close()
"""
        (self.case/'manifest.json').write_text(json.dumps(self.data))
        done = subprocess.run([sys.executable,'-B','-c',code,str(runtime),str(self.case)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,done.returncode,done.stderr)
        self.open()
        self.assertIsNotNone(self.store.result_reservation(self.running).committed_at_ms)
        self.assertEqual(0, stopped_children(self.store, self.intent['runs']))
        ServiceRecordAuthority(self.store._fence.digest, True).check(self.store,self.running,'K1')
        self.assertIsNotNone(self.store.read_result(self.running))

    @requirements('RS-B-B4t-8')
    def test_nonempty_takeover_counts_are_produced_by_the_real_history_handler(self):
        from codex_agents.history_service import HistoryService,HistoryAuthorization
        from codex_agents.history_api import HistoryRequest,history_request_digest,encode_history_request,decode_history_response
        self.open()
        request=HistoryRequest(1,'history.takeover_report','takeover',self.data['project'],self.store._fence.digest,{})
        service=HistoryService(request.project_id,request.runtime_digest,lambda:self.store,close_history=False,
            authorizer=lambda offered,token:HistoryAuthorization(request.project_id,request.runtime_digest,
                history_request_digest(offered),'host',frozenset({'history.takeover_report'})))
        before=self.store._connection.total_changes
        with closing(service):reply=decode_history_response(service.handle(encode_history_request(request),object()),request)
        self.assertEqual((1,0),(reply['records'][0]['runs_listed'],reply['records'][0]['runs_unlisted']))
        self.assertEqual(before,self.store._connection.total_changes)

    @requirements('RS-B-B4t-8','RS-B-B4t-10')
    def test_B4t_A1_overflow_count_survives_the_journal(self):
        from codex_agents.takeover_journal import TakeoverJournal
        from codex_agents.project_takeover import takeover_report,takeover_lines
        from codex_agents.project_history import SETTLEMENT_REPLAY_WINDOW_MS
        journal=TakeoverJournal(self.case)
        intent=Operations(self.case).preflight()
        document=journal.create(intent,[run_entry(i) for i in range(70)])
        for _ in range(4):document=journal.reconcile(document,[run_entry(i) for i in range(70)])
        self.assertEqual((64,6),(len(document['runs']),document['runs_unlisted']))
        self.intent.update(runs=document['runs'],runs_unlisted=document['runs_unlisted'])
        self.open()
        row=self.rows('takeover_record')[0]
        self.assertEqual((64,6),row[-2:])
        from codex_agents.project_takeover import complete_takeovers
        complete_takeovers(self.store)
        self.assertEqual('complete',self.rows('takeover_record')[0][5])
        journal.remove()  # J14's product removal; status has no journal left.
        with mock.patch.object(TakeoverJournal,'read',side_effect=AssertionError('status depended on removed journal')):
            self.assertIn('70 runs ended by the update (6 of them not listed one by one)',
                          '\n'.join(takeover_lines(takeover_report(self.store))))
            self.store._purge_takeover(self.data['now']+SETTLEMENT_REPLAY_WINDOW_MS+1,0)
            self.assertEqual([],self.rows('takeover_runs'))
            self.assertIn('70 runs ended by the update (6 of them not listed one by one)',
                          '\n'.join(takeover_lines(takeover_report(self.store))))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-5')
    def test_B4t_2_committed_results_stay_recordable(self):
        from codex_agents.service_record import ServiceRecordAuthority
        self.open()
        before = self.store.read_result(self.finished)
        for _ in range(2):
            authority = ServiceRecordAuthority(self.store._fence.digest, True)
            authority.check(self.store, self.finished, 'K1')
            self.assertEqual(before, self.store.read_result(self.finished))
            self.open(takeover=False)
        recorded = self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertTrue(recorded['admission']['host_outcome_digest'])
        self.assertEqual(1, len(self.rows('project_result_recorders')))
        original_records = self.rows('records')
        self.open(takeover=False)
        self.assertIsNone(self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True)))
        self.assertEqual(original_records, self.rows('records'))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_predecessors_classification_is_not_the_successors_takeover_authority(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.predecessor_records(classify_only=True)
        self.open(takeover=False)
        before = self.rows('records'), self.rows('project_holds')
        with self.assertRaises(HistoryError) as refused:
            self.store.admit_service_result(self.finished,
                final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertEqual('history_unrecordable_other_runtime', refused.exception.reason_code)
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))
        self.open()
        self.assertTrue(self.store.admit_service_result(self.finished,
            final_check=ServiceRecordAuthority(self.store._fence.digest, True))['admission']['host_outcome_digest'])

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_taken_over_result_requires_its_own_takeover_authority(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        with self.store._transaction():
            row = self.store._connection.execute("SELECT evidence FROM project_holds WHERE kind='result_custody' "
                "AND identity=?", (f'{self.finished.run_handle}:{self.finished.attempt}',)).fetchone()
            evidence = json.loads(row[0])
            self.assertIn('takeover_id', evidence)
            del evidence['takeover_id']
            self.store._connection.execute("UPDATE project_holds SET evidence=? WHERE kind='result_custody' AND identity=?",
                (json.dumps(evidence), f'{self.finished.run_handle}:{self.finished.attempt}'))
        before = self.rows('records'), self.rows('project_holds')
        with self.assertRaises(HistoryError):
            self.store.admit_service_result(self.finished,
                final_check=ServiceRecordAuthority(self.store._fence.digest, True))
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_a_result_cannot_borrow_an_unrelated_or_noncurrent_takeover_record(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        from codex_agents.collector_client import PACKAGE_VERSION
        self.open()
        identity = f'{self.finished.run_handle}:{self.finished.attempt}'
        with self.store._transaction():
            self.store._connection.execute('INSERT INTO takeover_record(takeover_id,predecessor_digest,predecessor_version,successor_digest,successor_version,state,recorded_at_ms,runs_listed,runs_unlisted) VALUES(?,?,?,?,?,?,0,0,0)',
                ('6' * 32, '5' * 64, '0.8.60', '6' * 64, '0.8.61', 'adopted'))
            evidence = json.loads(self.store._connection.execute("SELECT evidence FROM project_holds "
                "WHERE kind='result_custody' AND identity=?", (identity,)).fetchone()[0])
            evidence['takeover_id'] = '6' * 32
            self.store._connection.execute("UPDATE project_holds SET evidence=? WHERE kind='result_custody' AND identity=?",
                                            (json.dumps(evidence), identity))
        for current in (False, True):
            with self.subTest(current=current):
                if current:
                    with self.store._transaction():
                        self.store._connection.execute('UPDATE takeover_record SET successor_digest=?,successor_version=? '
                            'WHERE takeover_id=?', (self.store._fence.digest, PACKAGE_VERSION, '6' * 32))
                before = self.rows('records'), self.rows('project_holds')
                with self.assertRaises(HistoryError):
                    self.store.admit_service_result(self.finished,
                        final_check=ServiceRecordAuthority(self.store._fence.digest, True))
                self.assertEqual(before, (self.rows('records'), self.rows('project_holds')))

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_tampered_takeover_run_is_refused_without_relabelling_another_run(self):
        from codex_agents.project_takeover import takeover_report
        self.open()
        with self.store._transaction():
            self.store._connection.execute('UPDATE takeover_runs SET run_handle=?', (self.finished.run_handle,))
        before = self.rows('project_holds'), self.rows('result_leases')
        report = takeover_report(self.store, run_handle=self.finished.run_handle)
        self.assertEqual(1, report['refused_unverifiable'])
        self.assertEqual((0, 0), (report['lost'], report['lost_held']))
        self.assertEqual(before, (self.rows('project_holds'), self.rows('result_leases')))

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_run_cannot_borrow_an_unrelated_takeover_id_for_its_annotation(self):
        from codex_agents.project_takeover import takeover_report
        self.open()
        with self.store._transaction():
            self.store._connection.execute('INSERT INTO takeover_record(takeover_id,predecessor_digest,predecessor_version,successor_digest,successor_version,state,recorded_at_ms,runs_listed,runs_unlisted) VALUES(?,?,?,?,?,?,0,0,0)',
                ('6' * 32, '5' * 64, '0.8.60', '6' * 64, '0.8.61', 'adopted'))
            self.store._connection.execute('UPDATE takeover_runs SET takeover_id=?', ('6' * 32,))
        before = self.rows('project_holds'), self.rows('result_leases')
        report = takeover_report(self.store, run_handle=self.running.run_handle)
        self.assertEqual(1, report['refused_unverifiable'])
        self.assertEqual([], report['records'])
        self.assertEqual((0, 0), (report['lost'], report['lost_held']))
        self.assertEqual(before, (self.rows('project_holds'), self.rows('result_leases')))

    @requirements('RS-B-B4t-3')
    def test_B4t_3_an_old_binary_cannot_reopen_either_store(self):
        self.open().close()
        self.store = None
        old_runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from codex_agents.project_history import ProjectHistory;"
                "ProjectHistory(Path(sys.argv[2]),sys.argv[3])")
        before = (self.case / 'history/history.sqlite3').read_bytes()
        done = subprocess.run([sys.executable, '-B', '-c', code, str(old_runtime), str(self.case / 'history'),
                               self.data['project']], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(0, done.returncode)
        self.assertRegex(done.stderr, 'newer|incompatible')
        self.assertEqual(before, (self.case / 'history/history.sqlite3').read_bytes())

    @requirements('RS-B-B4t-3')
    def test_B4t_3_injected_stale_owner_commits_nothing(self):
        from codex_agents.history_contracts import HistoryError
        import fcntl
        stale = self.open()
        fcntl.flock(stale._writer_fd, fcntl.LOCK_UN)
        self.store = None
        successor = self.open()
        self.addCleanup(stale.close)
        before = self.rows('records')
        with self.assertRaises(HistoryError):
            stale._connection.execute('DELETE FROM records')
        stale._connection.rollback()
        self.assertEqual(before, self.rows('records'))
        self.assertIsNotNone(successor._fence.epoch)

    @requirements('RS-B-B4t-3')
    def test_B4t_3_real_predecessor_is_excluded_by_proven_exit(self):
        from codex_agents.history_contracts import HistoryError
        import select
        runtime = self.archive / 'packages/codex-agents/native-package/.codex/orchestration'
        code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from codex_agents.project_history import ProjectHistory;"
                "s=ProjectHistory(Path(sys.argv[2]),sys.argv[3]);print('ready',flush=True);"
                "sys.stdin.readline();s.close()")
        p = subprocess.Popen([sys.executable, '-B', '-c', code, str(runtime), str(self.case / 'history'),
                              self.data['project']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.kill() if p.poll() is None else None)
        self.assertTrue(select.select([p.stdout], [], [], 10)[0])
        self.assertEqual('ready', p.stdout.readline().strip())
        with self.assertRaises(HistoryError):
            self.open()
        p.communicate('\n', timeout=10)
        self.assertEqual(0, p.returncode)
        self.open()

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once(self):
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.learning_contracts import canonical_json
        from codex_agents.lost_run_closure import lost_run_closure
        from codex_agents.project_takeover import takeover_report
        self.open()
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'lost-' + '2' * 64)
        lease = self.store.result_lease(self.scope)
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                 self.running.claim_id, lease.lease_digest).payload()
        outbox = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'lost-' + '2' * 64,
                                                    payload, runtime_digest=self.store._fence.digest)
        report = takeover_report(self.store)
        self.assertEqual(1, report['lost_held'])
        closure = canonical_json(lost_run_closure(self.key, self.scope, lease_digest=lease.lease_digest,
            assignment_digest=lease.assignment_digest, bridge_run_id=self.running.claim_id, release_kind='lost',
            released_at_ms=self.data['now'] + 5, request_digest=outbox['request_digest']))
        for _ in range(2):
            self.store.record_account_settlement(self.scope, closure)
        report = takeover_report(self.store)
        self.assertEqual((1, 0), (report['lost'], report['lost_held']))
        self.assertEqual(1, len(self.rows('takeover_runs')))

    @requirements('RS-B-B4t-8')
    def test_B4t_8_status_names_the_takeover(self):
        from codex_agents.project_takeover import takeover_report, takeover_lines
        self.open()
        report = takeover_report(self.store)
        lines = '\n'.join(takeover_lines(report))
        self.assertIn('Update hand-over', lines)
        self.assertIn('0.8.66', lines)
        self.assertIn('finished results kept', lines)
        self.assertNotIn(str(self.case), lines)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_post_stop_count_includes_an_unsettled_orphan_but_excludes_a_result_committed_before_stop(self):
        from codex_agents.project_takeover import stopped_children
        self.open()
        finished = dict(run_handle=self.finished.run_handle, attempt=self.finished.attempt,
                        bridge_run_id=self.finished.claim_id)
        self.assertEqual(1, stopped_children(self.store, [*self.intent['runs'], finished]))
        self.assertEqual(0, stopped_children(self.store, [finished]))
        forged = dict(self.intent['runs'][0], run_handle=self.finished.run_handle)
        self.assertEqual(0, stopped_children(self.store, [forged]))

    @requirements('RS-B-B4t-0', 'RS-B-B4t-9')
    def test_T0_does_not_ignore_an_offline_predecessor_store_without_a_trusted_package(self):
        from codex_agents.learning_store import LearningState
        from codex_agents.collector_contracts import CollectorError
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.update_takeover import TakeoverError
        state = LearningState(self.case / 'state')
        state.key_path.write_bytes(self.key)
        project = self.case / 'project'
        project.mkdir()
        directory = state.root / 'projects' / self.data['project']
        directory.parent.mkdir(mode=0o700)
        shutil.copytree(self.source / 'history', directory)
        row = dict(project_id=self.data['project'], canonical_root=str(project), launch_copy=None, launch_digest=None)
        database = directory / 'history.sqlite3'
        before = database.read_bytes()
        with mock.patch('codex_agents.takeover_preflight._registry', return_value=(row, 18, {})), \
                mock.patch('codex_agents.takeover_preflight.CollectorClient._request', side_effect=CollectorError('down')), \
                mock.patch('codex_agents.project_host_client._hinted_host', return_value=None):
            with self.assertRaises(TakeoverError) as caught:
                read_plan(state, project)
        self.assertEqual('predecessor_unverified', caught.exception.code)
        self.assertEqual(before, database.read_bytes())
        self.assertFalse((state.root / 'update-takeover.json').exists())

    @requirements('RS-B-B4t-8')
    def test_status_wording_uses_fixed_outcomes_and_other_project_commands(self):
        from codex_agents.project_takeover import takeover_lines
        report = dict(records=[dict(predecessor_version='0.8.66', successor_version='0.8.67', state='complete')],
                      lost=1, lost_held=0, finished_kept=2, refused_unverifiable=0)
        account = dict(recorded_at_ms=0, other_projects=1, other_runs=1,
                       updates=[dict(version='0.8.66', children=1, update_message='Update command from B4m')])
        text = '\n'.join(takeover_lines(report, account=account))
        self.assertIn('1970-01-01 00:00 UTC, CoCoLo 0.8.66 to 0.8.67', text)
        self.assertIn('1 run ended by the update. 2 finished results kept.', text)
        self.assertIn('Runs ended by the update: 1, slot freed.', text)
        self.assertIn('Other projects still on an earlier version: 1.', text)
        self.assertIn('Update command from B4m', text)
        report['refused_unverifiable'] = 3
        self.assertIn('Kept unchanged, could not be verified: 3 records of CoCoLo 0.8.66. Nothing was freed.',
                      '\n'.join(takeover_lines(report)))

    @requirements('RS-B-B4t-4')
    def test_B4t_4_memory_holds_and_pending_removals_carry_over(self):
        # Adoption cannot manufacture memory-release evidence or remove retry duties.
        with closing(sqlite3.connect(self.case / 'history/history.sqlite3')) as db:
            before = db.execute("SELECT * FROM project_holds WHERE kind IN ('preparation','driver_entry')").fetchall()
        self.open()
        after = [tuple(row) for row in self.store._connection.execute(
            "SELECT * FROM project_holds WHERE kind IN ('preparation','driver_entry')")]
        self.assertEqual(before, after)

    @requirements('RS-B-B4t-4')
    def test_B4t_4_open_bindings_end_by_their_own_rules(self):
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.project_holds_schema import attempt_identity
        self.open()
        before = self.rows('result_leases')
        self.open()
        self.assertEqual(before, self.rows('result_leases'))
        self.assertEqual('bound', self.store.result_lease(self.scope).state)
        bound_id=attempt_identity(self.running.run_handle,self.running.attempt)
        pending=ResultLeaseBinding(**self.data['pending'])
        pending_id=attempt_identity(pending.run_handle,pending.attempt)
        def duty(identity):
            return dict(self.store._connection.execute("SELECT * FROM project_holds WHERE kind='open_binding' AND identity=?",(identity,)).fetchone())
        self.assertEqual(('released','claimed'),(duty(bound_id)['state'],duty(bound_id)['release_kind']))
        before=duty(pending_id)
        self.assertEqual('held',before['state'])
        self.store.withdraw_earlier_open_bindings('new-instance',project_id=self.data['project'],runtime_digest=self.store._fence.digest)
        self.assertEqual(before,duty(pending_id))
        self.assertEqual('legacy_open_inputs_missing',json.loads(before['evidence'])['reason'])
        self.test_B4t_4_the_old_child_ends_lost_by_update_and_its_slot_frees_once()
        self.assertEqual(('released','claimed'),(duty(bound_id)['state'],duty(bound_id)['release_kind']))
        # The account's durable open in flight is a different lifecycle: a new
        # request conflicts with its original key until the old window expires.
        from tests.test_account_original_open import OriginalAccountOpenPendingTests
        from codex_agents.account_open_contracts import OpenError
        from codex_agents.learning_store import LearningLedger
        case=OriginalAccountOpenPendingTests(methodName='runTest')
        case.setUp(); self.addCleanup(case.doCleanups)
        original,_=case.preflight()
        rows=case.pending()
        state=case.ledger.state
        case.ledger.close()
        case.ledger=LearningLedger(state,allow_schema_upgrade=True,takeover=self.intent)
        self.addCleanup(case.ledger.close)
        self.assertEqual(rows,case.pending())
        with self.assertRaisesRegex(OpenError,'open_conflict'):
            case.preflight(request_id='successor-open')
        self.assertEqual(rows,case.pending())
        from codex_agents.learning_store import OPEN_REQUEST_RETENTION_MS
        case.ledger.sweep_account_retention(now_ms=original.observation_deadline_at_ms+OPEN_REQUEST_RETENTION_MS+1)
        self.assertEqual([],case.pending())

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_B4t_4_rollover_replay_across_the_takeover(self):
        self.open()
        before = self.rows('project_orchestrations')
        self.open()
        self.assertEqual(before, self.rows('project_orchestrations'))
        self.assertFalse(any(row[-1] == 'closed' for row in self.store._connection.execute(
            'SELECT * FROM project_orchestrations WHERE orchestration_handle=?',
            (self.finished.orchestration_handle,))))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-9')
    def test_adopted_open_and_paused_orchestrations_are_not_closed_by_runtime_change(self):
        from codex_agents.orchestration_api import OrchestrationScope, SCOPE_FIELDS
        self.open()
        owner = self.store._orchestration_owner()
        current = OrchestrationScope(**{name: getattr(self.finished, name) for name in SCOPE_FIELDS})
        current = replace(current, runtime_digest=self.store._fence.digest)
        before = self.rows('project_orchestrations'), self.rows('project_holds')
        self.assertEqual(0, owner.end_earlier(current, 10))
        self.assertEqual(before, (self.rows('project_orchestrations'), self.rows('project_holds')))

    @requirements('RS-B-B4t-2', 'RS-B-B4t-8', 'RS-B-B4t-10')
    def test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata(self):
        from codex_agents.project_takeover import complete_takeovers, takeover_report
        self.open()
        before = self.rows('result_leases'), self.rows('project_orchestrations'), self.rows('takeover_obligations')
        self.assertEqual('adopted', self.store._connection.execute('SELECT state FROM takeover_record').fetchone()[0])
        complete_takeovers(self.store)
        complete_takeovers(self.store)
        self.assertEqual('complete', self.store._connection.execute('SELECT state FROM takeover_record').fetchone()[0])
        self.assertEqual(before, (self.rows('result_leases'), self.rows('project_orchestrations'), self.rows('takeover_obligations')))
        report = takeover_report(self.store)
        self.assertEqual({'predecessor_version', 'successor_version', 'state', 'runs_listed', 'runs_unlisted'}, set(report['records'][0]))
        self.assertEqual((1, 0), (report['records'][0]['runs_listed'], report['records'][0]['runs_unlisted']))
        self.assertEqual('complete', report['records'][0]['state'])

    @requirements('RS-B-B4t-5')
    def test_B4t_5_no_double_record_in_any_order(self):
        from codex_agents.service_record import ServiceRecordAuthority
        for order in (('service', 'primary'), ('primary', 'service'), ('old', 'service', 'primary')):
            with self.subTest(order=order):
                if self.store is not None:
                    self.store.close()
                    self.store = None
                self.case = Path(tempfile.mkdtemp(dir=self.root))
                shutil.copytree(self.source / 'history', self.case / 'history')
                if order[0] == 'old':
                    self.predecessor_records()
                self.open()
                for operation in order:
                    if operation == 'service':
                        self.store.admit_service_result(self.finished,
                            final_check=ServiceRecordAuthority(self.store._fence.digest, True))
                    elif operation == 'primary':
                        self.primary_record()
                self.assertEqual(1, len(self.rows('project_result_recorders')))
                before = self.rows('records'), self.rows('project_result_recorders')
                for _ in range(2):
                    self.open()
                    self.primary_record()
                    self.assertIsNone(self.store.admit_service_result(self.finished,
                        final_check=ServiceRecordAuthority(self.store._fence.digest, True)))
                    self.assertEqual(before, (self.rows('records'), self.rows('project_result_recorders')))
                self.assertEqual(1, len(self.rows('takeover_record')))

    @requirements('RS-B-B4t-6')
    def test_B4t_6_primary_recheck_idempotent_across_takeover(self):
        from codex_agents.service_record import ServiceRecordAuthority
        self.open()
        before = self.rows('records')
        authority = ServiceRecordAuthority(self.store._fence.digest, False)
        for _ in range(3):
            authority.check(self.store, self.finished, 'K2')
        self.assertEqual(before, self.rows('records'))
        recorded = self.primary_record()
        rows = self.rows('records'), self.rows('project_result_recorders')
        for _ in range(3):
            self.assertEqual(recorded, self.primary_record())
            self.open(takeover=False)
        from codex_agents.history_contracts import HistoryError
        with self.assertRaises(HistoryError):
            self.primary_record(corrupt=True)
        self.assertEqual(rows, (self.rows('records'), self.rows('project_result_recorders')))

    @requirements('UH-5')
    def test_UH_5_a_run_still_held_at_adoption_that_later_goes_releasing_is_not_taken_for_unverified(self):
        """Review r1 HIGH 2 (decision 367): a valid listed run still `held` when B adopts, later `releasing` through
        the successor's own arbitration and outbox, has no obligation row; it is never 'unverified', its conflict is
        retried and, when late, it is reported `hold_overdue`."""
        import time
        from codex_agents import health_holds
        from codex_agents.health_monitor import CheckContext
        from codex_agents.lost_run_contracts import SettleLostInput
        self.open()
        state = "SELECT state FROM project_holds WHERE kind='reservation' AND identity=?"
        self.assertEqual('held', self.store._connection.execute(state, (self.running.claim_id,)).fetchone()[0])
        lease = self.store.result_lease(self.scope)
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'b4t-lost')
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                  self.running.claim_id, lease.lease_digest).payload()
        self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost', payload,
                                           runtime_digest=self.store._fence.digest)
        self.assertEqual('releasing', self.store._connection.execute(state, (self.running.claim_id,)).fetchone()[0])
        self.assertIs(False, self.store.takeover_unverified(self.running.run_handle, self.running.attempt,
                                                            self.running.claim_id))
        with self.store._transaction():
            self.store._connection.execute(
                "UPDATE project_holds SET due_at_ms=NULL,next_check_ms=9000000000000 WHERE NOT "
                "(kind='reservation' AND identity=?)", (self.running.claim_id,))
            self.store._connection.execute(
                "UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 WHERE kind='reservation' AND identity=?",
                (self.running.claim_id,))
        check = health_holds.HoldsCheck(None, sweep_interval_seconds=60)
        with closing(health_holds.open_reader(self.case / 'history/history.sqlite3')) as reader:
            found = {f.reason_code for f in check.check_project(
                reader, self.data['project'], CheckContext(time.monotonic() + 5, 61_001, 60_000), self.key)}
        self.assertIn('hold_overdue', found)
        self.assertNotIn('hold_unverified_takeover', found)

    @requirements('RS-B-B4t-5', 'RS-B-B4t-10')
    def test_B4t_5_cross_digest_outbox_replay(self):
        from codex_agents.lost_run_contracts import SettleLostInput
        self.open(takeover=False)
        lease = self.store.result_lease(self.scope)
        self.store.arbitrate_publication(self.running.run_handle, self.running.attempt, 'b4t-lost')
        payload = SettleLostInput(self.running.run_handle, 'd' * 64, self.scope.claim_request_id,
                                 self.running.claim_id, lease.lease_digest).payload()
        from codex_agents.launch_journal import LaunchJournal
        journal = LaunchJournal(self.case / 'history/launches')
        journal.claimed(self.running.claim_id, run_handle=self.running.run_handle, attempt=self.running.attempt,
                        host_client='bridge-v1', lease=self.scope.as_data(), identity_digest='d' * 64,
                        claim_request_id=self.scope.claim_request_id,
                        storage_evidence=bytes.fromhex(self.data['running_storage_evidence']), workspace=None)
        journal.release()
        original = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost',
                                                      payload, runtime_digest=self.running.runtime_digest)
        self.open()
        offered = self.store.open_account_settlement(self.running.run_handle, self.running.attempt, 'b4t-lost',
                                                     payload, runtime_digest=self.store._fence.digest)
        self.assertNotEqual(original['request_digest'], offered['request_digest'])
        self.assertEqual(original['request_digest'], self.store._connection.execute(
            "SELECT request_digest FROM project_holds WHERE kind='reservation' AND identity=?",
            (self.running.claim_id,)).fetchone()[0])
        self.assertEqual(1, len(self.rows('takeover_obligations')))
        self.assertEqual(self.running.runtime_digest, self.rows('takeover_obligations')[0][-1])

    @requirements('RS-B-B4t-9')
    def test_B4t_9_corrupt_receipts_context_and_outbox_are_refused(self):
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        with self.store._transaction():
            self.store._connection.execute('UPDATE project_result_contexts SET context_digest=? WHERE run_handle=?',
                                           ('e' * 64, self.finished.run_handle))
        with self.assertRaises(HistoryError):
            ServiceRecordAuthority(self.store._fence.digest, True).check(self.store, self.finished, 'K1')
        self.assertEqual('committed', self.store.result_reservation(self.finished).state)

    @requirements('RS-B-B4t-9')
    def test_B4t_2_revoked_enrollment_refuses_recording(self):
        from types import SimpleNamespace
        from codex_agents.history_api import HistoryRequest, history_request_digest
        from codex_agents.host_result_contracts import HostResultReference
        from codex_agents.hosts import CODEX_HOST_CLIENT, CLAUDE_HOST_CLIENT
        from codex_agents.service_host import ProjectServiceHost
        from codex_agents.service_record import ServiceRecordAuthority
        from codex_agents.history_contracts import HistoryError
        self.open()
        reservation = self.store.result_reservation(self.finished)
        terminal = self.store.record(reservation.terminal_record_id)
        reference = HostResultReference(self.finished, terminal.record_id, terminal.content_digest,
                                        reservation.result_digest)
        request = HistoryRequest(1, 'history.admit_host_result', self.finished.claim_id, self.store.project_id,
                                 self.store._fence.digest, reference.as_data())
        host = ProjectServiceHost.__new__(ProjectServiceHost)
        host._frontend = mock.Mock()
        lane = SimpleNamespace(stored_result_context=lambda binding, host_class:
                               self.store.stored_result_context(binding, host_class=host_class))
        def read_context(read, **kwargs):
            try:
                return read(lane)
            except HistoryError:
                return None
        host._stored = read_context
        enrolled = SimpleNamespace(role='host', host_client=CODEX_HOST_CLIENT,
                                   request_digest=history_request_digest(request))
        host._frontend.context.return_value = SimpleNamespace(kind='project', enrolled=enrolled)
        self.assertIsNotNone(host._authorize_history(request, 'current-token'))
        before = self.rows('records'), self.rows('project_holds'), self.rows('takeover_record')
        host._frontend.context.return_value = None
        self.assertIsNone(host._authorize_history(request, 'revoked-token'))
        enrolled.host_client = CLAUDE_HOST_CLIENT
        host._frontend.context.return_value = SimpleNamespace(kind='project', enrolled=enrolled)
        self.assertIsNone(host._authorize_history(request, 'changed-host-class'))
        from codex_agents.history_service import HistoryService
        from codex_agents.history_api import encode_history_request, decode_history_response
        enrolled.host_client = CODEX_HOST_CLIENT
        entry = SimpleNamespace(kind='project', enrolled=enrolled)
        checks = []
        def reauthorizer(offered, token):
            checks.append(token)
            host._frontend.context.return_value = entry if len(checks) == 1 else None
            return host._authorize_history(offered, token)
        service = HistoryService(self.store.project_id, self.store._fence.digest, lambda: self.store,
                                 authorizer=reauthorizer, close_history=False)
        self.addCleanup(service.close)
        with self.assertRaises(HistoryError) as revoked:
            decode_history_response(service.handle(encode_history_request(request), 'revoked-during-body-IO'), request)
        self.assertEqual('history_not_authorized', revoked.exception.reason_code)
        self.assertGreaterEqual(len(checks), 2)
        forged = replace(self.finished, runtime_digest='f' * 64)
        with self.assertRaises(HistoryError):
            ServiceRecordAuthority(self.store._fence.digest, True).check(self.store, forged, 'K1')
        self.assertEqual(before, (self.rows('records'), self.rows('project_holds'), self.rows('takeover_record')))
        self.assertEqual('committed', self.store.result_reservation(self.finished).state)
        service._authorizer = host._authorize_history
        host._frontend.context.return_value = entry
        result = decode_history_response(service.handle(encode_history_request(request), 'fresh-current-token'), request)
        self.assertTrue(result['admission']['host_outcome_digest'])
        self.assertEqual(1, len(self.rows('project_result_recorders')))

    @requirements('RS-B-B4t-9')
    def test_B4t_machine_local_tables_never_travel(self):
        from codex_agents import learning_source_image, learning_store, project_history
        import inspect
        for module, tables in ((learning_source_image, ('runtime_floor', 'runtime_lineage')),
                               (learning_store, ('runtime_floor', 'runtime_lineage')),
                               (project_history, ('runtime_floor', 'takeover_record', 'takeover_runs',
                                                  'takeover_obligations'))):
            source = inspect.getsource(module)
            for name in tables:
                self.assertIn(name, source)


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
                        with self.assertRaises(TakeoverError) as caught:require_free_locks([path])
                        self.assertEqual('predecessor_lock_inherited',caught.exception.code)
                    child.stdin.close();child.stdin=None;child.communicate(timeout=5)
                    self.assertEqual(0,child.returncode)
                    require_free_locks([path])
                finally:
                    if child.poll() is None:child.kill()
                    child.communicate(timeout=5)


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
        from tests.test_rsf_s3_resumable import open_owner
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
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.orchestration_api import OrchestrationScope
        from types import SimpleNamespace
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


class AccountLineageTests(unittest.TestCase):
    __why_not_scenario__ = 'Archived admitted custody and real current ledger functions; native enrollment is separate.'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-account-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'
        cls.archive.mkdir()
        here = Path(__file__).parent
        packed = subprocess.run(['git', '-C', str(here.parents[2]), 'archive', 'afb6f610',
                                 'packages/codex-agents', 'scripts'], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(cls.archive / 'scripts')
        made = subprocess.run([sys.executable, '-B', str(here / 'b4t_fixture.py'), str(cls.archive),
                               str(cls.source), 'account'], env=env, capture_output=True, text=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        cls.data = json.loads((cls.source / 'manifest.json').read_bytes())

    def setUp(self):
        from codex_agents import learning_store, collector_client
        from codex_agents.result_contracts import ResultLeaseBinding
        self.expected = ResultLeaseBinding(**self.data['expected'])
        self.project = self.expected.project_id
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'state', self.case / 'state')
        for name, value in (('_clock_ms', self.data['now']), ('_capacity_clock_reference', self.data['clock_reference_ns'])):
            patch = mock.patch.object(learning_store.LearningLedger, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.state = learning_store.LearningState(self.case / 'state')
        self.intent = dict(takeover_id='2' * 32, project_id=self.project, recorded_at_ms=self.data['now'],
            predecessor_collector=dict(version='0.8.66', digest=self.expected.runtime_digest),
            predecessor_service=dict(version='0.8.66', digest=self.expected.runtime_digest),
            successor=dict(version=collector_client.PACKAGE_VERSION, digest=collector_client.COLLECTOR_RUNTIME_DIGEST))
        self.ledger = learning_store.LearningLedger(self.state, allow_schema_upgrade=True, takeover=self.intent)
        self.addCleanup(self.ledger.close)

    def snapshot(self):
        return {name: [tuple(row) for row in self.ledger._connection.execute('SELECT * FROM ' + name)]
                for (name,) in self.ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def terminal(self):
        from codex_agents.terminal_delivery import decode_terminal_submission
        return self.ledger._accept_project_terminal(self.project, runtime_digest=self.ledger._fence.digest,
            caller_scope=self.data['caller_scope'], submission=decode_terminal_submission(self.data['submission']),
            storage_binding=self.expected, storage_evidence=bytes.fromhex(self.data['bound']),
            terminal_evidence=bytes.fromhex(self.data['terminal']))

    @requirements('S14b-6')
    def test_S14b_6_takeover_adoption_keeps_the_retention_indexes_of_the_consented_step(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.learning_store import LearningLedger
        from codex_agents import run_retention
        names = ', '.join("'%s'" % n for n in run_retention.RETENTION_INDEX_NAMES)
        query = f"SELECT count(*) FROM sqlite_master WHERE name IN ({names})"
        # setUp's consented open performed the 17 -> 18 step, whose transaction created them (decision 352).
        self.assertEqual(5, self.ledger._connection.execute(query).fetchone()[0])
        root = self.case / 'P'
        root.mkdir()
        project_p = self.ledger.register_project(root)
        self.ledger.close()
        OwnerOperations(self.state, Path('/cli'), root, lambda: self.intent).adopt_ledger(
            dict(self.intent, project_id=project_p))
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.assertEqual(5, self.ledger._connection.execute(query).fetchone()[0])

    @requirements('RS-B-B4t-4', 'RS-B-B4t-8')
    def test_B4t_4_another_projects_child_keeps_running(self):
        from codex_agents.takeover_owner import OwnerOperations
        from codex_agents.account_service import AccountOwnerViews
        from codex_agents.account_takeover import takeover_report
        from codex_agents.learning_store import LearningLedger
        child = subprocess.Popen([sys.executable,'-I','-B','-c',
                                  'import sys; print("ready",flush=True); sys.stdin.read()'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.addCleanup(lambda: child.communicate(timeout=5))
        self.assertEqual('ready',child.stdout.readline().strip())
        root = self.case/'P'; root.mkdir()
        project_p = self.ledger.register_project(root)
        before = [tuple(r) for r in self.ledger._connection.execute('SELECT * FROM active_run_capacity WHERE project_id=?',(self.project,))]
        self.assertTrue(before)
        self.ledger.close()
        ops = OwnerOperations(self.state,Path('/cli'),root,lambda:self.intent)
        ops.adopt_ledger(dict(self.intent,project_id=project_p))
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        after=[tuple(r) for r in self.ledger._connection.execute('SELECT * FROM active_run_capacity WHERE project_id=?',(self.project,))]
        self.assertEqual(before,after)
        self.assertIsNone(child.poll())
        report=takeover_report(AccountOwnerViews(self.ledger),project_p,{})
        self.assertEqual((1,1),(report['other_projects'],report['other_runs']))
        self.assertEqual('0.8.66',report['updates'][0]['version'])
        self.assertEqual(1,report['updates'][0]['children'])
        # Q's own update can now settle exactly that old claim, once.
        self.test_successor_lost_settlement_and_closure_find_the_original_claim_once()
        self.assertIsNotNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity '
            'WHERE project_id=?',(self.project,)).fetchone()[0])
        child.stdin.close(); child.stdin=None
        child.communicate(timeout=5)

    @requirements('RS-B-B4t-5', 'RS-B-B4t-9')
    def test_successor_terminal_uses_original_scope_and_capacity_row_and_missing_lineage_writes_nothing(self):
        from codex_agents.account_claim_contracts import ClaimError
        before = self.snapshot()
        with mock.patch('codex_agents.runtime_lineage.release_versions', return_value={}):
            with self.assertRaises(ClaimError) as caught:
                self.terminal()
        self.assertEqual('claim_terminal_invalid', caught.exception.reason_code)
        self.assertEqual(before, self.snapshot())
        first = self.terminal()
        self.assertTrue(first['receipt_digest'])
        counted = self.snapshot()
        second = self.terminal()
        self.assertTrue(second['known'])
        self.assertEqual(first['receipt_digest'], second['receipt_digest'])
        self.assertEqual(counted, self.snapshot())
        self.assertEqual(1, len(counted['capacity_completions']))

    @requirements('RS-B-B4t-4', 'RS-B-B4t-5', 'RS-B-B4t-9')
    def test_successor_lost_settlement_and_closure_find_the_original_claim_once(self):
        from codex_agents.account_lost_run import settle_lost_run, lost_run_closure
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.claim_storage import verify_pending_storage_evidence
        from codex_agents.history_contracts import AttemptBinding
        from codex_agents.account_claim_contracts import ClaimError
        lease = verify_pending_storage_evidence(self.state.digest_key(), self.expected, bytes.fromhex(self.data['pending']))
        binding = AttemptBinding(**self.data['binding'])
        settle = SettleLostInput(self.expected.run_handle, self.data['identity_digest'],
                                self.expected.claim_request_id, binding.claim_id, lease.lease_digest)
        arguments = dict(runtime_digest=self.ledger._fence.digest, caller_scope=self.data['caller_scope'],
                         settle=settle, storage_binding=self.expected, storage_evidence=bytes.fromhex(self.data['pending']))
        before = self.snapshot()
        with mock.patch('codex_agents.runtime_lineage.release_versions', return_value={}):
            for function, extras in ((settle_lost_run, {}), (lost_run_closure, {'request_digest': 'e' * 64})):
                with self.assertRaises(ClaimError) as caught:
                    function(self.ledger, self.project, **arguments, **extras)
                self.assertEqual('claim_cleanup_invalid', caught.exception.reason_code)
                self.assertEqual(before, self.snapshot())
        settled = settle_lost_run(self.ledger, self.project, **arguments)
        self.assertEqual('settled', settled['state'])
        counted = self.snapshot()
        self.assertTrue(settle_lost_run(self.ledger, self.project, **arguments)['known'])
        closure = lost_run_closure(self.ledger, self.project, **arguments, request_digest='e' * 64)
        self.assertEqual('lost', closure['release_kind'])
        self.assertEqual(counted, self.snapshot())

    @requirements('RS-B-B4t-9')
    def test_earlier_runtime_is_passed_only_at_the_four_approved_call_sites(self):
        from tests.support import ORCHESTRATION_ROOT
        sites = set()
        for path in (ORCHESTRATION_ROOT / 'codex_agents').glob('*.py'):
            tree = ast.parse(path.read_text())
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and any(arg.arg == 'earlier_runtime' for arg in node.keywords):
                        sites.add((path.name, function.name))
                        self.assertTrue(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == 'successor_custody' for call in ast.walk(function)))
        self.assertEqual({('account_lost_run.py', '_known_claim'), ('account_cleanup.py', 'release_capacity'),
                          ('learning_store.py', '_settle_unlaunched'),
                          ('learning_store.py', '_accept_project_terminal')}, sites)


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


class FloorTests(unittest.TestCase):
    __why_not_scenario__ = 'Actual owner acquisitions at a lower build version must change zero database bytes.'

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_current_ledger_open_keeps_the_predecessors_capacity_coverage_refusal(self):
        from codex_agents import learning_store
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            project_root = root / 'project'
            project_root.mkdir()
            state = learning_store.LearningState(root / 'state')
            owner = learning_store.LearningLedger(state)
            project = owner.register_project(project_root)
            with owner._account_transaction():
                owner._connection.execute("INSERT INTO launch_claims VALUES(?,?,?,'claimed',?,?)",
                                           (project, 'a' * 64, 'lost-capacity-row', 1, 'brg_' + 'b' * 32))
            owner.close()
            before = state.database.read_bytes()
            with self.assertRaises(learning_store.LedgerError):
                with closing(learning_store.LearningLedger(state)):
                    pass
            self.assertEqual(before, state.database.read_bytes())

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_a_B4t_build_below_the_floor_cannot_advance_either_epoch(self):
        from codex_agents import collector_client, project_history, learning_store
        from codex_agents.history_contracts import HistoryError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root = Path(folder).resolve()
            directory = root / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '9' * 32
            history = project_history.ProjectHistory(directory, project)
            self.assertEqual(23, history._connection.execute('PRAGMA user_version').fetchone()[0])
            history.close()
            state = learning_store.LearningState(root / 'state')
            ledger = learning_store.LearningLedger(state)
            self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])
            ledger.close()
            before = {path: path.read_bytes() for path in (directory / 'history.sqlite3', state.database)}
            with mock.patch.object(collector_client, 'PACKAGE_VERSION', '0.8.66'):
                with self.assertRaises(HistoryError):
                    project_history.ProjectHistory(directory, project)
                with self.assertRaises(learning_store.LedgerError):
                    learning_store.LearningLedger(state)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    @requirements('RS-B-B4t-3', 'RS-B-B4t-9')
    def test_a_store_written_by_this_build_refuses_0868_and_0869(self):
        # IP-D (decision 370): the takeover floor this build writes is its own PACKAGE_VERSION (0.8.70), so the
        # released 0.8.68 and 0.8.69 builds refuse a store that already carries the unverified marker.
        from codex_agents import collector_client, project_history
        from codex_agents.history_contracts import HistoryError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            directory = Path(folder).resolve() / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '8' * 32
            project_history.ProjectHistory(directory, project).close()
            history = project_history.ProjectHistory(directory, project)
            self.assertEqual([(1, '0.8.70')], [tuple(r) for r in history._connection.execute('SELECT * FROM runtime_floor')])
            history.close()
            before = (directory / 'history.sqlite3').read_bytes()
            for older in ('0.8.68', '0.8.69'):
                with mock.patch.object(collector_client, 'PACKAGE_VERSION', older):
                    with self.assertRaises(HistoryError):
                        project_history.ProjectHistory(directory, project)
            self.assertEqual(before, (directory / 'history.sqlite3').read_bytes())

    @requirements('RS-B-B4t-3')
    def test_archived_0866_and_0832_cannot_reopen_either_published_store(self):
        from codex_agents import project_history, learning_store
        with tempfile.TemporaryDirectory(prefix='b4t-old-', dir='/tmp') as folder:
            root = Path(folder).resolve()
            directory = root / 'history'
            directory.mkdir(mode=0o700)
            project = 'prj_' + '8' * 32
            project_history.ProjectHistory(directory, project).close()
            state = learning_store.LearningState(root / 'state')
            learning_store.LearningLedger(state).close()
            top = Path(__file__).parents[3]
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
            for revision in ('afb6f610', 'v0.3.4'):
                archive = root / revision
                archive.mkdir()
                packed = subprocess.run(['git', '-C', str(top), 'archive', revision,
                                         'packages/codex-agents'], capture_output=True, check=True)
                subprocess.run(['tar', '-x', '-C', str(archive)], input=packed.stdout, check=True)
                runtime = archive / 'packages/codex-agents/native-package/.codex/orchestration'
                for kind in ('ledger', 'project'):
                    with self.subTest(revision=revision, kind=kind):
                        database = state.database if kind == 'ledger' else directory / 'history.sqlite3'
                        before = database.read_bytes()
                        code = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]=='ledger':
 from codex_agents.learning_store import LearningState,LearningLedger
 owner=LearningLedger(LearningState(Path(sys.argv[3])))
else:
 from codex_agents.project_history import ProjectHistory
 owner=ProjectHistory(Path(sys.argv[3]),sys.argv[4])
owner.close()
'''
                        done = subprocess.run([sys.executable, '-B', '-c', code, str(runtime), kind,
                            str(state.root if kind == 'ledger' else directory), project],
                            env=env, capture_output=True, text=True, timeout=15)
                        self.assertNotEqual(0, done.returncode)
                        self.assertRegex(done.stderr, 'newer|incompatible')
                        self.assertEqual(before, database.read_bytes())


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
            project.mkdir(); other.mkdir()
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
        wizard=Wizard.__new__(Wizard); wizard.t=mock.Mock()
        wizard.choice_needed=mock.Mock(return_value=0);wizard.begin_step=mock.Mock()
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


class ChainedOutboxTests(unittest.TestCase):
    __why_not_scenario__ = 'Two real archived claims, both owner stores and authenticated B4s closures; no live service.'
    fixture_mode = 'chain'
    setUpClass = classmethod(StoreTests.setUpClass.__func__)

    @contextmanager
    def build_identity(self, digest, version):
        from codex_agents import collector_client, project_history, project_takeover, learning_store, account_lost_run
        from codex_agents.runtime_lineage import release_versions
        releases = dict(release_versions(), **{B: '0.8.68'})
        releases[self.data['runtime_digest']] = self.data['version']
        with ExitStack() as stack:
            for module in (collector_client, project_history, project_takeover, learning_store, account_lost_run):
                stack.enter_context(mock.patch.object(module, 'COLLECTOR_RUNTIME_DIGEST', digest))
                if hasattr(module, 'PACKAGE_VERSION'):
                    stack.enter_context(mock.patch.object(module, 'PACKAGE_VERSION', version))
            stack.enter_context(mock.patch('codex_agents.runtime_lineage.release_versions', return_value=releases))
            yield

    def owners(self, root, predecessor, successor, number):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.learning_store import LearningState, LearningLedger
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        data = self.data
        intent = dict(takeover_id=str(number) * 32, project_id=data['project'], recorded_at_ms=data['now'],
            predecessor_collector=predecessor, predecessor_service=predecessor, successor=successor,
            runs=[dict(run_handle=r['binding']['run_handle'], attempt=r['binding']['attempt'],
                       bridge_run_id=r['binding']['claim_id']) for r in data['runs']])
        history = ProjectHistory(root / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
            result_clock_ms=lambda: data['now'], allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(history.close)
        ledger = LearningLedger(LearningState(root / 'state'), allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(ledger.close)
        return history, ledger

    def send(self, history, ledger, run, digest):
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.lost_run_contracts import SettleLostInput
        from codex_agents.account_lost_run import settle_lost_run, lost_run_closure
        scope = ResultLeaseBinding(**run['scope'])
        offered = history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                 run['payload'], runtime_digest=digest)
        arguments = dict(runtime_digest=digest, caller_scope='a' * 64, settle=SettleLostInput(**run['payload']),
                         storage_binding=scope, storage_evidence=bytes.fromhex(run['pending']))
        reply = settle_lost_run(ledger, self.data['project'], **arguments)
        self.assertEqual('settled', reply['state'])
        raw = lost_run_closure(ledger, self.data['project'], request_digest=offered['request_digest'], **arguments)
        from codex_agents.learning_contracts import canonical_json
        return scope, canonical_json(raw)

    @requirements('RS-B-B4t-8', 'RS-B-B4t-9')
    def test_a_corrupt_original_outbox_is_reported_unverifiable_and_kept(self):
        from codex_agents import learning_store
        from codex_agents.project_takeover import takeover_report
        from codex_agents.result_contracts import ResultLeaseBinding
        from codex_agents.history_contracts import HistoryError
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        run = self.data['runs'][0]
        with closing(sqlite3.connect(root / 'history/history.sqlite3')) as db:
            db.execute("UPDATE project_holds SET request_digest=? WHERE kind='reservation' AND identity=?",
                       ('f' * 64, run['binding']['claim_id']))
            db.commit()
        with self.build_identity(B, '0.8.68'), \
                mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=self.data['now']), \
                mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                  return_value=self.data['clock_reference_ns']):
            control = Path(tempfile.mkdtemp(dir=self.root))
            shutil.copytree(self.source / 'history', control / 'history')
            shutil.copytree(self.source / 'state', control / 'state')
            intact, control_ledger = self.owners(control,
                dict(version=self.data['version'], digest=self.data['runtime_digest']), dict(version='0.8.68', digest=B), 3)
            baseline = takeover_report(intact)['refused_unverifiable']
            intact.close(); control_ledger.close()
            history, _ = self.owners(root, dict(version=self.data['version'], digest=self.data['runtime_digest']),
                                     dict(version='0.8.68', digest=B), 3)
            before = [tuple(r) for r in history._connection.execute('SELECT * FROM project_holds ORDER BY rowid')]
            self.assertEqual(baseline + 1, takeover_report(history)['refused_unverifiable'])
            scope = ResultLeaseBinding(**run['scope'])
            with self.assertRaises(HistoryError):
                history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                run['payload'], runtime_digest=B)
            self.assertEqual(before, [tuple(r) for r in history._connection.execute('SELECT * FROM project_holds ORDER BY rowid')])

    def unverified_case(self, *, corrupt):
        """0.8.70 UH: the corrupt-original fixture above (or the intact one), adopted by B, with only the first run's
        holds made due so the holds check would look at them."""
        from codex_agents import learning_store
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        run = self.data['runs'][0]
        if corrupt:
            with closing(sqlite3.connect(root / 'history/history.sqlite3')) as db:
                db.execute("UPDATE project_holds SET request_digest=? WHERE kind='reservation' AND identity=?",
                           ('f' * 64, run['binding']['claim_id']))
                db.commit()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.build_identity(B, '0.8.68'))
        stack.enter_context(mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=self.data['now']))
        stack.enter_context(mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                              return_value=self.data['clock_reference_ns']))
        history, _ = self.owners(root, dict(version=self.data['version'], digest=self.data['runtime_digest']),
                                 dict(version='0.8.68', digest=B), 3)
        with history._transaction():
            history._connection.execute(
                "UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 WHERE state='releasing' AND "
                "((kind='reservation' AND identity=?) OR (kind='lease' AND identity=?))",
                (run['binding']['claim_id'], run['binding']['run_handle'] + ':' + str(run['binding']['attempt'])))
            history._connection.execute(
                "UPDATE project_holds SET due_at_ms=NULL,next_check_ms=9000000000000 WHERE NOT "
                "((kind='reservation' AND identity=?) OR (kind='lease' AND identity=?))",
                (run['binding']['claim_id'], run['binding']['run_handle'] + ':' + str(run['binding']['attempt'])))
        return history, root, run

    def holds_codes(self, root, now=61_001):
        import time
        from codex_agents import health_holds
        from codex_agents.health_monitor import CheckContext
        check = health_holds.HoldsCheck(None, sweep_interval_seconds=60)
        with closing(health_holds.open_reader(root / 'history/history.sqlite3')) as reader:
            found = check.check_project(reader, self.data['project'], CheckContext(time.monotonic() + 5, now, 60_000),
                                        bytes.fromhex(self.data['key']))
        return {f.reason_code: f for f in found}

    @requirements('UH-1', 'UH-2')
    def test_UH_a_hold_the_takeover_left_unverified_is_its_own_plain_finding_not_overdue(self):
        history, root, run = self.unverified_case(corrupt=True)
        self.assertEqual('releasing', history._connection.execute(
            "SELECT state FROM project_holds WHERE kind='reservation' AND identity=?",
            (run['binding']['claim_id'],)).fetchone()[0])
        self.assertEqual('unverified', history._connection.execute(
            "SELECT original_request_digest FROM takeover_obligations WHERE kind='reservation' AND identity=?",
            (run['binding']['claim_id'],)).fetchone()[0])
        found = self.holds_codes(root)
        self.assertNotIn('hold_overdue', found)
        self.assertIn('hold_unverified_takeover', found)
        text = found['hold_unverified_takeover'].text
        for words in ('could not be checked', 'harmless', 'no run slot', 'will not clear by itself'):
            self.assertIn(words, text)

    @requirements('UH-5')
    def test_UH_a_verified_takeover_hold_that_is_overdue_is_still_reported_overdue(self):
        history, root, run = self.unverified_case(corrupt=False)
        found = self.holds_codes(root)
        self.assertIn('hold_overdue', found)
        self.assertNotIn('hold_unverified_takeover', found)

    @requirements('UH-3')
    def test_UH_the_writer_says_which_runs_the_takeover_left_unverified(self):
        corrupt, _, run = self.unverified_case(corrupt=True)
        scope = run['scope']
        self.assertIs(True, corrupt.takeover_unverified(scope['run_handle'], scope['attempt'], run['binding']['claim_id']))
        other = self.data['runs'][1]
        self.assertIs(False, corrupt.takeover_unverified(other['scope']['run_handle'], other['scope']['attempt'],
                                                         other['binding']['claim_id']))
        intact, _, run = self.unverified_case(corrupt=False)
        self.assertIs(False, intact.takeover_unverified(scope['run_handle'], scope['attempt'], run['binding']['claim_id']))

    def reopen(self, root):
        """A later start of the same build: no takeover intent, the store as it was left."""
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        data = self.data
        history = ProjectHistory(root / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
            result_clock_ms=lambda: data['now'], allow_legacy_orchestration_reference=True)
        self.addCleanup(history.close)
        return history

    @requirements('UH-1', 'UH-2', 'UH-6')
    def test_UH_6_the_unverified_fact_written_at_adoption_survives_completion_maintenance_restart_and_a_copy(self):
        """Review r1 HIGH 1 (decision 367): the takeover records the hold it refused in its own adoption transaction;
        completing the journal, the ordinary maintenance pass, a restart and a copied (backed-up, restored or moved)
        store all keep it, so health keeps the plain finding and the writer keeps answering 'unverified'."""
        from codex_agents.project_takeover import complete_takeovers
        history, root, run = self.unverified_case(corrupt=True)
        claim, scope = run['binding']['claim_id'], run['scope']

        def still_unverified(store, where):
            self.assertIs(True, store.takeover_unverified(scope['run_handle'], scope['attempt'], claim))
            found = self.holds_codes(where)
            self.assertNotIn('hold_overdue', found)
            self.assertIn('hold_unverified_takeover', found)
        complete_takeovers(history)
        self.assertEqual({'complete'}, {row[0] for row in history._connection.execute('SELECT state FROM takeover_record')})
        still_unverified(history, root)
        history.expire_results(limit=10)
        still_unverified(history, root)
        history.close()
        restarted = self.reopen(root)
        still_unverified(restarted, root)
        restarted.close()
        copy = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(root / 'history', copy / 'history')
        still_unverified(self.reopen(copy), copy)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-5', 'RS-B-B4t-9', 'RS-B-B4t-10')
    def test_two_archived_outboxes_survive_A_B_C_with_one_effect_and_immutable_originals(self):
        from codex_agents import learning_store
        from codex_agents.history_contracts import HistoryError
        from codex_agents.result_contracts import ResultLeaseBinding
        root = Path(tempfile.mkdtemp(dir=self.root))
        shutil.copytree(self.source / 'history', root / 'history')
        shutil.copytree(self.source / 'state', root / 'state')
        data = self.data
        def rows(owner, table):
            return [tuple(r) for r in owner._connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
        with mock.patch.object(learning_store.LearningLedger, '_clock_ms', return_value=data['now']), \
                mock.patch.object(learning_store.LearningLedger, '_capacity_clock_reference',
                                  return_value=data['clock_reference_ns']):
            with self.build_identity(B, '0.8.68'):
                history, ledger = self.owners(root, dict(version=data['version'], digest=data['runtime_digest']),
                                              dict(version='0.8.68', digest=B), 3)
                self.assertEqual(2, len(rows(history, 'takeover_obligations')))
                obligations, bindings = rows(history, 'takeover_obligations'), rows(history, 'result_leases')
                originals = [tuple(r) for r in history._connection.execute(
                    "SELECT identity,request_id,request_digest FROM project_holds WHERE kind='reservation' ORDER BY identity")]
                scope, raw = self.send(history, ledger, data['runs'][0], B)
                history.record_account_settlement(scope, raw)
                self.send(history, ledger, data['runs'][1], B)  # accepted; reply never recorded by B
                releases = [tuple(r) for r in ledger._connection.execute(
                    'SELECT handle_digest,released_at_ms,release_digest FROM active_run_capacity ORDER BY handle_digest')]
                history.close(); ledger.close()
            with self.build_identity(C, '0.8.69'):
                history, ledger = self.owners(root, dict(version='0.8.68', digest=B), dict(version='0.8.69', digest=C), 4)
                from codex_agents.project_takeover import takeover_report
                report = takeover_report(history)
                self.assertEqual((1, 1), (report['lost'], report['lost_held']))
                self.assertEqual(obligations, rows(history, 'takeover_obligations'))
                self.assertEqual(bindings, rows(history, 'result_leases'))
                self.assertEqual(originals, [tuple(r) for r in history._connection.execute(
                    "SELECT identity,request_id,request_digest FROM project_holds WHERE kind='reservation' ORDER BY identity")])
                run = data['runs'][1]
                # Missing either level's final edge never dispatches or frees custody.
                with history._transaction():
                    saved = history._connection.execute('SELECT * FROM takeover_record WHERE predecessor_digest=?', (B,)).fetchone()
                    history._connection.execute('UPDATE takeover_record SET predecessor_digest=? WHERE predecessor_digest=?',
                                                ('d' * 64, B))
                before = rows(history, 'project_holds')
                scope = ResultLeaseBinding(**run['scope'])
                with self.assertRaises(HistoryError):
                    history.open_account_settlement(scope.run_handle, scope.attempt, run['request_id'],
                                                    run['payload'], runtime_digest=C)
                self.assertEqual(before, rows(history, 'project_holds'))
                with history._transaction():
                    history._connection.execute('UPDATE takeover_record SET predecessor_digest=? WHERE takeover_id=?',
                                                (B, saved['takeover_id']))
                from codex_agents.account_claim_contracts import ClaimError
                with ledger._account_transaction():
                    edge = ledger._connection.execute('SELECT * FROM runtime_lineage WHERE predecessor_digest=?', (B,)).fetchone()
                    ledger._connection.execute('DELETE FROM runtime_lineage WHERE predecessor_digest=?', (B,))
                count = ledger._connection.total_changes
                with self.assertRaises(ClaimError):
                    self.send(history, ledger, run, C)
                self.assertEqual(count, ledger._connection.total_changes)
                self.assertEqual(before, rows(history, 'project_holds'))
                with ledger._account_transaction():
                    ledger._connection.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,?,?,?)', tuple(edge))
                for _ in range(2):
                    scope, raw = self.send(history, ledger, run, C)
                    history.record_account_settlement(scope, raw)
                self.assertEqual(releases, [tuple(r) for r in ledger._connection.execute(
                    'SELECT handle_digest,released_at_ms,release_digest FROM active_run_capacity ORDER BY handle_digest')])
                self.assertEqual(2, len(rows(history, 'project_account_settlements')))
                self.assertEqual(obligations, rows(history, 'takeover_obligations'))


class MemoryTakeoverTests(unittest.TestCase):
    __why_not_scenario__ = 'A SIGKILLed archived 0.8.67 fixture leaves genuine memory holds and lock inodes.'
    build_identity = ChainedOutboxTests.build_identity
    fixture_mode = 'memory'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='b4t-memory-', dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.archive = cls.root / 'old'; cls.archive.mkdir()
        packed = subprocess.run(['git', '-C', str(Path(__file__).parents[3]), 'archive', 'ce81bd96',
                                 'packages/codex-agents', 'scripts'],
                                 capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(cls.archive)], input=packed.stdout, check=True)

    def setUp(self):
        import select
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'PYTHON', 'CLAUDE'))}
        env['PYTHONPATH'] = str(self.archive / 'scripts')
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('b4t_fixture.py')),
            str(self.archive), str(self.case), self.fixture_mode,
            'inside' if 'inside' in self._testMethodName else 'after'],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd='/')
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertTrue(select.select([child.stdout], [], [], 10)[0])
        ready = child.stdout.readline().strip()
        if ready != self.fixture_mode + '-custody-ready':
            child.kill(); _, error = child.communicate(timeout=5)
            self.fail(ready + error)
        child.kill(); child.communicate(timeout=5)
        self.assertLess(child.returncode, 0)
        self.data = json.loads((self.case / 'manifest.json').read_bytes())

    def open(self):
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
        from codex_agents.orchestration_api import OrchestrationLimits
        data = self.data
        old = dict(version=data['version'], digest=data['runtime_digest'])
        intent = dict(takeover_id='5' * 32, project_id=data['project'], recorded_at_ms=data['now'],
            predecessor_service=old, predecessor_collector=old, successor=dict(version='0.8.68', digest=B), runs=[])
        store = ProjectHistory(self.case / 'history', data['project'], result_key=bytes.fromhex(data['key']),
            result_clock_ms=lambda: data['now'], orchestration_limits=OrchestrationLimits(8, 12),
            result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000),
            allow_schema_upgrade=True, takeover=intent)
        self.addCleanup(store.close)
        return store

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_archived_memory_holds_recover_only_with_positive_inode_evidence_and_retry_the_removal(self):
        from codex_agents import memory_holds, codex_preparation, bridge
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            rows = history._connection.execute("SELECT kind,state,release_kind,evidence FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind").fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(tuple(r)[1:3] == ('released', 'service_stopped') for r in rows))
            pending = list(memory_holds.pending_removals(history, 'e' * 32))
            self.assertEqual(1, len(pending))
            identity, workspace = pending[0]
            with mock.patch.object(bridge, 'scratch_base', return_value=str(self.case.parent)), \
                    mock.patch.object(bridge, 'SCRATCH_CONTAINER', self.case.name):
                with mock.patch.object(codex_preparation, '_remove', return_value=False):
                    self.assertIs(False, codex_preparation.remove_recorded_workspace(workspace))
                self.assertEqual(pending, list(memory_holds.pending_removals(history, 'e' * 32)))
                self.assertTrue(Path(workspace).exists())
                self.assertIs(True, codex_preparation.remove_recorded_workspace(workspace))
            memory_holds.removal_proven(history, identity)
            self.assertEqual([], list(memory_holds.pending_removals(history, 'e' * 32)))
            self.assertFalse(Path(workspace).exists())
            before = history._connection.total_changes
            memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            self.assertEqual(before, history._connection.total_changes)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-9')
    def test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds(self):
        from codex_agents import memory_holds
        (self.case / 'launches' / (self.data['instance'] + '.lock')).unlink()
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            before = [tuple(r) for r in history._connection.execute("SELECT * FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind")]
            self.assertEqual(2, len(before))
            for _ in range(2):
                memory_holds.recover(history, self.case / 'launches', 'e' * 32)
            after = [tuple(r) for r in history._connection.execute("SELECT * FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') ORDER BY kind")]
            self.assertEqual([row[:-1] for row in before], [row[:-1] for row in after])
            for prior, current in zip(before, after):
                facts, observed = json.loads(prior[-1]), json.loads(current[-1])
                self.assertTrue(all(observed.get(name) == value for name, value in facts.items() if name != 'reason'))
            self.assertIn('instance_unproven', after[0][-1])
            self.assertEqual(2, history._connection.execute("SELECT COUNT(*) FROM project_holds "
                "WHERE kind IN ('driver_entry','preparation') AND state='held'").fetchone()[0])


class RolloverTakeoverTests(unittest.TestCase):
    __why_not_scenario__ = 'SIGKILL inside/after an archived U1 transaction, then current authenticated lifecycle handler.'
    fixture_mode = 'rollover'
    setUpClass = classmethod(MemoryTakeoverTests.setUpClass.__func__)
    setUp = MemoryTakeoverTests.setUp
    open = MemoryTakeoverTests.open
    build_identity = ChainedOutboxTests.build_identity

    def replay(self, committed):
        from codex_agents.orchestration_api import (OrchestrationScope, OrchestrationRequest,
            encode_orchestration_request, decode_orchestration_response, orchestration_request_digest)
        from codex_agents.orchestration_service import OrchestrationService, OrchestrationAuthorization
        from codex_agents.history_contracts import HistoryError
        with self.build_identity(B, '0.8.68'):
            history = self.open()
            self.assertEqual(2 if committed else 1, history._connection.execute(
                'SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
            scope = OrchestrationScope(**self.data['scope'])
            request = OrchestrationRequest(1, 'orchestration.rollover', 'handoff', scope, self.data['payload'])
            token = object()
            from types import SimpleNamespace
            from codex_agents.service_host import ProjectServiceHost
            from codex_agents.project_takeover import adopted_orchestration_scope
            host = ProjectServiceHost.__new__(ProjectServiceHost)
            host._scope = replace(scope, runtime_digest=B)
            host._frontend = mock.Mock()
            enrolled = SimpleNamespace(role='host', host_client='codex-host-v1')
            entry = SimpleNamespace(kind='project', enrolled=enrolled)
            lane = SimpleNamespace(adopted_orchestration_scope=lambda original, handle, client:
                                   adopted_orchestration_scope(history, original, handle, client))
            host._stored = lambda read: read(lane)
            def authorize(offered, context):
                host._frontend.context.return_value = entry if context is token else None
                return host._authorize_orchestration(offered, context)
            service = OrchestrationService(history.project_id, B, lambda: history,
                                           authorizer=authorize, close_history=False)
            self.addCleanup(service.close)
            encoded = encode_orchestration_request(request)
            first = decode_orchestration_response(service.handle(encoded, token), request)
            self.assertEqual(committed, first['known'])
            self.assertTrue(first['rolled_over'])
            again = decode_orchestration_response(service.handle(encoded, token), request)
            self.assertTrue(again['known'])
            self.assertEqual(first['current'], again['current'])
            self.assertEqual(2, history._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
            self.assertEqual(1, history._orchestration_owner()._open_count())
            before = history._connection.total_changes
            with self.assertRaises(HistoryError):
                decode_orchestration_response(service.handle(encoded, object()), request)
            self.assertEqual(before, history._connection.total_changes)
            with history._transaction():
                history._connection.execute('UPDATE takeover_record SET predecessor_digest=?', ('d' * 64,))
            before = history._connection.total_changes
            with self.assertRaises(HistoryError):
                decode_orchestration_response(service.handle(encoded, token), request)
            self.assertEqual(before, history._connection.total_changes)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_rollover_killed_inside_its_transaction_replays_both_ends_after_takeover(self):
        self.replay(False)

    @requirements('RS-B-B4t-4', 'RS-B-B4t-10')
    def test_rollover_killed_after_commit_returns_the_same_successor_after_takeover(self):
        self.replay(True)


if __name__ == '__main__':
    unittest.main()


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
        from types import SimpleNamespace
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
    def test_B4t_A1_idle_predecessor_updates_without_process_proof(self):
        # Reproduces 146955fc's unconditional idle refusal with the same EPERM result.
        from codex_agents.takeover_owner import OwnerOperations
        with self.plan_case() as (preflight, state, project):
            plan = preflight.read_plan(state, project)
            self.assertEqual('hint', plan['report']['service_stop'])
            self.assertTrue(plan['needed'])
            old = plan['intent']['predecessor_service']
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
                with self.assertRaises(TakeoverError) as caught:ops.stop_service(intent)
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
            lock=directory/'hint.lock';lock.touch(mode=0o600)
            fd=os.open(lock,os.O_RDWR); self.addCleanup(os.close,fd)
            fcntl.flock(fd,fcntl.LOCK_EX)
            ops=OwnerOperations(state,Path('/cli'),project,lambda:intent)
            with mock.patch('codex_agents.takeover_owner.time.monotonic',side_effect=[0,6]):
                with self.assertRaises(TakeoverError) as caught:
                    ops.withdraw_hint(intent)
                self.assertEqual('service_stop_failed',caught.exception.code)
            state.runtime_lock.touch(mode=0o600,exist_ok=True)
            owner_fd=os.open(state.runtime_lock,os.O_RDWR); self.addCleanup(os.close,owner_fd)
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
            root=Path(folder).resolve(); old=root/'old';old.mkdir(); project=root/'project';project.mkdir()
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
                root=Path(folder).resolve(); journal=TakeoverJournal(root)
                intent=Operations(root).preflight();intent['prior_authorization']=b'x'*1024
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
            root=Path(folder).resolve();intent=Operations(root).preflight()
            intent['predecessor_service']['entry']='/'+'x'*4095
            with self.assertRaises(JournalError):
                TakeoverJournal(root).create(intent,[])
            self.assertFalse((root/'update-takeover.json').exists())


    @requirements('RS-B-B4t-9','RS-B-B4t-10')
    def test_unbounded_identity_fields_refuse_before_journal_creation(self):
        from codex_agents.takeover_journal import TakeoverJournal,JournalError
        for change in (dict(unknown='x'),dict(pid=2**63),dict(birth='x'*33),dict(birth='\x01'*32),dict(version='١.١.١'),dict(service_generation=2**63)):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                root=Path(folder).resolve();intent=Operations(root).preflight()
                intent['predecessor_service'].update(change)
                with self.assertRaises(JournalError):
                    TakeoverJournal(root).create(intent,[])
                self.assertFalse((root/'update-takeover.json').exists())


    @requirements('RS-B-B4t-9','RS-B-B4t-10')
    def test_later_stop_generation_and_stopped_count_have_fixed_integer_widths(self):
        from codex_agents.takeover_journal import TakeoverJournal,MAX_INTEGER,JournalError
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            root=Path(folder).resolve();journal=TakeoverJournal(root);intent=Operations(root).preflight()
            intent['predecessor_service']['stop_op']=True
            document=journal.create(intent,[run_entry(i) for i in range(64)])
            with self.subTest(field='future generation'):
                self.assertEqual(MAX_INTEGER,json.loads(journal.widest(document))['predecessor_service']['service_generation'])
            with self.subTest(field='stopped count'):
                document.update(runs_unlisted=MAX_INTEGER,stopped_children=MAX_INTEGER+1)
                with self.assertRaises(JournalError):journal.write(document)



class RetentionA1Tests(unittest.TestCase):
    __why_not_scenario__ = 'Real owner retention transactions on disposable SQLite connections; no transport.'

    def project(self):
        from codex_agents.takeover_schema import PROJECT_SCHEMA
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(PROJECT_SCHEMA + "CREATE TABLE project_holds(kind,identity,state,evidence);"
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
        db.execute("INSERT INTO project_holds VALUES('reservation','held','releasing','refused_unverifiable')")
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
                root=Path(folder).resolve();state=root/'state';state.mkdir(mode=0o700);project=root/'project';project.mkdir()
                entry=root/'project_host.py';lock=state/'history.lock'
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
                    kernel=Kernel();pin=pin_predecessor(child.pid,entry=entry,state_root=state,project_root=project,kernel=kernel)
                    with self.assertRaises(TakeoverError):require_free_locks([lock])
                    def execute():
                        child.stdin.write('exec\n');child.stdin.flush()
                        self.assertTrue(select.select([child.stdout],[],[],10)[0])
                        self.assertEqual('new '+str(child.pid),child.stdout.readline().strip())
                        with self.assertRaises(TakeoverError):require_free_locks([lock])
                    sent=[];real_send=kernel.send
                    if before:
                        execute()
                        with mock.patch.object(kernel,'send') as send:
                            with self.assertRaises(TakeoverError):pin.stop()
                            send.assert_not_called()
                        pin=None
                    else:
                        def send(handle,pid,number):
                            execute()  # hook strictly after I4, before signal
                            sent.append(pid)
                            real_send(handle,pid,number)
                        with mock.patch.object(kernel,'send',side_effect=send):pin.stop()
                        pin=None
                        child.wait(timeout=5)
                        self.assertEqual(0,child.returncode)
                        self.assertEqual([child.pid],sent)
                        require_free_locks([lock])
                finally:
                    if pin is not None:pin.close()
                    if child.poll() is None:child.kill()
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
                    tree=ast.parse(path.read_bytes())
                    parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
                    for node in ast.walk(tree):
                        if isinstance(node,ast.ImportFrom) and (node.module or '').endswith('signature_process'):
                            self.assertFalse({alias.name for alias in node.names}&{'main','_verify'},str(path))
                        if not isinstance(node,ast.Call):continue
                        if isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='os' and node.func.attr.startswith('exec'):
                            parent=node
                            while parent in parents and not isinstance(parent,ast.FunctionDef):parent=parents[parent]
                            sites.add((path.name,getattr(parent,'name','<module>')))
                        if path.name=='signature_process.py' and isinstance(node.func,ast.Name) and node.func.id=='_verify':
                            parent=node
                            while parent in parents and not isinstance(parent,ast.FunctionDef):parent=parents[parent]
                            self.assertEqual('main',parent.name)
                self.assertEqual({('cli.py','main'),('signature_process.py','_verify')},sites)
                execution=(runtime/'codex_agents/signature_execution.py').read_text()
                self.assertIn('from codex_agents.signature_process import main',execution)
                self.assertIn('_BOOTSTRAP',execution)
                self.assertIn('create_child(' if revision=='afb6f610' else 'subprocess.Popen(',execution)
