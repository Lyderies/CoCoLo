"""Report completeness can detect changes, but cannot grant undo authority."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from test_install import installer


class FolderReportTests(unittest.TestCase):
    def test_a_report_cannot_omit_a_new_snapshotted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            relative = ".claude/agents/item.md"
            snapshot = installer._snapshot(target, "claude", {"changed_paths": [relative]})
            path = target / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(b"package bytes\n")
            reply = {"writes": [{"path": relative, "before": None,
                                  "after": hashlib.sha256(path.read_bytes()).hexdigest(),
                                  "mode": path.stat().st_mode & 0o7777}],
                     "folders_created": []}
            journal = installer._journal_from("claude", snapshot, reply)
            with self.assertRaisesRegex(installer.InstallRefused, "no_write_report"):
                installer._check_journal(target, "claude", snapshot, journal)
            self.assertIn(".claude/agents", journal["left"])
            ok, left = installer._undo(target, "claude", snapshot, journal)
            self.assertFalse(ok)
            self.assertIn(".claude/agents", left)
            self.assertTrue(path.parent.is_dir())
            self.assertFalse(path.exists())  # a checked file remains independently undoable
