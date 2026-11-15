"""Owned process cleanup and durable capacity release; no real AI client."""
from __future__ import annotations
from tests.support import SHORT_TMP

from copy import copy
from copy import deepcopy
import contextlib
import itertools
import json
import os
from pathlib import Path
import signal
import struct
import sys
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.test_bridge as fixtures
from codex_agents import bridge, process_tree
from codex_agents import learning_store
from codex_agents import owned_execution
from codex_agents.learning_store import LearningLedger, LedgerError
from codex_agents.owned_execution import ExecutionCustodyError, ExecutionTicket, OwnedExecutionKey, OwnedExecutionRegistry


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _procargs2(path, argv, environment, apple):
    """A KERN_PROCARGS2 buffer as macOS lays it out: argc, the executable path padded
    with NULs so argv starts 8-byte aligned after argc, then argv, environment and
    the kernel's own strings, each list ended by an empty string."""
    start = 4 + -(-(len(path) + 1) // 8) * 8
    head = struct.pack("=i", len(argv)) + path
    head += b"\0" * (start - len(head))
    return head + b"".join(item + b"\0" for item in argv + environment) + b"\0" + b"".join(item + b"\0" for item in apple)


class RunMarkerParsingTests(unittest.TestCase):
    """The per-run marker is read from each process's environment, never its arguments."""

    def test_procargs2_yields_the_environment_after_argv(self):
        raw = _procargs2(b"/bin/sh", [b"sh", b"COCOLO_RUN_TAG=" + b"a" * 32],
                         [b"A=1", b"COCOLO_RUN_TAG=" + b"b" * 32], [b"ptr_munge=1"])
        self.assertEqual((b"/bin/sh", [b"A=1", b"COCOLO_RUN_TAG=" + b"b" * 32]), process_tree.parse_procargs2(raw))
        # What macOS returns for one of its own signed tools: the arguments, no environment.
        self.assertEqual((b"/bin/sleep", []),
                         process_tree.parse_procargs2(_procargs2(b"/bin/sleep", [b"/bin/sleep", b"5"], [], [])))
        for broken in (b"", b"\1\0", struct.pack("=i", 5) + b"/bin/sh\0a\0b\0", struct.pack("=i", -1) + b"x\0",
                       struct.pack("=i", 1) + b"/bin/sh-without-end",
                       struct.pack("=i", 1) + b"/bin/sleep\0x\0\0\0\0\0"):  # Padding cut short by a string.
            with self.subTest(raw=broken):
                self.assertIsNone(process_tree.parse_procargs2(broken))

    def test_procargs2_keeps_the_environment_boundary_after_an_empty_argv0(self):
        tag = b"COCOLO_RUN_TAG=" + b"c" * 32
        for path in (b"/bin/sh", b"/usr/bin/x", b"/opt/homebrew/bin/python3.14"):
            with self.subTest(path=path):
                raw = _procargs2(path, [b"", b"-c"], [tag, b"A=1"], [])
                self.assertEqual((path, [tag, b"A=1"]), process_tree.parse_procargs2(raw))

    def test_linux_environ_splits_on_nul(self):
        self.assertEqual([b"A=1", b"B="], process_tree.parse_environ(b"A=1\0B=\0"))
        self.assertEqual([], process_tree.parse_environ(b""))

    def test_a_marker_is_a_fresh_128_bit_value(self):
        first, second = process_tree.new_run_tag(), process_tree.new_run_tag()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(b"COCOLO_RUN_TAG=" + first.encode(), process_tree.run_tag_entry(first))

    def test_birth_times_compare_as_numbers(self):
        self.assertTrue(process_tree._born_before("9.999999", "10.000000"))
        self.assertFalse(process_tree._born_before("10.000000", "10.000000"))
        self.assertTrue(process_tree._born_before("99", "100"))
        self.assertFalse(process_tree._born_before("unreadable", "10.000000"))  # Unknown: it must be read.

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "kernel process records")
    def test_a_real_processs_environment_is_read(self):
        tag = process_tree.new_run_tag()
        # This interpreter, not a macOS system tool, whose environment macOS withholds.
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                                   env={"PATH": os.defpath, "COCOLO_RUN_TAG": tag}, start_new_session=True)
        try:
            for _ in range(200):
                environment = process_tree.process_environment(process.pid)
                if environment is not None and process_tree.run_tag_entry(tag) in environment:
                    break
                time.sleep(0.01)
            self.assertIn(process_tree.run_tag_entry(tag), environment)
            own = process_tree.process_record(os.getpid())[2]
            self.assertTrue(process_tree.tagged_process_running(tag, own))
            # This process itself never counts, and a fresh marker matches nothing.
            self.assertFalse(process_tree.tagged_process_running(process_tree.new_run_tag(), own))
        finally:
            process.kill()
            process.wait()
        self.assertFalse(process_tree.tagged_process_running(tag, own))


class UnreadableParentageTests(unittest.TestCase):
    """An unreadable process born since the child is judged by the parent its environment came from.

    The pids are made up, so Linux's systemd check (it reads /proc) is held to "not systemd".
    """

    TAG = "f" * 32
    CHILD_BIRTH = "10.000000"

    def scan(self, processes, *, sessions=None):
        """`processes`: pid -> (parent, birth, environment or None when unreadable)."""
        table = {pid: value[0] for pid, value in processes.items()}
        records = {pid: (value[0], pid, value[1], False) for pid, value in processes.items()}
        arguments = {pid: (b"/opt/tool", value[2]) for pid, value in processes.items() if value[2] is not None}
        sessions = sessions or {}
        with mock.patch.object(process_tree, "process_table", return_value=table), \
                mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree, "process_gone", side_effect=lambda pid: pid not in records), \
                mock.patch.object(process_tree, "process_arguments", side_effect=arguments.get), \
                mock.patch.object(process_tree, "_in_session",
                                  side_effect=lambda pid, session, *rest: sessions.get(pid) == session), \
                mock.patch.object(process_tree, "_subreaper", return_value=False), \
                mock.patch.object(process_tree.time, "sleep"):
            return process_tree.tagged_process_running(self.TAG, self.CHILD_BIRTH, 20000)

    def tagged(self):
        return [b"COCOLO_RUN_TAG=" + self.TAG.encode()]

    def test_an_unreadable_process_with_an_untagged_readable_parent_does_not_block(self):
        # `login` (setuid) for a Terminal tab opened during the run: its parent is Terminal.
        self.assertIs(False, self.scan({500: (1, "1.000000", []), 30000: (500, "11.000000", None)}))
        # A parent born since the child, readable and untagged, decides the same way.
        self.assertIs(False, self.scan({500: (1, "10.500000", [b"PATH=/bin"]), 30000: (500, "11.000000", None)}))

    def test_an_unreadable_process_whose_readable_parent_is_tagged_blocks(self):
        self.assertIs(True, self.scan({500: (1, "10.500000", self.tagged()), 30000: (500, "11.000000", None)}))
        # Its parent still in the child's session decides too.
        self.assertIs(True, self.scan({500: (1, "10.500000", None), 30000: (500, "11.000000", None)},
                                      sessions={500: 20000}))

    def test_an_unreadable_detached_process_born_since_the_child_is_not_proof(self):
        self.assertIsNone(self.scan({30000: (1, "11.000000", None)}))
        # Every step up is unreadable and born since the child, up to launchd.
        self.assertIsNone(self.scan({500: (1, "10.500000", None), 30000: (500, "11.000000", None)}))

    def test_a_parent_that_cannot_be_the_listed_one_is_not_proof(self):
        # Born after its supposed child: the pid passed to another process.
        self.assertIsNone(self.scan({500: (1, "12.000000", []), 30000: (500, "11.000000", None)}))
        # A parent the listing does not have.
        self.assertIsNone(self.scan({30000: (500, "11.000000", None)}))

    def test_the_walk_up_is_bounded(self):
        depth = process_tree.MAX_PARENT_DEPTH
        for length, expected in ((depth - 1, False), (depth + 1, None)):
            with self.subTest(length=length):
                processes = {1000: (1, "1.000000", [])}  # An untagged ancestor born before the child.
                for index in range(length):
                    processes[1001 + index] = (1000 + index, "11.%06d" % index, None)
                self.assertIs(expected, self.scan(processes))


class ScanStabilityTests(unittest.TestCase):
    """A scan lists again until no new process appears, so a fork-and-exit cannot slip between."""

    TAG = "e" * 32
    CHILD_BIRTH = "10.000000"

    def scan(self, listings, processes, gone_after_listing=()):
        """`listings`: the pid sets `ps` returns in turn (the last repeats); `processes`:
        pid -> (parent, birth, environment); pids in `gone_after_listing` read as reaped."""
        listings = list(listings)
        def table():
            pids = listings.pop(0) if len(listings) > 1 else listings[0]
            return {pid: processes[pid][0] for pid in pids}
        records = {pid: (value[0], pid, value[1], False) for pid, value in processes.items()
                   if pid not in gone_after_listing}
        arguments = {pid: (b"/opt/tool", value[2]) for pid, value in processes.items()
                     if pid not in gone_after_listing}
        with mock.patch.object(process_tree, "process_table", side_effect=table), \
                mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree, "process_gone", side_effect=lambda pid: pid not in records), \
                mock.patch.object(process_tree, "process_arguments", side_effect=arguments.get), \
                mock.patch.object(process_tree, "_in_session", return_value=False), \
                mock.patch.object(process_tree, "_subreaper", return_value=False), \
                mock.patch.object(process_tree.time, "sleep"):
            return process_tree.tagged_process_running(self.TAG, self.CHILD_BIRTH, 20000)

    def tagged(self):
        return [b"COCOLO_RUN_TAG=" + self.TAG.encode()]

    def test_a_tagged_process_forked_by_one_that_exited_before_it_was_read_is_found(self):
        # 30000, already an orphan, was listed, forked 30001 and was reaped before its read;
        # 30001 was not listed yet.
        processes = {500: (1, "1.000000", []), 30000: (1, "11.000000", self.tagged()),
                     30001: (1, "12.000000", self.tagged())}
        self.assertIs(True, self.scan([{500, 30000}, {500, 30001}], processes, gone_after_listing={30000}))

    def test_processes_ending_under_a_clean_parent_do_not_extend_the_scan(self):
        # A busy machine: short-lived processes under a readable, untagged parent (`make`, say)
        # end before they are read in every listing; they could not have carried the marker.
        processes = {500: (1, "1.000000", [])}
        listings = []
        for index in range(process_tree.MAX_SCAN_ROUNDS * process_tree.TAG_SCAN_ATTEMPTS + 2):
            processes[30000 + index] = (500, "11.%06d" % index, [b"A=1"])
            listings.append({500, 30000 + index})
        gone = {30000 + index for index in range(len(listings))}
        self.assertIs(False, self.scan(listings, processes, gone_after_listing=gone))

    def test_orphans_that_keep_ending_unread_are_not_proof(self):
        processes = {500: (1, "1.000000", [])}
        listings = []
        # A new orphan ending unread in every listing, for longer than every attempt's rounds.
        for index in range((process_tree.MAX_SCAN_ROUNDS + 1) * process_tree.TAG_SCAN_ATTEMPTS + 2):
            processes[30000 + index] = (1, "11.%06d" % index, [b"A=1"])
            listings.append({500, 30000 + index})
        gone = {30000 + index for index in range(len(listings))}
        self.assertIsNone(self.scan(listings, processes, gone_after_listing=gone))


class ReusedSessionIdTests(unittest.TestCase):
    """After the child is reaped its pid may lead a new, unrelated session."""

    def test_a_process_of_a_new_session_on_the_childs_reused_pid_is_not_counted(self):
        child_birth = "10.000000"
        records = {20000: (1, 20000, "50.000000", False)}  # A later shell now holds the child's pid.
        with mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree.os, "getsid", return_value=20000):
            reused = process_tree.session_reused_at(20000, child_birth)
            self.assertEqual("50.000000", reused)
            # Born after the new leader: a member of its session, not the child's.
            self.assertFalse(process_tree._in_session(30000, 20000, "51.000000", reused))
            # Born before it: still in the child's old session.
            self.assertTrue(process_tree._in_session(30000, 20000, "20.000000", reused))
        records = {20000: (1, 20000, child_birth, False)}
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertIsNone(process_tree.session_reused_at(20000, child_birth))

class CleanupEvidenceTests(unittest.TestCase):
    def identified(self, child_exited=False):
        root, child = 20000, 20001
        records = {root: (1, root, "root", False), child: (root, root, "child", child_exited)}
        tree = process_tree.ProcessTree(root)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertTrue(tree.identify({root: 1, child: root}))
        return tree, records, SimpleNamespace(pid=root, returncode=0)

    def test_unreadable_descendant_is_not_forgotten_as_cleanup_proof(self):
        tree, records, root = self.identified()
        records.pop(20001)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree, "process_gone", return_value=False):  # Unreadable, not gone.
            tree.update({20000: 1})
            self.assertNotIn(20001, tree.tracked)
            self.assertFalse(tree.cleanup_after_owned_wait(root))
        self.assertIn((20001, "child"), tree._cleanup_seen)

    def test_unresolved_adoption_candidate_cannot_prove_a_clean_tree(self):
        for candidate in (None, (1, 20001, "escaped", False)):
            with self.subTest(candidate=candidate):
                records = {20000: (1, 20000, "root", False), 20001: candidate}
                tree = process_tree.ProcessTree(20000)
                with mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                        mock.patch.object(process_tree, "process_gone", return_value=False):
                    self.assertTrue(tree.identify({20000: 1, 20001: 20000}))
                    self.assertNotIn(20001, tree.tracked)
                    self.assertFalse(tree.cleanup_after_owned_wait(SimpleNamespace(pid=20000, returncode=0)))

    def test_a_descendant_that_exited_and_was_reaped_counts_as_stopped(self):
        # A child's quick shell command: its parent reaps it, so its pid is simply gone.
        tree, records, root = self.identified()
        records.pop(20001)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree, "process_gone", side_effect=lambda pid: pid == 20001):
            tree.update({20000: 1})
            self.assertTrue(tree.cleanup_after_owned_wait(root))
        # A candidate already reaped between the snapshot and its read is no loss of tracking either.
        records = {20000: (1, 20000, "root", False)}
        tree = process_tree.ProcessTree(20000)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get), \
                mock.patch.object(process_tree, "process_gone", side_effect=lambda pid: pid == 20001):
            self.assertTrue(tree.identify({20000: 1, 20001: 20000}))
            self.assertTrue(tree.cleanup_after_owned_wait(SimpleNamespace(pid=20000, returncode=0)))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "kernel process records")
    def test_gone_needs_positive_evidence(self):
        reaped = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        reaped.wait()
        self.assertTrue(process_tree.process_gone(reaped.pid))
        self.assertFalse(process_tree.process_gone(os.getpid()))  # Running.
        self.assertFalse(process_tree.process_gone(1))  # Never claimed for init/launchd.
        with mock.patch.object(process_tree.sys, "platform", "plan9"):
            self.assertFalse(process_tree.process_gone(reaped.pid))  # No kernel read: no evidence.

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "kernel process records")
    def test_a_real_child_with_many_quick_grandchildren_is_confirmed_clean(self):
        # What a real Codex child does: run short shell commands, each reaped by the child itself.
        script = "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do /bin/echo $i >/dev/null; done"
        process = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True)
        tree = process_tree.ProcessTree(process.pid)
        self.assertTrue(tree.identify(process_tree.process_table()))
        while process.poll() is None:
            table = process_tree.process_table()
            if table is not None:
                tree.update(table)
        self.assertTrue(tree.cleanup_after_owned_wait(process))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "kernel process records")
    def test_a_grandchild_that_escapes_still_refuses_confirmation(self):
        # A descendant that outlives its parent (reparented, still running) is never proof.
        process = subprocess.Popen(["/bin/sh", "-c", "/bin/sleep 5 & sleep 0.3"], start_new_session=True)
        tree = process_tree.ProcessTree(process.pid)
        self.assertTrue(tree.identify(process_tree.process_table()))
        while process.poll() is None:
            table = process_tree.process_table()
            if table is not None:
                tree.update(table)
        try:
            self.assertFalse(tree.cleanup_after_owned_wait(process))
        finally:
            os.killpg(process.pid, signal.SIGKILL)

    def tagged(self, *, escapee=None, environment=None):
        """An identified tree whose child carries a run marker; `escapee` is a detached process outside it."""
        tag = "f" * 32
        root, child = 20000, 20001
        records = {root: (1, root, "root", False), child: (root, root, "child", True)}
        tree = process_tree.ProcessTree(root, tag)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertTrue(tree.identify({root: 1, child: root}))
        if escapee is not None:
            records[30000] = escapee
        arguments = {30000: None if environment is None else (b"/opt/tool", environment)}
        patches = contextlib.ExitStack()
        patches.enter_context(mock.patch.object(process_tree, "process_record", side_effect=records.get))
        patches.enter_context(mock.patch.object(process_tree, "process_gone", side_effect=lambda pid: pid not in records))
        patches.enter_context(mock.patch.object(process_tree, "process_arguments", side_effect=arguments.get))
        patches.enter_context(mock.patch.object(process_tree, "process_table",
                                                return_value={30000: 1} if escapee else {}))  # The root is reaped.
        patches.enter_context(mock.patch.object(process_tree.time, "sleep"))
        self.sessions = {}
        patches.enter_context(mock.patch.object(process_tree, "_in_session",
                                                side_effect=lambda pid, session, *rest: self.sessions.get(pid) == session))
        return tree, records, SimpleNamespace(pid=root, returncode=0), patches, tag

    def test_a_detached_process_that_carries_the_run_marker_refuses_confirmation(self):
        # A grandchild that called setsid after its parent exited between two snapshots:
        # never adopted, reparented to init, still running with the child's environment.
        tag = "f" * 32
        tree, records, root, patches, _ = self.tagged(
            escapee=(1, 30000, "late", False), environment=[b"PATH=/bin", b"COCOLO_RUN_TAG=" + tag.encode()])
        with patches:
            self.assertFalse(tree.cleanup_after_owned_wait(root))
            records[20000] = (*records[20000][:3], True)
            self.assertFalse(tree.cleanup_confirmed())
            records[30000] = (1, 30000, "late", True)  # It exits: confirmation passes.
            self.assertTrue(tree.cleanup_after_owned_wait(root))
            self.assertTrue(tree.cleanup_confirmed())

    def test_a_detached_process_without_the_marker_does_not_block_confirmation(self):
        for environment in ([b"PATH=/bin"], [b"COCOLO_RUN_TAG=" + b"0" * 32], [b"COCOLO_RUN_TAG=" + b"f" * 31]):
            with self.subTest(environment=environment):
                tree, _, root, patches, _ = self.tagged(escapee=(1, 30000, "late", False), environment=environment)
                with patches:
                    self.assertTrue(tree.cleanup_after_owned_wait(root))

    def test_an_unreadable_environment_counts_only_for_a_process_born_since_the_child(self):
        tree, records, root, patches, _ = self.tagged(escapee=(1, 30000, "9.000000", False), environment=None)
        with patches:
            tree.root_birth = "10.000000"
            records[20000] = (1, 20000, "10.000000", False)
            tree._cleanup_seen = {(20000, "10.000000"): False, (20001, "child"): True}
            # Born before the child: it cannot have inherited the marker, so it is not read at all.
            self.assertTrue(tree.cleanup_after_owned_wait(root))
            records[30000] = (1, 30000, "11.000000", False)
            # Born since and unreadable: not proof.
            self.assertFalse(tree.cleanup_after_owned_wait(root))
            # Unreadable because its pid passed to a new process while being read: no longer counts.
            reads = itertools.cycle([(1, 30000, "11.000000", False), (1, 30000, "12.000000", False)])
            with mock.patch.object(process_tree, "process_record",
                                   side_effect=lambda pid: next(reads) if pid == 30000 else records.get(pid)):
                self.assertTrue(tree.cleanup_after_owned_wait(root))

    def test_a_process_left_in_the_childs_session_refuses_confirmation(self):
        # One that never called setsid: whatever its environment says (even nothing at all, as
        # macOS shows for its own system tools), it is still in the session the child started.
        tree, records, root, patches, _ = self.tagged(escapee=(1, 30000, "late", False), environment=[])
        with patches:
            self.assertTrue(tree.cleanup_after_owned_wait(root))
            self.sessions[30000] = 20000
            self.assertFalse(tree.cleanup_after_owned_wait(root))
            records[30000] = (1, 30000, "late", True)
            self.assertTrue(tree.cleanup_after_owned_wait(root))

    def test_a_lost_process_table_or_too_many_processes_is_not_proof(self):
        tree, _, root, patches, tag = self.tagged(escapee=(1, 30000, "late", False), environment=[b"PATH=/bin"])
        with patches:
            with mock.patch.object(process_tree, "process_table", return_value=None):
                self.assertFalse(tree.cleanup_after_owned_wait(root))
            with mock.patch.object(process_tree, "MAX_TAG_SCAN_PROCESSES", 0):
                self.assertFalse(tree.cleanup_after_owned_wait(root))
            self.assertTrue(tree.cleanup_after_owned_wait(root))
        self.assertIsNone(process_tree.tagged_process_running("not-a-tag", "1.000000"))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "kernel process records")
    def test_a_real_setsid_escapee_carrying_the_marker_refuses_confirmation(self):
        # The child forks; that fork starts a new session, forks the sleeper and exits at once,
        # so no snapshot ever links the sleeper to the tracked tree.
        interpreter = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
        cases = {
            # A new session, but its environment can be read: the marker is found.
            "setsid interpreter": (interpreter, True),
            # No new session: found in the child's session, even as a macOS system tool,
            # whose environment macOS withholds from other processes.
            "system tool in the session": (["/bin/sleep", "30"], False),
        }
        if sys.platform.startswith("linux"):
            cases["setsid system tool"] = (["/bin/sleep", "30"], True)  # Linux shows its environment.
        for name, (sleeper, setsid) in cases.items():
            with self.subTest(case=name):
                self.real_escapee(sleeper, setsid)

    def real_escapee(self, sleeper, setsid):
        tag = process_tree.new_run_tag()
        code = ("import os, sys, time\n"
                "reader, writer = os.pipe()\n"
                "middle = os.fork()\n"
                "if middle == 0:\n"
                "    if sys.argv[1] == 'setsid':\n"
                "        os.setsid()\n"
                "    grandchild = os.fork()\n"
                "    if grandchild == 0:\n"
                "        os.execv(sys.argv[2], sys.argv[2:])\n"
                "    os.write(writer, b'%d\\n' % grandchild)\n"
                "    os._exit(0)\n"
                "os.waitpid(middle, 0)\n"
                "sys.stdout.write(os.read(reader, 64).decode()); sys.stdout.flush()\n"
                "time.sleep(0.5)\n")
        process = subprocess.Popen([sys.executable, "-I", "-c", code, "setsid" if setsid else "stay", *sleeper],
                                   stdout=subprocess.PIPE,
                                   env={"PATH": os.defpath, process_tree.RUN_TAG_VARIABLE: tag},
                                   start_new_session=True)
        grandchild = int(process.stdout.readline())
        process.stdout.close()
        self.addCleanup(_kill_quietly, grandchild)
        tree = process_tree.ProcessTree(process.pid, tag)
        self.assertTrue(tree.identify(process_tree.process_table()))
        self.assertNotIn(grandchild, tree.tracked)  # Already detached: never adopted.
        while process.poll() is None:
            table = process_tree.process_table()
            if table is not None:
                tree.update(table)
        self.assertIs(True, process_tree.tagged_process_running(tag, tree.root_birth, tree.root))
        self.assertFalse(tree.cleanup_after_owned_wait(process))
        # The same tree without the marker would have been confirmed with the sleeper still running.
        untagged = copy(tree)
        untagged.run_tag = None
        self.assertTrue(untagged.cleanup_after_owned_wait(process))
        _kill_quietly(grandchild)
        deadline = time.monotonic() + 10
        while process_tree.tagged_process_running(tag, tree.root_birth, tree.root) is not False:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertTrue(tree.cleanup_after_owned_wait(process))

    def test_positive_exit_survives_later_disappearance(self):
        tree, records, root = self.identified(child_exited=True)
        records.pop(20001)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            tree.update({20000: 1})
            self.assertTrue(tree.cleanup_after_owned_wait(root))

    def test_running_descendant_snapshot_loss_and_overflow_refuse_confirmation(self):
        tree, records, root = self.identified()
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertFalse(tree.cleanup_after_owned_wait(root))
            tree._cleanup_incomplete = True
            records[20001] = (*records[20001][:3], True)
            self.assertFalse(tree.cleanup_after_owned_wait(root))
        with mock.patch.object(process_tree, "MAX_CLEANUP_IDENTITIES", 1):
            tree, records, root = self.identified(child_exited=True)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertFalse(tree.cleanup_after_owned_wait(root))


class OwnedExecutionRegistryTests(unittest.TestCase):
    def key(self):
        return OwnedExecutionKey("prj_" + "a" * 32, "b" * 64, "run:" + "c" * 64, "brg_" + "d" * 32)

    def owned_ticket(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        entry = registry._entry(ticket)
        self.addCleanup(self.reap_fixture, entry)
        return registry, ticket, entry

    def reap_fixture(self, entry):
        process = entry.process
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def spawn_sleep(self, ticket):
        return ticket.spawn([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={"PATH": os.defpath}, cwd=SHORT_TMP, start_new_session=True)

    def assert_custody_held(self, registry, ticket):
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)
        with self.assertRaises(ExecutionCustodyError):
            registry.enter(ticket)

    def test_factory_refuses_a_shared_process_group_before_creating_a_child(self):
        for options in ({}, {"start_new_session": False}, {"start_new_session": 1}):
            with self.subTest(options=options):
                registry, ticket, _ = self.owned_ticket()
                with mock.patch.object(owned_execution, "_POPEN") as factory, \
                        self.assertRaises(ExecutionCustodyError):
                    ticket.spawn([], **options)
                factory.assert_not_called()

    def test_handoff_fault_reaps_actual_child_preserves_error_and_retains_capacity(self):
        for error in (KeyboardInterrupt("handoff"), OSError("handoff")):
            with self.subTest(error=type(error).__name__):
                registry, ticket, entry = self.owned_ticket()
                original_entry = registry._entry
                calls = []

                def handoff(value):
                    calls.append(True)
                    if len(calls) == 2:
                        raise error
                    return original_entry(value)

                with mock.patch.object(registry, "_entry", side_effect=handoff), \
                        self.assertRaises(type(error)) as caught:
                    self.spawn_sleep(ticket)
                self.assertIs(error, caught.exception)
                self.assertIsNotNone(entry.process)
                self.assertIsNotNone(entry.process.returncode)
                self.assertTrue(entry.process.stdout.closed)
                self.assertEqual("unknown", entry.state)
                self.assert_custody_held(registry, ticket)

    def test_tree_construction_fault_still_reaps_the_new_child(self):
        registry, ticket, entry = self.owned_ticket()
        error = OSError("tree unavailable")
        with mock.patch.object(owned_execution, "ProcessTree", side_effect=error), \
                self.assertRaises(OSError) as caught:
            self.spawn_sleep(ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_errors_preserve_nonlocal_exit_and_uncertain_process_custody(self):
        first = KeyboardInterrupt("original handoff")
        second = KeyboardInterrupt("cleanup interruption")
        for original, cleanup, expected in ((first, OSError("cleanup"), first),
                                            (first, SystemExit(3), first),
                                            (OSError("handoff"), second, second)):
            with self.subTest(original=type(original).__name__, cleanup=type(cleanup).__name__):
                registry, ticket, entry = self.owned_ticket()
                original_entry = registry._entry
                calls = []

                def handoff(value):
                    calls.append(True)
                    if len(calls) == 2:
                        raise original
                    return original_entry(value)

                with mock.patch.object(registry, "_entry", side_effect=handoff), \
                        mock.patch.object(owned_execution, "_abort_spawn", side_effect=cleanup), \
                        self.assertRaises(type(expected)) as caught:
                    self.spawn_sleep(ticket)
                self.assertIs(expected, caught.exception)
                self.assertIsNotNone(entry.process)
                self.assertEqual("unknown", entry.state)
                self.assert_custody_held(registry, ticket)

    def test_bridge_tree_handoff_fault_cannot_bypass_owned_child_cleanup(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("bridge tree handoff")
        with mock.patch.object(registry, "tree", side_effect=error), \
                self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                             deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_tracking_error_still_kills_and_waits_without_masking_interruption(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("frame interruption")

        def interrupt(_frame):
            raise error

        with mock.patch.object(process_tree.ProcessTree, "terminate", side_effect=OSError("tracking failed")), \
                self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; print('{}', flush=True); time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                             deadline_seconds=5, on_frame=interrupt, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_entry_fault_cannot_skip_stop_wait_or_mask_original_interrupt(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                registry, ticket, entry = self.owned_ticket()
                original = KeyboardInterrupt("original frame") if interrupted else bridge._Abort("cancelled")
                cleanup = OSError("cleanup entry unavailable")

                def abort(_frame):
                    raise original

                expected = KeyboardInterrupt if interrupted else OSError
                with mock.patch.object(registry, "begin_cleanup", side_effect=cleanup), \
                        self.assertRaises(expected) as caught:
                    bridge.run_child([sys.executable, "-I", "-B", "-c",
                                      "import time; print('{}', flush=True); time.sleep(30)"],
                                     environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                                     deadline_seconds=5, on_frame=abort, _execution=ticket)
                self.assertIs(original if interrupted else cleanup, caught.exception)
                self.assertIsNotNone(entry.process.returncode)
                self.assertTrue(entry.process.stdout.closed)
                self.assert_custody_held(registry, ticket)

    def test_first_cleanup_interrupt_survives_later_ordinary_stop_error(self):
        registry, ticket, entry = self.owned_ticket()
        first = SystemExit(7)
        stop = bridge._stop_child

        def stop_then_fail(process, tree):
            stop(process, tree)
            raise OSError("later cleanup error")

        def abort(_frame):
            raise bridge._Abort("cancelled")

        with mock.patch.object(registry, "begin_cleanup", side_effect=first), \
                mock.patch.object(bridge, "_stop_child", side_effect=stop_then_fail), \
                self.assertRaises(SystemExit) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c",
                              "import time; print('{}', flush=True); time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                             deadline_seconds=5, on_frame=abort, _execution=ticket)
        self.assertIs(first, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assert_custody_held(registry, ticket)

    def test_interruption_after_factory_return_before_local_assignment_still_reaps_child(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("after factory return")
        original_spawn = ExecutionTicket.spawn

        def handoff(value, *args, **kwargs):
            original_spawn(value, *args, **kwargs)
            raise error

        with mock.patch.object(ExecutionTicket, "spawn", handoff), self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path(SHORT_TMP),
                             deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_constructed_copied_cross_registry_and_wrong_thread_tickets_are_refused(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        for forged in (ExecutionTicket(registry), copy(ticket), OwnedExecutionRegistry().issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)):
            with self.subTest(kind=type(forged).__name__), self.assertRaises(ExecutionCustodyError):
                registry.enter(forged)
        errors = []

        def wrong_thread():
            try:
                registry.enter(ticket)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([ExecutionCustodyError], [type(error) for error in errors])
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_actual_fixed_runner_waits_and_confirms_original_custody_once(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        with tempfile.TemporaryDirectory() as directory:
            outcome = bridge.run_child([sys.executable, "-I", "-B", "-c", "print('{}')"],
                                       environment={"PATH": os.defpath}, cwd=Path(directory),
                                       deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertEqual({"returncode": 0, "abort": None}, outcome)
        self.assertEqual(self.key(), registry.key(ticket, cleaned=True))
        with self.assertRaises(ExecutionCustodyError):
            registry.enter(ticket)
        registry.acknowledge_release(ticket)
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_custom_process_factory_cannot_generate_registered_completion(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        factory = mock.Mock(side_effect=AssertionError("custom factory executed"))
        outcome = bridge.run_child([], environment={}, cwd=Path(SHORT_TMP), deadline_seconds=1,
                                   on_frame=lambda frame: None, popen=factory, _execution=ticket)
        self.assertEqual("launch_failed", outcome["abort"])
        factory.assert_not_called()
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_existing_owned_process_and_constructed_tree_are_not_substitutable(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        registry.enter(ticket)
        with subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            tree = process_tree.ProcessTree(process.pid)
            self.assertTrue(tree.identify(process_tree.process_table()))
            with self.assertRaises(ExecutionCustodyError):
                registry.bind(ticket, process, tree)
            process.wait(timeout=5)

        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        process = ticket.spawn([sys.executable, "-I", "-B", "-c", "pass"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            constructed = process_tree.ProcessTree(process.pid)
            self.assertTrue(constructed.identify(process_tree.process_table()))
            with self.assertRaises(ExecutionCustodyError):
                ticket.bind(process, constructed)
        finally:
            process.wait(timeout=5)

    def test_expired_and_regressed_monotonic_time_never_enter_the_process_factory(self):
        for now, expected in ((250, "timed_out"), (50, "launch_failed")):
            registry = OwnedExecutionRegistry()
            with mock.patch.object(owned_execution.time, "monotonic_ns", return_value=100):
                ticket = registry.issue(self.key(), deadline_ns=200)
            factory = mock.Mock(side_effect=AssertionError("expired process factory"))
            with mock.patch.object(owned_execution.time, "monotonic_ns", return_value=now), \
                    mock.patch.object(owned_execution, "_POPEN", factory):
                result = bridge.run_child([], environment={}, cwd=Path(SHORT_TMP), deadline_seconds=10,
                                          on_frame=lambda frame: None, _execution=ticket)
            self.assertEqual(expected, result["abort"])
            factory.assert_not_called()
            with self.assertRaises(ExecutionCustodyError):
                registry.key(ticket, cleaned=True)


class CapacityCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexBridgeTests("test_happy_path_observes_before_and_after")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.tickets = []
        original = self.fixture.ledger._new_owned_execution

        def remember(*args):
            ticket = original(*args)
            self.tickets.append(ticket)
            return ticket

        patcher = mock.patch.object(self.fixture.ledger, "_new_owned_execution", side_effect=remember)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def ledger(self):
        return self.fixture.ledger

    def run_one(self):
        route, assigned, handle = self.fixture.codex_run()
        self.fixture.frames("codex", [{"type": "thread.started", "thread_id": "t1"},
                                     {"type": "turn.completed", "usage": {"input_tokens": 1}}])
        (self.fixture.tools / "codex-last-message.json").write_text(json.dumps(fixtures.null_filled(fixtures.result())))
        return handle, self.fixture.dispatch(route, assigned, handle)

    def held(self):
        return self.ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL").fetchone()[0]

    def reopen(self):
        self.ledger.close()
        self.fixture.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.addCleanup(self.fixture.ledger.close)

    def test_three_completed_owned_runs_release_default_slots_without_relaunch(self):
        for _ in range(3):
            last_handle, output = self.run_one()
            self.assertEqual("result", output["bridge_report"]["outcome"])
            self.assertEqual("released", output["bridge_report"]["capacity_state"])
            self.assertEqual(0, self.held())
        self.assertEqual(3, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(3, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_completions WHERE receipt_digest IS NOT NULL").fetchone()[0])
        with self.assertRaisesRegex(LedgerError, "owned execution is unavailable"):
            key = self.ledger._capacity_executions.key(self.tickets[-1])
            # A released claim never creates another execution ticket.
            self.ledger._new_owned_execution(key.project_id, last_handle, key.bridge_run_id)

    def reserve_claim(self):
        route, assigned, handle = self.fixture.codex_run()
        intent = self.ledger.record_dispatch_intent(self.fixture.project_id, run_handle=handle, assignment=assigned,
                                                     task=self.fixture.task(), route=route, deadline_seconds=10)
        self.ledger.bind_host_agent(self.fixture.project_id, run_handle=handle, agent_id=intent["bridge_run_id"])
        return handle, intent

    def prepare_claim(self):
        handle, intent = self.reserve_claim()
        ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        return handle, intent, ticket

    def test_registration_time_consumes_the_original_claimed_budget(self):
        clock = [100_000_000_000]
        execute = self.ledger._connection.execute

        def delayed(sql, *args, **kwargs):
            result = execute(sql, *args, **kwargs)
            if sql.startswith("INSERT INTO capacity_executions"):
                clock[0] += 1_000_000_000
            return result

        with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
            handle, intent = self.reserve_claim()
            claimed = self.ledger._connection.execute("SELECT claimed_at_ms FROM active_run_capacity").fetchone()[0]
            clock[0] += 8_000_000_000
            with mock.patch.object(self.ledger, "_clock_ms", return_value=claimed + 8000), \
                    mock.patch.object(self.ledger._connection, "execute", side_effect=delayed):
                ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(110_000_000_000, ticket.deadline_ns())

    def test_partial_wall_clock_rollback_cannot_renew_the_claim_budget(self):
        clock = [100_000_000_000]
        with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
            handle, intent = self.reserve_claim()
            claimed = self.ledger._connection.execute("SELECT claimed_at_ms FROM active_run_capacity").fetchone()[0]
            clock[0] += 9_000_000_000
            with mock.patch.object(self.ledger, "_clock_ms", return_value=claimed + 1000):
                ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
            self.assertEqual(110_000_000_000, ticket.deadline_ns())

    def test_original_monotonic_expiry_or_regression_refuses_registration_despite_valid_wall_time(self):
        for registration in (99_000_000_000, 110_000_000_000):
            with self.subTest(registration=registration):
                clock = [100_000_000_000]
                with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
                    handle, intent = self.reserve_claim()
                    row = self.ledger._connection.execute(
                        "SELECT claimed_at_ms FROM active_run_capacity WHERE bridge_run_id=?", (intent["bridge_run_id"],),
                    ).fetchone()
                    clock[0] = registration
                    with mock.patch.object(self.ledger, "_clock_ms", return_value=row[0] + 1000), \
                            self.assertRaisesRegex(LedgerError, "deadline"):
                        self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
                self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])

    def test_clock_owner_cannot_be_reconstructed_after_restart_before_registration(self):
        handle, intent = self.reserve_claim()
        self.reopen()
        with self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_structurally_valid_clock_owner_tampering_refuses_registration(self):
        handle, intent = self.reserve_claim()
        with self.ledger._connection:
            self.ledger._connection.execute("UPDATE active_run_capacity SET clock_owner_digest=?", ("0" * 64,))
        with self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_expired_claim_refuses_registration_without_reissuing_execution(self):
        handle, intent = self.reserve_claim()
        deadline = self.ledger._connection.execute("SELECT deadline_at_ms FROM active_run_capacity").fetchone()[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=deadline), self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_registration_before_spawn_remains_single_use_across_restart(self):
        handle, intent, _ = self.prepare_claim()
        self.reopen()
        with self.assertRaisesRegex(LedgerError, "already registered"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(1, self.held())

    def test_registration_commit_rollback_does_not_burn_an_unreturned_ticket(self):
        handle, intent = self.reserve_claim()
        error = KeyboardInterrupt("registration before commit")
        with mock.patch.object(self.ledger.state, "harden_database_files", side_effect=error), \
                self.assertRaises(KeyboardInterrupt) as caught:
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertIs(error, caught.exception)
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(1, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, len(self.ledger._capacity_executions._pending))
        row = self.ledger._connection.execute("SELECT execution_deadline_ns FROM active_run_capacity").fetchone()
        self.assertEqual(row[0], ticket.deadline_ns())
        self.assertEqual(1, self.held())

    def test_terminal_record_and_caller_flags_without_actual_execution_cannot_release(self):
        handle, intent, ticket = self.prepare_claim()
        self.ledger.record_dispatch_result(self.fixture.project_id, run_handle=handle, bridge_run_id=intent["bridge_run_id"],
                                           result=fixtures.null_filled(fixtures.result()), fenced=False,
                                           reviewed=True, terminal_state=None)
        self.assertEqual("held", self.ledger._finish_owned_execution(ticket)["state"])
        self.assertEqual(1, self.held())

    def test_known_release_survives_restart_and_clock_rollback(self):
        handle, output = self.run_one()
        bridge_id = output["bridge_report"]["bridge_run_id"]
        first = self.ledger._release_active_capacity(self.tickets[0])
        self.assertTrue(first["known"])
        self.reopen()
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1):
            self.assertEqual(first, self.ledger._known_capacity_release(self.fixture.project_id, handle, bridge_id))

    def test_released_backup_requires_execution_and_completion_provenance(self):
        self.run_one()
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.fixture.project_id,
                                                         ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION)
        self.assertEqual(1, learning_store._verify_project_backup_body(body)["capacity_completions"])
        for table in ("capacity_completions", "capacity_executions"):
            broken = deepcopy(body)
            broken["tables"][table] = []
            broken["counts"][table] = 0
            with self.subTest(table=table), self.assertRaisesRegex(LedgerError, "completion coverage"):
                learning_store._verify_project_backup_body(broken)

    def test_tampered_release_receipt_is_not_accepted_as_known_metadata(self):
        handle, output = self.run_one()
        self.ledger._connection.execute("UPDATE capacity_completions SET receipt_digest=?", ("f" * 64,))
        self.ledger._connection.execute("UPDATE active_run_capacity SET release_digest=?", ("f" * 64,))
        with self.assertRaisesRegex(LedgerError, "capacity completion is unavailable"):
            self.ledger._known_capacity_release(self.fixture.project_id, handle, output["bridge_report"]["bridge_run_id"])

    def test_missing_cleanup_proof_and_restart_keep_capacity_and_consumed_registration(self):
        # A forged success flag still cannot update the registry's actual state.
        with mock.patch.object(OwnedExecutionRegistry, "finish", return_value=True):
            handle, output = self.run_one()
        self.assertEqual("held", output["bridge_report"]["capacity_state"])
        self.assertEqual(1, self.held())
        bridge_id = output["bridge_report"]["bridge_run_id"]
        self.reopen()
        self.assertIsNone(self.ledger._known_capacity_release(self.fixture.project_id, handle, bridge_id))
        with self.assertRaisesRegex(LedgerError, "owned execution is unavailable"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, bridge_id)
        self.assertEqual(1, self.held())

    def test_interrupted_release_rolls_back_both_tables_and_reuses_original_cleanup(self):
        class FixtureStop(BaseException):
            pass

        execute = self.ledger._connection.execute

        def interrupted(sql, *args, **kwargs):
            value = execute(sql, *args, **kwargs)
            if sql.startswith("UPDATE capacity_completions SET released_at_ms="):
                raise FixtureStop()
            return value

        with mock.patch.object(self.ledger._connection, "execute", side_effect=interrupted), self.assertRaises(FixtureStop):
            self.run_one()
        self.assertEqual(1, self.held())
        self.assertIsNone(self.ledger._connection.execute("SELECT receipt_digest FROM capacity_completions").fetchone()[0])
        receipt = self.ledger._release_active_capacity(self.tickets[0])
        self.assertEqual("released", receipt["state"])
        self.assertFalse(receipt["known"])
        self.assertEqual(0, self.held())
        self.assertEqual(1, len([item for item in self.fixture.invocations("codex") if "exec" in item["argv"]]))

    def test_clock_rollback_cannot_create_a_new_release_but_ready_custody_is_retained(self):
        with mock.patch.object(self.ledger, "_finish_owned_execution", return_value={"state": "held"}):
            self.run_one()
        self.assertEqual(1, self.held())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1), self.assertRaises(LedgerError):
            self.ledger._release_active_capacity(self.tickets[0])
        self.assertEqual(1, self.held())
        self.assertEqual("released", self.ledger._release_active_capacity(self.tickets[0])["state"])

    def test_lost_registry_acknowledgement_retrieves_the_committed_receipt(self):
        class FixtureStop(BaseException):
            pass

        with mock.patch.object(self.ledger._capacity_executions, "acknowledge_release", side_effect=FixtureStop), \
                self.assertRaises(FixtureStop):
            self.run_one()
        self.assertEqual(0, self.held())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1):
            known = self.ledger._release_active_capacity(self.tickets[0])
        self.assertTrue(known["known"])
        self.assertEqual("released", known["state"])

    def test_forged_and_foreign_tickets_cannot_free_a_held_slot(self):
        with mock.patch.object(OwnedExecutionRegistry, "finish", return_value=False):
            self.run_one()
        real = self.tickets[0]
        foreign = OwnedExecutionRegistry().issue(self.ledger._capacity_executions.key(real), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        for forged in (ExecutionTicket(self.ledger._capacity_executions), copy(real), foreign):
            self.assertEqual("held", self.ledger._finish_owned_execution(forged)["state"])
        self.assertEqual(1, self.held())


if __name__ == "__main__":
    unittest.main()
