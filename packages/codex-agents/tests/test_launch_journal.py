"""The launch journal a project service keeps for the runs it claimed, and its liveness evidence.

A later service settles an earlier service's run only on positive kernel
evidence that the child, its process group and session, and its watchdog are
gone. Anything unreadable or ambiguous is not evidence.
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import TEMP_ROOT

from unittest import mock

from codex_agents import launch_journal, process_tree
from codex_agents.bridge import PYTHON_ISOLATION_FLAGS, WATCHDOG_SCRIPT
from codex_agents.launch_journal import LaunchJournal, JournalError, tree_state
from codex_agents.process_tree import WATCHDOG_READY_SECONDS, await_ready, process_record, write_cleanup_marker

BRIDGE = 'brg_' + '1' * 32
# Fresh per test process, as each child's is: a parallel suite's processes never share one.
TAG = secrets.token_hex(16)
OTHER = 'brg_' + '2' * 32


def claimed(journal, bridge=BRIDGE):
    journal.claimed(bridge, run_handle='hrn_' + 'a' * 48, attempt=1, host_client='codex-host-v1',
                    lease={'project_id': 'prj_' + 'b' * 32}, identity_digest='c' * 64,
                    claim_request_id='claim-' + 'd' * 32, storage_evidence=b'{"evidence":1}', workspace=None)


def spawn(*arguments, **options):
    process = subprocess.Popen(list(arguments), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    for _ in range(200):
        if process_record(process.pid) is not None:
            break
        time.sleep(0.01)
    return process


def birth(pid):
    return process_record(pid)[2]


class LaunchJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'launches'
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
            process.wait(5)

    def started(self, *arguments, **options):
        process = spawn(*arguments, **options)
        self.processes.append(process)
        return process

    def sleeper(self, seconds=30):
        return self.started('/bin/sleep', str(seconds), start_new_session=True)

    def watched(self, journal, root, watchdog, *, confirmed=False):
        claimed(journal)
        journal.spawning(BRIDGE)
        journal.launched(BRIDGE, root.pid, birth(root.pid), TAG)
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        if confirmed:
            self.confirm(journal, root.pid, birth(root.pid))
        return self.entry(journal)

    @staticmethod
    def confirm(journal, pid, born, bridge=BRIDGE):
        """What the real watchdog writes once the tree it tracked is confirmed empty."""
        write_cleanup_marker(str(journal.marker_path(bridge)), bridge, pid, born)

    def real_watchdog(self, journal, root, *, deadline=60.0):
        """The bridge's own detached watchdog for `root`, writing its confirmation beside the journal."""
        watchdog = subprocess.Popen(
            [sys.executable, *PYTHON_ISOLATION_FLAGS, str(WATCHDOG_SCRIPT), str(root.pid), birth(root.pid),
             repr(time.monotonic() + deadline), str(journal.marker_path(BRIDGE)), BRIDGE, TAG],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            close_fds=True, env={'PATH': os.defpath})
        self.processes.append(watchdog)
        try:
            self.assertTrue(await_ready(watchdog.stdout, WATCHDOG_READY_SECONDS))
        finally:
            watchdog.stdout.close()
        claimed(journal)
        journal.spawning(BRIDGE)
        journal.launched(BRIDGE, root.pid, birth(root.pid), TAG)
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        return watchdog

    def escaping_root(self, *, pause):
        """A child whose own child starts a new session (setsid) and keeps running; the child then ends."""
        code = ('import subprocess, sys, time\n'
                "grandchild = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\n"
                'print(grandchild.pid, flush=True)\n'
                'time.sleep(%r)\n' % pause)
        root = subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(root)
        grandchild = int(root.stdout.readline())
        root.stdout.close()
        self.addCleanup(self.kill_quietly, grandchild)
        return root, grandchild

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    @staticmethod
    def running(pid):
        record = process_record(pid)
        return record is not None and not record[3]

    def wait_until(self, condition, seconds=15):
        end = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), end)
            time.sleep(0.05)

    @staticmethod
    def entry(journal, bridge=BRIDGE):
        return [entry for entry in journal.entries() if entry.get('bridge_run_id') == bridge][0]

    @staticmethod
    def end(process):
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(5)

    def replacing_fstat(self, journal, times):
        """An fstat that first replaces the entry file `times` times: a write landing while a reader has it open."""
        real, left, base = os.fstat, [times], journal.entry(BRIDGE)

        def fstat(descriptor):
            if left[0]:
                left[0] -= 1
                journal._write(BRIDGE, {**base, 'attempt': 9 - left[0]})
            return real(descriptor)
        return fstat

    def test_an_entry_replaced_while_it_is_read_is_read_again_not_missing(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        with mock.patch.object(launch_journal.os, 'fstat', self.replacing_fstat(journal, 1)):
            value = launch_journal._read_entry(journal._path(BRIDGE))
        self.assertIsNotNone(value)
        self.assertEqual(9, value['attempt'])  # The new entry, not None and not the old one
        with mock.patch.object(launch_journal.os, 'fstat', self.replacing_fstat(journal, 2)):
            self.assertEqual('claimed', journal.entry(BRIDGE)['state'])

    def test_an_entry_with_two_links_is_still_refused(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        os.link(journal._path(BRIDGE), self.directory / 'other-name')
        self.assertIsNone(journal.entry(BRIDGE))
        self.assertIsNone(launch_journal._read_entry(journal._path(BRIDGE)))

    def test_an_entry_replaced_every_time_it_is_opened_ends_in_a_fixed_failure(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        with mock.patch.object(launch_journal.os, 'fstat', self.replacing_fstat(journal, 99)):
            with self.assertRaisesRegex(JournalError, 'kept changing while it was read'):
                journal.entry(BRIDGE)
            self.assertIsNone(launch_journal._read_entry_or_none(journal._path(BRIDGE)))
        self.assertEqual('claimed', journal.entry(BRIDGE)['state'])

    def test_a_missing_entry_is_still_none(self):
        journal = LaunchJournal(self.directory)
        self.assertIsNone(journal.entry(BRIDGE))

    def test_a_run_claimed_but_never_spawned_is_gone(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        entry = self.entry(journal)
        self.assertEqual('claimed', entry['state'])
        self.assertEqual('gone', tree_state(entry))

    def test_a_run_that_may_have_spawned_without_a_watched_child_is_unknown(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        journal.spawning(BRIDGE)
        self.assertEqual('unknown', tree_state(self.entry(journal)))
        root = self.sleeper()
        journal.launched(BRIDGE, root.pid, birth(root.pid), TAG)
        self.end(root)
        # The child is gone, but no watchdog ever confirmed its tree: nothing proves its descendants ended.
        self.assertEqual('unknown', tree_state(self.entry(journal)))

    def test_spawning_needs_a_claimed_entry_and_a_settled_one_never_spawns(self):
        journal = LaunchJournal(self.directory)
        with self.assertRaises(Exception):
            journal.spawning(BRIDGE)
        claimed(journal)
        journal.settled(BRIDGE, 'settled')
        with self.assertRaises(Exception):
            journal.spawning(BRIDGE)

    def test_a_child_and_watchdog_that_are_both_gone_are_gone(self):
        journal = LaunchJournal(self.directory)
        root, watchdog = self.sleeper(), self.sleeper()
        entry = self.watched(journal, root, watchdog)
        self.assertEqual('alive', tree_state(entry))
        self.end(root)
        self.assertEqual('alive', tree_state(entry))  # Its watchdog still runs.
        self.end(watchdog)
        # Both are gone, but no watchdog confirmed the tree it tracked: not evidence.
        self.assertEqual('unknown', tree_state(entry))
        self.confirm(journal, root.pid, entry['child']['birth'], bridge=OTHER)  # Another run's confirmation.
        self.assertEqual('unknown', tree_state(entry))
        self.confirm(journal, root.pid, '1.000000')  # Another child's.
        self.assertEqual('unknown', tree_state(entry))
        self.confirm(journal, root.pid, entry['child']['birth'])
        self.assertEqual('gone', tree_state(entry))

    def test_a_watchdog_that_is_gone_does_not_hide_a_running_child(self):
        journal = LaunchJournal(self.directory)
        root, watchdog = self.sleeper(), self.sleeper()
        entry = self.watched(journal, root, watchdog)
        self.end(watchdog)
        self.assertEqual('alive', tree_state(entry))

    def test_a_descendant_left_in_the_childs_process_group_keeps_it_alive(self):
        journal = LaunchJournal(self.directory)
        root = self.started('/bin/sh', '-c', '/bin/sleep 30 & sleep 0.3', start_new_session=True)
        watchdog = self.sleeper()
        entry = self.watched(journal, root, watchdog, confirmed=True)
        root.wait(5)
        self.end(watchdog)
        self.assertEqual('alive', tree_state(entry))  # The background sleep is still in the group.
        os.killpg(root.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while tree_state(entry) != 'gone':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    def test_a_descendant_that_left_the_group_but_not_the_session_keeps_it_alive(self):
        journal = LaunchJournal(self.directory)
        code = ('import subprocess, time\n'
                "subprocess.Popen(['/bin/sleep', '30'], process_group=0)\n"
                'time.sleep(0.3)\n')
        root = self.started(sys.executable, '-I', '-c', code, start_new_session=True)
        watchdog = self.sleeper()
        entry = self.watched(journal, root, watchdog, confirmed=True)
        root.wait(5)
        self.end(watchdog)
        self.assertEqual('alive', tree_state(entry))
        # Only the session check still sees it: its group is its own now.
        found = [int(pid) for pid in subprocess.run(['/bin/ps', '-A', '-o', 'pid='], capture_output=True,
                                                     text=True).stdout.split() if self.in_session(int(pid), root.pid)]
        self.assertEqual(1, len(found))
        os.kill(found[0], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while tree_state(entry) != 'gone':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    @staticmethod
    def in_session(pid, session):
        try:
            return os.getsid(pid) == session and pid != session
        except OSError:
            return False

    def test_a_pid_reused_by_an_unrelated_process_is_never_taken_for_the_child(self):
        journal = LaunchJournal(self.directory)
        # An unrelated process in this test's own group and session now has the pid the
        # journal names, but it was born at another time: the child the journal saw is gone.
        unrelated, watchdog = self.started('/bin/sleep', '30'), self.sleeper()
        # A moment earlier: a long-lived process of this user's (an ssh-agent, say) predates it.
        real = birth(unrelated.pid)
        earlier = '%d.000000' % (int(real.split('.')[0]) - 1) if '.' in real else str(int(real) - 1)
        claimed(journal)
        journal.spawning(BRIDGE)
        journal.launched(BRIDGE, unrelated.pid, earlier, TAG)
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        self.confirm(journal, unrelated.pid, earlier)
        self.end(watchdog)
        self.assertEqual('gone', tree_state(self.entry(journal)))
        # The check signals nothing: the unrelated process is still running.
        self.assertIsNone(unrelated.poll())
        self.assertEqual(birth(unrelated.pid), process_record(unrelated.pid)[2])

    def test_a_setsid_escapee_whose_watchdog_never_confirmed_the_tree_is_never_settled(self):
        journal = LaunchJournal(self.directory)
        root, grandchild = self.escaping_root(pause=2)
        watchdog = self.real_watchdog(journal, root)
        os.killpg(watchdog.pid, signal.SIGKILL)  # The watchdog ends before it could confirm anything.
        watchdog.wait(5)
        root.wait(10)
        # Child and watchdog are gone and their groups and sessions are empty, but the
        # grandchild runs on in a session of its own: only the missing confirmation shows it.
        self.assertTrue(self.running(grandchild))
        self.assertFalse(journal.marker_path(BRIDGE).exists())
        self.assertEqual('unknown', tree_state(self.entry(journal)))

    def test_a_watchdog_that_confirms_its_tracked_tree_lets_the_run_settle(self):
        for escapes in (False, True):
            with self.subTest(escapes=escapes):
                journal = LaunchJournal(Path(self.temporary.name) / ('launches-%s' % escapes))
                if escapes:
                    # Seen by the watchdog before the child ends: it is killed with the tree.
                    root, grandchild = self.escaping_root(pause=2)
                else:
                    root, grandchild = self.started(sys.executable, '-I', '-c', 'import time; time.sleep(1)',
                                                    start_new_session=True), None
                watchdog = self.real_watchdog(journal, root)
                root.wait(10)
                watchdog.wait(15)
                self.assertEqual(0, watchdog.returncode)
                self.assertTrue(journal.marker_path(BRIDGE).exists())
                self.assertEqual(0o600, journal.marker_path(BRIDGE).stat().st_mode & 0o777)
                if grandchild is not None:
                    self.wait_until(lambda: not self.running(grandchild))
                self.wait_until(lambda: tree_state(self.entry(journal)) == 'gone')

    def test_a_detached_process_carrying_the_runs_marker_keeps_it_from_settling(self):
        # A descendant that started its own session after its parent exited between two of the
        # watchdog's snapshots: never tracked, so the watchdog confirmed an empty tree.
        journal = LaunchJournal(self.directory)
        root, watchdog = self.sleeper(), self.sleeper()
        entry = self.watched(journal, root, watchdog, confirmed=True)
        escapee = self.started(sys.executable, '-I', '-c', 'import time; time.sleep(30)', start_new_session=True,
                               env={'PATH': os.defpath, 'COCOLO_RUN_TAG': TAG})
        self.end(root)
        self.end(watchdog)
        self.assertEqual('alive', tree_state(entry))
        # Another run's marker, or none, is not this run's.
        other = dict(entry, run_tag=secrets.token_hex(16))
        self.assertEqual('gone', tree_state(other))
        self.end(escapee)
        self.assertEqual('gone', tree_state(entry))

    def test_a_launched_entry_needs_its_runs_marker(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        journal.spawning(BRIDGE)
        for tag in (None, 'E' * 32, 'e' * 31):
            with self.subTest(tag=tag), self.assertRaises(JournalError):
                journal.launched(BRIDGE, os.getpid(), birth(os.getpid()), tag)

    def test_the_kernel_checks_must_come_back_empty_twice(self):
        journal = LaunchJournal(self.directory)
        root, watchdog = self.sleeper(), self.sleeper()
        entry = self.watched(journal, root, watchdog, confirmed=True)
        self.end(root)
        self.end(watchdog)
        self.assertEqual('gone', tree_state(entry))
        # Something joins one of the sessions between the first scan and the second.
        answers = iter([False, True])
        with mock.patch.object(launch_journal, '_session_members', lambda sessions, *rest: next(answers)):
            self.assertEqual('alive', tree_state(entry))

    def test_sessions_of_other_users_processes_can_be_read(self):
        # launchd/init (pid 1) belongs to root. If getsid refused other users' processes,
        # every session scan would be unknown and no run could ever be settled.
        os.getsid(1)
        self.assertIs(False, launch_journal._session_members({0x7FFFFFF0}))

    def test_a_session_member_forked_by_one_that_exited_before_it_was_read_is_found(self):
        # 40000 was listed and gone before its read; it had forked 40001 into the session.
        listings = iter([{1: 0, 40000: 1}, {1: 0, 40001: 1}, {1: 0, 40001: 1}])
        sessions = {1: 1, 40001: 20000}
        def getsid(pid):
            if pid not in sessions:
                raise ProcessLookupError(pid)
            return sessions[pid]
        with mock.patch.object(launch_journal, '_listed_pids', lambda: next(listings)), \
                mock.patch.object(launch_journal.os, 'getsid', side_effect=getsid):
            self.assertIs(True, launch_journal._session_members({20000}))

    def test_a_new_session_on_a_reused_session_id_is_not_counted(self):
        # The child (pid 20000, born at 10) is gone; a later shell born at 50 holds its pid.
        records = {20000: (1, 20000, '50.000000', False), 40001: (20000, 20000, '51.000000', False),
                   40002: (1, 20000, '20.000000', False)}
        def run(members):
            with mock.patch.object(launch_journal, '_listed_pids', lambda: {1: 0, 20000: 1, **{member: 1 for member in members}}), \
                    mock.patch.object(launch_journal.os, 'getsid',
                                      side_effect=lambda pid: 1 if pid == 1 else 20000), \
                    mock.patch.object(launch_journal, 'process_record', side_effect=records.get), \
                    mock.patch.object(process_tree, 'process_record', side_effect=records.get):
                return launch_journal._session_members({20000}, {20000: '10.000000'})
        self.assertIs(False, run([40001]))  # Born after the new leader: its session, not the child's.
        self.assertIs(True, run([40002]))  # Born before it: left in the child's session.

    def test_a_lock_file_appears_only_once_its_instance_holds_it(self):
        other = LaunchJournal(self.directory)
        original = launch_journal.fcntl.flock

        def racing(descriptor, operation):
            # Another instance tidies stopped instances while this one is starting.
            for instance in other.instances():
                other.forget_instance(instance)
            return original(descriptor, operation)

        with mock.patch.object(launch_journal.fcntl, 'flock', racing):
            journal = LaunchJournal(self.directory)
        self.assertTrue((self.directory / (journal.instance + '.lock')).exists())
        self.assertFalse(other.instance_stopped(journal.instance))

    def test_a_lock_is_kept_while_an_entry_cannot_be_read_or_counted(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        path = self.directory / (BRIDGE + '.json')
        path.write_text('{"truncated"')
        other = LaunchJournal(self.directory)
        journal.release()
        self.assertTrue((self.directory / (journal.instance + '.lock')).exists())
        other.forget_instance(journal.instance)
        self.assertTrue((self.directory / (journal.instance + '.lock')).exists())
        path.unlink()
        claimed(other, OTHER)
        other.settled(OTHER, 'settled')
        # O10 D6 (a) replaced the listing cap here: every file is read, so the cap no longer keeps the lock
        # (test_D6a_a_stopped_instance_is_forgotten_although_the_journal_is_past_the_cap).
        other.forget_instance(journal.instance)
        self.assertFalse((self.directory / (journal.instance + '.lock')).exists())

    def test_nothing_is_written_once_the_lock_is_released(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        journal.release()
        with self.assertRaises(JournalError):
            journal.spawning(BRIDGE)
        with self.assertRaises(JournalError):
            journal.settled(BRIDGE, 'settled')
        with self.assertRaises(JournalError):
            claimed(journal, OTHER)

    def test_entries_that_cannot_be_read_are_unknown_and_left_in_place(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        claimed(journal, OTHER)
        path = self.directory / (OTHER + '.json')
        path.write_text('{"state": "claimed"')  # Truncated.
        (self.directory / ('brg_' + '3' * 32 + '.json')).write_text(json.dumps({'state': 'gone-for-sure'}))
        entries = journal.entries()
        unreadable = [entry for entry in entries if entry.get('bridge_run_id') != BRIDGE]
        self.assertEqual(2, len(unreadable))
        self.assertTrue(all(tree_state(entry) == 'unknown' for entry in unreadable))
        self.assertTrue(path.exists())
        # A watched entry whose pids are missing or malformed is not evidence either.
        path.unlink()
        claimed(journal, OTHER)
        journal.spawning(OTHER)
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'state': 'watched', 'child': None, 'watchdog': None}))
        self.assertEqual('unknown', tree_state(self.entry(journal, OTHER)))

    def test_an_instance_is_stopped_only_once_its_lock_is_released(self):
        first = LaunchJournal(self.directory)
        claimed(first)
        second = LaunchJournal(self.directory)
        self.assertNotEqual(first.instance, second.instance)
        self.assertFalse(second.instance_stopped(first.instance))
        self.assertFalse(second.instance_stopped('0' * 32))  # No lock file: nothing proves it stopped.
        first.release()
        self.assertTrue(second.instance_stopped(first.instance))
        self.assertEqual(first.instance, self.entry(second)['instance'])

    def test_files_are_owner_only(self):
        journal = LaunchJournal(self.directory)
        claimed(journal)
        self.assertEqual(0o700, self.directory.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.directory / (BRIDGE + '.json')).stat().st_mode & 0o777)

    # O10 D6 (a): every journal file is read, however many there are.
    @staticmethod
    def bridges(count):
        return ['brg_%032x' % (index + 1) for index in range(count)]

    def test_D6a_successive_listings_reach_every_entry_past_the_cap(self):
        journal = LaunchJournal(self.directory)
        names = self.bridges(7)
        for bridge in names:
            claimed(journal, bridge)
        seen = set()
        with mock.patch.object(launch_journal, 'MAX_ENTRIES', 3):
            for _ in range(3):  # Bounded work per pass: at most three files each time.
                listed = journal.entries()
                self.assertLessEqual(len(listed), 3)
                seen.update(entry['bridge_run_id'] for entry in listed)
        self.assertEqual(set(names), seen)

    def test_D6a_a_stopped_instance_is_forgotten_although_the_journal_is_past_the_cap(self):
        first = LaunchJournal(self.directory)
        second = LaunchJournal(self.directory)
        for bridge in self.bridges(5):
            claimed(second, bridge)
            second.settled(bridge, 'settled')
        claimed(first, 'brg_' + 'f' * 32)
        first.spawning('brg_' + 'f' * 32)
        first.release()
        lock = self.directory / (first.instance + '.lock')
        self.assertTrue(lock.exists())  # Kept while its run is unsettled.
        second.settled('brg_' + 'f' * 32, 'settled')  # Settled later, by a later instance.
        with mock.patch.object(launch_journal, 'MAX_ENTRIES', 2):
            second.forget_instance(first.instance)
        self.assertFalse(lock.exists())

    def test_D6a_an_unsettled_entry_past_the_cap_still_keeps_its_instances_lock(self):
        first = LaunchJournal(self.directory)
        second = LaunchJournal(self.directory)
        for bridge in self.bridges(5):
            claimed(second, bridge)
        claimed(first, 'brg_' + 'f' * 32)  # Sorted last: past any cap below six.
        first.spawning('brg_' + 'f' * 32)
        first.release()
        with mock.patch.object(launch_journal, 'MAX_ENTRIES', 2):
            second.forget_instance(first.instance)
        self.assertTrue((self.directory / (first.instance + '.lock')).exists())

    def test_D6a_old_tombstones_past_the_cap_are_pruned_in_later_passes(self):
        journal = LaunchJournal(self.directory)
        names = self.bridges(5)
        now = 2 + launch_journal.TOMBSTONE_MS
        for index, bridge in enumerate(names):
            claimed(journal, bridge)
            journal.settled(bridge, 'settled', now_ms=now if index < 2 else 1)  # The first two are recent.
        names = names[2:]
        with mock.patch.object(launch_journal, 'MAX_ENTRIES', 2):
            for _ in range(3):
                journal.prune(now_ms=now)
        self.assertEqual([], [bridge for bridge in names if (self.directory / (bridge + '.json')).exists()])

    # O10 D6 (b): a lock temporary whose creator is gone is removed.
    def lock_temporary(self, mode=0o600):
        path = self.directory / ('.%s.lock.tmp' % secrets.token_hex(16))
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, mode)
        os.fchmod(descriptor, mode)
        return path, descriptor

    def test_D6b_a_lock_temporary_left_by_a_stopped_creator_is_removed(self):
        journal = LaunchJournal(self.directory)
        path, descriptor = self.lock_temporary()
        os.close(descriptor)  # Its creator stopped before it held the lock or linked it into place.
        journal.prune()
        self.assertFalse(os.path.lexists(path))
        self.assertTrue((self.directory / (journal.instance + '.lock')).exists())

    def test_D6b_only_a_checked_unheld_lock_temporary_is_removed(self):
        journal = LaunchJournal(self.directory)
        kept = []
        held, descriptor = self.lock_temporary()  # Its creator is still starting: it holds the lock.
        launch_journal.fcntl.flock(descriptor, launch_journal.fcntl.LOCK_EX | launch_journal.fcntl.LOCK_NB)
        self.addCleanup(os.close, descriptor)
        kept.append(held)
        linked, other = self.lock_temporary()  # Linked into place but not yet unlinked: two links.
        os.close(other)
        os.link(linked, self.directory / (secrets.token_hex(16) + '.lock.other'))
        kept.append(linked)
        shared, other = self.lock_temporary(0o644)  # Not owner-only.
        os.close(other)
        kept.append(shared)
        target = Path(self.temporary.name) / 'target'
        target.write_text('x')
        target.chmod(0o600)
        symlink = self.directory / ('.%s.lock.tmp' % secrets.token_hex(16))
        symlink.symlink_to(target)
        kept.append(symlink)
        folder = self.directory / ('.%s.lock.tmp' % secrets.token_hex(16))
        folder.mkdir(mode=0o700)
        kept.append(folder)
        unrelated = self.directory / '.notes.lock.tmp'
        unrelated.write_text('x')
        unrelated.chmod(0o600)
        kept.append(unrelated)
        journal.prune()
        self.assertEqual([], [path.name for path in kept if not os.path.lexists(path)])
        self.assertTrue(target.exists())

    def test_D6b_a_journal_still_starts_when_its_temporary_is_removed_before_it_holds_it(self):
        other = LaunchJournal(self.directory)
        original = launch_journal.fcntl.flock
        raced = []

        def racing(descriptor, operation):
            # Another instance tidies the folder between this one's create and its lock.
            if not raced:
                raced.append(True)
                other.prune()
            return original(descriptor, operation)

        with mock.patch.object(launch_journal.fcntl, 'flock', racing):
            journal = LaunchJournal(self.directory)
        self.assertTrue(raced)
        self.assertTrue((self.directory / (journal.instance + '.lock')).exists())
        self.assertFalse(other.instance_stopped(journal.instance))
        self.assertEqual([], [name for name in os.listdir(self.directory) if name.endswith('.lock.tmp')])

    # O10 D6 (c): an entry that cannot be settled is shown as unresolved, with its age.
    def test_D6c_the_never_settling_states_stay_unknown(self):
        """Today's proof is kept: no new evidence of 'gone' for these states (a kernel-group design is coming)."""
        journal = LaunchJournal(self.directory)
        claimed(journal)
        journal.spawning(BRIDGE)
        self.assertEqual('unknown', tree_state(self.entry(journal)))
        root = self.sleeper()
        journal.launched(BRIDGE, root.pid, birth(root.pid), TAG)
        self.end(root)
        self.assertEqual('unknown', tree_state(self.entry(journal)))
        watchdog = self.sleeper()
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        self.end(watchdog)
        self.assertEqual('unknown', tree_state(self.entry(journal)))  # No confirmation from its watchdog.

    def test_D6c_unresolved_lists_unsettled_entries_of_stopped_instances_with_their_age(self):
        stopped = LaunchJournal(self.directory)
        names = self.bridges(5)
        claimed(stopped, names[0])
        stopped.spawning(names[0])
        claimed(stopped, names[1])
        stopped.spawning(names[1])
        stopped.launched(names[1], 4242, '1.000000', TAG)
        claimed(stopped, names[2])
        stopped.settled(names[2], 'settled')
        stopped.release()
        live = LaunchJournal(self.directory)
        claimed(live, names[3])
        live.spawning(names[3])  # Its own service still runs it: not unresolved.
        (self.directory / (names[4] + '.json')).write_text('{"truncated"')
        os.chmod(self.directory / (names[4] + '.json'), 0o600)
        changed = time.time() - 120
        for name in (names[0], names[1], names[4]):
            os.utime(self.directory / (name + '.json'), (changed, changed))
        now_ms = int(time.time() * 1000)
        report = launch_journal.unresolved(self.directory, now_ms=now_ms)
        self.assertEqual(3, report['count'])
        listed = {item['bridge_run_id']: item for item in report['entries']}
        self.assertEqual({names[0]: 'spawning', names[1]: 'launched', names[4]: None},
                         {name: item['state'] for name, item in listed.items()})
        for item in listed.values():
            self.assertGreaterEqual(item['age_ms'], 119_000)
            self.assertLess(item['age_ms'], 600_000)
        self.assertEqual(max(item['age_ms'] for item in listed.values()), report['oldest_age_ms'])

    def test_D6c_unresolved_is_bounded_and_empty_without_a_journal(self):
        self.assertEqual({'count': 0, 'oldest_age_ms': None, 'entries': []},
                         launch_journal.unresolved(self.directory / 'missing'))
        stopped = LaunchJournal(self.directory)
        for bridge in self.bridges(launch_journal.MAX_UNRESOLVED_LISTED + 2):
            claimed(stopped, bridge)
            stopped.spawning(bridge)
        stopped.release()
        report = launch_journal.unresolved(self.directory)
        self.assertEqual(launch_journal.MAX_UNRESOLVED_LISTED + 2, report['count'])
        self.assertEqual(launch_journal.MAX_UNRESOLVED_LISTED, len(report['entries']))

    def test_D6c_status_shows_the_projects_unresolved_launches(self):
        from codex_agents import client_runtime
        from codex_agents.learning_store import LearningState, authorize_collector_runtime
        from tests.support import ORCHESTRATION_ROOT
        base = Path(self.temporary.name)
        state = LearningState(base / 'state')
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / 'cli.py')
        project = base / 'project'
        (project / '.git').mkdir(parents=True)
        project_id = 'prj_' + 'a' * 32
        (Path(state.root) / 'projects' / project_id).mkdir(parents=True, mode=0o700)
        stopped = LaunchJournal(Path(state.root) / 'projects' / project_id / 'launches')
        claimed(stopped)
        stopped.spawning(BRIDGE)
        stopped.release()
        owner = mock.Mock()
        owner.owner_key.return_value = {'state': 'present', 'fingerprint': 'f', 'replacements': 0,
                                        'last_replaced_at_ms': None}
        with mock.patch.object(client_runtime, '_account_target', return_value=(project_id, 'e')), \
                mock.patch.object(client_runtime, 'enroll_clients'), \
                mock.patch.object(client_runtime, 'AccountClient', return_value=owner), \
                mock.patch.object(client_runtime.CollectorClient, 'health',
                                  side_effect=client_runtime.CollectorError('x')):
            report = client_runtime.status_client(state.root, project)
        self.assertEqual(1, report['unresolved_launches']['count'])
        self.assertEqual([(BRIDGE, 'spawning')], [(item['bridge_run_id'], item['state'])
                                                 for item in report['unresolved_launches']['entries']])


if __name__ == '__main__':
    unittest.main()
