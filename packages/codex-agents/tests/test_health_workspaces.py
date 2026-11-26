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

Fix round (r1 review): a folder is removed only with positive proof that its owner is gone (an owner marker naming
this state root and a launch-journal instance whose lock is free); removal goes through pinned directory
descriptors; any truncation of the owner records removes nothing; work inside one folder is bounded and resumes; a
rotating cursor reaches folders behind live ones; only FileNotFoundError proves a folder gone.

The module is imported inside each test, so before the code exists every test fails by itself.
"""
from __future__ import annotations

import fcntl
import inspect
import itertools
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_framework import requirements
import tests.support  # noqa: F401
from tests.support import SHORT_TMP

from codex_agents import health_monitor as hm

PROJECT = 'prj_' + 'a' * 32
OTHER_PROJECT = 'prj_' + 'b' * 32
# The project and launch-journal instance the fixture's folders name as their owner (a stopped one by default).
OWNER_PROJECT = 'prj_' + 'c' * 32
STOPPED_INSTANCE = 'd' * 32
DAY_SECONDS = 86_400
MARKER = 'cocolo-owner.json'


def workspaces():
    from codex_agents import health_workspaces
    return health_workspaces


def context(now_ms=None, seconds=30.0):
    return hm.CheckContext(deadline=time.monotonic() + seconds,
                           now_ms=int(time.time() * 1000) if now_ms is None else now_ms, since_last_pass_ms=None)


class CountedContext:
    """A check context whose time share ends after `calls` looks at it (the check's own deadline checks)."""

    def __init__(self, calls):
        self.calls, self.now_ms, self.since_last_pass_ms = calls, int(time.time() * 1000), None
        self.deadline = time.monotonic() + 30

    def expired(self):
        self.calls -= 1
        return self.calls < 0


def ordered_scandir(key):
    """A scandir whose listings come back in `key` order (the real order is the file system's own)."""
    real = os.scandir

    class Listing:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(self.entries)

        def close(self):
            pass

    def scandir(path='.'):
        with real(path) as listing:
            return Listing(sorted(listing, key=lambda entry: key(entry.name)))

    return scandir


class Case(unittest.TestCase):
    """A scratch container with folders, and a check wired to readers the test controls."""

    __why_not_scenario__ = ("One health pass over a real scratch folder is the scenario (listing, journal reads, "
                            "removal, observed absence); each method isolates one rule of it, and the monitor "
                            "registration test needs the pass's own worker thread.")

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
        self.state = self.base / 'state'

    def owner_lock(self, instance=STOPPED_INSTANCE, project=OWNER_PROJECT) -> Path:
        """The launch-journal instance lock a marker names (free: its service has stopped)."""
        directory = self.state / 'projects' / project / 'launches'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = directory / (instance + '.lock')
        if not lock.exists():
            lock.touch(mode=0o600)
        return lock

    def marker(self, path, *, state_root=None, instance=STOPPED_INSTANCE, project=OWNER_PROJECT):
        value = {'instance': instance, 'kind': 'cocolo_preparation', 'project_id': project,
                 'state_root': os.path.realpath(self.state if state_root is None else state_root), 'version': 1}
        (path / MARKER).write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))

    def folder(self, name=None, *, age_days=2, contents=True, marker=True) -> Path:
        path = self.container / (name or 'prepare-' + os.urandom(4).hex())
        path.mkdir(mode=0o700)
        if contents:
            for child in ('export', 'control', 'tmp'):
                (path / child).mkdir(mode=0o700)
            (path / 'export' / 'file.txt').write_text('data')
        old = time.time() - age_days * DAY_SECONDS
        if marker:
            self.owner_lock()
            self.marker(path)
            os.utime(path / MARKER, (old, old))
        os.utime(path, (old, old))
        return path

    def check(self, **options):
        defaults = {'container': self.container, 'live': lambda: self.live, 'pending': lambda: self.pending,
                    'state_root': self.state}
        return workspaces().WorkspaceCheck(**(defaults | options))

    def run_check(self, check=None, **context_options):
        return (check or self.check()).run(context(**context_options))

    @staticmethod
    def reasons(found):
        return sorted(item.reason_code for item in found)


class OrphanTests(Case):
    @requirements('HM2A-1')
    def test_HM2A_1_an_orphan_folder_is_removed_and_reported_fixed(self):
        orphan, keeper = self.folder(), self.folder()
        self.live = {str(keeper)}
        found = self.run_check()
        self.assertFalse(os.path.lexists(orphan))
        self.assertTrue(keeper.is_dir())
        self.assertEqual(['orphan_removed'], self.reasons(found))
        self.assertEqual(('fixed', 'machine', orphan.name), (found[0].outcome, found[0].scope, found[0].subject))

    @requirements('HM2A-1')
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

    @requirements('HM2A-2')
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

    @requirements('HM2A-2')
    def test_HM2A_2_a_young_folder_is_kept_and_not_reported(self):
        young = self.folder(age_days=0)
        found = self.run_check()
        self.assertEqual([], found)
        self.assertTrue(young.is_dir())
        # The same folder, once it is older than the minimum age, is an orphan.
        later = int(time.time() * 1000) + int((workspaces().MIN_ORPHAN_AGE_SECONDS + 60) * 1000)
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check(now_ms=later)))


class UnsafeEntryTests(Case):
    @requirements('HM2A-3')
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

    @requirements('HM2A-3')
    def test_HM2A_3_a_foreign_owned_folder_is_reported_and_kept(self):
        foreign = self.folder()
        found = self.run_check(self.check(uid=os.geteuid() + 1))
        self.assertEqual(['folder_foreign_owner'], self.reasons(found))
        self.assertTrue((foreign / 'export' / 'file.txt').is_file())

    @requirements('HM2A-3')
    def test_HM2A_3_a_file_named_like_a_preparation_folder_is_reported_and_kept(self):
        plain = self.container / 'prepare-0badf00d'
        plain.write_text('not a folder')
        found = self.run_check()
        self.assertEqual(['folder_not_a_directory'], self.reasons(found))
        self.assertTrue(plain.is_file())


class PendingTests(Case):
    @requirements('HM2A-4')
    def test_HM2A_4_a_pending_folder_is_left_to_the_driver(self):
        owed = self.folder()
        self.pending = {str(owed)}
        calls = []
        check = self.check(remove=lambda path: calls.append(path))
        self.assertEqual([], self.run_check(check))
        self.assertEqual([], calls)  # no second removal beside the driver's own retries
        self.assertTrue(owed.is_dir())

    @requirements('HM2A-4')
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
    @requirements('HM2A-5')
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

    @requirements('HM2A-5')
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

    @requirements('HM2A-5')
    def test_HM2A_5_names_outside_the_prepare_pattern_are_never_listed(self):
        other = [self.folder('probe-12345678'), self.folder('prepare'), self.folder('xprepare-12345678'),
                 self.folder('Prepare-12345678')]
        self.assertEqual([], self.run_check())
        self.assertTrue(all(path.is_dir() for path in other))


class BoundTests(Case):
    @requirements('HM2A-6')
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

    @requirements('HM2A-6')
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

    @requirements('HM2A-6')
    def test_HM2A_6_removal_stops_when_the_time_share_ends(self):
        orphans = [self.folder() for _ in range(3)]
        found = self.run_check(seconds=-1.0)
        self.assertEqual(['folders_waiting'], self.reasons(found))
        self.assertTrue(all(path.is_dir() for path in orphans))

    @requirements('HM2A-6')
    def test_HM2A_6_more_journal_entries_than_the_reader_takes_remove_nothing(self):
        hw = workspaces()
        state = self.base / 'state'
        directory = state / 'projects' / PROJECT / 'launches'
        directory.mkdir(mode=0o700, parents=True)
        for number in range(hw.MAX_JOURNAL_ENTRIES):
            (directory / ('lnc_%032x.json' % number)).write_text('{}')
        from codex_agents import launch_journal
        readable = {'workspace': None, 'state': 'settled'}  # isolates the count from the entry's own checks
        with mock.patch.object(launch_journal, '_read_entry_or_none', return_value=readable):
            self.assertEqual(set(), hw.journal_live_workspaces(state)())  # exactly the limit is read in full
            (directory / ('lnc_%032x.json' % hw.MAX_JOURNAL_ENTRIES)).write_text('{}')
            reader = hw.journal_live_workspaces(state)
            self.assertIsNone(reader())  # one past it: the live set is not complete
            orphan = self.folder()
            found = self.run_check(self.check(live=reader))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-6')
    def test_HM2A_6_a_reader_that_hands_back_more_paths_than_the_check_takes_removes_nothing(self):
        hw = workspaces()
        orphan = self.folder()
        found = self.run_check(self.check(live=lambda: (f'/x/{n}' for n in range(hw.MAX_RECORD_PATHS + 1))))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())
        found = self.run_check(self.check(live=lambda: (f'/x/{n}' for n in range(hw.MAX_RECORD_PATHS))))
        self.assertEqual(['orphan_removed'], self.reasons(found))

    @requirements('HM2A-6')
    def test_HM2A_6_more_journal_projects_than_the_reader_takes_remove_nothing(self):
        hw = workspaces()
        state = self.base / 'state'
        (state / 'projects').mkdir(mode=0o700, parents=True)
        for number in range(hw.MAX_JOURNAL_PROJECTS + 1):
            (state / 'projects' / ('prj_%032x' % number)).mkdir(mode=0o700)
        self.assertIsNone(hw.journal_live_workspaces(state)())


class FailSafeTests(Case):
    @requirements('HM2A-7')
    def test_HM2A_7_unreadable_live_records_remove_nothing_and_are_reported(self):
        orphan = self.folder()
        self.live = None
        found = self.run_check()
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-7')
    def test_HM2A_7_unreadable_pending_records_remove_nothing_and_are_reported(self):
        orphan = self.folder()
        self.pending = None
        found = self.run_check()
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-7')
    def test_HM2A_7_a_reader_that_raises_is_reported_by_type_only(self):
        orphan = self.folder()

        def broken():
            raise OSError('/secret/path went wrong')

        found = self.run_check(self.check(pending=broken))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertNotIn('secret', found[0].text)
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-7')
    def test_HM2A_7_a_check_without_its_readers_does_nothing(self):
        orphan = self.folder()
        hw = workspaces()
        for options in ({}, {'live': lambda: set()}, {'pending': lambda: set()}):
            with self.subTest(options=sorted(options)):
                self.assertEqual([], hw.WorkspaceCheck(container=self.container, **options).run(context()))
        self.assertTrue(orphan.is_dir())


class WordingTests(Case):
    @requirements('HM2A-8')
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


class RegistrationTests(Case):
    @requirements('HM2A-10')
    def test_HM2A_10_register_adds_the_check_to_a_monitor_and_a_pass_removes_and_shows_it(self):
        hw = workspaces()
        orphan = self.folder()
        monitor = hm.HealthMonitor.with_builtin_checks(sweep_interval_seconds=60.0)
        with mock.patch.object(hw.WorkspaceCheck, '_container_path', lambda check: self.container):
            hw.register(monitor, self.state, pending=lambda: set())
            monitor.run_pass()
        self.assertFalse(os.path.lexists(orphan))
        view = monitor.view(None)
        self.assertEqual({'self_check': 'ok', 'workspaces': 'ok'}, {c['check']: c['state'] for c in view['checks']})
        self.assertEqual(['orphan_removed'], [item['reason_code'] for item in view['findings']])
        with self.assertRaises(ValueError):  # registered once
            hw.register(monitor, self.base / 'state', pending=lambda: set())


class CollectorWiringTests(Case):
    """HM2A-11: the collector registers the workspaces check at its documented point."""

    @requirements('HM2A-11')
    def test_HM2A_11_the_collector_registers_the_check_and_a_pass_removes_an_orphan_and_keeps_a_live_folder(self):
        from tests import test_learning_store as store_tests
        from codex_agents import bridge, learning_store
        fixture = store_tests.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.state = fixture.state.root  # the owner markers name the collector's own state root
        orphan = self.folder()
        live = self.folder()
        driver = self.state / 'projects' / OWNER_PROJECT / 'launches' / ('e' * 32)
        driver.parent.mkdir(parents=True, exist_ok=True)
        self.marker(live, instance='e' * 32)
        lock = driver.with_suffix('.lock')
        lock.touch(mode=0o600)
        descriptor = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # its driver is running
        server = learning_store.CollectorServer(fixture.ledger)
        self.assertIn('workspaces', [check.name for check in server.health_monitor._checks])
        with mock.patch.object(workspaces().WorkspaceCheck, '_container_path', lambda check: self.container):
            server._run_health_pass()
        self.assertFalse(os.path.lexists(orphan))
        self.assertTrue(live.is_dir())
        view = server.health_monitor.view(None)
        self.assertIn('orphan_removed', [item['reason_code'] for item in view['findings']])

    @requirements('HM2A-11')
    def test_HM2A_11_registering_does_no_io_and_the_pending_reader_goes_through_the_projects_histories(self):
        from tests import test_learning_store as store_tests
        from codex_agents import learning_store
        fixture = store_tests.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        before = sorted(p.name for p in fixture.state.root.iterdir())
        server = learning_store.CollectorServer(fixture.ledger)
        self.assertEqual(before, sorted(p.name for p in fixture.state.root.iterdir()))
        check = next(c for c in server.health_monitor._checks if c.name == 'workspaces')
        self.assertEqual(set(), check._pending())  # no project history owes a removal
        directory = fixture.state.root / 'projects' / fixture.project_a_id
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'history.sqlite3').write_bytes(b'not a database')
        self.assertIsNone(check._pending())  # unreadable records: the check removes nothing and says so

    @requirements('HM2A-11')
    def test_HM2A_11_the_pending_reader_goes_past_its_bounds_and_says_unknown(self):
        import sqlite3
        from codex_agents import memory_holds
        root = self.base / 'state2'
        for name in ('prj_a', 'prj_b', 'prj_c'):
            (root / 'projects' / name).mkdir(parents=True)
        self.assertIsNone(self.read_with(memory_holds, 'MAX_PENDING_READ_PROJECTS', 2, root))  # more projects than the bound
        self.assertEqual(set(), memory_holds.pending_workspaces(root))  # no histories: nothing owed
        database = root / 'projects' / 'prj_a' / 'history.sqlite3'
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE project_holds(kind TEXT, identity TEXT, evidence TEXT, taken_at_ms INTEGER)')
        for number in range(3):
            connection.execute('INSERT INTO project_holds VALUES(?,?,?,?)', (
                'preparation', 'i%d' % number, json.dumps({'removal': 'pending', 'workspace': '/x/prepare-%d' % number}, separators=(',', ':')), number))
        connection.commit()
        connection.close()
        self.assertEqual({'/x/prepare-0', '/x/prepare-1', '/x/prepare-2'}, memory_holds.pending_workspaces(root))
        self.assertIsNone(self.read_with(memory_holds, 'MAX_PENDING_REMOVALS', 2, root))  # more owed than the bound

    def _real_history(self, root, tombstones):
        """A project history with the real project_holds schema: many released preparation rows and one pending."""
        import sqlite3
        from codex_agents.project_holds_schema import PROJECT_HOLDS_SCHEMA
        (root / 'projects' / 'prj_a').mkdir(parents=True)
        connection = sqlite3.connect(root / 'projects' / 'prj_a' / 'history.sqlite3')
        connection.execute('CREATE TABLE history_identity(project_id TEXT PRIMARY KEY, next_source_sequence INTEGER, '
                           'next_delivery_sequence INTEGER)')
        connection.execute("INSERT INTO history_identity VALUES('prj_a',1,1)")
        connection.executescript(PROJECT_HOLDS_SCHEMA)
        done = json.dumps({'removal': 'done'}, separators=(',', ':'))
        connection.executemany(
            "INSERT INTO project_holds(project_id,kind,identity,state,instance,taken_at_ms,released_at_ms,release_kind,"
            "evidence) VALUES('prj_a','preparation',?,'released','i',?,?,'retired',?)",
            ((f't{n}', n, n, done) for n in range(tombstones)))
        connection.execute(
            "INSERT INTO project_holds(project_id,kind,identity,state,instance,taken_at_ms,released_at_ms,release_kind,"
            "evidence) VALUES('prj_a','preparation','pending','released','i',9,9,'retired',?)",
            (json.dumps({'removal': 'pending', 'workspace': '/x/prepare-p'}, separators=(',', ':')),))
        connection.commit()
        connection.close()

    @requirements('HM2A-11')
    def test_HM2A_11_the_pending_read_is_index_backed_and_finds_one_row_among_many_tombstones(self):
        import sqlite3
        from codex_agents import memory_holds
        root = self.base / 'state3'
        self._real_history(root, 50_000)
        self.assertEqual({'/x/prepare-p'}, memory_holds.pending_workspaces(root))
        connection = sqlite3.connect(root / 'projects' / 'prj_a' / 'history.sqlite3')
        self.addCleanup(connection.close)
        plan = ' '.join(row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + memory_holds.PENDING_SQL,
                                                              ('prj_a',)))
        self.assertIn('USING INDEX', plan)
        self.assertNotRegex(plan, r'\bSCAN\b')
        self.assertNotIn('TEMP B-TREE', plan)

    @requirements('HM2A-11')
    def test_HM2A_11_the_pending_read_stops_at_its_deadline_and_says_unknown(self):
        from codex_agents import memory_holds
        root = self.base / 'state4'
        self._real_history(root, 20_000)
        ticks = itertools.count()
        # The injected clock passes the one deadline after a few reads: SQL steps and Python rows both consult it.
        with mock.patch.object(memory_holds, '_monotonic', lambda: next(ticks) * 0.5):
            self.assertIsNone(memory_holds.pending_workspaces(root))
        self.assertEqual({'/x/prepare-p'}, memory_holds.pending_workspaces(root))  # a fresh clock: the full answer

    @staticmethod
    def read_with(module, name, value, root):
        with mock.patch.object(module, name, value):
            return module.pending_workspaces(root)


class OwnerProofTests(Case):
    """Fix round, review r1 HIGH 1: deleting a folder needs positive proof that its owner is gone."""

    @requirements('HM2A-1')
    def test_HM2A_1_a_folder_without_an_owner_marker_is_reported_and_never_removed(self):
        unmarked = self.folder(marker=False)
        found = self.run_check()
        self.assertEqual(['owner_unknown'], self.reasons(found))
        self.assertEqual(('reported', unmarked.name), (found[0].outcome, found[0].subject))
        self.assertTrue((unmarked / 'export' / 'file.txt').is_file())

    @requirements('HM2A-1')
    def test_HM2A_1_a_marker_naming_another_state_root_is_reported_and_kept(self):
        other = self.base / 'other-state'
        other.mkdir(mode=0o700)
        foreign = self.folder()
        self.marker(foreign, state_root=other)  # another CoCoLo instance shares the per-user scratch container
        old = time.time() - 2 * DAY_SECONDS
        os.utime(foreign / MARKER, (old, old))
        self.assertEqual(['owner_unknown'], self.reasons(self.run_check()))
        self.assertTrue((foreign / 'export' / 'file.txt').is_file())

    @requirements('HM2A-1')
    def test_HM2A_1_a_marker_whose_instance_lock_is_missing_is_reported_and_kept(self):
        unproven = self.folder()
        self.marker(unproven, instance='e' * 32)  # no lock file: not proof that the instance stopped
        old = time.time() - 2 * DAY_SECONDS
        os.utime(unproven / MARKER, (old, old))
        self.assertEqual(['owner_unknown'], self.reasons(self.run_check()))
        self.assertTrue(unproven.is_dir())

    @requirements('HM2A-1')
    def test_HM2A_1_an_oversized_marker_is_not_owner_proof(self):
        hw = workspaces()
        padded = self.folder()
        value = json.loads((padded / MARKER).read_text())
        text = json.dumps(value) + ' ' * hw.MAX_MARKER_BYTES  # still valid JSON, but past the size a marker may have
        (padded / MARKER).write_text(text)
        old = time.time() - 2 * DAY_SECONDS
        os.utime(padded / MARKER, (old, old))
        self.assertEqual(['owner_unknown'], self.reasons(self.run_check()))
        self.assertTrue(padded.is_dir())

    @requirements('HM2A-1')
    def test_HM2A_1_a_running_owner_keeps_its_folder_even_after_a_clock_jump(self):
        live = self.folder()
        descriptor = os.open(self.owner_lock(), os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the owner's service is running
            jumped = int(time.time() * 1000) + 30 * DAY_SECONDS * 1000
            self.assertEqual([], self.run_check(now_ms=jumped))
            self.assertTrue((live / 'export' / 'file.txt').is_file())
        finally:
            os.close(descriptor)
        # Once that service is gone (its lock free), the same folder goes.
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check()))

    @requirements('HM2A-1')
    def test_HM2A_1_a_check_without_a_state_root_removes_nothing(self):
        kept = self.folder()
        self.assertEqual(['owner_unknown'], self.reasons(self.run_check(self.check(state_root=None))))
        self.assertTrue(kept.is_dir())

    @requirements('HM2A-1')
    def test_HM2A_1_only_not_found_proves_a_folder_gone(self):
        orphan = self.folder()

        def lose_access(path):
            os.chmod(self.container, 0o000)  # the folder is still there, it just cannot be looked at

        self.addCleanup(os.chmod, self.container, 0o700)
        found = self.run_check(self.check(remove=lose_access))
        os.chmod(self.container, 0o700)
        self.assertEqual(['orphan_removal_failed'], self.reasons(found))
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-1')
    def test_HM2A_1_the_driver_names_its_journal_instance_as_its_preparations_owner(self):
        from codex_agents import project_driver
        source = inspect.getsource(project_driver.RunDriver.__init__)
        journal = source.index('self._journal = LaunchJournal(')
        named = source.index('self._preparation.set_owner(')
        self.assertLess(journal, named)  # the instance exists (and its lock is held) before any folder is made
        self.assertIn('self._journal.instance', source[named:named + 200])


class PinnedRemovalTests(Case):
    """Fix round, review r1 HIGH 2: removal goes through pinned, verified directory descriptors."""

    def decoy(self, root, name):
        """A folder shaped like a preparation folder, outside the container, with read-only parts."""
        target = root / name
        for child in ('export', 'control', 'tmp'):
            (target / child).mkdir(mode=0o700, parents=True)
        (target / 'export' / 'file.txt').write_text('precious')
        for child in ('control', 'tmp'):
            os.chmod(target / child, 0o500)
        self.addCleanup(lambda: [os.chmod(target / c, 0o700) for c in ('control', 'tmp') if (target / c).is_dir()])
        return target

    def assertDecoyIntact(self, target):
        self.assertEqual('precious', (target / 'export' / 'file.txt').read_text())
        for child in ('control', 'tmp'):
            self.assertEqual(0o500, os.lstat(target / child).st_mode & 0o777, child)

    def swap_on_first_chmod(self, swap):
        """Run `swap` once, at the remover's first change of a folder's mode (by path or by descriptor)."""
        state = {'done': False}
        real_chmod, real_fchmod = os.chmod, os.fchmod

        def once():
            if not state['done']:
                state['done'] = True
                swap()

        def chmod(path, mode, *args, **options):
            once()
            return real_chmod(path, mode, *args, **options)

        def fchmod(fd, mode):
            once()
            return real_fchmod(fd, mode)

        return mock.patch.multiple(os, chmod=chmod, fchmod=fchmod), state

    @requirements('HM2A-5')
    def test_HM2A_5_a_container_swapped_for_a_link_mid_removal_never_reaches_the_link_target(self):
        orphan = self.folder()
        os.chmod(orphan / 'control', 0o500)  # read-only, so a remover must change its mode to empty it
        decoys = self.base / 'decoys'
        target = self.decoy(decoys, orphan.name)
        moved = self.base / 'container-moved'
        self.addCleanup(lambda: (moved / orphan.name / 'control').is_dir()
                        and os.chmod(moved / orphan.name / 'control', 0o700))

        def swap():
            os.rename(self.container, moved)
            os.symlink(decoys, self.container)

        patch, state = self.swap_on_first_chmod(swap)
        with patch:
            found = self.run_check()
        self.assertTrue(state['done'])
        self.assertDecoyIntact(target)
        self.assertFalse(os.path.lexists(moved / orphan.name))  # the real folder, where it now is, went
        self.assertEqual(['orphan_removed'], self.reasons(found))

    @requirements('HM2A-5')
    def test_HM2A_5_a_subfolder_swapped_for_a_link_mid_removal_never_reaches_the_link_target(self):
        orphan = self.folder()
        decoys = self.base / 'decoys'
        target = self.decoy(decoys, 'inner')
        for child in ('a', 'b'):
            (orphan / 'export' / child).mkdir(mode=0o700)
        export = orphan / 'export'
        state = {'done': False}
        real_chmod, real_fchmod = os.chmod, os.fchmod
        original = os.lstat(export)

        def swap():
            state['done'] = True
            os.rename(export, orphan / 'export-away')
            os.symlink(target / 'control', export)  # a read-only folder elsewhere

        def chmod(path, mode, *args, **options):  # a remover that changes modes by path
            if not state['done'] and str(path) == str(export):
                swap()
            return real_chmod(path, mode, *args, **options)

        def fchmod(fd, mode):  # a remover that changes modes through descriptors
            info = os.fstat(fd)
            if not state['done'] and (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino):
                swap()
            return real_fchmod(fd, mode)

        os.chmod(export, 0o500)  # read-only, so a remover must change its mode to empty it
        self.addCleanup(lambda: os.path.isdir(orphan / 'export-away') and os.chmod(orphan / 'export-away', 0o700))
        with mock.patch.multiple(os, chmod=chmod, fchmod=fchmod):
            self.run_check()
        self.assertTrue(state['done'])
        self.assertDecoyIntact(target)
        for _ in range(3):  # what is left of the real folder goes at later passes
            self.run_check()
        self.assertDecoyIntact(target)
        self.assertFalse(os.path.lexists(orphan))


class FolderBoundTests(Case):
    """Fix round, review r1 MED 4: work inside one folder is bounded by entries and time, and resumes."""

    @requirements('HM2A-6')
    def test_HM2A_6_removal_inside_one_folder_stops_at_its_entry_limit_and_resumes(self):
        hw = workspaces()
        orphan = self.folder()
        for number in range(hw.MAX_REMOVAL_ENTRIES_PER_PASS + 10):
            (orphan / 'export' / ('f%05d' % number)).touch()
        check = self.check()
        found = self.run_check(check)
        self.assertEqual(['orphan_removal_continues'], self.reasons(found))
        self.assertTrue((orphan / MARKER).is_file())  # the owner proof stays until the very end
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check(check)))
        self.assertFalse(os.path.lexists(orphan))

    @requirements('HM2A-6')
    def test_HM2A_6_removal_inside_one_folder_stops_when_the_time_share_ends(self):
        orphan = self.folder()
        for number in range(50):
            (orphan / 'export' / ('f%05d' % number)).touch()
        found = workspaces().WorkspaceCheck(container=self.container, live=lambda: set(), pending=lambda: set(),
                                            state_root=self.state).run(CountedContext(12))
        self.assertEqual(['orphan_removal_continues'], self.reasons(found))
        left = len(os.listdir(orphan / 'export'))
        self.assertGreater(left, 30)  # it stopped at the deadline, not after the folder
        time.sleep(0.05)
        self.assertEqual(left, len(os.listdir(orphan / 'export')))  # and nothing goes on deleting after it returned
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check()))

    @requirements('HM2A-6')
    def test_HM2A_6_a_folder_deeper_than_the_remover_goes_is_reported_and_kept(self):
        hw = workspaces()
        orphan = self.folder()
        deep = orphan / 'export'
        for _ in range(hw.MAX_REMOVAL_DEPTH + 1):
            deep = deep / 'd'
        deep.mkdir(mode=0o700, parents=True)
        found = self.run_check()
        self.assertEqual(['orphan_removal_failed'], self.reasons(found))
        self.assertTrue((orphan / MARKER).is_file())


class TruncationTests(Case):
    """Fix round, review r1 HIGH 3: any truncation of the owner records means nothing is removed that pass."""

    @requirements('HM2A-7')
    def test_HM2A_7_a_projects_folder_longer_than_the_reader_scans_is_incomplete(self):
        hw = workspaces()
        owned = self.folder()
        from codex_agents.launch_journal import LaunchJournal
        directory = self.state / 'projects' / PROJECT / 'launches'
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        journal = LaunchJournal(directory)
        self.addCleanup(journal.release)
        journal.claimed('brg_' + '1' * 32, run_handle='hrn_' + 'a' * 48, attempt=1, host_client='codex-host-v1',
                        lease={'project_id': PROJECT}, identity_digest='c' * 64, claim_request_id='claim-' + 'd' * 32,
                        storage_evidence=b'{"evidence":1}', workspace=str(owned))
        for number in range(hw.MAX_ENTRIES_SCANNED + 1):  # review probe 1: entries that are not projects
            (self.state / 'projects' / ('x%05d' % number)).touch()
        reader = hw.journal_live_workspaces(self.state)
        with mock.patch.object(os, 'scandir', ordered_scandir(lambda name: (name.startswith('prj_'), name))):
            self.assertIsNone(reader())
            found = self.run_check(self.check(live=reader))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertIn('incomplete', found[0].text)
        self.assertTrue(owned.is_dir())

    @requirements('HM2A-7')
    def test_HM2A_7_repeated_paths_do_not_hide_a_live_tail(self):
        hw = workspaces()
        owned = self.folder()
        # Review probe 2: the reader's first MAX_RECORD_PATHS + 1 items are one path repeated; the live folder follows.
        tail = lambda: itertools.chain(itertools.repeat('/x/same', hw.MAX_RECORD_PATHS + 1), [str(owned)])
        found = self.run_check(self.check(live=tail))
        self.assertEqual([], found)
        self.assertTrue(owned.is_dir())

    @requirements('HM2A-7')
    def test_HM2A_7_a_reader_handing_back_more_items_than_the_check_reads_is_incomplete(self):
        hw = workspaces()
        orphan = self.folder()
        endless = lambda: itertools.repeat('/x/same', hw.MAX_RECORD_ITEMS + 1)
        found = self.run_check(self.check(live=endless))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertTrue(orphan.is_dir())
        exactly = lambda: itertools.repeat('/x/same', hw.MAX_RECORD_ITEMS)
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check(self.check(live=exactly))))

    @requirements('HM2A-7')
    def test_HM2A_7_a_launches_folder_longer_than_the_reader_takes_is_incomplete(self):
        hw = workspaces()
        directory = self.state / 'projects' / PROJECT / 'launches'
        directory.mkdir(mode=0o700, parents=True)
        for number in range(hw.MAX_JOURNAL_NAMES):
            (directory / ('%032x.lock' % number)).touch()  # names that are not entries count too
        self.assertEqual(set(), hw.journal_live_workspaces(self.state)())
        (directory / ('%032x.lock' % hw.MAX_JOURNAL_NAMES)).touch()
        pulled = []
        real = os.scandir

        def counting(path='.'):
            listing = real(path)
            if str(path) != str(directory):
                return listing

            class Counted:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    listing.close()

                def __iter__(self):
                    for entry in listing:
                        pulled.append(entry.name)
                        yield entry

            return Counted()

        with mock.patch.object(os, 'scandir', counting):
            self.assertIsNone(hw.journal_live_workspaces(self.state)())
        self.assertLessEqual(len(pulled), hw.MAX_JOURNAL_NAMES + 1)  # checked while iterating, not after


class CursorTests(Case):
    """Fix round, review r1 MED 5 and LOW 7: a rotating cursor reaches leftovers behind live or other entries."""

    @requirements('HM2A-6')
    def test_HM2A_6_leftovers_behind_live_folders_are_reached_by_later_passes(self):
        hw = workspaces()
        live = [self.folder('prepare-0%07x' % number, contents=False) for number in range(hw.MAX_FOLDERS_LISTED)]
        self.live = {str(path) for path in live}
        orphan = self.folder('prepare-ffffffff', contents=False)
        check = self.check()
        with mock.patch.object(os, 'scandir', ordered_scandir(lambda name: name)):
            for _ in range(3):
                self.run_check(check)
        self.assertFalse(os.path.lexists(orphan))
        self.assertTrue(all(path.is_dir() for path in live))

    @requirements('HM2A-6')
    def test_HM2A_6_more_entries_than_one_pass_scans_are_reached_by_later_passes(self):
        hw = workspaces()
        for number in range(hw.MAX_ENTRIES_SCANNED + 1):  # past the real 1,024 scan limit
            (self.container / ('other-%05d' % number)).touch()
        orphan = self.folder()
        check = self.check()
        with mock.patch.object(os, 'scandir', ordered_scandir(lambda name: (name.startswith('prepare-'), name))):
            first = self.run_check(check)
            self.assertTrue(orphan.is_dir())  # this pass read only the first MAX_ENTRIES_SCANNED entries
            self.assertEqual(['too_many_folders'], self.reasons(first))
            second = self.run_check(check)
        self.assertFalse(os.path.lexists(orphan))
        self.assertIn('orphan_removed', self.reasons(second))


class FinalFixTests(Case):
    """Final fix (confirming check r2c): no mode change before owner proof and only through a verified descriptor;
    the marker goes only right before the folder's rmdir, after the last budget check; the container listing counts
    every entry it reads and checks the time share (the marker write is in the codex_preparation tests)."""

    @requirements('HM2A-1')
    def test_HM2A_1_a_folder_whose_owner_is_not_proven_keeps_its_mode(self):
        unproven = self.folder(marker=False)
        os.chmod(unproven, 0o300)  # not listable: looking for its marker must not make it so
        self.addCleanup(lambda: os.path.isdir(unproven) and os.chmod(unproven, 0o700))
        found = self.run_check()
        self.assertEqual(0o300, os.lstat(unproven).st_mode & 0o777)
        self.assertEqual(['owner_unknown'], self.reasons(found))
        self.assertTrue(os.path.lexists(unproven))

    @requirements('HM2A-5')
    def test_HM2A_5_permission_recovery_never_changes_a_folder_swapped_in_before_it(self):
        orphan = self.folder()
        export = orphan / 'export'
        os.chmod(export, 0o100)  # not listable: emptying it needs a mode change
        state = {'done': False}
        real_open = os.open

        def opener(path, flags, mode=0o777, *, dir_fd=None):
            if not state['done'] and dir_fd is not None and path == 'export':
                state['done'] = True  # after the remover looked at `export`, before it opens or recovers it
                os.chmod(export, 0o700)
                os.rename(export, orphan / 'export-away')
                os.mkdir(export, 0o700)
                os.chmod(export, 0o100)  # a replacement, also not listable
            return real_open(path, flags, mode, dir_fd=dir_fd)

        self.addCleanup(lambda: os.path.isdir(export) and os.chmod(export, 0o700))
        with mock.patch.object(os, 'open', opener):
            found = self.run_check()
            self.assertTrue(state['done'])
            replacement = os.lstat(export)
        self.assertEqual(0o100, replacement.st_mode & 0o777)  # the swapped-in folder's mode is unchanged
        self.assertNotIn('orphan_removed', self.reasons(found))
        self.assertTrue((orphan / MARKER).is_file())
        if hasattr(os, 'O_SEARCH'):  # where a mode can be changed through a descriptor, later passes finish it
            for _ in range(3):
                self.run_check()
            self.assertFalse(os.path.lexists(orphan))

    @requirements('HM2A-6')
    def test_HM2A_6_a_deadline_at_any_removal_step_leaves_the_folder_resumable(self):
        calls, finished = 0, False
        while not finished and calls <= 80:
            orphan = self.folder()
            with self.subTest(calls=calls):
                found = workspaces().WorkspaceCheck(container=self.container, live=lambda: set(),
                                                    pending=lambda: set(), state_root=self.state).run(
                    CountedContext(calls))
                finished = 'orphan_removed' in self.reasons(found)
                if not finished:
                    self.assertTrue((orphan / MARKER).is_file())  # a stopped folder keeps its owner proof
                    for _ in range(3):
                        self.run_check()
                    self.assertFalse(os.path.lexists(orphan))
            calls += 1
        self.assertTrue(finished)
        self.assertGreater(calls, 5)  # it stopped at each step in between

    @requirements('HM2A-6')
    def test_HM2A_6_a_folder_rmdir_failing_after_its_marker_went_is_still_resumed(self):
        orphan = self.folder()
        real_rmdir, failed = os.rmdir, []

        def rmdir(path, *, dir_fd=None):
            if not failed and path == orphan.name:
                failed.append(path)
                raise OSError(5, 'injected')
            return real_rmdir(path, dir_fd=dir_fd)

        with mock.patch.object(os, 'rmdir', rmdir):
            found = self.run_check()
        self.assertEqual([orphan.name], failed)
        self.assertEqual(['orphan_removal_failed'], self.reasons(found))
        self.assertTrue((orphan / MARKER).is_file())
        self.assertEqual(['orphan_removed'], self.reasons(self.run_check()))
        self.assertFalse(os.path.lexists(orphan))

    @requirements('HM2A-6')
    def test_HM2A_6_a_cursor_deep_in_a_huge_folder_stays_within_the_cap_and_progresses(self):
        hw = workspaces()
        total = 3 * hw.MAX_ENTRIES_SCANNED + 10
        for number in range(total):
            (self.container / ('other-%05d' % number)).touch()
        orphan = self.folder()  # listed last
        pinned = os.lstat(self.container)
        real = os.scandir
        pulled = []

        class Listing:  # the container's entries in name order, counting each one read
            def __init__(self, entries):
                self.entries = iter(entries)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self.entries)
                pulled[-1] += 1
                return entry

            def close(self):
                pass

        def scandir(path='.'):
            info = os.fstat(path) if type(path) is int else os.stat(path)
            if (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino):
                return real(path)
            with real(path) as listing:
                return Listing(sorted(listing, key=lambda entry: (entry.name.startswith('prepare-'), entry.name)))

        check = self.check()
        passes = 0
        with mock.patch.object(os, 'scandir', scandir):
            while os.path.lexists(orphan) and passes < 8:
                pulled.append(0)
                self.run_check(check)
                passes += 1
                self.assertLessEqual(pulled[-1], hw.MAX_ENTRIES_SCANNED, pulled)
                if passes == 2:  # a pass whose time share is over stops listing soon, deep cursor or not
                    pulled.append(0)
                    check.run(CountedContext(0))
                    self.assertLessEqual(pulled[-1], hw._LISTING_DEADLINE_EVERY, pulled)
        self.assertFalse(os.path.lexists(orphan))
        self.assertLessEqual(passes, (total + 1) // hw.MAX_ENTRIES_SCANNED + 2)
        self.assertGreaterEqual(sum(pulled), total + 1)  # every entry was read, a cap's worth at a time


class OwnerThreadTests(unittest.TestCase):
    __why_not_scenario__ = "A source scan of the module; it isolates one rule of the same scenario."

    @requirements('HM2A-9')
    def test_HM2A_9_the_module_has_no_ledger_or_preparation_write_access(self):
        hw = workspaces()
        source = Path(hw.__file__).read_text(encoding='utf-8')
        imports = re.findall(r'^\s*(?:from|import)\s+([\w.]+)', source, re.M)
        banned = ('sqlite3', 'learning_store', 'ledger', 'history', 'codex_preparation', 'orchestration_store')
        self.assertEqual([], [name for name in imports if any(word in name for word in banned)])
        self.assertNotRegex(source, r'sqlite3|\.execute\(|ledger_owner|owner_thread|HistoryClient')
        # It is given its readers: its constructor takes the pending reader as a plain callable.
        self.assertIn('pending', hw.WorkspaceCheck.__init__.__code__.co_varnames)


class LiveFindingTests(Case):
    """Decision 365 (live finding on 0.8.68): the pending read returned None for a store without project_holds (an older
    but valid store) and said nothing about why; every other None names its reason."""

    PRJ = 'prj_' + 'a' * 32
    OLD = 'prj_' + 'b' * 32

    def projects(self):
        root = self.base / 'live-state'
        (root / 'projects').mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(root / 'projects', 0o700)
        return root

    def history(self, root, project, *, pending=0, older=False, tombstones=0):
        """A real project history made through ProjectHistory; `older` drops the holds table, the shape of a store
        written before project_holds existed (still a valid store)."""
        import sqlite3
        from contextlib import closing
        from codex_agents.project_history import ProjectHistory
        from codex_agents.project_holds_schema import evidence_json, release_hold, take_hold
        directory = root / 'projects' / project
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        with closing(ProjectHistory(directory, project)) as history:
            connection = history._connection
            for number in range(pending + tombstones):
                kind = evidence_json(removal='pending', workspace=f'/x/prepare-{project[-4:]}{number}') if number < pending \
                    else evidence_json(removal='done')
                take_hold(connection, project, 'preparation', f'p{number}', now_ms=1, instance='i', evidence=kind)
                release_hold(connection, project, 'preparation', f'p{number}', 'retired', now_ms=2)
            if older:
                try:
                    connection.execute('DROP TABLE project_holds')
                except sqlite3.Error as error:  # a table another object depends on: say so plainly
                    self.fail(f'cannot make the older store shape: {error}')
        return directory / 'history.sqlite3'

    @requirements('HM2A-12')
    def test_HM2A_12_a_store_without_project_holds_owes_nothing_and_does_not_fail_the_read(self):
        from codex_agents import memory_holds
        root = self.projects()
        database = self.history(root, self.OLD, older=True)
        import sqlite3
        names = {row[0] for row in sqlite3.connect(database).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('project_holds', names)
        self.assertEqual(set(), memory_holds.pending_workspaces(root))

    @requirements('HM2A-12')
    def test_HM2A_12_an_older_store_beside_a_normal_project_returns_the_normal_projects_removals(self):
        from codex_agents import memory_holds
        root = self.projects()
        self.history(root, self.PRJ, pending=3, tombstones=2)
        self.history(root, self.OLD, older=True)
        expected = {f'/x/prepare-{self.PRJ[-4:]}{n}' for n in range(3)}
        self.assertEqual(expected, memory_holds.pending_workspaces(root))

    @requirements('HM2A-12')
    def test_HM2A_12_a_pass_with_an_older_store_cleans_an_orphan_and_keeps_what_a_driver_owes(self):
        from codex_agents import memory_holds
        root = self.projects()
        self.history(root, self.PRJ, pending=3)
        self.history(root, self.OLD, older=True)
        owed = [self.folder(f'prepare-{self.PRJ[-4:]}{n}') for n in range(3)]
        orphan = self.folder()
        reader = memory_holds.pending_workspaces_reader(root)
        # the pending records name /x/..., so point the reader at this container's folders
        names = {str(path) for path in owed}
        found = self.run_check(self.check(pending=lambda: names | (reader() or set())))
        self.assertEqual(['orphan_removed'], self.reasons(found))
        self.assertFalse(os.path.lexists(orphan))
        self.assertTrue(all(path.is_dir() for path in owed))
        self.assertIsNone(reader.reason)

    @requirements('HM2A-12')
    def test_HM2A_12_a_corrupt_store_is_never_read_as_owing_nothing(self):
        from codex_agents import memory_holds
        root = self.projects()
        directory = root / 'projects' / self.OLD
        directory.mkdir(mode=0o700)
        (directory / 'history.sqlite3').write_bytes(b'not a database at all' * 20)
        self.assertIsNone(memory_holds.pending_workspaces(root))

    @requirements('HM2A-13')
    def test_HM2A_13_every_unknown_names_its_reason(self):
        from codex_agents import memory_holds
        root = self.projects()
        reader = memory_holds.pending_workspaces_reader(root)
        self.assertEqual(set(), reader())
        self.assertIsNone(reader.reason)
        # too many projects
        for name in ('prj_' + c * 32 for c in 'cde'):
            (root / 'projects' / name).mkdir()
        with mock.patch.object(memory_holds, 'MAX_PENDING_READ_PROJECTS', 2):
            self.assertIsNone(reader())
        self.assertEqual('too_many_projects', reader.reason)
        # an unreadable history names its project and the error class, nothing else
        bad = root / 'projects' / ('prj_' + 'c' * 32)
        (bad / 'history.sqlite3').write_bytes(b'not a database at all' * 20)
        self.assertIsNone(reader())
        self.assertEqual('history_unreadable:DatabaseError:' + bad.name, reader.reason)
        shutil.rmtree(bad)
        # too many pending in one project
        full = self.history(root, self.PRJ, pending=3)
        with mock.patch.object(memory_holds, 'MAX_PENDING_REMOVALS', 2):
            self.assertIsNone(reader())
        self.assertEqual('too_many_pending:' + self.PRJ, reader.reason)
        # the deadline
        ticks = itertools.count()
        with mock.patch.object(memory_holds, '_monotonic', lambda: next(ticks) * 0.5):
            self.assertIsNone(reader())
        self.assertEqual('deadline', reader.reason)
        # the projects folder itself cannot be listed
        shutil.rmtree(root / 'projects')
        (root / 'projects').write_text('a file')
        self.assertIsNone(reader())
        self.assertEqual('projects_unreadable:NotADirectoryError', reader.reason)
        self.assertTrue(full.name)

    @requirements('HM2A-13')
    def test_HM2A_13_the_finding_names_the_reason_in_its_text_and_subject_with_a_plain_next_step(self):
        for reason, step in (('too_many_projects', 'No action'), ('too_many_pending:' + self.PRJ, 'report'),
                             ('history_unreadable:OperationalError:' + self.PRJ, 'report'), ('deadline', 'No action')):
            reader = lambda: None
            reader.reason = reason
            orphan = self.folder()
            found = self.run_check(self.check(pending=reader))
            record = [item for item in found if item.reason_code == 'records_unreadable']
            self.assertEqual(1, len(record), reason)
            item = record[0]
            self.assertEqual(reason, item.subject)
            self.assertIn(reason, item.text)
            self.assertIn(step, item.text)
            self.assertLessEqual(len(item.text), 240)
            self.assertTrue(item.text.endswith('.'))
            self.assertNotIn('/', item.text)
            self.assertTrue(orphan.is_dir())  # nothing is removed while the records are unknown
            shutil.rmtree(orphan)

    @requirements('HM2A-13')
    def test_HM2A_13_an_unknown_without_a_reason_keeps_the_old_finding(self):
        orphan = self.folder()
        found = self.run_check(self.check(pending=lambda: None))
        self.assertEqual(['records_unreadable'], self.reasons(found))
        self.assertEqual('records', found[0].subject)
        self.assertTrue(orphan.is_dir())

    @requirements('HM2A-13')
    def test_HM2A_13_the_collector_wires_the_reason_reader(self):
        from tests import test_learning_store as store_tests
        from codex_agents import learning_store
        fixture = store_tests.LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        server = learning_store.CollectorServer(fixture.ledger)
        check = next(c for c in server.health_monitor._checks if c.name == 'workspaces')
        self.assertTrue(hasattr(check._pending, 'reason'))


if __name__ == '__main__':
    unittest.main()
