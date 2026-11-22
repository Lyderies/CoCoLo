"""Passing negative controls for the ML harness, without library implementation."""
from __future__ import annotations

import stat
import unittest
from dataclasses import replace

from tests.support import TEMP_ROOT  # noqa: F401 -- native package bootstrap
from codex_agents import merge_library as ml
from tests.merge_library_support import (
    Fixture, ReplayPoint, assert_no_git_writes, assert_trace, durable_endpoint,
    outcome_oracle, owner_preservation_oracle, put, replay_key, report_oracle, tree,
)


class HarnessOracles(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.batch = self.f.batch({"a.txt": b"CHILD"})

    def report(self, state, *, classification="new", retained=()):
        return ml.Report(state, batch_no=1, recorded=True,
                         paths=(ml.PathReport("a.txt", classification, retained),))

    def trace(self):
        calls = []
        def pair(op, path=None, other=None, **detail):
            for side in ("before", "after"):
                calls.append(ml.IOCall(op, side, "forward", path, other, detail,
                                       True if op == "project_checks" else None))
        history = str(self.f.private / "history.sqlite")
        target, staged = str(self.f.project / "a.txt"), str(self.f.project / ".cocolo/staging/c/e")
        pair("sqlite_write", history, scope="history")
        pair("fullfsync", history, scope="history")
        pair("sqlite_commit", commit="A", parents=(), full_flush=True)
        pair("write", staged, scope="staging")
        pair("fullfsync", staged, scope="staging")
        pair("exchange", target, staged, scope="tree")
        pair("fullfsync", str(self.f.project))
        pair("fullfsync", str(self.f.project / ".cocolo/staging/c"))
        pair("sqlite_write", history, scope="history")
        pair("fullfsync", history, scope="history")
        pair("project_checks")
        pair("sqlite_commit", commit="B", parents=(), full_flush=True)
        return calls

    def test_H1_rejects_missing_either_parent_and_stale_history_flush(self):
        valid = self.trace()
        assert_trace(self, valid)
        paths = (str(self.f.project), str(self.f.project / ".cocolo/staging/c"))
        for path in paths:
            with self.subTest(parent=path), self.assertRaises(AssertionError):
                assert_trace(self, [c for c in valid if not (c.operation == "fullfsync" and c.path == path)])
        last_flush = max(i for i, c in enumerate(valid) if c.operation == "fullfsync")
        stale = [c for i, c in enumerate(valid) if i not in (last_flush - 1, last_flush)]
        with self.assertRaises(AssertionError):
            assert_trace(self, stale)

    def test_H2_rejects_reverted_new_tree_and_landed_base_tree(self):
        put(self.f.project / "a.txt", b"CHILD")
        with self.assertRaises(AssertionError):
            outcome_oracle(self, self.f, self.batch, self.report("reverted"),
                           (ml.HistoryRow(1, self.batch, "reverted"),), "forward")
        put(self.f.project / "a.txt", self.batch.events[0].base.data)
        with self.assertRaises(AssertionError):
            outcome_oracle(self, self.f, self.batch, self.report("landed", classification="base"),
                           (ml.HistoryRow(1, self.batch, "landed"),), "forward")
        with self.assertRaises(AssertionError):
            outcome_oracle(self, self.f, self.batch, self.report("landed", classification="base"), (), "forward")

    def test_H2_reverse_and_undo_use_operation_specific_durable_direction(self):
        forward = ml.HistoryRow(1, self.batch, "landed")
        applying_undo = ml.HistoryRow(2, self.batch, "applying", kind="undo", undoes=1)
        self.assertEqual(self.f.expected(self.batch),
                         durable_endpoint(self.f, self.batch, (forward, applying_undo), "undoing"))
        committed = (replace(forward, state="undone"), replace(applying_undo, state="landed"))
        expected = durable_endpoint(self.f, self.batch, committed, "undoing")
        put(self.f.project / "a.txt", b"CHILD")
        with self.assertRaises(AssertionError):
            self.assertEqual(expected, tree(self.f.project))
        self.assertEqual(self.f.before, durable_endpoint(self.f, self.batch, (forward,), "reverse"))

    def test_H3_rejects_owner_somewhere_with_unrelated_damage_or_missing_child(self):
        put(self.f.project / "a.txt", b"OWNER")
        owner = tree(self.f.project)
        self.f.owner_versions["a.txt"] = {(stat.S_IFREG, 0o644, b"OWNER")}
        saved = self.f.project / ".cocolo/staging/c/owner"
        put(saved, b"OWNER")
        report = self.report("needs_owner", classification="owner", retained=(str(saved),))
        with self.assertRaises(AssertionError):
            owner_preservation_oracle(self, self.f, self.batch, (report,), owner)
        put(saved, b"CHILD")
        owner_preservation_oracle(self, self.f, self.batch, (report,), owner)
        put(self.f.project / "z.txt", b"unrelated damage")
        with self.assertRaises(AssertionError):
            owner_preservation_oracle(self, self.f, self.batch, (report,), owner)

    def test_M1_rejects_different_operation_phase_side_paths_or_occurrence(self):
        original = ml.IOCall("exchange", "before", "compensation", str(self.f.project / "a.txt"),
                             str(self.f.project / ".cocolo/staging/c/e"))
        key = replay_key([original], 0, self.f)
        for changed in (replace(original, operation="unlink"), replace(original, phase="forward"),
                        replace(original, when="after"), replace(original, path=str(self.f.project / "z.txt")),
                        replace(original, other=str(self.f.project / "other"))):
            point = ReplayPoint(key, self.f, lambda _: None)
            point(0, changed)
            with self.assertRaises(AssertionError):
                point.assert_fired(self)
        point = ReplayPoint((key[0], 2), self.f, lambda _: None)
        point(0, original)
        with self.assertRaises(AssertionError):
            point.assert_fired(self)
        point(9, original)  # unrelated inserted calls do not shift the selected occurrence
        point.assert_fired(self)

    def test_M2_rejects_retained_wrong_bytes_mode_and_false_moved_identity(self):
        put(self.f.project / "a.txt", b"CHILD")
        saved = self.f.project / ".cocolo/staging/c/e"
        put(saved, b"corrupted")
        report = self.report("landed", retained=(str(saved),))
        with self.assertRaises(AssertionError):
            report_oracle(self, self.f, report, self.batch)
        put(saved, b"CHILD", 0o600)
        with self.assertRaises(AssertionError):
            report_oracle(self, self.f, report, self.batch)
        put(saved, b"CHILD")
        report_oracle(self, self.f, report, self.batch)
        destination = self.f.outside / "moved"
        (self.f.project / "a.txt").rename(destination)
        moved_report = self.report("needs_owner", classification="moved", retained=(str(saved),))
        info = destination.stat()
        with self.assertRaises(AssertionError):
            report_oracle(self, self.f, moved_report, self.batch,
                          moved={"a.txt": (destination, (info.st_dev, info.st_ino + 1))})
        report_oracle(self, self.f, moved_report, self.batch,
                      moved={"a.txt": (destination, (info.st_dev, info.st_ino))})

    def test_M6_rejects_git_directory_destination_and_relative_write(self):
        for call in (ml.IOCall("rename_noreplace", "before", "forward", "a.txt", ".git"),
                     ml.IOCall("write", "before", "forward", ".git/../.git/config"),
                     ml.IOCall("open", "before", "forward", ".git/config", detail={"mutating": True})):
            with self.subTest(call=call), self.assertRaises(AssertionError):
                assert_no_git_writes(self, self.f, [call])


if __name__ == "__main__":
    unittest.main()
