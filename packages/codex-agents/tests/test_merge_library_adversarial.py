"""ML crash/fault/race tests; real SIGKILL, no sockets, only throwaway directories.

Run this module separately with unittest (under 120 s in the unimplemented red
state). Sweeps discover the implementation's hook boundaries rather than a
fixed list of checkpoints. They cover forward, compensation, reverse, undo,
recovery and cleanup. Real-image tests are opt-in because portable syscall
injection cannot prove FAT/HFS+, APFS/ext4 flush or genuine disk exhaustion.
Set COCOLO_ML_REAL_IMAGES=1 on macOS to run temporary mounted image profiles.
Never point these tests at a real project or at ~/.cocolo.
"""
from __future__ import annotations

import contextlib
import errno
import os
import pickle
import signal
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from tests.support import TEMP_ROOT  # noqa: F401 -- native package bootstrap
from codex_agents import merge_library as ml
from tests.merge_library_support import (
    declare_ml_tests,
    FAULTABLE, Fixture, Trace, checkpoint, child_run, fault_sweep,
    kill_oracle, put, report_oracle, seed_applied, tree, ReplayPoint, replay_key,
    owner_preservation_oracle, version_at, sample_boundaries, replay_points,
    outcome_oracle, recover_checked,
)


def forward(f, batch, hook):
    return f.land(batch, hook=hook)


def reverse(f, batch, hook):
    seed_applied(f, batch)
    f.operation_before = tree(f.project)
    return ml.rollback(f.project, f.private, 1, hook=hook)


def recovering(f, batch, hook):
    seed_applied(f, batch)
    f.operation_before = tree(f.project)
    reports = ml.recover(f.project, f.private, hook=hook)
    return reports[-1]


def undoing(f, batch, hook):
    landed = f.land(batch)
    f.operation_before = tree(f.project)
    return ml.undo(f.project, f.private, landed.batch_no, hook=hook)


def cleaning(f, batch, hook):
    seed_applied(f, batch, state="landed")
    f.operation_before = tree(f.project)
    reports = ml.cleanup(f.project, f.private, hook=hook)
    return reports[-1]


class Adversarial(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def test_ml_5_kill_every_pre_A_history_write(self):
        """ML-5: SIGKILL at every history write before A leaves no applying row/tree change."""
        def unchanged(f, batch, call, rows):
            self.assertFalse(any(r.state == "applying" for r in rows))
            self.assertEqual(f.before, tree(f.project))
        kill_oracle(self, forward, select=lambda c: c.phase == "history" and c.operation in
                    ("sqlite_write", "write", "fsync", "fullfsync"), oracle=unchanged)

    def test_ml_6_kill_both_sides_A_B(self):
        """ML-6: actual SIGKILL immediately before and after each A/B commit."""
        kill_oracle(self, forward, select=lambda c: c.operation == "sqlite_commit"
                    and c.detail.get("commit") in ("A", "B"))

    def test_ml_6_kill_at_mkdir_inspects_durable_folder_identities(self):
        """ML-6/20: A has emptied identities; mkdir cannot already have a created identity."""
        batch = self.f.batch({"made/add": b"child", "removed/old.txt": None})
        folder = self.f.project / "removed"
        emptied = (folder.stat().st_dev, folder.stat().st_ino)
        marker = self.f.root / "killed-mkdir"
        def run():
            def hook(call):
                if (call.operation == "mkdir" and call.when == "after"
                        and call.path == str(self.f.project / "made")):
                    marker.write_text("mkdir completed")
                    os.kill(os.getpid(), signal.SIGKILL)
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
        self.assertEqual("mkdir completed", marker.read_text())
        rows = ml.history(self.f.project, self.f.private)  # real durable A, never _seed_history
        row = next(r for r in rows if r.batch == batch)
        self.assertEqual("applying", row.state)
        self.assertEqual(emptied, row.emptied_folders["removed"])
        self.assertFalse(row.created_folders.get("made"))
        created = self.f.project / "made"
        identity = (created.stat().st_dev, created.stat().st_ino)
        reports = ml.recover(self.f.project, self.f.private)
        self.assertEqual(identity, (created.stat().st_dev, created.stat().st_ino))
        expected = dict(self.f.before, made=(stat.S_IFDIR, 0o755, None))
        self.assertEqual(expected, tree(self.f.project))
        self.assertTrue(any(p.path == "made" for r in reports for p in r.paths))

    def test_ml_6_emptied_identities_are_durable_immediately_after_A(self):
        """ML-6/23: read real A after SIGKILL at its after hook, before any mkdir."""
        batch = self.f.batch({"made/add": b"child", "removed/old.txt": None})
        folder = self.f.project / "removed"
        emptied = (folder.stat().st_dev, folder.stat().st_ino)
        marker = self.f.root / "killed-after-A"
        def run():
            def hook(call):
                if (call.operation == "sqlite_commit" and call.when == "after"
                        and call.detail.get("commit") == "A"):
                    marker.write_text("A committed")
                    os.kill(os.getpid(), signal.SIGKILL)
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
        self.assertEqual("A committed", marker.read_text())
        self.assertFalse((self.f.project / "made").exists())
        self.assertEqual(self.f.before, tree(self.f.project))
        rows = ml.history(self.f.project, self.f.private)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(batch, row.batch)
        self.assertEqual("applying", row.state)
        self.assertEqual({"removed": emptied}, row.emptied_folders)
        self.assertEqual({}, row.created_folders)

    def test_ml_7_safe_save_captured_then_kill(self):
        """ML-7: safe-save B captured by exchange survives kill before displaced inspection."""
        batch = self.f.batch({"a.txt": b"child\n"})
        def run():
            armed = []
            def hook(call):
                if call.operation == "exchange" and call.phase == "forward":
                    if call.when == "before" and not armed:
                        save = self.f.project / "editor-save"
                        put(save, b"OWNER safe-save B\n")
                        os.replace(save, self.f.project / "a.txt")
                        armed.append(True)
                    elif call.when == "after" and armed:
                        os.kill(os.getpid(), signal.SIGKILL)
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
        reports = ml.recover(self.f.project, self.f.private)
        named = {str(self.f.project / "a.txt")} | {p for r in reports for row in r.paths for p in row.retained}
        self.assertTrue(any(Path(p).is_file() and Path(p).read_bytes() == b"OWNER safe-save B\n" for p in named))

    def test_ml_8_synchronized_reader_partial_write_and_boundaries(self):
        """ML-8: synchronized readers see complete base/new or legitimate absence at every boundary."""
        batch = self.f.batch()
        request, sampled, stop = threading.Event(), threading.Event(), threading.Event()
        errors, observations, partial = [], [], []
        allowed = {e.path: {None if v is None else v.data for v in (e.base, e.new)}
                   for e in batch.events if e.kind == "file"}
        def reader():
            while request.wait(timeout=3):
                request.clear()
                if stop.is_set():
                    return
                try:
                    for name, versions in allowed.items():
                        path = self.f.project / name
                        try:
                            data = path.read_bytes()
                        except FileNotFoundError:
                            data = None
                        if data not in versions:
                            errors.append((name, data))
                    observations.append(True)
                except BaseException as error:
                    errors.append((type(error).__name__, str(error)))
                finally:
                    sampled.set()
        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        def hook(call):
            override = None
            if call.operation == "write" and call.when == "before" and len(call.detail["data"]) > 1:
                data = call.detail["data"]
                count = os.write(call.detail["fd"], data[:max(1, len(data) // 2)])
                partial.append(count)
                override = ml.IOOverride(count)
            sampled.clear()
            request.set()
            if not sampled.wait(timeout=3):
                raise AssertionError("synchronized reader did not sample boundary")
            return override
        try:
            self.f.land(batch, hook=hook)
        finally:
            stop.set()
            request.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(partial)
        self.assertTrue(observations)
        self.assertEqual([], errors)

    def test_ml_10_overlap_keeps_both_and_states_open_handle_limit(self):
        """ML-10: overlap keeps named owner/child versions and reports the old-handle residual."""
        batch = self.f.batch({"a.txt": b"CHILD\ntwo\nthree\n"})
        put(self.f.project / "a.txt", b"OWNER\ntwo\nthree\n")
        report = self.f.land(batch)
        self.assertIn(report.state, ("needs_owner", "waiting_owner"))
        self.assertEqual("owner", report.reason)
        versions = {Path(p).read_bytes() for r in report.paths for p in r.retained}
        versions.add((self.f.project / "a.txt").read_bytes())
        self.assertIn(b"OWNER\ntwo\nthree\n", versions)
        self.assertIn(b"CHILD\ntwo\nthree\n", versions)
        notes = " ".join(report.notes).lower()
        self.assertIn("handle", notes)
        self.assertIn("cleanup", notes)

    def test_ml_10_old_open_handle_write_during_cleanup(self):
        """ML-10: old handle writes before cleanup content check are retained and named."""
        batch = self.f.batch({"a.txt": b"child\n"})
        with (self.f.project / "a.txt").open("r+b") as old:
            def edit(_):
                old.seek(0)
                old.write(b"OWNER via old handle\n")
                old.truncate()
                old.flush()
            hook, fired = checkpoint("cleanup_inspect", edit)
            report = self.f.land(batch, hook=hook)
        self.assertTrue(fired)
        retained = {p for r in report.paths for p in r.retained} | set(report.leftovers)
        self.assertTrue(any(Path(p).is_file() and Path(p).read_bytes() == b"OWNER via old handle\n" for p in retained))
        self.assertIn("handle", " ".join(report.notes).lower())

    def test_ml_11_applying_all_new_reverses_from_history(self):
        """ML-11: even all-new applying tree is put back; staging is not a direction oracle."""
        batch = self.f.batch()
        staging, _ = seed_applied(self.f, batch)
        put(staging / "pretend-landed-state", b'{"state":"landed"}')
        reports = ml.recover(self.f.project, self.f.private)
        self.assertEqual(self.f.before, tree(self.f.project))
        self.assertTrue(any(r.state == "reverted" for r in reports))
        self.assertEqual(b'{"state":"landed"}', (staging / "pretend-landed-state").read_bytes())
        first = tree(self.f.project, control=True)
        rows = ml.history(self.f.project, self.f.private)
        ml.recover(self.f.project, self.f.private)
        self.assertEqual(first, tree(self.f.project, control=True))
        self.assertEqual(rows, ml.history(self.f.project, self.f.private))

    def test_ml_11_kill_every_compensation_call(self):
        """ML-11: kill every compensation boundary; recover twice without losing captured owner bytes."""
        compensation_sweep(self, kill=True)

    def test_ml_12_fault_every_compensation_call(self):
        """ML-12: ENOSPC/EIO at every compensation call keeps owner versions and history pinned."""
        compensation_sweep(self, kill=False)

    def test_ml_12_missing_staging_failed_restore_then_retry(self):
        """ML-12: missing displaced copy may require allocation; ENOSPC pins history; retry works."""
        batch = self.f.batch()
        _, paths = seed_applied(self.f, batch)
        Path(paths["a.txt"]).unlink()
        def no_space(call):
            if call.when == "before" and call.operation in ("write", "ftruncate", "sqlite_write"):
                raise OSError(errno.ENOSPC, "really no restore space in injected profile")
        reports = ml.recover(self.f.project, self.f.private, hook=no_space)
        for result in reports:
            report_oracle(self, self.f, result, batch)
        report = reports[-1]
        self.assertEqual("needs_owner", report.state)
        self.assertEqual("io", report.reason)
        self.assertTrue(all(r.contents_kept for r in ml.history(self.f.project, self.f.private)))
        self.assertNotIn("allocation-free", " ".join(report.notes))
        result = ml.retry(self.f.project, self.f.private, 1, policy=self.f.policy)
        report_oracle(self, self.f, result, batch)
        self.assertEqual("reverted", result.state)
        self.assertEqual(self.f.before, tree(self.f.project))

    def test_ml_12_failed_terminal_commit_not_claimed_recorded(self):
        """ML-12: failed terminal record is pending/unresolved, pinned, and blocks admission."""
        batch = self.f.batch()
        def fail_terminal(call):
            if call.operation == "sqlite_commit" and call.when == "before" and call.detail.get("commit") != "A":
                raise OSError(errno.EIO, "terminal commit unavailable")
        report = self.f.land(batch, hook=fail_terminal)
        report_oracle(self, self.f, report, batch)
        self.assertFalse(report.recorded)
        self.assertEqual("needs_owner", report.state)
        self.assertEqual("io", report.reason)
        self.assertIn("pending", " ".join(report.notes))
        self.assertTrue(all(r.contents_kept for r in ml.history(self.f.project, self.f.private)))
        second = self.f.batch({"second": b"two"}, "second")
        self.assertEqual("waiting_owner", self.f.land(second).state)
        result = ml.retry(self.f.project, self.f.private, report.batch_no, policy=self.f.policy)
        report_oracle(self, self.f, result, batch)

    def test_ml_13_undo_kill_around_combined_commit(self):
        """ML-13: crash either side of combined undo/X-undone transaction follows durable state."""
        kill_oracle(self, undoing, select=lambda c: c.operation == "sqlite_commit"
                    and c.detail.get("commit") == "undo")

    def test_ml_14_unknown_contents_reported(self):
        """ML-14: inaccessible contents are unknown, distinct from an owner conflict."""
        batch = self.f.batch({"a.txt": b"child"})
        def deny(call):
            if call.operation in ("open", "read") and call.path == str(self.f.project / "a.txt"):
                raise PermissionError(errno.EACCES, "oracle inaccessible")
        report = self.f.land(batch, hook=deny)
        self.assertIn("unknown", [p.classification for p in report.paths if p.path == "a.txt"])
        self.assertEqual("io", report.reason)

    def test_ml_18_kill_during_retirement(self):
        """ML-18: SIGKILL each retirement call never drops contents before cleanup/handoff."""
        def prepared(f):
            first = f.land(f.batch({"p0": b"0"}, "c0"))
            for i in range(1, 11):
                f.land(f.batch({f"p{i}": str(i).encode()}, f"c{i}"))
            return first
        prepared(self.f)
        trace = Trace()
        ml.cleanup(self.f.project, self.f.private, hook=trace)
        points = replay_points(trace.calls, self.f, lambda c: c.phase == "retirement")
        self.assertTrue(points)
        for point, _ in sample_boundaries(points, reproducer="retirement"):
            f = Fixture()
            try:
                prepared(f)
                marker = f.root / "retirement-fired"
                def inject(call):
                    marker.write_text("matched")
                    os.kill(os.getpid(), signal.SIGKILL)
                replay = ReplayPoint(point, f, inject)
                status = child_run(lambda: ml.cleanup(f.project, f.private, hook=Trace(replay)))
                self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
                self.assertEqual("matched", marker.read_text())
                for row in ml.history(f.project, f.private):
                    self.assertTrue(row.contents_kept or row.cleanup_complete)
                ml.recover(f.project, f.private)
            finally:
                f.close()

    def test_ml_21_owner_delete_after_A_not_recreated(self):
        """ML-21: owner deletes target immediately after A; no displaced copy means no restore."""
        batch = self.f.batch({"a.txt": None})
        def run():
            def hook(call):
                if call.operation == "sqlite_commit" and call.when == "after" and call.detail.get("commit") == "A":
                    (self.f.project / "a.txt").unlink()
                    os.kill(os.getpid(), signal.SIGKILL)
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status))
        ml.recover(self.f.project, self.f.private)
        self.assertFalse((self.f.project / "a.txt").exists())

    def test_ml_21_owner_empty_folder_after_A_kept(self):
        """ML-21: owner creates empty folder after A; without identity evidence it stays."""
        batch = self.f.batch({"new/add": b"child"})
        def run():
            def hook(call):
                if call.operation == "sqlite_commit" and call.when == "after" and call.detail.get("commit") == "A":
                    (self.f.project / "new").mkdir()
                    os.kill(os.getpid(), signal.SIGKILL)
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status))
        owner_identity = (self.f.project / "new").stat().st_ino
        ml.recover(self.f.project, self.f.private)
        self.assertEqual(owner_identity, (self.f.project / "new").stat().st_ino)
        self.assertEqual([], list((self.f.project / "new").iterdir()))

    def test_ml_24_killed_staging_listed_then_removed_on_start(self):
        """ML-24: kill leaves one named staging folder; restart removes known contents."""
        batch = self.f.batch()
        def run():
            hook, _ = checkpoint("staging_complete", lambda _: os.kill(os.getpid(), signal.SIGKILL))
            self.f.land(batch, hook=hook)
        status = child_run(run)
        self.assertTrue(os.WIFSIGNALED(status))
        staging = self.f.project / ".cocolo/staging/child-1"
        self.assertTrue(staging.is_dir())
        self.assertEqual([staging], list(staging.parent.iterdir()))
        reports = ml.status(self.f.project, self.f.private)
        self.assertIn(str(staging), {p for r in reports for p in r.leftovers})
        ml.recover(self.f.project, self.f.private)
        self.assertFalse(staging.exists())


def cases(prefix, requirement, values, body):
    for name, value in values:
        def test(self, value=value):
            body(self, value)
        test.__name__ = f"test_{prefix}_{name}"
        test.__doc__ = f"{requirement}: {name.replace('_', ' ')}."
        setattr(Adversarial, test.__name__, test)


def staging_foreign(test, kind):
    batch = test.f.batch()
    staging, paths = seed_applied(test.f, batch)
    displaced = Path(paths["a.txt"])
    if kind == "missing":
        displaced.unlink()
    else:
        put(displaced, b"OWNER complete foreign" if kind == "complete" else b"OW")
    retained = ({str(displaced): version_at(displaced)} if kind != "missing" else {})
    reports = ml.recover(test.f.project, test.f.private)
    for report in reports:
        report_oracle(test, test.f, report, batch, retained=retained)
    if kind != "missing":
        expected = b"OWNER complete foreign" if kind == "complete" else b"OW"
        test.assertEqual(expected, displaced.read_bytes())
        test.assertIn(str(displaced), {p for r in reports for row in r.paths for p in row.retained}
                      | {p for r in reports for p in r.leftovers})
    else:
        test.assertEqual(test.f.before, tree(test.f.project))
    test.assertEqual(staging, test.f.project / ".cocolo/staging/child-1")


cases("ml_7_staging", "ML-7", [(n, n) for n in ("complete", "partial", "missing")], staging_foreign)


def killed(test, value):
    operation = {"forward": forward, "reverse": reverse, "undo": undoing,
                 "recovery": recovering, "cleanup": cleaning}[value]
    kill_oracle(test, operation)


cases("ml_11_kill", "ML-11", [(n, n) for n in ("forward", "reverse", "undo", "recovery", "cleanup")], killed)


def faults(test, value):
    operation = {"forward": forward, "reverse": reverse, "undo": undoing,
                 "recovery": recovering, "cleanup": cleaning}[value]
    fault_sweep(test, operation)


cases("ml_12_fault_sweep", "ML-12", [(n, n) for n in ("forward", "reverse", "undo", "recovery", "cleanup")], faults)


def uncertain_commit(test, commit):
    batch = test.f.batch()
    fired = []
    def hook(call):
        if (not fired and call.operation == "sqlite_commit" and call.when == "after"
                and call.detail.get("commit") == commit):
            fired.append(True)
            raise OSError(errno.EIO, "commit succeeded but acknowledgement failed")
    report = test.f.land(batch, hook=hook)
    test.assertTrue(fired)
    test.assertTrue(report.recorded)
    test.assertEqual("landed" if commit == "B" else "reverted", report.state)
    outcome_oracle(test, test.f, batch, report, ml.history(test.f.project, test.f.private), "forward")
    recover_checked(test, test.f, batch, "forward")
    test.assertEqual(test.f.expected(batch) if commit == "B" else test.f.before, tree(test.f.project))


cases("ml_12_uncertain", "ML-12", [(n, n) for n in ("A", "B")], uncertain_commit)


def compensation_sweep(test, *, kill):
    owner_bytes = b"OWNER captured race\n"
    def run(f, batch, selected=None, fault=None):
        raced = []
        def action(call):
            (f.root / "injection-fired").write_text("matched")
            if kill:
                os.kill(os.getpid(), signal.SIGKILL)
            raise OSError(fault, "compensation fault")
        point = ReplayPoint(selected, f, action) if selected else None
        def inject(index, call):
            if not raced and call.operation == "exchange" and call.phase == "forward" and call.when == "before":
                put(f.project / "a.txt", owner_bytes)
                f.owner_versions["a.txt"] = {(stat.S_IFREG, 0o644, owner_bytes)}
                (f.root / "owner-snapshot").write_bytes(pickle.dumps(tree(f.project)))
                raced.append(True)
            return point(index, call) if point else None
        trace = Trace(inject)
        report = f.land(batch, hook=trace)
        if point:
            point.assert_fired(test)
        return trace, report
    discovery = Fixture()
    try:
        trace, _ = run(discovery, discovery.batch({"a.txt": b"child"}))
        points = replay_points(trace.calls, discovery, lambda c: c.phase == "compensation" and (kill or c.operation in FAULTABLE))
        test.assertTrue(points)
        test.assertTrue({"before", "after"}.issubset({key[0][2] for key, _ in points}))
    finally:
        discovery.close()
    for point, _ in sample_boundaries(points, reproducer="compensation"):
        for fault in (None,) if kill else (errno.ENOSPC, errno.EIO):
            f = Fixture()
            try:
                batch = f.batch({"a.txt": b"child"})
                f.owner_versions["a.txt"] = {(stat.S_IFREG, 0o644, owner_bytes)}
                if kill:
                    status = child_run(lambda: run(f, batch, point))
                    test.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
                    reports = ml.recover(f.project, f.private)
                else:
                    _, report = run(f, batch, point, fault)
                    test.assertEqual("needs_owner", report.state)
                    test.assertEqual("io", report.reason)
                    test.assertTrue(all(r.contents_kept for r in ml.history(f.project, f.private)))
                    report_oracle(test, f, report, batch)
                    owner = pickle.loads((f.root / "owner-snapshot").read_bytes())
                    owner_preservation_oracle(test, f, batch, (report,), owner)
                    reports = ml.recover(f.project, f.private)
                test.assertEqual("matched", (f.root / "injection-fired").read_text())
                owner = pickle.loads((f.root / "owner-snapshot").read_bytes())
                owner_preservation_oracle(test, f, batch, reports, owner)
                for report in reports:
                    report_oracle(test, f, report, batch)
                first = tree(f.project, control=True)
                records = ml.history(f.project, f.private)
                again = ml.recover(f.project, f.private)
                owner_preservation_oracle(test, f, batch, again, owner)
                for report in again:
                    report_oracle(test, f, report, batch)
                test.assertEqual(first, tree(f.project, control=True))
                test.assertEqual(records, ml.history(f.project, f.private))
            finally:
                f.close()


def owner_edit(test, value):
    phase, edit = value
    # Discover every hook in the relevant phase with an actual successful run.
    operation = reverse if phase == "reverse" else recovering if phase == "recovery" else forward
    discovery = Fixture()
    try:
        batch = discovery.batch({"a.txt": b"CHILD\n"})
        trace = Trace()
        if phase == "compensation":
            # Force a captured owner save to enter the single-compensation path.
            def race(index, call):
                if call.operation == "exchange" and call.phase == "forward" and call.when == "before":
                    put(discovery.project / "a.txt", b"FIRST OWNER\n")
            trace.inject = race
        operation(discovery, batch, trace)
        points = replay_points(trace.calls, discovery, lambda c: c.phase == phase)
        test.assertTrue(points, phase)
    finally:
        discovery.close()
    for point, _ in sample_boundaries(points, reproducer="owner_" + phase):
        f = Fixture()
        try:
            batch = f.batch({"a.txt": b"CHILD\n"})
            def edit_owner(call):
                target = f.project / "a.txt"
                if edit == "in_place":
                    put(target, b"OWNER unique final\n")
                elif edit == "safe_save":
                    save = f.project / "editor-save"
                    put(save, b"OWNER unique final\n")
                    os.replace(save, target)
                elif edit == "delete":
                    target.unlink(missing_ok=True)
                else:
                    target.chmod(0o600)
                if target.exists():
                    f.owner_versions.setdefault("a.txt", set()).add(version_at(target))
            replay = ReplayPoint(point, f, edit_owner)
            def inject(index, call):
                if phase == "compensation" and call.operation == "exchange" and call.phase == "forward" and call.when == "before":
                    put(f.project / "a.txt", b"FIRST OWNER\n")
                    f.owner_versions.setdefault("a.txt", set()).add(version_at(f.project / "a.txt"))
                return replay(index, call)
            report = operation(f, batch, Trace(inject))
            replay.assert_fired(test)
            retained = {str(f.project / "a.txt")} | {p for r in report.paths for p in r.retained} | set(report.leftovers)
            if edit == "delete":
                test.assertFalse((f.project / "a.txt").exists())
            elif edit == "chmod":
                test.assertTrue(any(Path(p).is_file() and stat.S_IMODE(Path(p).stat().st_mode) == 0o600 for p in retained))
            else:
                test.assertTrue(any(Path(p).is_file() and Path(p).read_bytes() == b"OWNER unique final\n" for p in retained))
            if phase != "cleanup":
                test.assertIn(report.state, ("needs_owner", "waiting_owner", "reverted"))
            report_oracle(test, f, report, batch)
        finally:
            f.close()


cases("ml_10_edit", "ML-10", [(f"{phase}_{edit}", (phase, edit))
      for phase in ("forward", "compensation", "reverse", "recovery", "cleanup")
      for edit in ("in_place", "safe_save", "delete", "chmod")], owner_edit)


def created_folder(test, kind):
    batch = test.f.batch({"made/add": b"child"})
    _, paths = seed_applied(test.f, batch)
    folder = test.f.project / "made"
    (folder / "add").unlink()
    created = {"made": (folder.stat().st_dev, folder.stat().st_ino)}
    if kind == "substituted":
        folder.rename(test.f.outside / "original-made")
        folder.mkdir()
    elif kind == "unrecorded":
        created = {"made": None}
    else:
        put(folder / "OWNER", b"keep")
    ml._seed_history(test.f.project, test.f.private, (ml.HistoryRow(1, batch, "applying"),),
                     created_folders=created, staged_paths=paths)
    before = tree(folder, metadata=True)
    identity = folder.stat().st_ino
    ml.recover(test.f.project, test.f.private)
    test.assertEqual(identity, folder.stat().st_ino)
    test.assertEqual(before, tree(folder, metadata=True))


cases("ml_20_created", "ML-20", [(n, n) for n in ("substituted", "unrecorded", "nonempty")], created_folder)


def empty_cleanup(test, kind):
    name = "unlisted/removed" if kind == "unlisted_ancestor" else "removed"
    if kind == "unlisted_ancestor":
        put(test.f.project / name / "old.txt", b"deleted")
    batch = test.f.batch({name + "/old.txt": None})
    folder = test.f.project / name
    identity = (folder.stat().st_dev, folder.stat().st_ino)
    (folder / "old.txt").unlink()
    ml._seed_history(test.f.project, test.f.private, (ml.HistoryRow(1, batch, "landed"),),
                     emptied_folders={name: identity})
    if kind == "owner_file":
        put(folder / "OWNER", b"keep")
    elif kind == "owner_empty":
        folder.rename(test.f.outside / "prior-emptied-folder")
        folder.mkdir()  # an actually empty directory created by the owner
    elif kind == "owner_nonempty_directory":
        (folder / "OWNER-empty").mkdir()
    elif kind == "swapped":
        folder.rename(test.f.outside / "old-folder")
        folder.mkdir()
    elif kind == "unlisted_ancestor":
        # This intermediate ancestor becomes empty after the listed child is removed.
        test.assertEqual([folder], list(folder.parent.iterdir()))
    before = tree(folder, metadata=True)
    ino = folder.stat().st_ino
    ml.cleanup(test.f.project, test.f.private)
    if kind == "unlisted_ancestor":
        test.assertFalse(folder.exists())
        test.assertTrue(folder.parent.is_dir())
        test.assertEqual([], list(folder.parent.iterdir()))
    else:
        test.assertEqual(ino, folder.stat().st_ino)
        test.assertEqual(before, tree(folder, metadata=True))


cases("ml_23_cleanup", "ML-23", [(n, n) for n in
      ("owner_file", "owner_empty", "owner_nonempty_directory", "swapped", "unlisted_ancestor")], empty_cleanup)


def swapped_walk(test, kind):
    batch = test.f.batch()
    canary = test.f.outside / "canary"
    put(canary, b"OUTSIDE UNTOUCHED", 0o640)
    before = tree(test.f.outside, metadata=True)
    target = {"parent_symlink": test.f.project / "removed", "cocolo_symlink": test.f.project / ".cocolo",
              "staging_symlink": test.f.project / ".cocolo/staging",
              "child_staging_symlink": test.f.project / ".cocolo/staging/child-1",
              "history_symlink": test.f.private, "mount_swap": test.f.project / "removed"}[kind]
    fired = []
    def hook(call):
        if kind == "mount_swap":
            if call.when == "before" and call.phase != "intake" and call.operation in ("stat", "fstat", "lstat") and call.path == str(target):
                fired.append(call)
                info = list(target.stat())
                info[2] += 1
                return ml.IOOverride(os.stat_result(info))
        elif (not fired and call.operation == "checkpoint" and call.when == "before"
              and call.detail.get("name") == ("staging_complete" if "staging" in kind or "cocolo" in kind else "after_intake")):
            fired.append(call)
            if target.exists():
                target.rename(target.with_name(target.name + "-owner-moved"))
            target.symlink_to(test.f.outside, target_is_directory=True)
        return None
    report = test.f.land(batch, hook=hook)
    test.assertTrue(fired)
    test.assertNotEqual("landed", report.state)
    test.assertEqual(before, tree(test.f.outside, metadata=True))
    test.assertIn(report.reason, ("moved", "unsafe_path", "cross_device"))


cases("ml_15_walk", "ML-15", [(n, n) for n in ("parent_symlink", "mount_swap", "cocolo_symlink",
      "staging_symlink", "child_staging_symlink", "history_symlink")], swapped_walk)


def moved_report(test, where):
    batch = test.f.batch()
    source = test.f.project / "removed"
    destination = (test.f.outside if where == "outside" else test.f.project) / "owner-moved"
    info = (source / "old.txt").stat()
    identity = (info.st_dev, info.st_ino)
    hook, fired = checkpoint("after_intake", lambda _: source.rename(destination))
    report = test.f.land(batch, hook=hook)
    test.assertTrue(fired)
    test.assertNotEqual("landed", report.state)
    test.assertEqual("moved", report.reason)
    test.assertIn("moved", [p.classification for p in report.paths])
    test.assertTrue(any("unverified" in n.lower() for n in report.notes))
    test.assertEqual(b"deleted\n", (destination / "old.txt").read_bytes())
    moved = {"removed/old.txt": (destination / "old.txt", identity)}
    report_oracle(test, test.f, report, batch, moved=moved)
    reports = ml.recover(test.f.project, test.f.private)
    for result in reports:
        report_oracle(test, test.f, result, batch, moved=moved)


cases("ml_14_move", "ML-14", [(n, n) for n in ("inside", "outside")], moved_report)


def cancel_phase(test, phase):
    batch = test.f.batch()
    ticket = ml.enqueue(test.f.project, test.f.private, batch, policy=test.f.policy)
    fired = []
    def hook(call):
        if call.phase == phase and call.when == "before" and not fired:
            fired.append(True)
            # Cancellation is a durable request from another caller, not re-entrant land.
            ml.cancel(test.f.project, test.f.private, ticket.ticket)
    report = test.f.land(batch, hook=hook)
    test.assertTrue(fired)
    test.assertIn(report.state, ("cancelled", "reverted", "waiting_owner"))
    test.assertEqual(test.f.before, tree(test.f.project))
    second = test.f.batch({"second": b"second"}, "second")
    test.assertEqual("waiting_owner", test.f.land(second).state)
    ml.accept(test.f.project, test.f.private, report.batch_no)
    test.assertEqual("landed", test.f.land(second).state)


cases("ml_17_cancel", "ML-17", [(n, n) for n in
      ("intake", "history", "staging", "forward", "validation")], cancel_phase)


@contextlib.contextmanager
def disk_image(root, filesystem, *, mountpoint=None):
    """Actual macOS image, image file and mount point both under the fixture temp dir."""
    image = root / (filesystem.replace("+", "plus") + ".dmg")
    mount = mountpoint or root / "mounted"
    mount.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "48m", "-fs", filesystem,
                    "-volname", "ML_THROWAWAY", str(image)], check=True, timeout=15,
                   capture_output=True)
    subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(mount), str(image)],
                   check=True, timeout=15, capture_output=True)
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], check=True, timeout=15,
                       capture_output=True)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("COCOLO_ML_REAL_IMAGES") == "1",
                     "opt-in temporary FAT/HFS+/full-image profile; no portable image-mount permission assumed")
class RealImages(unittest.TestCase):
    def test_ml_4_FAT_HFS_images(self):
        """ML-4: real FAT/HFS+ probes, plus cross-drive staging/history/target rejection."""
        outer = Fixture()
        self.addCleanup(outer.close)
        for filesystem in ("MS-DOS FAT32", "HFS+"):
            with self.subTest(filesystem=filesystem), disk_image(outer.root, filesystem) as mount:
                f = Fixture(parent=mount)
                try:
                    batch = f.batch({"a.txt": b"child"})
                    before = tree(f.project)
                    report = f.land(batch)
                    self.assertEqual("refused", report.state)
                    self.assertEqual(before, tree(f.project))
                    # Separate physical cross-device case: private history outside image.
                    report = ml.land(f.project, outer.private, batch, policy=f.policy)
                    self.assertEqual("refused", report.state)
                finally:
                    f.close()

    def test_ml_4_actual_mounted_target_and_staging(self):
        """ML-4: APFS mounted subfolder target and actual cross-drive child staging."""
        for kind in ("target", "staging"):
            f = Fixture()
            try:
                target = f.project / ("mounted" if kind == "target" else ".cocolo/staging/child-1")
                with disk_image(f.root, "APFS", mountpoint=target):
                    name = "mounted/add" if kind == "target" else "a.txt"
                    batch = f.batch({name: b"child"})
                    before = tree(f.project)
                    report = f.land(batch)
                    self.assertEqual("refused", report.state)
                    self.assertIn("device", report.reason)
                    self.assertEqual(before, tree(f.project))
            finally:
                f.close()

    def test_ml_12_really_full_image(self):
        """ML-12: real ENOSPC on temporary APFS image, restore/retry after freeing space."""
        outer = Fixture()
        self.addCleanup(outer.close)
        with disk_image(outer.root, "APFS") as mount:
            f = Fixture(parent=mount)
            try:
                batch = f.batch({"a.txt": b"child"})
                seed_applied(f, batch)
                fill = mount / "fill"
                with fill.open("wb", buffering=0) as out:
                    with self.assertRaises(OSError) as caught:
                        while True:
                            out.write(b"X" * (1024 * 1024))
                    self.assertEqual(errno.ENOSPC, caught.exception.errno)
                reports = ml.recover(f.project, f.private)
                self.assertIn(reports[-1].state, ("reverted", "needs_owner"))
                if reports[-1].state == "needs_owner":
                    self.assertEqual("io", reports[-1].reason)
                self.assertTrue(all(r.contents_kept for r in ml.history(f.project, f.private)))
                fill.unlink()
                ml.retry(f.project, f.private, 1, policy=f.policy)
                self.assertEqual(f.before, tree(f.project))
            finally:
                f.close()


declare_ml_tests(globals())

if __name__ == "__main__":
    unittest.main()


# The original methods remain the source of truth, including soak users.
# Discovery partitions them by fault area; no assertion or boundary is removed.
def campaign_area(name):
    if name.startswith('test_ml_10_edit_'):
        return 'owners'
    if name in {'test_ml_11_kill_' + kind for kind in ('forward', 'reverse', 'undo', 'recovery', 'cleanup')}:
        return 'kills'
    if name == 'test_ml_12_fault_sweep_forward':
        return 'forward_faults'
    if name in {'test_ml_12_fault_sweep_' + kind for kind in ('reverse', 'recovery', 'cleanup')}:
        return 'recovery_faults'
    return 'adversarial'


def load_tests(loader, tests, pattern):
    selected = unittest.TestSuite(Adversarial(name) for name in loader.getTestCaseNames(Adversarial)
                                  if campaign_area(name) == 'adversarial')
    selected.addTests(loader.loadTestsFromTestCase(RealImages))
    return selected
