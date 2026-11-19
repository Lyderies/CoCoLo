"""F3: the owner's backup script writes the streamed format-3 backup, for any installed schema.

Release 0.3.1 made the ledger's own backups stream into a private SQLite file
(format 3), but `scripts/backup_installed_project.py`, which the owner runs
against whatever runtime a project has installed, still built one JSON document
in memory and refused a project over 128 MiB. These tests hold it to PLAN F3-1
to F3-5: this clone's project_backup writes and checks the file in a separate
isolated Python, and the installed runtime's ledger registers it.
"""

import os
import runpy
import stat
import subprocess
import sys
import tracemalloc
import unittest
from unittest import mock

from tests import test_learning_source_image_legacy as legacy
from tests.support import REPOSITORY_ROOT
from tests.test_account_launch_claims import downgrade_ledger_to_six
from tests.test_large_project_backups import BOUND, add_events
from codex_agents import learning_store, project_backup

SCRIPT = REPOSITORY_ROOT / "scripts" / "backup_installed_project.py"
TOOL = runpy.run_path(str(SCRIPT))
RECORDS = 16_000  # About 11 MB of records (test_large_project_backups).


class OwnerBackupStreamedTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def backup(self, file_id="owner_backup", schema_version=None):
        schema = int(self.source._connection.execute("PRAGMA user_version").fetchone()[0])
        return TOOL["backup_partition"](self.source, self.project, file_id,
                                        schema_version=schema if schema_version is None else schema_version,
                                        expires_at_ms=self.source._clock_ms() + 3_600_000)

    def partition_files(self):
        folder = self.source.state.backups / self.project
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def registered(self, file_id):
        return self.source._connection.execute(
            "SELECT relative_path,content_digest FROM stored_files WHERE project_id=? AND file_id=? AND kind='backup'",
            (self.project, file_id)).fetchone()

    def test_F3_1_the_backup_is_a_registered_format_three_copy_equal_to_the_records(self):
        report = self.backup()
        path = self.source.state.backups / self.project / "owner_backup.sqlite3"
        self.assertEqual(str(path), report["path"])
        self.assertEqual(3, report["format"])
        info = os.stat(path, follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(["owner_backup.sqlite3"], self.partition_files())  # no partial file left
        row = self.registered("owner_backup")
        self.assertEqual(report["backup_sha256"], row["content_digest"])
        # F3-4: the runtime it was made for accepts it, and it equals the live records row by row.
        verified = self.source.verify_sanitized_backup(self.project, file_id="owner_backup")
        self.assertEqual(report["backup_sha256"], verified["backup_sha256"])
        self.assertEqual(report["manifest_sha256"], verified["manifest_sha256"])
        self.assertEqual(report["counts"], verified["counts"])
        project_backup.verify(path, project_id=self.project, compare=self.source._connection,
                              exclude_file_id="owner_backup")

    def test_F3_1_an_earlier_schema_project_is_streamed_without_holding_it_in_memory(self):
        size = add_events(self.source, self.project, RECORDS)
        downgrade_ledger_to_six(self.source)
        tracemalloc.start()
        try:
            report = self.backup("schema_six")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(6, report["ledger_schema_version"])
        path = self.source.state.backups / self.project / "schema_six.sqlite3"
        self.assertGreater(path.stat().st_size, size)
        self.assertLess(peak, BOUND, f"the script held {peak:,} bytes for {size:,} bytes of records")
        project_backup.verify(path, project_id=self.project, schema_version=6, compare=self.source._connection,
                              exclude_file_id="schema_six")

    def test_F3_2_the_copy_is_written_by_this_clones_package_in_an_isolated_python(self):
        launched = []
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            launched.append(list(argv))
            return real_run(argv, *args, **kwargs)
        with mock.patch.object(subprocess, "run", run):
            self.backup()
        [argv] = launched
        self.assertEqual([sys.executable, "-I", "-B", "-S", str(SCRIPT), "--stream-child"], argv[:6])

    def test_F3_3_a_partition_over_the_limit_is_refused_and_nothing_is_left(self):
        add_events(self.source, self.project, 2_000)
        with mock.patch.dict(TOOL, {"BACKUP_LIMIT_BYTES": 256 * 1024}), self.assertRaises(RuntimeError):
            self.backup("too_big")
        self.assertEqual([], self.partition_files())
        self.assertIsNone(self.registered("too_big"))

    def test_F3_3_a_copy_that_fails_its_checks_is_never_registered(self):
        real_run = subprocess.run

        def broken(argv, *args, **kwargs):
            done = real_run(argv, *args, **kwargs)
            partial = self.source.state.backups / self.project / ".checked_badly.partial"
            with open(partial, "r+b") as handle:  # the copy changes after the child checked it
                handle.seek(200)
                handle.write(b"\0" * 16)
            return done
        with mock.patch.object(subprocess, "run", broken), self.assertRaises(RuntimeError):
            self.backup("checked_badly")
        self.assertEqual([], self.partition_files())
        self.assertIsNone(self.registered("checked_badly"))

    def test_F3_3_an_existing_backup_of_that_name_is_refused_and_kept(self):
        first = self.backup("same_name")
        with self.assertRaises(RuntimeError):
            self.backup("same_name")
        self.assertEqual(["same_name.sqlite3"], self.partition_files())
        self.assertEqual(first["backup_sha256"], self.registered("same_name")["content_digest"])

    def test_F3_5_the_report_keeps_its_fields_and_adds_the_format(self):
        report = self.backup()
        self.assertEqual({"ready", "project_id", "file_id", "path", "format", "ledger_schema_version",
                          "backup_sha256", "manifest_sha256", "counts", "isolated_restore_integrity",
                          "isolated_restore_foreign_keys"}, set(report))
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, report["ledger_schema_version"])
        self.assertEqual(("ok", "ok"), (report["isolated_restore_integrity"], report["isolated_restore_foreign_keys"]))


if __name__ == "__main__":
    unittest.main()
