"""HM-2a: leftover CoCoLo preparation folders (docs/requirements/s11-hm2a.md; plan step S11, SW-8, decision 309).

Requirements (each maps to its tests in docs/requirements/s11-hm2a.md):
- HM2A-1 An orphan `prepare-*` folder (no live owner, no pending record) is removed and reported fixed.
- HM2A-2 A live driver's folder is kept (and a young one).
- HM2A-3 A symlink, foreign-owned folder or file is reported, never followed or removed.
- HM2A-4 A folder pending under decision 309 is left to the driver (no double removal).
- HM2A-5 Path safety equals `codex_preparation.remove_recorded_workspace`.
- HM2A-6 Reads and work are bounded; each count limit has a past-the-limit test.
- HM2A-7 Records that cannot be read: nothing is removed, and it is reported.
- HM2A-8 Plain, closed, path-free owner wording.
- HM2A-9 No ledger, history or preparation-state access by the module.

The module is imported inside each test, so before the code exists every test fails by itself.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import SHORT_TMP

from codex_agents import health_monitor as hm

PROJECT = 'prj_' + 'a' * 32
OTHER_PROJECT = 'prj_' + 'b' * 32
DAY_SECONDS = 86_400


def workspaces():
    from codex_agents import health_workspaces
    return health_workspaces


def context(now_ms=None, seconds=30.0):
    return hm.CheckContext(deadline=time.monotonic() + seconds,
                           now_ms=int(time.time() * 1000) if now_ms is None else now_ms, since_last_pass_ms=None)


class Case(unittest.TestCase):
    """A scratch container with folders, and a check wired to readers the test controls."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.base = Path(os.path.realpath(folder.name))
        os.chmod(self.base, 0o700)
        self.container = self.base / 'codex-agents-scratch'
        self.container.mkdir(mode=0o700)
        self.outside = self.base / 'elsewhere'
        self.outside.mkdir(mode=0o700)
        self.live: set | None = set()
        self.pending: set | None = set()
        self.removed: list = []

    def folder(self, name=None, *, age_days=2, contents=True) -> Path:
        path = self.container / (name or 'prepare-' + os.urandom(4).hex())
        path.mkdir(mode=0o700)
        if contents:
            for child in ('export', 'control', 'tmp'):
                (path / child).mkdir(mode=0o700)
            (path / 'export' / 'file.txt').write_text('data')
        old = time.time() - age_days * DAY_SECONDS
        os.utime(path, (old, old))
        return path

    def check(self, **options):
        defaults = {'container': self.container, 'live': lambda: self.live, 'pending': lambda: self.pending}
        return workspaces().WorkspaceCheck(**(defaults | options))

    def run_check(self, check=None, **context_options):
        return (check or self.check()).run(context(**context_options))

    @staticmethod
    def reasons(found):
        return sorted(item.reason_code for item in found)


class OrphanTests(Case):
    def test_HM2A_1_an_orphan_folder_is_removed_and_reported_fixed(self):
        orphan, keeper = self.folder(), self.folder()
        self.live = {str(keeper)}
        found = self.run_check()
        self.assertFalse(os.path.lexists(orphan))
        self.assertTrue(keeper.is_dir())
        self.assertEqual(['orphan_removed'], self.reasons(found))
        self.assertEqual(('fixed', 'machine', orphan.name), (found[0].outcome, found[0].scope, found[0].subject))

    def test_HM2A_1_a_removal_that_does_not_prove_the_folder_gone_is_reported_and_retried(self):
        orphan = self.folder()
        attempts = []

        def refuse(path):
            attempts.append(path)  # a remover that raises or returns proves nothing: only absence does

        check = self.check(remove=refuse)
        found = self.run_check(check)
        self.assertEqual(['orphan_removal_failed'], self.reasons(found))
        self.assertEqual('reported', found[0].outcome)
        self.assertTrue(orphan.is_dir())
        self.run_check(check)
        self.assertEqual([orphan, orphan], attempts)  # tried again at the next pass
        real = self.check()
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check(real)))
        self.assertFalse(os.path.lexists(orphan))


class LiveOwnerTests(Case):
    def journal(self, project, workspace, *, name='1'):
        from codex_agents.launch_journal import LaunchJournal
        directory = self.state / 'projects' / project / 'launches'
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        journal = LaunchJournal(directory)
        self.addCleanup(journal.release)
        journal.claimed('brg_' + name * 32, run_handle='hrn_' + 'a' * 48, attempt=1, host_client='codex-host-v1',
                        lease={'project_id': project}, identity_digest='c' * 64, claim_request_id='claim-' + 'd' * 32,
                        storage_evidence=b'{"evidence":1}', workspace=workspace)
        return journal

    def test_HM2A_2_a_folder_named_by_a_launch_journal_entry_is_kept(self):
        self.state = self.base / 'state'
        owned, other_owned, orphan = self.folder(), self.folder(), self.folder()
        self.journal(PROJECT, str(owned), name='1')
        self.journal(OTHER_PROJECT, str(other_owned), name='2')
        live = workspaces().journal_live_workspaces(self.state)
        self.assertEqual({str(owned), str(other_owned)}, live())
        found = self.run_check(self.check(live=live))
        self.assertTrue(owned.is_dir() and other_owned.is_dir())
        self.assertFalse(os.path.lexists(orphan))
        self.assertEqual(['orphan_removed'], self.reasons(found))

    def test_HM2A_2_a_young_folder_is_kept_and_not_reported(self):
        young = self.folder(age_days=0)
        found = self.run_check()
        self.assertEqual([], found)
        self.assertTrue(young.is_dir())
        # The same folder, once it is older than the minimum age, is an orphan.
        later = int(time.time() * 1000) + int((workspaces().MIN_ORPHAN_AGE_SECONDS + 60) * 1000)
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check(now_ms=later)))


class UnsafeEntryTests(Case):
    def test_HM2A_3_a_symlink_is_reported_and_never_followed_or_removed(self):
        target = self.outside / 'precious'
        target.mkdir(mode=0o700)
        (target / 'keep.txt').write_text('keep')
        link = self.container / 'prepare-deadbeef'
        link.symlink_to(target)
        found = self.run_check()
        self.assertEqual(['folder_is_a_link'], self.reasons(found))
        self.assertTrue(link.is_symlink())
        self.assertEqual('keep', (target / 'keep.txt').read_text())

    def test_HM2A_3_a_foreign_owned_folder_is_reported_and_kept(self):
        foreign = self.folder()
        found = self.run_check(self.check(uid=os.geteuid() + 1))
        self.assertEqual(['folder_foreign_owner'], self.reasons(found))
        self.assertTrue((foreign / 'export' / 'file.txt').is_file())

    def test_HM2A_3_a_file_named_like_a_preparation_folder_is_reported_and_kept(self):
        plain = self.container / 'prepare-0badf00d'
        plain.write_text('not a folder')
        found = self.run_check()
        self.assertEqual(['folder_not_a_directory'], self.reasons(found))
        self.assertTrue(plain.is_file())


class PendingTests(Case):
    def test_HM2A_4_a_pending_folder_is_left_to_the_driver(self):
        owed = self.folder()
        self.pending = {str(owed)}
        calls = []
        check = self.check(remove=lambda path: calls.append(path))
        self.assertEqual([], self.run_check(check))
        self.assertEqual([], calls)  # no second removal beside the driver's own retries
        self.assertTrue(owed.is_dir())

    def test_HM2A_4_a_folder_pending_for_many_passes_is_reported_then_forgotten_when_gone(self):
        owed = self.folder()
        self.pending = {str(owed)}
        check = self.check()
        passes = workspaces().PENDING_REPORT_PASSES
        for _ in range(passes - 1):
            self.assertEqual([], self.run_check(check))
        found = self.run_check(check)
        self.assertEqual(['removal_still_pending'], self.reasons(found))
        self.assertEqual('reported', found[0].outcome)
        self.assertTrue(owed.is_dir())
        # The driver removes it: nothing is said and nothing about it is kept.
        shutil.rmtree(owed)
        self.pending = set()
        self.assertEqual([], self.run_check(check))
        self.assertEqual({}, check.pending_passes)


class PathSafetyTests(Case):
    def test_HM2A_5_the_path_test_matches_remove_recorded_workspace_case_by_case(self):
        from codex_agents import bridge, codex_preparation
        hw = workspaces()
        base = self.base / 'parity'
        base.mkdir(mode=0o700)
        container = base / bridge.SCRATCH_CONTAINER
        container.mkdir(mode=0o700)
        ours = container / 'prepare-aaaa0001'
        ours.mkdir(mode=0o700)
        unrelated = container / 'probe-aaaa0002'
        unrelated.mkdir(mode=0o700)
        nested = container / 'prepare-aaaa0003'
        nested.mkdir(mode=0o700)
        (nested / 'prepare-inner').mkdir(mode=0o700)
        elsewhere = base / 'prepare-aaaa0004'
        elsewhere.mkdir(mode=0o700)
        link = container / 'prepare-aaaa0005'
        link.symlink_to(self.outside)
        plain = container / 'prepare-aaaa0006'
        plain.write_text('x')
        cases = {
            'real folder': (ours, None),
            'not a prepare name': (unrelated, 'not_a_preparation_name'),
            'nested below a prepare folder': (nested / 'prepare-inner', 'not_in_container'),
            'wrong parent': (elsewhere, 'not_in_container'),
            'a link': (link, 'folder_is_a_link'),
            'a file': (plain, 'folder_not_a_directory'),
            'dot dot': (Path(str(container) + '/../' + bridge.SCRATCH_CONTAINER + '/prepare-aaaa0001'), 'dot_dot'),
            'relative': (Path('prepare-aaaa0001'), 'not_absolute'),
        }
        with mock.patch.object(bridge, 'WORKSPACE_PARENT', str(base)):
            for label, (path, expected) in cases.items():
                with self.subTest(label):
                    self.assertEqual(expected, hw.workspace_problem(path, container), label)
                    before = os.path.lexists(path)
                    codex_preparation.remove_recorded_workspace(str(path))
                    removed = before and not os.path.lexists(path)
                    # S2's remover removes exactly the folders ours calls removable, and nothing else.
                    self.assertEqual(expected is None, removed, label)
                    if label == 'real folder':
                        ours.mkdir(mode=0o700)
        foreign = self.folder()
        self.assertEqual('folder_foreign_owner', hw.workspace_problem(foreign, self.container, uid=os.geteuid() + 1))
        self.assertIsNone(hw.workspace_problem(foreign, self.container))

    def test_HM2A_5_a_container_that_is_a_link_is_reported_and_nothing_is_removed(self):
        real = self.base / 'real-container'
        real.mkdir(mode=0o700)
        orphan = real / 'prepare-cafe0001'
        orphan.mkdir(mode=0o700)
        old = time.time() - 2 * DAY_SECONDS
        os.utime(orphan, (old, old))
        linked = self.base / 'linked-container'
        linked.symlink_to(real)
        found = self.run_check(self.check(container=linked))
        self.assertEqual(['container_unsafe'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    def test_HM2A_5_names_outside_the_prepare_pattern_are_never_listed(self):
        other = [self.folder('probe-12345678'), self.folder('prepare'), self.folder('xprepare-12345678'),
                 self.folder('Prepare-12345678')]
        self.assertEqual([], self.run_check())
        self.assertTrue(all(path.is_dir() for path in other))


class BoundTests(Case):
    def test_HM2A_6_more_orphans_than_one_pass_removes_wait_and_all_go(self):
        hw = workspaces()
        count = hw.MAX_REMOVALS_PER_PASS + 3
        orphans = [self.folder() for _ in range(count)]
        check = self.check()
        first = self.run_check(check)
        gone = [path for path in orphans if not os.path.lexists(path)]
        self.assertEqual(hw.MAX_REMOVALS_PER_PASS, len(gone))
        self.assertEqual(['folders_waiting'] + ['orphan_removed'] * hw.MAX_REMOVALS_PER_PASS, sorted(
            self.reasons(first), key=lambda reason: reason != 'folders_waiting'))
        for _ in range(count):  # the limit is per pass: later passes finish the rest
            self.run_check(check)
        self.assertEqual([], [path for path in orphans if os.path.lexists(path)])

    def test_HM2A_6_more_folders_than_one_pass_reads_are_reported_and_the_rest_still_work(self):
        hw = workspaces()
        keepers = [self.folder(contents=False) for _ in range(hw.MAX_FOLDERS_LISTED + 1)]
        self.live = {str(path) for path in keepers}
        found = self.run_check()
        self.assertEqual(['too_many_folders'], self.reasons(found))
        self.assertTrue(all(path.is_dir() for path in keepers))
        # Folders past the read limit are reached once earlier ones are gone: nothing is stuck for good.
        self.live = set()
        for _ in range(len(keepers) // hw.MAX_REMOVALS_PER_PASS + 3):
            self.run_check()
        self.assertEqual([], [path for path in keepers if os.path.lexists(path)])

    def test_HM2A_6_removal_stops_when_the_time_share_ends(self):
        orphans = [self.folder() for _ in range(3)]
        found = self.run_check(seconds=-1.0)
        self.assertEqual(['folders_waiting'], self.reasons(found))
        self.assertTrue(all(path.is_dir() for path in orphans))

    def test_HM2A_6_more_journal_entries_than_the_reader_takes_remove_nothing(self):
        hw = workspaces()
        state = self.base / 'state'
        directory = state / 'projects' / PROJECT / 'launches'
        directory.mkdir(mode=0o700, parents=True)
        for number in range(hw.MAX_JOURNAL_ENTRIES + 1):
            (directory / ('lnc_%032x.json' % number)).write_text('{}')
        self.assertIsNone(hw.journal_live_workspaces(state)())
        orphan = self.folder()
        found = self.run_check(self.check(live=hw.journal_live_workspaces(state)))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    def test_HM2A_6_more_journal_projects_than_the_reader_takes_remove_nothing(self):
        hw = workspaces()
        state = self.base / 'state'
        (state / 'projects').mkdir(mode=0o700, parents=True)
        for number in range(hw.MAX_JOURNAL_PROJECTS + 1):
            (state / 'projects' / ('prj_%032x' % number)).mkdir(mode=0o700)
        self.assertIsNone(hw.journal_live_workspaces(state)())


class FailSafeTests(Case):
    def test_HM2A_7_unreadable_live_records_remove_nothing_and_are_reported(self):
        orphan = self.folder()
        self.live = None
        found = self.run_check()
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    def test_HM2A_7_unreadable_pending_records_remove_nothing_and_are_reported(self):
        orphan = self.folder()
        self.pending = None
        found = self.run_check()
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    def test_HM2A_7_a_reader_that_raises_is_reported_by_type_only(self):
        orphan = self.folder()

        def broken():
            raise OSError('/secret/path went wrong')

        found = self.run_check(self.check(pending=broken))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertNotIn('secret', found[0].text)
        self.assertTrue(orphan.is_dir())

    def test_HM2A_7_a_check_without_its_readers_does_nothing(self):
        orphan = self.folder()
        hw = workspaces()
        for options in ({}, {'live': lambda: set()}, {'pending': lambda: set()}):
            with self.subTest(options=sorted(options)):
                self.assertEqual([], hw.WorkspaceCheck(container=self.container, **options).run(context()))
        self.assertTrue(orphan.is_dir())


class WordingTests(Case):
    def test_HM2A_8_findings_are_plain_closed_and_carry_no_path(self):
        orphan, failed = self.folder(), self.folder()
        link = self.container / 'prepare-11112222'
        link.symlink_to(self.outside)
        owed = self.folder()
        self.pending = {str(owed)}
        check = self.check(remove=lambda path: None if path == failed else shutil.rmtree(path))
        found = self.run_check(check)
        self.assertTrue(len(found) >= 3)
        for item in found:
            self.assertIs(hm.Finding, type(item))
            self.assertEqual('machine', item.scope)
            self.assertEqual(item.check, 'workspaces')
            self.assertIsNone(item.project_id)
            self.assertNotIn('/', item.text)
            self.assertNotIn(str(self.base), item.text)
            self.assertIsNone(re.search(r'decision|d309|Traceback|Error', item.text), item.text)
            self.assertTrue(item.text.endswith('.'))
        self.assertIn(orphan.name, ''.join(item.text for item in found))


class OwnerThreadTests(unittest.TestCase):
    def test_HM2A_9_the_module_has_no_ledger_or_preparation_write_access(self):
        hw = workspaces()
        source = Path(hw.__file__).read_text(encoding='utf-8')
        imports = re.findall(r'^\s*(?:from|import)\s+([\w.]+)', source, re.M)
        banned = ('sqlite3', 'learning_store', 'ledger', 'history', 'codex_preparation', 'orchestration_store')
        self.assertEqual([], [name for name in imports if any(word in name for word in banned)])
        self.assertNotRegex(source, r'sqlite3|\.execute\(|ledger_owner|owner_thread|HistoryClient')
        # It is given its readers: its constructor takes the pending reader as a plain callable.
        self.assertIn('pending', hw.WorkspaceCheck.__init__.__code__.co_varnames)


if __name__ == '__main__':
    unittest.main()
