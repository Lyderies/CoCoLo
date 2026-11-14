"""The launch journal a project service keeps for the runs it claimed, and its liveness evidence.

A later service settles an earlier service's run only on positive kernel
evidence that the child, its process group and session, and its watchdog are
gone. Anything unreadable or ambiguous is not evidence.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import TEMP_ROOT

from codex_agents.launch_journal import LaunchJournal, tree_state
from codex_agents.process_tree import process_record

BRIDGE = 'brg_' + '1' * 32
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

    def watched(self, journal, root, watchdog):
        claimed(journal)
        journal.spawning(BRIDGE)
        journal.launched(BRIDGE, root.pid, birth(root.pid))
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        return self.entry(journal)

    @staticmethod
    def entry(journal, bridge=BRIDGE):
        return [entry for entry in journal.entries() if entry.get('bridge_run_id') == bridge][0]

    @staticmethod
    def end(process):
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(5)

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
        journal.launched(BRIDGE, root.pid, birth(root.pid))
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
        entry = self.watched(journal, root, watchdog)
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
        entry = self.watched(journal, root, watchdog)
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
        claimed(journal)
        journal.spawning(BRIDGE)
        journal.launched(BRIDGE, unrelated.pid, '1.000000')
        journal.watched(BRIDGE, watchdog.pid, birth(watchdog.pid))
        self.end(watchdog)
        self.assertEqual('gone', tree_state(self.entry(journal)))
        # The check signals nothing: the unrelated process is still running.
        self.assertIsNone(unrelated.poll())
        self.assertEqual(birth(unrelated.pid), process_record(unrelated.pid)[2])

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


if __name__ == '__main__':
    unittest.main()
