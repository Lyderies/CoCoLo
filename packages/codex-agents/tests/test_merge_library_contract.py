"""ML-1..ML-30 final §11 contracts. Run only this module with unittest.

No existing merge code/fixtures are imported. Each generated case is its own
test, names its ML id in its docstring, and has otherwise valid sealed input.
Image/device capabilities use the IOCall seam; real mounted-image evidence
belongs to the opt-in image harness in test_merge_library_adversarial.
"""
from __future__ import annotations

import errno
import os
import stat
import sqlite3
import shutil
import subprocess
import threading
import signal
import tempfile
import time
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT  # noqa: F401 -- native package bootstrap
from codex_agents import merge_library as ml
from tests.merge_library_support import (
    declare_ml_tests,
    Fixture, Trace, assert_trace, checkpoint, child_run, content, fresh_queue, git, put,
    report_oracle, tree, assert_no_git_writes, location_refusal_oracle,
)


def bounded_concurrency(action, timeout=30):
    """Contain blocked sealer threads; always terminate/reap this owned child."""
    with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
        error_path = Path(directory) / 'error'
        pid = os.fork()
        if pid == 0:
            try:
                action()
            except BaseException:
                error_path.write_text(traceback.format_exc())
                os._exit(73)
            os._exit(0)
        reaped = False
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                found, status = os.waitpid(pid, os.WNOHANG)
                if found:
                    reaped = True
                    if error_path.exists():
                        raise AssertionError(error_path.read_text())
                    if status:
                        raise AssertionError('sealer child failed: ' + str(status))
                    return
                time.sleep(.001)
            raise AssertionError('sealer child exceeded bounded deadline')
        finally:
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)


class Contract(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def refused(self, batch, *, hook=None, policy=None):
        before = tree(self.f.project, metadata=True)
        report = self.f.land(batch, hook=hook, policy=policy)
        self.assertEqual("refused", report.state)
        self.assertTrue(report.reason)
        self.assertEqual(before, tree(self.f.project, metadata=True))
        return report

    def test_ml_1_refusing_check(self):
        """ML-1: a refusing project check is honored, with a fixed unchanged tree."""
        batch = self.f.batch()
        checks = mock.Mock(return_value=False)
        report = self.f.land(batch, policy=replace(self.f.policy, checks=checks))
        self.assertEqual("reverted", report.state)
        self.assertFalse(any(r.state == "landed" for r in ml.history(self.f.project, self.f.private)))
        checks.assert_called()
        self.assertEqual(self.f.before, tree(self.f.project))

    def test_ml_2_path_count_boundary(self):
        """ML-2: 2000 accepted; 2001 refused as one batch, with a reason, never split."""
        batch = self.f.batch({f"p{i:04d}": b"x" for i in range(2000)})
        self.assertEqual(2000, len(batch.events))
        self.assertEqual({f"p{i:04d}" for i in range(2000)}, {e.path for e in batch.events})
        report = self.f.land(batch)
        self.assertEqual("landed", report.state)
        self.assertEqual(2000, len([r for r in report.paths if r.path.startswith("p")]))
        self.assertEqual(self.f.expected(batch), tree(self.f.project))
        landed = [r for r in ml.history(self.f.project, self.f.private) if r.state == "landed"]
        self.assertEqual([batch], [r.batch for r in landed])
        other = Fixture()
        self.addCleanup(other.close)
        batch = other.batch({f"p{i:04d}": b"x" for i in range(2001)})
        before = tree(other.project)
        report = other.land(batch)
        self.assertEqual("refused", report.state)
        self.assertIn("2000", report.reason)
        self.assertEqual(before, tree(other.project))
        self.assertFalse(any(r.state == "landed" for r in ml.history(other.project, other.private)))
        self.assertEqual((), ml.queue(other.project, other.private))
        self.assertFalse((other.project / ".cocolo/staging" / batch.child_id).exists())

    def test_ml_6_normal_two_commit_order(self):
        """ML-6: exactly A/B; A precedes mutation, checks and parent flushes precede B."""
        trace = Trace()
        batch = self.f.batch()
        expected_folders = {e.path for e in batch.events if e.kind == "folder"}
        self.assertEqual({"new", "new/deep"}, expected_folders)
        self.assertTrue(expected_folders, "created-identity checks must be non-vacuous")
        report = self.f.land(batch, hook=trace)
        self.assertEqual("landed", report.state)
        assert_trace(self, trace.calls)
        row = next(r for r in ml.history(self.f.project, self.f.private) if r.batch_no == report.batch_no)
        self.assertEqual(batch, row.batch)
        self.assertEqual(expected_folders, set(row.created_folders))
        for event in row.batch.events:
            if event.kind == "folder":
                info = (self.f.project / event.path).stat()
                self.assertEqual((info.st_dev, info.st_ino), row.created_folders[event.path])

    def test_ml_6_failing_check_blocks_B(self):
        """ML-6: failed project checks prohibit commit B and restore the whole change."""
        trace = Trace()
        report = self.f.land(policy=replace(self.f.policy, checks=lambda _: False), hook=trace)
        self.assertEqual("reverted", report.state)
        self.assertFalse(any(c.operation == "sqlite_commit" and c.detail.get("commit") == "B"
                             for c in trace.calls))
        self.assertEqual(self.f.before, tree(self.f.project))

    def test_ml_9_syscall_allowlist_and_cross_folder_exchange(self):
        """ML-9: only allowed tree mutations; exchanges cross into child staging."""
        trace = Trace()
        batch = self.f.batch()
        report = self.f.land(batch, hook=trace)
        self.assertEqual("landed", report.state)
        mutations = [c for c in trace.calls if c.when == "before" and c.detail.get("mutating")
                     and c.detail.get("scope") in ("tree", "staging")]
        self.assertTrue(mutations)
        for call in mutations:
            self.assertIn(call.operation, {"exchange", "rename_noreplace", "mkdir", "rmdir", "unlink"})
            if call.operation == "exchange":
                self.assertNotEqual(Path(call.path).parent, Path(call.other).parent)
                self.assertTrue(any("/.cocolo/staging/" in p for p in (call.path, call.other)))
            if call.operation == "unlink":
                self.assertTrue(call.detail.get("terminal_durable"))
                self.assertTrue(call.detail.get("content_authorized"))
                self.assertEqual(1, call.detail.get("nlink"))
        compensation = [c for c in mutations if c.phase == "compensation"]
        self.assertLessEqual(len(compensation), 1)

    def test_ml_9_foreign_staging_file_is_unlink_guard_witness(self):
        """ML-9: foreign staging content survives; deleting it is the guard-removal witness."""
        batch = self.f.batch()
        staging = ml.stage(self.f.project, self.f.private, batch, policy=self.f.policy)
        foreign = staging / "foreign-owner-version"
        put(foreign, b"OWNER unique bytes")
        reports = ml.cleanup(self.f.project, self.f.private)
        self.assertEqual(b"OWNER unique bytes", foreign.read_bytes())
        self.assertIn(str(foreign), {p for r in reports for p in r.leftovers})

    def test_ml_13_exact_map_and_order(self):
        """ML-13: L1/L2/undo-L2/undo-L1 restores bytes/modes/absence and created dirs."""
        first = self.f.land(self.f.batch({"a.txt": b"L1\n", "new/deep/add": b"add\n"}, "L1"))
        l1 = tree(self.f.project)
        second = self.f.land(self.f.batch({"a.txt": b"L2\n"}, "L2"))
        before = tree(self.f.project)
        refused = ml.undo(self.f.project, self.f.private, first.batch_no)
        self.assertEqual("refused", refused.state)
        self.assertEqual(before, tree(self.f.project))
        self.assertEqual("landed", ml.undo(self.f.project, self.f.private, second.batch_no).state)
        self.assertEqual(l1, tree(self.f.project))
        self.assertEqual("landed", ml.undo(self.f.project, self.f.private, first.batch_no).state)
        self.assertEqual(self.f.before, tree(self.f.project))

    def test_ml_13_deleted_empty_folders_not_recreated(self):
        """ML-13: undo restores deleted files, but not unrelated deleted empty folders."""
        (self.f.project / "empty-owner-folder").mkdir()
        batch = self.f.batch({"removed/old.txt": None})
        landed = self.f.land(batch)
        (self.f.project / "empty-owner-folder").rmdir()
        ml.undo(self.f.project, self.f.private, landed.batch_no)
        self.assertEqual(b"deleted\n", (self.f.project / "removed/old.txt").read_bytes())
        self.assertFalse((self.f.project / "empty-owner-folder").exists())

    def test_ml_13_internal_mode_fixture(self):
        """ML-13: internal history restores full bits; candidate chmod is tested separately."""
        batch = self.f.batch({"tool": b"new tool\n"}, modes={"tool": 0o640})
        put(self.f.project / "tool", b"new tool\n", 0o640)
        ml._seed_history(self.f.project, self.f.private, (ml.HistoryRow(1, batch, "landed"),))
        self.assertEqual("landed", ml.undo(self.f.project, self.f.private, 1).state)
        self.assertEqual(self.f.before, tree(self.f.project))

    def test_ml_13_ancestor_overlap_refused(self):
        """ML-13: newer work under a created folder blocks undo of its ancestor."""
        first = self.f.land(self.f.batch({"new/a": b"one"}, "L1"))
        self.f.land(self.f.batch({"new/b": b"two"}, "L2"))
        before = tree(self.f.project)
        report = ml.undo(self.f.project, self.f.private, first.batch_no)
        self.assertEqual("refused", report.state)
        self.assertEqual(before, tree(self.f.project))

    def test_ml_13_owner_change_refuses_undo(self):
        """ML-13: owner modification makes undo a refusal with nothing changed."""
        landed = self.f.land()
        put(self.f.project / "a.txt", b"OWNER")
        before = tree(self.f.project, metadata=True)
        self.assertEqual("refused", ml.undo(self.f.project, self.f.private, landed.batch_no).state)
        self.assertEqual(before, tree(self.f.project, metadata=True))

    def test_ml_14_report_independent_oracle(self):
        """ML-14: independently classify every target and name every staging leftover."""
        batch = self.f.batch()
        report = self.f.land(batch)
        report_oracle(self, self.f, report, batch)

    def test_ml_16_git_never_written(self):
        """ML-16: no write-open/rename/unlink in .git during merge, staging or cleanup."""
        batch = self.f.batch()
        before = tree(self.f.project / ".git", metadata=True, control=True)
        trace = Trace()
        self.f.land(batch, hook=trace)
        ml.cleanup(self.f.project, self.f.private, hook=trace)
        self.assertEqual(before, tree(self.f.project / ".git", metadata=True, control=True))
        assert_no_git_writes(self, self.f, trace.calls)

    def test_ml_16_head_change_final_validation(self):
        """ML-16: owner HEAD change before B puts the landing back, without undoing HEAD."""
        batch = self.f.batch()
        new_head = git(self.f.project, "commit-tree", "HEAD^{tree}", "-p", "HEAD",
                       data=b"owner commit\n").decode().strip()
        hook, fired = checkpoint("final_validation", lambda _: git(self.f.project, "update-ref", "HEAD", new_head))
        report = self.f.land(batch, hook=hook)
        self.assertTrue(fired)
        self.assertEqual("reverted", report.state)
        self.assertEqual(self.f.before, tree(self.f.project))
        self.assertEqual(new_head, git(self.f.project, "rev-parse", "HEAD").decode().strip())

    def test_ml_17_concurrent_sealers_fifo_restart(self):
        """ML-17: concurrent sealers get durable ordered tickets, preserved across restart."""
        bounded_concurrency(self._concurrent_sealers_fifo_restart)

    def _concurrent_sealers_fifo_restart(self):
        batches = [self.f.batch({f"q{i}": str(i).encode()}, f"q{i}") for i in range(3)]
        barrier = threading.Barrier(3)
        tickets, errors = [], []
        def sealer(batch):
            try:
                barrier.wait(timeout=3)
                tickets.append(ml.enqueue(self.f.project, self.f.private, batch, policy=self.f.policy))
            except BaseException as error:
                errors.append(error)
        workers = [threading.Thread(target=sealer, args=(b,)) for b in batches]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        order = [t.child_id for t in sorted(tickets, key=lambda t: t.ticket)]
        self.assertEqual(order, [t.child_id for t in ml.queue(self.f.project, self.f.private)])
        self.assertEqual(order, fresh_queue(self.f))
        ml.recover(self.f.project, self.f.private)
        self.assertEqual(order, [t.child_id for t in ml.queue(self.f.project, self.f.private)])
        # Calling the second ticket first must not bypass the durable head.
        before = tree(self.f.project)
        blocked = self.f.land(next(b for b in batches if b.child_id == order[1]))
        self.assertEqual("refused", blocked.state)
        self.assertEqual(before, tree(self.f.project))
        self.assertFalse(any(r.state == "landed" for r in ml.history(self.f.project, self.f.private)))
        self.assertEqual(order, [q.child_id for q in ml.queue(self.f.project, self.f.private)])
        for child in order:
            self.assertEqual("landed", self.f.land(next(b for b in batches if b.child_id == child)).state)
        self.assertEqual(order, [r.batch.child_id for r in ml.history(self.f.project, self.f.private)
                                 if r.state == "landed"])

    def test_ml_17_stale_default_waits_without_builder(self):
        """ML-17/28/29: changed HEAD stops the original with no default builder."""
        batch = self.f.batch({"a.txt": b"ONE\ntwo\nthree\n"})
        ml.enqueue(self.f.project, self.f.private, batch, policy=self.f.policy)
        put(self.f.project / "a.txt", b"one\ntwo\nTHREE\n")
        git(self.f.project, "add", "a.txt")
        git(self.f.project, "commit", "-qm", "owner before turn")
        owner, trace, builder = tree(self.f.project), Trace(), mock.Mock()
        report = self.f.land(batch, hook=trace, policy=replace(self.f.policy, builder=builder))
        self.assertEqual("waiting_owner", report.state)
        self.assertEqual(owner, tree(self.f.project))
        builder.assert_not_called()
        self.assertFalse(any(c.operation in ("builder_spawn", "exchange", "rename_noreplace")
                             and c.phase == "forward" for c in trace.calls))
        self.assertFalse(any(r.batch == batch and r.state in ("applying", "landed")
                             for r in ml.history(self.f.project, self.f.private)))
        stale = next(q for q in ml.queue(self.f.project, self.f.private) if q.child_id == batch.child_id)
        self.assertEqual(batch.base_commit, stale.base_commit)
        self.assertEqual("waiting_owner", stale.state)

    def test_ml_17_unresolved_blocks_until_accept(self):
        """ML-17: unresolved work blocks new admission until owner accepts or retries."""
        batch = self.f.batch()
        ml._seed_history(self.f.project, self.f.private, (ml.HistoryRow(1, batch, "needs_owner"),))
        second = self.f.batch({"other": b"two"}, "second")
        report = self.f.land(second)
        self.assertEqual("waiting_owner", report.state)
        self.assertEqual(self.f.before, tree(self.f.project))
        ml.accept(self.f.project, self.f.private, 1)
        self.assertEqual("landed", self.f.land(second).state)

    def test_ml_18_retention_10_vs_11(self):
        """ML-18: contents retire after ten newer landings, only after cleanup/handoff."""
        first = self.f.land(self.f.batch({"p0": b"0"}, "c0"))
        for i in range(1, 10):
            self.f.land(self.f.batch({f"p{i}": str(i).encode()}, f"c{i}"))
        row = next(r for r in ml.history(self.f.project, self.f.private) if r.batch_no == first.batch_no)
        self.assertTrue(row.contents_kept, "10 total = only 9 newer")
        self.f.land(self.f.batch({"p10": b"10"}, "c10"))
        ml.cleanup(self.f.project, self.f.private)
        row = next(r for r in ml.history(self.f.project, self.f.private) if r.batch_no == first.batch_no)
        self.assertTrue(row.cleanup_complete)
        self.assertFalse(row.contents_kept, "11 total = 10 newer")

    def test_ml_19_full_flush_order(self):
        """ML-19: full file/directory/SQLite flush order; power loss needs APFS/ext4 evidence."""
        trace = Trace()
        self.f.land(hook=trace)
        assert_trace(self, trace.calls)

    def test_ml_22_reinspect_every_path(self):
        """ML-22: final-validation owner edit stays; other paths are put back before B."""
        batch = self.f.batch()
        hook, fired = checkpoint("final_validation", lambda _: put(self.f.project / "z.txt", b"OWNER final"))
        report = self.f.land(batch, hook=hook)
        self.assertTrue(fired)
        self.assertEqual("needs_owner", report.state)
        expected = dict(self.f.before)
        expected["z.txt"] = (stat.S_IFREG, 0o644, b"OWNER final")
        self.assertEqual(expected, tree(self.f.project))
        self.assertIn("z.txt", report.conflicts)

    def test_ml_24_ignored_staging_and_negative_control(self):
        """ML-24: staging path, '*' ignore, clean status; removing ignore reveals staging."""
        batch = self.f.batch({"a.txt": b"child"})
        staging = ml.stage(self.f.project, self.f.private, batch, policy=self.f.policy)
        self.assertEqual(self.f.project / ".cocolo/staging/child-1", staging)
        self.assertEqual("*", (staging / ".gitignore").read_text().strip())
        self.assertEqual(b"", git(self.f.project, "status", "--porcelain", "--untracked-files=all"))
        (staging / ".gitignore").unlink()
        self.assertIn(b".cocolo/staging/child-1/", git(self.f.project, "status", "--porcelain", "--untracked-files=all"))
        put(staging / ".gitignore", b"*\n")
        self.assertEqual("landed", self.f.land(batch).state)
        self.assertFalse(staging.exists())

    def test_ml_27_merged_report_checks_before_B(self):
        """ML-27: merged files listed, checks see merged bytes; failed check puts back."""
        batch = self.f.batch({"a.txt": b"ONE\ntwo\nthree\n"})
        put(self.f.project / "a.txt", b"one\ntwo\nTHREE\n")
        owner = tree(self.f.project)
        observed = []
        def inspect_merged(project):
            observed.append((project / "a.txt").read_bytes())
            return False
        checks = mock.Mock(side_effect=inspect_merged)
        trace = Trace()
        report = self.f.land(batch, policy=replace(self.f.policy, checks=checks), hook=trace)
        self.assertEqual("reverted", report.state)
        self.assertIn("a.txt", report.merged)
        checks.assert_called_once_with(self.f.project)
        self.assertEqual([b"ONE\ntwo\nTHREE\n"], observed)
        self.assertEqual(owner, tree(self.f.project))
        self.assertFalse(any(c.operation == "sqlite_commit" and c.detail.get("commit") == "B" for c in trace.calls))

    def test_ml_29_conflict_waits_no_builder(self):
        """ML-29: overlapping conflict waits visibly for owner; no builder is spawned."""
        batch = self.f.batch({"a.txt": b"child conflict\n"})
        put(self.f.project / "a.txt", b"OWNER conflict\n")
        builder = mock.Mock(side_effect=AssertionError("builder forbidden before write children"))
        trace = Trace()
        report = self.f.land(batch, policy=replace(self.f.policy, builder=builder, write_children=False), hook=trace)
        self.assertEqual("waiting_owner", report.state)
        self.assertIn("a.txt", report.conflicts)
        builder.assert_not_called()
        self.assertFalse(any(c.operation == "builder_spawn" for c in trace.calls))
        self.assertEqual(b"OWNER conflict\n", (self.f.project / "a.txt").read_bytes())
        self.assertIn("waiting_owner", [q.state for q in ml.queue(self.f.project, self.f.private)])


def cases(prefix, requirement, values, body):
    """Separate unittest methods, so every otherwise-valid predicate gets a red result."""
    for name, value in values:
        def test(self, value=value):
            body(self, value)
        test.__name__ = f"test_{prefix}_{name}"
        test.__doc__ = f"{requirement}: {name.replace('_', ' ')}."
        setattr(Contract, test.__name__, test)


def binding_tamper(test, field):
    batch = test.f.batch()
    bad = replace(batch, **{field: b"tampered" if field == "manifest" else "0" * 64})
    test.refused(bad)


cases("ml_1_binding", "ML-1", [(n, n) for n in
      ("base_commit", "manifest", "candidate_digest", "git_link", "batch_digest", "approval")], binding_tamper)


def intake(test, value):
    location, kind = value
    batch = test.f.batch({"a.txt": b"child"})
    root = batch.candidate_root if location == "candidate" else test.f.project
    path = root / "a.txt"
    if kind in ("symlink", "hardlink", "fifo", "directory"):
        path.unlink()
        if kind == "symlink":
            put(test.f.outside / "canary", b"OUTSIDE")
            path.symlink_to(test.f.outside / "canary")
        elif kind == "hardlink":
            source = root / "other"
            put(source, b"child" if location == "candidate" else b"one\ntwo\nthree\n")
            os.link(source, path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    elif kind == "dot_git":
        batch = test.f.batch({".git/forged": b"child"})
    elif kind in ("socket", "char_device", "block_device"):
        # A stat-result injection exercises each forbidden type without creating sockets/devices.
        mode = {"socket": stat.S_IFSOCK, "char_device": stat.S_IFCHR, "block_device": stat.S_IFBLK}[kind]
        def hook(call):
            if call.operation in ("stat", "lstat", "fstat") and call.when == "before" and call.path == str(path):
                original = list(path.lstat())
                original[0] = mode | 0o644
                return ml.IOOverride(os.stat_result(original))
        test.refused(batch, hook=hook)
        return
    if kind == "fifo":
        before = tree(test.f.project, metadata=True)
        def action():
            report = test.f.land(batch)
            if report.state != "refused" or not report.reason:
                raise AssertionError(report)
        status = child_run(action, timeout=2)
        test.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)
        test.assertEqual(before, tree(test.f.project, metadata=True))
    else:
        test.refused(batch)


cases("ml_2_intake", "ML-2", [(f"{where}_{kind}", (where, kind))
      for where in ("candidate", "target")
      for kind in ("symlink", "hardlink", "fifo", "directory", "socket", "char_device", "block_device")]
      + [("dot_git", ("target", "dot_git"))], intake)


def limit(test, value):
    which, over = value
    size = ml.MAX_FILE_BYTES + over
    changes = {f"large{i}": b"x" * size for i in range(4 if which == "total" else 1)}
    if which == "total" and over:
        changes = {f"large{i}": b"x" * ml.MAX_FILE_BYTES for i in range(4)} | {"extra": b"x"}
    batch = test.f.batch(changes)
    if over:
        test.refused(batch)
    else:
        test.assertEqual("landed", test.f.land(batch).state)


cases("ml_2_limit", "ML-2", [(f"{which}_{'over' if over else 'at'}", (which, over))
      for which in ("file", "total") for over in (0, 1)], limit)


def old_limit(test, which):
    changes = {f"old{i}": b"x" * ml.MAX_FILE_BYTES for i in range(4 if which == "total" else 1)}
    if which == "file":
        changes["old0"] += b"!"
    else:
        changes["old4"] = b"!"
    for name, data in changes.items():
        put(test.f.project / name, data)
    git(test.f.project, "add", "-A")
    git(test.f.project, "commit", "-qm", "oversize base")
    test.f.base = git(test.f.project, "rev-parse", "HEAD").decode().strip()
    test.f.before = tree(test.f.project)
    test.refused(test.f.batch({name: None for name in changes}))


cases("ml_2_base_limit", "ML-2", [(n, n) for n in ("file", "total")], old_limit)


def forbidden_parent(test, value):
    where, kind = value
    batch = test.f.batch({"removed/old.txt": b"child"})
    root = batch.candidate_root if where == "candidate" else test.f.project
    parent = root / "removed"
    outside_before = tree(test.f.outside, metadata=True)
    if kind in ("socket", "char_device", "block_device"):
        bits = {"socket": stat.S_IFSOCK, "char_device": stat.S_IFCHR, "block_device": stat.S_IFBLK}[kind]
        def hook(call):
            if call.when == "before" and call.operation in ("stat", "lstat", "fstat") and call.path == str(parent):
                info = list(parent.stat())
                info[0] = bits | 0o644
                return ml.IOOverride(os.stat_result(info))
        test.refused(batch, hook=hook)
    else:
        (parent / "old.txt").unlink()
        parent.rmdir()
        if kind == "symlink":
            parent.symlink_to(test.f.outside, target_is_directory=True)
        elif kind == "fifo":
            os.mkfifo(parent)
        elif kind == "hardlink":
            source = root / "linked-parent"
            put(source, b"parent file")
            os.link(source, parent)
        else:
            put(parent, b"regular parent file")
        if kind == "fifo":
            before = tree(test.f.project, metadata=True)
            def action():
                report = test.f.land(batch)
                if report.state != "refused":
                    raise AssertionError(report)
            status = child_run(action, timeout=2)
            test.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)
            test.assertEqual(before, tree(test.f.project, metadata=True))
        else:
            test.refused(batch)
    test.assertEqual(outside_before, tree(test.f.outside, metadata=True))


cases("ml_2_parent", "ML-2", [(f"{where}_{kind}", (where, kind))
      for where in ("candidate", "target")
      for kind in ("symlink", "hardlink", "fifo", "regular", "socket", "char_device", "block_device")], forbidden_parent)


def precondition(test, kind):
    f = test.f
    batch = f.batch({"a.txt": b"child"})
    if kind in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"):
        put(f.project / ".git" / kind, (f.base + "\n").encode())
    elif kind in ("rebase-merge", "rebase-apply"):
        (f.project / ".git" / kind).mkdir()
    elif kind == "head":
        git(f.project, "commit", "--allow-empty", "-qm", "owner HEAD")
    elif kind == "sparse":
        git(f.project, "config", "core.sparseCheckout", "true")
    elif kind == "skip_worktree":
        git(f.project, "update-index", "--skip-worktree", "a.txt")
    elif kind == "submodule":
        git(f.project, "update-index", "--add", "--cacheinfo", f"160000,{f.base},sub")
    elif kind == "nested_repository":
        (f.project / "nested").mkdir()
        git(f.project / "nested", "init", "-q")
        batch = f.batch({"nested/a": b"child"})
    elif kind == "autocrlf":
        git(f.project, "config", "core.autocrlf", "true")
    elif kind.startswith("attribute_"):
        put(f.project / ".git/info/attributes", f"a.txt {kind.removeprefix('attribute_')}\n".encode())
    elif kind == "chmod":
        batch = f.batch({"a.txt": b"child"}, modes={"a.txt": 0o600})
    elif kind == "exec_change":
        batch = f.batch({"a.txt": b"child"}, modes={"a.txt": 0o755})
    elif kind in ("duplicate", "dotdot", "absolute", "rename", "case_alias", "unicode_alias", "aux_alias"):
        path = {"duplicate": "a.txt", "dotdot": "../outside/canary", "absolute": str(f.outside / "canary"),
                "rename": "renamed.txt", "case_alias": "A.TXT", "unicode_alias": "e\u0301.txt",
                "aux_alias": ".COCOLO/staging/child-1/a"}[kind]
        if kind == "unicode_alias":
            put(f.project / "é.txt", b"owner")
        changes = {"a.txt": b"child", path: b"child"}
        if kind == "rename":
            batch = f.batch({path: b"one\ntwo\nthree\n"})
            event = replace(batch.events[0], kind="rename", base=content(b"one\ntwo\nthree\n"))
            test.refused(resign(f, replace(batch, events=(event,))))
            return
        if kind in ("dotdot", "absolute", "duplicate"):
            event = replace(batch.events[0], path=path)
            # Re-sign the structurally invalid event, so signature rejection cannot mask intake.
            batch = resign(f, replace(batch, events=batch.events + (event,)))
        else:
            batch = f.batch(changes)
    elif kind == "add_existing":
        batch = f.batch({"added": b"child"})
        put(f.project / "added", b"OWNER")
    elif kind == "base_mode":
        (f.project / "a.txt").chmod(0o600)
    elif kind == "base_bytes":
        put(f.project / "a.txt", b"OWNER overlapping\n")
        report = f.land(batch)
        test.assertIn(report.state, ("needs_owner", "waiting_owner", "refused"))
        test.assertEqual(b"OWNER overlapping\n", (f.project / "a.txt").read_bytes())
        return
    test.refused(batch)


def resign(f, batch):
    # Invalid paths/events are genuinely approved in these tests; they must fail their own guard.
    import hashlib
    import hmac
    from tests.merge_library_support import binding, canonical, digest, event_value
    manifest = canonical([event_value(e) for e in batch.events])
    batch = replace(batch, manifest=manifest, candidate_digest=digest(manifest),
                    git_link=digest(b"fake GA-19 receipt\0" + batch.base_commit.encode() + manifest),
                    batch_digest=digest(canonical({"events": [digest(canonical(event_value(e))) for e in batch.events],
                                                   "base": batch.base_commit, "candidate": digest(manifest)})))
    batch = replace(batch, approval=hmac.new(f.secret, binding(batch), hashlib.sha256).hexdigest())
    f.receipts[batch.child_id] = batch.approval
    (f.private / "seals.json").write_bytes(canonical(f.receipts))
    f.write_records(f.private, batch)
    return batch


cases("ml_3_predicate", "ML-3", [(n.replace("-", "_").replace("=", "_"), n) for n in (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply",
    "head", "sparse", "skip_worktree", "submodule", "nested_repository", "autocrlf",
    "attribute_filter=custom", "attribute_text", "attribute_eol=crlf", "attribute_working-tree-encoding=UTF-16",
    "attribute_ident", "chmod", "exec_change", "duplicate", "dotdot", "absolute", "rename", "case_alias",
    "unicode_alias", "aux_alias", "add_existing", "base_mode", "base_bytes")], precondition)


def converted_source(test, value):
    source, attribute = value
    data = f"a.txt {attribute}\n".encode()
    if source == "base":
        put(test.f.project / ".gitattributes", data)
        git(test.f.project, "add", ".gitattributes")
        git(test.f.project, "commit", "-qm", "base attribute")
        test.f.base = git(test.f.project, "rev-parse", "HEAD").decode().strip()
        changes = {"a.txt": b"child"}
    else:
        changes = {"a.txt": b"child", ".gitattributes": data}
    test.refused(test.f.batch(changes))


cases("ml_3_converted", "ML-3", [(f"{source}_{i}", (source, attribute))
      for source in ("base", "candidate")
      for i, attribute in enumerate(("filter=custom", "text", "eol=crlf", "working-tree-encoding=UTF-16", "ident"))],
      converted_source)


def fs_refusal(test, kind):
    batch = test.f.batch()
    fired = []
    def hook(call):
        if call.when != "before":
            return None
        if kind in ("fat_no_exchange", "hfs_no_noreplace") and call.phase == "intake" and call.operation == (
                "exchange" if kind == "fat_no_exchange" else "rename_noreplace"):
            fired.append(call)
            raise OSError(errno.ENOTSUP, "unsupported image operation")
        if call.operation in ("stat", "fstat", "lstat"):
            target = {"mounted_target": test.f.project / "removed", "cross_drive_staging": test.f.project / ".cocolo",
                      "cross_drive_history": test.f.private}.get(kind)
            if target and call.path and Path(call.path).is_relative_to(target):
                fired.append(call)
                info = list(target.stat())
                info[2] += 1  # st_dev
                return ml.IOOverride(os.stat_result(info))
        return None
    test.refused(batch, hook=hook)
    test.assertTrue(fired, "the named capability/device guard must actually be exercised")


cases("ml_4_filesystem", "ML-4", [(n, n) for n in (
    "fat_no_exchange", "hfs_no_noreplace", "mounted_target", "cross_drive_staging", "cross_drive_history")], fs_refusal)


def corrupt_history(test, part):
    batch = test.f.batch()
    fired = []
    def hook(call):
        if call.operation == "sqlite_read" and call.when == "before" and call.detail.get("part") == part:
            fired.append(call)
            return ml.IOOverride(b"CORRUPT HISTORY " + part.encode())
    trace = Trace(lambda _, call: hook(call))
    test.refused(batch, hook=trace)
    test.assertTrue(fired)
    test.assertFalse(any(c.operation == "sqlite_commit" and c.detail.get("commit") == "A" for c in trace.calls))


cases("ml_5_history", "ML-5", [(n, n) for n in ("events", "old_contents", "new_contents", "batch_digest")], corrupt_history)
cases("ml_1_history", "ML-1", [("tampered_row", "events")], corrupt_history)


def merge_case(test, kind):
    base = b"one\ntwo\nthree\n"
    owner, child, expected = {
        "owner_only": (b"one\ntwo\nTHREE\n", base, b"one\ntwo\nTHREE\n"),
        "child_only": (base, b"ONE\ntwo\nthree\n", b"ONE\ntwo\nthree\n"),
        "disjoint": (b"one\ntwo\nTHREE\n", b"ONE\ntwo\nthree\n", b"ONE\ntwo\nTHREE\n"),
        "overlap": (b"OWNER\ntwo\nthree\n", b"CHILD\ntwo\nthree\n", None),
    }[kind]
    batch = test.f.batch({"a.txt": child})
    put(test.f.project / "a.txt", owner)
    trace = Trace()
    invoked = []
    real_popen = subprocess.Popen
    def launch(args, *positional, **kwargs):
        argv = list(args)
        if "merge-file" in argv:
            cwd = Path(kwargs.get("cwd") or os.getcwd())
            inputs = tuple((cwd / p).read_bytes() for p in argv[-3:])
            invoked.append((argv, cwd, inputs))
        return real_popen(args, *positional, **kwargs)
    with mock.patch("subprocess.Popen", side_effect=launch):
        report = test.f.land(batch, hook=trace)
    if expected is None:
        test.assertEqual("waiting_owner", report.state)
        test.assertEqual(owner, (test.f.project / "a.txt").read_bytes())
        retained = [Path(p).read_bytes() for r in report.paths for p in r.retained]
        test.assertIn(child, retained)
    else:
        test.assertEqual("landed", report.state)
        test.assertEqual(expected, (test.f.project / "a.txt").read_bytes())
        if kind == "disjoint":
            test.assertIn("a.txt", report.merged)
            test.assertEqual(expected, test.f.check_calls[-1]["a.txt"][2])
            assert_trace(test, trace.calls)
    merge_calls = [c for c in trace.calls if c.operation == "merge_file" and c.when == "before"]
    test.assertTrue(merge_calls)
    test.assertTrue(invoked, "observe a real git merge-file subprocess, not only a hook label")
    test.assertEqual(len(invoked), len(merge_calls))
    staging = test.f.project / ".cocolo/staging" / batch.child_id
    for call, (actual_argv, actual_cwd, inputs) in zip(merge_calls, invoked):
        argv = call.detail["argv"]
        test.assertEqual(actual_argv, list(argv))
        test.assertEqual("git", Path(argv[0]).name)
        test.assertIn("merge-file", argv)
        test.assertEqual(3, len(argv[-3:]))
        cwd = Path(call.detail["cwd"])
        test.assertEqual(actual_cwd, cwd)
        test.assertCountEqual((base, owner, child), inputs)
        for argument in argv[-3:]:
            path = Path(os.path.abspath(cwd / argument))
            test.assertTrue(path.is_relative_to(staging), path)
    assert_no_git_writes(test, test.f, trace.calls)


cases("ml_25_merge", "ML-25", [(n, n) for n in ("owner_only", "child_only", "disjoint", "overlap")], merge_case)


def whole_batch(test, nth):
    batch = test.f.batch({"a.txt": b"a child", "b": b"b child", "z.txt": b"z child"})
    count = []
    def hook(call):
        if call.when == "before" and call.phase == "forward" and call.operation in ("exchange", "rename_noreplace"):
            count.append(call)
            if len(count) == nth:
                raise OSError(errno.EIO, "fail Nth file")
    report = test.f.land(batch, hook=hook)
    test.assertEqual(nth, len(count))
    test.assertEqual("reverted", report.state)
    test.assertEqual(test.f.before, tree(test.f.project))
    test.assertFalse(any(r.state == "landed" for r in ml.history(test.f.project, test.f.private)))


cases("ml_26_whole", "ML-26", [(f"fail_file_{i}", i) for i in (1, 2, 3)], whole_batch)


def owner_commit(test, clean):
    batch = test.f.batch({"a.txt": b"ONE\ntwo\nthree\n"})
    ticket = ml.enqueue(test.f.project, test.f.private, batch, policy=test.f.policy)
    owner = b"one\ntwo\nTHREE\n" if clean else b"OWNER\ntwo\nthree\n"
    put(test.f.project / "a.txt", owner)
    git(test.f.project, "add", "a.txt")
    git(test.f.project, "commit", "-qm", "owner ahead of queued child")
    current = git(test.f.project, "rev-parse", "HEAD").decode().strip()
    observations, rebuilt = [], []
    def builder(candidate, head):
        observations.append((candidate, head, tree(test.f.project), ml.queue(test.f.project, test.f.private),
                             ml.history(test.f.project, test.f.private)))
        if not clean:
            return None
        test.f.base = head
        result = test.f.batch({"a.txt": b"ONE\ntwo\nTHREE\n"}, "rebuilt-child")
        rebuilt.append(result)
        return result
    owner_tree = tree(test.f.project)
    fake = mock.Mock(side_effect=builder)
    report = test.f.land(batch, policy=replace(test.f.policy, builder=fake, write_children=True))
    fake.assert_called_once()
    test.assertEqual(1, len(observations))
    candidate, head, observed_tree, queue, history = observations[0]
    test.assertEqual(batch, candidate)
    test.assertEqual(current, head)
    test.assertEqual(owner_tree, observed_tree, "stale original must stop before builder")
    test.assertEqual("owner", queue[0].kind)
    test.assertEqual(current, queue[0].base_commit)
    stale = next(q for q in queue if q.ticket == ticket.ticket)
    test.assertEqual(batch.base_commit, stale.base_commit, "never rewrite a sealed stale base")
    test.assertEqual("waiting_owner", stale.state)
    test.assertFalse(any(r.batch == batch and r.state in ("applying", "landed") for r in history))
    rows = ml.history(test.f.project, test.f.private)
    test.assertFalse(any(r.batch == batch and r.state == "landed" for r in rows))
    if clean:
        test.assertEqual("landed", report.state)
        test.assertEqual(b"ONE\ntwo\nTHREE\n", (test.f.project / "a.txt").read_bytes())
        test.assertEqual([rebuilt[0]], [r.batch for r in rows if r.state == "landed"])
        test.assertEqual(current, rebuilt[0].base_commit)
        test.assertTrue(test.f.verify(rebuilt[0]))
    else:
        test.assertEqual("waiting_owner", report.state)
        test.assertEqual(owner_tree, tree(test.f.project))
    test.assertEqual(current, git(test.f.project, "rev-parse", "HEAD").decode().strip())


cases("ml_28_owner_first", "ML-28", [("clean_reapply", True), ("unclean_stops", False)], owner_commit)


def forgery(test, kind):
    batch = test.f.batch()
    editable = test.f.project / ".cocolo"
    editable.mkdir()
    if kind == "private_location":
        forged = replace(batch, approval="child invented approval")
        # Model an otherwise-working authority adapter using this misplaced root.
        # Removing only the root-location guard would trust the child's receipt.
        test.f.write_records(editable, forged)
        put(editable / "seals.json", ('{"child-1":"' + forged.approval + '"}').encode())
        def verify_child_authority(b):
            import json
            return (json.loads((editable / "seals.json").read_text()).get(b.child_id) == b.approval
                    and test.f.verify_records(editable, b))
        child_authority = replace(test.f.policy, verify_seal=verify_child_authority)
        put(editable / "approvals.json", b'{"child-1":"child invented approval"}')
        test.assertTrue(child_authority.verify_seal(forged), "valid except for authority location")
        before = tree(test.f.project)
        report = ml.land(test.f.project, editable, forged, policy=child_authority)
        test.assertEqual("refused", report.state)
        test.assertEqual(before, tree(test.f.project))
        location_refusal_oracle(test, report, before, tree(test.f.project))
        # Explicit negative control: identical forged data and verifier, but an
        # outside root, must land. Location is the sole failing precondition above.
        relocated = test.f.root / "relocated-child-authority"
        shutil.copytree(editable, relocated)
        accepted = ml.land(test.f.project, relocated, forged, policy=child_authority)
        test.assertEqual("landed", accepted.state)
        test.assertEqual(test.f.expected(forged), tree(test.f.project))
        test.assertEqual([forged], [r.batch for r in ml.history(test.f.project, relocated)
                                  if r.state == "landed"])
        return
    forged = replace(batch, approval="child invented approval")
    source = test.f.root / "forged-private"
    source.mkdir()
    # These JSON stores are valid under the fixture's real authority adapter.
    test.f.write_records(source, forged)
    test.assertTrue(test.f.verify_records(source, forged))
    if kind == "history.sqlite":
        ml._seed_history(test.f.project, source, (ml.HistoryRow(99, forged, "landed"),))
        test.assertEqual((ml.HistoryRow(99, forged, "landed"),), ml.history(test.f.project, source))
        databases = [p for p in source.rglob("*") if p.is_file()
                     and p.read_bytes().startswith(b"SQLite format 3\0")]
        test.assertEqual(1, len(databases))
        # SQLite backup includes committed WAL pages; copying the main file does not.
        with sqlite3.connect(databases[0]) as src, sqlite3.connect(editable / kind) as dst:
            src.backup(dst)
        with sqlite3.connect(editable / kind) as db:
            test.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
    else:
        shutil.copyfile(source / kind, editable / kind)
    before = tree(test.f.project, control=True)
    report = test.f.land(forged)
    test.assertEqual("refused", report.state)
    test.assertEqual(before, tree(test.f.project, control=True))
    test.assertFalse(any(r.batch == forged for r in ml.history(test.f.project, test.f.private)))
    test.assertEqual("landed", test.f.land(batch).state, "forged project records must not deny a valid private receipt")
    test.assertEqual([batch], [r.batch for r in ml.history(test.f.project, test.f.private) if r.state == "landed"])
    test.assertFalse(any(p.name == "objects" for p in test.f.private.rglob("*")), "no loose blob store")


cases("ml_30_forgery", "ML-30", [(n, n) for n in
      ("history.sqlite", "ledger.json", "signatures.json", "approvals.json", "private_location")], forgery)


declare_ml_tests(globals())

if __name__ == "__main__":
    unittest.main()
