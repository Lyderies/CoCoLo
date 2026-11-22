"""Passing negative controls for the ML harness, without library implementation."""
from __future__ import annotations

import errno
import os
import stat
import unittest
from dataclasses import replace

from tests.support import TEMP_ROOT  # noqa: F401 -- native package bootstrap
from codex_agents import merge_library as ml
from tests.merge_library_support import (
    declare_ml_tests,
    Fixture, ReplayPoint, assert_no_git_writes, assert_trace, assert_commit_flushes, durable_endpoint,
    outcome_oracle as _outcome, owner_preservation_oracle, put, replay_key, report_oracle, tree,
    FAULTABLE, fault_outcome_oracle as _fault_outcome, location_refusal_oracle,
)


def outcome_oracle(test, f, batch, report, rows, kind):
    return _outcome(test, f, batch, report, rows, kind, read_history=lambda: rows)


def fault_outcome_oracle(test, f, batch, report, rows, kind, call, fault):
    return _fault_outcome(test, f, batch, report, rows, kind, call, fault, read_history=lambda: rows)


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

    def test_H1_write_opens_require_staged_file_and_parent_flushes(self):
        staged = str(self.f.project / ".cocolo/staging/c/e")
        for flags in (os.O_CREAT | os.O_TRUNC | os.O_WRONLY, os.O_CREAT,
                      os.O_TRUNC, os.O_WRONLY, os.O_RDWR, os.O_APPEND, 0):
            with self.subTest(flags=flags):
                valid = [replace(c, operation="open", detail={"scope": "staging", "flags": flags,
                                                               "mutating": flags == 0})
                         if c.operation == "write" and c.path == staged else c for c in self.trace()]
                assert_trace(self, valid)
                with self.assertRaisesRegex(AssertionError, "missing fresh full flush"):
                    assert_trace(self, [c for c in valid
                                        if not (c.operation == "fullfsync" and c.path == staged)])
                # Isolate the open: no exchange can independently create its parent obligation.
                isolated = [c for c in valid if c.operation != "exchange"]
                assert_commit_flushes(self, isolated)
                with self.assertRaisesRegex(AssertionError, "missing fresh full flush"):
                    assert_commit_flushes(self, [c for c in isolated if not (
                        c.operation == "fullfsync" and c.path == str(self.f.project / ".cocolo/staging/c"))])

    def test_ML19_all_commit_labels_require_fresh_flush_and_own_history_write(self):
        history = str(self.f.private / "history.sqlite")
        for label in ("queue", "retire", "undo", "reverse"):
            with self.subTest(commit=label):
                suffix = [ml.IOCall(op, side, "retirement" if label == "retire" else label,
                                   history if op != "sqlite_commit" else None,
                                   detail={"scope": "history", "commit": label})
                          for op in ("sqlite_write", "fullfsync", "sqlite_commit")
                          for side in ("before", "after")]
                # assert_trace retains exactly A/B counting, while the sibling also
                # handles standalone undo/reverse traces with their own commit labels.
                check = assert_trace if label in ("queue", "retire") else assert_commit_flushes
                prefix = self.trace()
                valid = prefix + suffix
                check(self, valid)
                with self.assertRaisesRegex(AssertionError, "missing fresh full flush"):
                    check(self, prefix + [c for c in suffix if c.operation != "fullfsync"])
                with self.assertRaisesRegex(AssertionError, "each commit must expose its own history writes"):
                    check(self, prefix + [c for c in suffix if c.operation != "sqlite_write"])
                if label in ("undo", "reverse"):
                    assert_commit_flushes(self, suffix)
                    with self.assertRaisesRegex(AssertionError, "missing fresh full flush"):
                        assert_commit_flushes(self, [c for c in suffix if c.operation != "fullfsync"])

    def test_ML12_rejects_swallowed_IO_failures_even_with_landed_tree_and_row(self):
        put(self.f.project / "a.txt", b"CHILD")
        report = self.report("landed")
        rows = (ml.HistoryRow(1, self.batch, "landed"),)
        outcome_oracle(self, self.f, self.batch, report, rows, "forward")
        for operation in FAULTABLE - {"sqlite_commit"}:
            for side in ("before", "after"):
                for fault in (errno.ENOSPC, errno.EIO):
                    call = ml.IOCall(operation, side, "forward")
                    with self.subTest(operation=operation, side=side, fault=fault), self.assertRaises(AssertionError):
                        fault_outcome_oracle(self, self.f, self.batch, report, rows, "forward", call, fault)
        for call, fault in ((ml.IOCall("write", "before", "staging"), "short"),
                            (ml.IOCall("sqlite_commit", "after", "forward"), errno.EIO)):
            fault_outcome_oracle(self, self.f, self.batch, report, rows, "forward", call, fault)
        with self.assertRaises(AssertionError):
            fault_outcome_oracle(self, self.f, self.batch, report, (), "forward",
                                 ml.IOCall("sqlite_commit", "after", "forward"), errno.EIO)

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
        self.assertEqual(self.f.before, expected)
        put(self.f.project / "a.txt", b"CHILD")
        undone_report = replace(self.report("landed"), batch_no=2)
        with self.assertRaises(AssertionError):
            outcome_oracle(self, self.f, self.batch, undone_report, committed, "undoing")
        put(self.f.project / "a.txt", self.batch.events[0].base.data)
        outcome_oracle(self, self.f, self.batch, replace(undone_report, paths=(ml.PathReport("a.txt", "base"),)),
                       committed, "undoing")
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
        assert_no_git_writes(self, self.f, [
            ml.IOCall("write", "before", "staging", ".cocolo/staging/c/e"),
            ml.IOCall("rename_noreplace", "before", "forward", ".cocolo/staging/c/e", "a.txt"),
            ml.IOCall("open", "before", "forward", "a.txt", detail={"flags": os.O_WRONLY}),
            ml.IOCall("open", "before", "intake", ".git/config", detail={"flags": os.O_RDONLY}),
        ])
        for call in (ml.IOCall("rename_noreplace", "before", "forward", "a.txt", ".git"),
                     ml.IOCall("write", "before", "forward", ".git/../.git/config"),
                     ml.IOCall("open", "before", "forward", ".git/config", detail={"mutating": True})):
            with self.subTest(call=call), self.assertRaises(AssertionError):
                assert_no_git_writes(self, self.f, [call])

    def test_ML30_location_oracle_rejects_otherwise_valid_forgery_landing(self):
        forged = replace(self.batch, approval="child invented approval")
        editable = self.f.project / ".cocolo"
        editable.mkdir()
        self.f.write_records(editable, forged)
        self.assertTrue(self.f.verify_records(editable, forged))
        before = tree(self.f.project)
        location_refusal_oracle(self, ml.Report("refused"), before, before)
        # Negative control for removing just location refusal: a verifier trusting
        # these child-controlled authority records admits this exact forged batch.
        def without_location_check(batch):
            self.assertTrue(self.f.verify_records(editable, batch))
            return ml.Report("landed", recorded=True, batch_no=1)
        landed = without_location_check(forged)
        self.assertEqual("landed", landed.state)
        with self.assertRaises(AssertionError):
            location_refusal_oracle(self, landed, before, before)


declare_ml_tests(globals())

if __name__ == "__main__":
    unittest.main()
