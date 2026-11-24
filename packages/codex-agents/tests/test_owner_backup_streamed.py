"""F3: the owner's backup script writes the streamed format-3 backup, for any installed schema.

Release 0.3.1 made the ledger's own backups stream into a private SQLite file
(format 3), but `scripts/backup_installed_project.py`, which the owner runs
against whatever runtime a project has installed, still built one JSON document
in memory and refused a project over 128 MiB. These tests hold it to PLAN F3-1
to F3-5: this clone's project_backup writes and checks the file in a separate
isolated Python, and the installed runtime's ledger registers it.
"""

import json
import os
import runpy
import signal
import stat
import subprocess
import sys
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_source_image_legacy as legacy
from tests.support import REPOSITORY_ROOT
from tests.test_account_launch_claims import downgrade_ledger_to_six
from tests.test_large_project_backups import BOUND, add_events
from codex_agents import learning_store, project_backup

SCRIPT = REPOSITORY_ROOT / "scripts" / "backup_installed_project.py"
TOOL = runpy.run_path(str(SCRIPT))
# The script's own globals (run_path returns a copy), for patching what its functions read.
SCRIPT_GLOBALS = TOOL["backup_partition"].__globals__
RECORDS = 16_000  # About 11 MB of records (test_large_project_backups).


def child_runs(launched=None, after=None, rewrite=None, started=None):
    """Patch the script's child launch: record each argv, act once the child has answered (`after`),
    rewrite its report (`rewrite`), or act just after it has started (`started`, given the process)."""
    real = subprocess.Popen

    class Watched(real):
        def __init__(self, argv, *args, **kwargs):
            if launched is not None:
                launched.append(list(argv))
            super().__init__(argv, *args, **kwargs)
            if started is not None:
                started(self)

        def communicate(self, *args, **kwargs):
            out, err = super().communicate(*args, **kwargs)
            if after is not None:
                after()
            return (rewrite(out) if rewrite is not None else out), err
    return mock.patch.object(subprocess, "Popen", Watched)


class OwnerBackupStreamedTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def owner_backup(self, file_id="owner_backup", schema_version=None):
        if not hasattr(self, "earlier_files"):  # the fixture keeps backups of its own there
            self.earlier_files = self.partition_files(all_files=True)
        schema = int(self.source._connection.execute("PRAGMA user_version").fetchone()[0])
        return TOOL["backup_partition"](self.source, self.project, file_id,
                                        schema_version=schema if schema_version is None else schema_version,
                                        expires_at_ms=self.source._clock_ms() + 3_600_000)

    def partition_files(self, all_files=False):
        """The files this test's backups left in the project's backup folder."""
        folder = self.source.state.backups / self.project
        names = sorted(path.name for path in folder.iterdir()) if folder.exists() else []
        return names if all_files else [name for name in names if name not in self.earlier_files]

    def registered(self, file_id):
        return self.source._connection.execute(
            "SELECT relative_path,content_digest FROM stored_files WHERE project_id=? AND file_id=? AND kind='backup'",
            (self.project, file_id)).fetchone()

    def test_F3_1_the_backup_is_a_registered_format_three_copy_equal_to_the_records(self):
        report = self.owner_backup()
        path = self.source.state.backups / self.project / "owner_backup.sqlite3"
        self.assertEqual(path.name, report["backup_name"])
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
            report = self.owner_backup("schema_six")
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
        with child_runs(launched=launched):
            self.owner_backup()
        [argv] = launched
        self.assertEqual([sys.executable, "-I", "-B", "-S", str(SCRIPT), "--stream-child"], argv[:6])

    def test_F3_3_a_partition_over_the_limit_is_refused_and_nothing_is_left(self):
        add_events(self.source, self.project, 2_000)
        with mock.patch.dict(SCRIPT_GLOBALS, {"BACKUP_LIMIT_BYTES": 256 * 1024}), self.assertRaises(RuntimeError):
            self.owner_backup("too_big")
        self.assertEqual([], self.partition_files())
        self.assertIsNone(self.registered("too_big"))

    def test_F3_3_a_copy_that_fails_its_checks_is_never_registered(self):
        def change():
            partial = self.source.state.backups / self.project / ".checked_badly.partial"
            with open(partial, "r+b") as handle:  # the copy changes after the child checked it
                handle.seek(0, os.SEEK_END)
                handle.write(b"\xff" * 16)
        with child_runs(after=change), self.assertRaises(RuntimeError):
            self.owner_backup("checked_badly")
        self.assertEqual([], self.partition_files())
        self.assertIsNone(self.registered("checked_badly"))

    def test_F3_3_an_existing_backup_of_that_name_is_refused_and_kept(self):
        first = self.owner_backup("same_name")
        with self.assertRaises(RuntimeError):
            self.owner_backup("same_name")
        self.assertEqual(["same_name.sqlite3"], self.partition_files())
        self.assertEqual(first["backup_sha256"], self.registered("same_name")["content_digest"])

    def test_F3_5_the_report_keeps_its_fields_and_adds_the_format(self):
        report = self.owner_backup()
        self.assertEqual({"ready", "project_id", "file_id", "backup_name", "format", "ledger_schema_version",
                          "backup_sha256", "manifest_sha256", "counts", "isolated_restore_integrity",
                          "isolated_restore_foreign_keys"}, set(report))
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, report["ledger_schema_version"])
        self.assertEqual(("ok", "ok"), (report["isolated_restore_integrity"], report["isolated_restore_foreign_keys"]))


    # Review 040-f3-owner-backup ------------------------------------------------------------------

    def assert_nothing_left(self, file_id):
        self.assertEqual([], self.partition_files())
        self.assertIsNone(self.registered(file_id))

    def test_F3_1_the_childs_memory_does_not_grow_with_the_partition(self):
        reports = []

        def keep(out):
            reports.append(json.loads(out))
            return out
        with child_runs(rewrite=keep):
            self.owner_backup("small")
        add_events(self.source, self.project, 4 * RECORDS)  # about 45 MB more
        with child_runs(rewrite=keep):
            self.owner_backup("large")
        small, large = (report["peak_rss_bytes"] for report in reports)
        self.assertLess(large - small, 16 * 1_048_576, f"the child grew from {small:,} to {large:,} bytes")

    def test_F3_1_every_schema_layout_from_5_to_14_is_written_checked_and_registered(self):
        from tests.test_account_launch_claims import drop_account_cleanup_for_legacy_fixture
        connection = self.source._connection
        for version in range(14, 4, -1):
            with self.subTest(version=version):
                # The layouts schema 10 and 9 left (drop_original_open_for_legacy_fixture, applied in steps).
                if version == 10:
                    drop_account_cleanup_for_legacy_fixture(self.source)
                if version == 9:
                    connection.execute("DROP TABLE account_open_requests")
                    connection.execute("DROP TABLE account_open_receipts")
                    connection.execute("ALTER TABLE host_run_bindings DROP COLUMN history_owner")
                report = self.owner_backup(f"schema_{version}", schema_version=version)
                self.assertEqual(version, report["ledger_schema_version"])
                path = self.source.state.backups / self.project / f"schema_{version}.sqlite3"
                project_backup.verify(path, project_id=self.project, schema_version=version,
                                      compare=self.source._connection, exclude_file_id=f"schema_{version}")

    def test_F3_1_the_limit_is_the_ledgers_backup_limit(self):
        launched = []
        with child_runs(launched=launched):
            self.owner_backup()
        self.assertIn(str(learning_store.MAX_PROJECT_BACKUP_BYTES), launched[0])

    def test_F3_3_registration_records_the_digest_the_expiry_and_the_place(self):
        expires = self.source._clock_ms() + 3_600_000
        report = TOOL["backup_partition"](self.source, self.project, "placed", schema_version=14,
                                          expires_at_ms=expires)
        row = self.source._connection.execute(
            "SELECT relative_path,content_digest,expires_at_ms FROM stored_files WHERE project_id=? AND file_id=?",
            (self.project, "placed")).fetchone()
        self.assertEqual((f"backups/{self.project}/placed.sqlite3", report["backup_sha256"], expires), tuple(row))

    def test_F3_3a_a_copy_replaced_by_another_file_after_its_check_is_never_registered(self):
        def replace():
            partial = self.source.state.backups / self.project / ".swapped.partial"
            twin = partial.with_name(".twin")
            twin.write_bytes(partial.read_bytes())  # same bytes, another file
            twin.chmod(0o600)
            os.replace(twin, partial)
        with child_runs(after=replace), self.assertRaisesRegex(RuntimeError, "not the file"):
            self.owner_backup("swapped")
        # Nothing published or registered; the replacement is not this run's file, so it is left alone
        # (the next backup of that name removes a stale partial).
        self.assertNotIn("swapped.sqlite3", self.partition_files())
        self.assertIsNone(self.registered("swapped"))

    def test_F3_3b_a_linked_folder_on_the_way_is_refused_and_nothing_is_written_there(self):
        backups = self.source.state.backups
        moved = backups.with_name("backups-real")
        os.replace(backups, moved)
        backups.symlink_to(moved)
        try:
            with self.assertRaisesRegex(RuntimeError, "link"):
                self.owner_backup("linked")
            written = [p.name for p in moved.rglob("*")]  # the real folder, behind the link
        finally:
            backups.unlink()
            os.replace(moved, backups)
        self.assertNotIn("linked.sqlite3", written)
        self.assertNotIn(".linked.partial", written)
        self.assert_nothing_left("linked")

    def test_F3_3b_a_final_file_that_appears_before_publication_is_kept_and_refused(self):
        final = self.source.state.backups / self.project / "raced.sqlite3"

        def appear():
            final.write_bytes(b"someone else's file")
        with child_runs(after=appear), self.assertRaises(RuntimeError):
            self.owner_backup("raced")
        self.assertEqual(b"someone else's file", final.read_bytes())
        self.assertNotIn(".raced.partial", self.partition_files())
        self.assertIsNone(self.registered("raced"))

    def test_F3_3c_a_failed_folder_sync_after_publication_leaves_nothing(self):
        real_fsync = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("folder sync failed")
            return real_fsync(descriptor)
        with mock.patch.object(os, "fsync", fsync), self.assertRaises(OSError):
            self.owner_backup("unsynced")
        self.assert_nothing_left("unsynced")

    def test_F3_3c_a_registration_failure_leaves_nothing(self):
        with mock.patch.object(self.source, "_register_stored_file",
                               side_effect=learning_store.LedgerError("registry refused")), \
                self.assertRaises(learning_store.LedgerError):
            self.owner_backup("unregistered")
        self.assert_nothing_left("unregistered")

    def test_F3_3c_an_incomplete_child_report_leaves_nothing(self):
        for missing in ("backup_sha256", "manifest_sha256", "counts", "identity"):
            with self.subTest(missing=missing):
                def drop(out):
                    report = json.loads(out)
                    report.pop(missing)
                    return json.dumps(report)
                with child_runs(rewrite=drop), self.assertRaises(RuntimeError):
                    self.owner_backup(f"without_{missing}")
                self.assert_nothing_left(f"without_{missing}")

    def test_F3_3d_a_termination_signal_stops_the_child_and_leaves_nothing(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=number.name):
                children = []

                def signal_now(process):
                    children.append(process.pid)
                    os.kill(os.getpid(), number)
                before = signal.getsignal(number)
                with child_runs(started=signal_now), self.assertRaisesRegex(RuntimeError, "signal"):
                    self.owner_backup(f"signalled_{number.name}")
                with self.assertRaises(ProcessLookupError):
                    os.kill(children[0], 0)  # stopped and reaped
                self.assertEqual(before, signal.getsignal(number))
                self.assert_nothing_left(f"signalled_{number.name}")

    def test_F3_3e_a_failed_quiesce_reports_the_backup_made_and_the_collector_not_stopped(self):
        report = self.owner_backup("kept")

        def quiesce():
            raise RuntimeError("authorization unreadable")
        final, code = TOOL["finish_backup"](report, leave_quiesced=True, quiesce=quiesce)
        self.assertEqual(3, code)
        self.assertTrue(final["ready"])
        self.assertFalse(final["collector_quiesced"])
        self.assertEqual("the backup could not be completed (RuntimeError)", final["quiesce_error"])
        self.assertIsNotNone(self.registered("kept"))
        final, code = TOOL["finish_backup"](report, leave_quiesced=True, quiesce=lambda: None)
        self.assertEqual((0, True), (code, final["collector_quiesced"]))
        final, code = TOOL["finish_backup"](report, leave_quiesced=False, quiesce=quiesce)
        self.assertEqual((0, False), (code, final["collector_quiesced"]))


    # Review 040-f3-owner-backup, verifier (F3-6 to F3-9) --------------------------------------------

    def test_F3_6_columns_the_child_wrote_must_equal_the_installed_schemas(self):
        def drop_a_column(out):
            report = json.loads(out)
            report["columns"]["records"] = report["columns"]["records"][:-1]
            return json.dumps(report)
        with child_runs(rewrite=drop_a_column), self.assertRaisesRegex(RuntimeError, "columns"):
            self.owner_backup("drifted")
        self.assert_nothing_left("drifted")

    def refused_inventory(self, file_id, pattern):
        before = self.partition_files(all_files=True)
        with self.assertRaisesRegex(RuntimeError, pattern):
            self.owner_backup(file_id)
        self.assertEqual(before, self.partition_files(all_files=True))

    def test_F3_18_an_unknown_table_a_renamed_local_table_or_an_extra_column_is_refused(self):
        """0.8.60 fix round (decision 247): the whole table inventory and each local table's columns."""
        db = self.source._connection
        self.owner_backup("fresh")  # a fresh ledger backs up (it holds the local tables)
        db.execute("CREATE TABLE hidden_project_rows(project_id TEXT, content BLOB)")
        self.refused_inventory("unknown", "ledger's tables")
        db.execute("DROP TABLE hidden_project_rows")
        db.execute("ALTER TABLE project_wake RENAME TO project_wake_renamed")
        self.refused_inventory("renamed", "ledger's tables")
        db.execute("ALTER TABLE project_wake_renamed RENAME TO project_wake")
        db.execute("ALTER TABLE project_wake ADD COLUMN extra TEXT")
        self.refused_inventory("extra", "project_wake exactly")
        db.execute("ALTER TABLE project_wake DROP COLUMN extra")
        # A STORED generated column cannot be added in place: rebuild the (empty) wake table with one.
        wake = db.execute("SELECT sql FROM sqlite_master WHERE name='project_wake'").fetchone()[0]
        self.assertEqual(0, db.execute("SELECT COUNT(*) FROM project_wake").fetchone()[0])
        db.execute("DROP TABLE project_wake")
        db.execute(wake[:-1] + ",gen INTEGER GENERATED ALWAYS AS (allocated_epoch+1) STORED)")
        self.refused_inventory("generated", "project_wake exactly")
        db.execute("DROP TABLE project_wake")
        db.execute(wake)
        db.execute("ALTER TABLE projects ADD COLUMN gen INTEGER GENERATED ALWAYS AS (created_at_ms+1) VIRTUAL")
        self.refused_inventory("generatedp", "projects exactly")
        db.execute("ALTER TABLE projects DROP COLUMN gen")
        self.owner_backup("again")

    def test_F3_18_a_ledger_from_the_released_0_8_59_without_local_tables_backs_up(self):
        db = self.source._connection
        for table in ("project_starts", "project_wake_instances", "project_wake"):
            db.execute("DROP TABLE " + table)
        for column in ("launch_digest", "launch_copy"):
            db.execute("ALTER TABLE projects DROP COLUMN " + column)
        self.owner_backup("released")

    def test_F3_7_a_partition_planned_over_the_limit_is_refused_before_it_is_written(self):
        add_events(self.source, self.project, 2_000)
        with mock.patch.dict(SCRIPT_GLOBALS, {"BACKUP_LIMIT_BYTES": 256 * 1024}), \
                self.assertRaisesRegex(RuntimeError, "planned"):
            self.owner_backup("planned_too_big")
        self.assert_nothing_left("planned_too_big")

    def test_F3_9_a_child_past_its_time_limit_is_stopped_and_nothing_is_left(self):
        add_events(self.source, self.project, 2_000)
        children = []
        with mock.patch.dict(SCRIPT_GLOBALS, {"CHILD_SECONDS": 0.01}), \
                child_runs(started=lambda process: children.append(process.pid)), \
                self.assertRaisesRegex(RuntimeError, "time limit"):
            self.owner_backup("too_slow")
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0], 0)
        self.assert_nothing_left("too_slow")

    def test_F3_9_the_childs_error_output_reaches_the_report(self):
        real = subprocess.Popen

        def crashing(argv, *args, **kwargs):
            return real([sys.executable, "-I", "-c", "import sys; sys.stderr.write('boom in the child'); sys.exit(3)"],
                        *args, **kwargs)
        with mock.patch.object(subprocess, "Popen", crashing), self.assertRaisesRegex(RuntimeError, r"could not be written or checked \(exit 3\)"):
            self.owner_backup("crashed")
        self.assert_nothing_left("crashed")


    # Review 040-f3-owner-backup-2 (F3-11 to F3-14) ---------------------------------------------------

    def test_F3_11_a_signal_right_after_registration_keeps_the_registered_backup(self):
        real = self.source._register_stored_file

        def register_then_signal(*args, **kwargs):
            real(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(self.source, "_register_stored_file", register_then_signal):
            report = self.owner_backup("committed")
        self.assertTrue(report["stopped_after_registration"])
        self.assertEqual(["committed.sqlite3"], self.partition_files())
        self.assertEqual(report["backup_sha256"], self.registered("committed")["content_digest"])
        self.assertEqual(before, signal.getsignal(signal.SIGTERM))

    def authorization(self):
        path = self.source.state.root / "authorization-under-test.json"
        path.write_bytes(b'{"runtime_digest":"' + b"a" * 64 + b'","runtime_version":"0.8.32","schema_version":1}\n')
        path.chmod(0o600)
        return path

    def test_F3_12_a_quiesce_failing_after_the_replacement_restores_the_authorization(self):
        path = self.authorization()
        before = path.read_bytes()
        real_chmod = os.chmod

        def chmod(target, mode, *args, **kwargs):
            if str(target) == str(path):
                raise OSError("chmod failed")
            return real_chmod(target, mode, *args, **kwargs)
        with mock.patch.object(os, "chmod", chmod), self.assertRaisesRegex(OSError, "chmod failed") as caught:
            TOOL["_quiesce_collector_authorization"](path)
        self.assertNotIsInstance(caught.exception, SCRIPT_GLOBALS["QuiesceLeftStopped"])
        self.assertEqual(before, path.read_bytes())  # as it was: the collector is not left stopped

    def test_F3_12_a_quiesce_that_cannot_be_undone_reports_the_collector_left_stopped(self):
        report = self.owner_backup("left_stopped")

        def quiesce():
            raise SCRIPT_GLOBALS["QuiesceLeftStopped"]("restore failed")
        final, code = TOOL["finish_backup"](report, leave_quiesced=True, quiesce=quiesce)
        self.assertEqual(3, code)
        self.assertTrue(final["collector_quiesced"])
        self.assertEqual("the backup could not be completed (QuiesceLeftStopped)", final["quiesce_error"])

    def test_F3_13_a_collector_that_does_not_restart_is_in_the_one_report(self):
        report = self.owner_backup("not_restarted")

        def resume():
            raise RuntimeError("collector would not start")
        final, code = TOOL["settle_maintenance"]({**report, "collector_quiesced": False}, 0, resume=resume)
        self.assertEqual(3, code)
        self.assertTrue(final["ready"])
        self.assertIn("the installed collector did not restart", final["restart_error"])
        self.assertNotIn("collector would not start", final["restart_error"])
        final, code = TOOL["settle_maintenance"]({**report, "collector_quiesced": True}, 0, resume=resume)
        self.assertEqual((0, None), (code, final.get("restart_error")))  # left stopped as asked: no restart

    def test_F3_14_a_file_cleanup_cannot_remove_is_named(self):
        real_unlink = os.unlink

        def unlink(name, *args, **kwargs):
            if str(name).endswith("stuck.partial"):
                os.stat(name, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)  # absent: as unlink would say
                raise PermissionError("cannot remove")
            return real_unlink(name, *args, **kwargs)
        with mock.patch.object(self.source, "_register_stored_file",
                               side_effect=learning_store.LedgerError("registry refused")), \
                mock.patch.object(os, "unlink", unlink), self.assertRaises(Exception) as caught:
            self.owner_backup("stuck")
        self.assertIn("could not be removed", str(caught.exception))
        self.assertNotIn(str(self.source.state.backups), str(caught.exception))  # decisions 36, 192
        self.assertNotIn(str(self.source.state.root), str(caught.exception))
        self.assertIn("1 partial file(s)", str(caught.exception))
        self.assertIsNone(self.registered("stuck"))


    # Review 040-f3-owner-backup-3 (F3-15 to F3-17) ---------------------------------------------------

    def test_F3_15_a_stop_held_before_the_backup_ends_it_once_the_child_runs(self):
        found = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
        try:
            os.kill(os.getpid(), signal.SIGTERM)  # held, as _main holds it from its start
            with self.assertRaisesRegex(RuntimeError, "signal"):
                self.owner_backup("held_stop")
            self.assertNotIn(signal.SIGTERM, signal.sigpending())  # taken, not left for later
            self.assertIn(signal.SIGTERM, signal.pthread_sigmask(signal.SIG_BLOCK, []))  # mask as found
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, found)
        self.assert_nothing_left("held_stop")

    def test_F3_16_one_ending_keeps_the_backup_and_reports_every_later_error(self):
        conclude = TOOL["conclude"]
        report = self.owner_backup("concluded")
        resumed = []

        def fail(message):
            def raise_it():
                raise RuntimeError(message)
            return raise_it
        final, code = conclude(report={**report, "collector_quiesced": False}, code=0, error=None,
                               close=fail("ledger would not close"), resume=lambda: resumed.append(True))
        self.assertEqual(3, code)
        self.assertTrue(final["ready"])
        self.assertEqual("the backup could not be completed (RuntimeError)", final["close_error"])
        self.assertEqual([True], resumed)  # restarted although closing failed
        final, code = conclude(report=None, code=0, error=RuntimeError("preflight refused"),
                               close=lambda: None, resume=fail("collector would not start"))
        self.assertEqual(2, code)
        self.assertFalse(final["ready"])
        self.assertEqual("the backup could not be completed (RuntimeError)", final["error"])
        self.assertIn("the installed collector did not restart", final["restart_error"])
        self.assertNotIn("collector would not start", final["restart_error"])
        final, code = conclude(report={**report, "collector_quiesced": True}, code=0, error=None,
                               close=lambda: None, resume=fail("must not be called"))
        self.assertEqual((0, None), (code, final.get("restart_error")))  # left stopped as asked

    def test_F3_17_an_authorization_already_quiesced_is_left_as_it_is(self):
        path = self.authorization()
        path.write_bytes(b'{"quiesced_for_upgrade":true,"runtime_digest":"' + b"a" * 64
                         + b'","runtime_version":"0.8.32","schema_version":1}\n')
        before = os.stat(path)
        TOOL["_quiesce_collector_authorization"](path)
        after = os.stat(path)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_F3_17_a_failed_rollback_reports_left_stopped_and_leaves_no_temporary_file(self):
        path = self.authorization()
        real_chmod, real_replace = os.chmod, os.replace
        calls = []

        def chmod(target, mode, *args, **kwargs):
            if str(target) == str(path):
                raise OSError("chmod failed")
            return real_chmod(target, mode, *args, **kwargs)

        def replace(source, target, *args, **kwargs):
            calls.append(target)
            if len(calls) > 1 and str(target) == str(path):
                raise OSError("restore failed")
            return real_replace(source, target, *args, **kwargs)
        with mock.patch.object(os, "chmod", chmod), mock.patch.object(os, "replace", replace), \
                self.assertRaises(SCRIPT_GLOBALS["QuiesceLeftStopped"]):
            TOOL["_quiesce_collector_authorization"](path)
        self.assertEqual([], [p.name for p in path.parent.glob(".collector-authorization.*")])


    # Review 040-f3-owner-backup-4 ------------------------------------------------------------------

    def test_F3_12_left_stopped_survives_a_temporary_file_that_cannot_be_removed(self):
        path = self.authorization()
        real_chmod, real_replace, real_unlink = os.chmod, os.replace, Path.unlink
        replaced = []

        def chmod(target, mode, *args, **kwargs):
            if str(target) == str(path):
                raise OSError("chmod failed")
            return real_chmod(target, mode, *args, **kwargs)

        def replace(source, target, *args, **kwargs):
            replaced.append(target)
            if len(replaced) > 1:
                raise OSError("restore failed")
            return real_replace(source, target, *args, **kwargs)

        def unlink(self_path, *args, **kwargs):
            if self_path.name.startswith(".collector-authorization."):
                raise OSError("temporary file stuck")
            return real_unlink(self_path, *args, **kwargs)
        with mock.patch.object(os, "chmod", chmod), mock.patch.object(os, "replace", replace), \
                mock.patch.object(Path, "unlink", unlink), \
                self.assertRaises(SCRIPT_GLOBALS["QuiesceLeftStopped"]) as caught:
            TOOL["_quiesce_collector_authorization"](path)
        self.assertNotIn("temporary file stuck", str(caught.exception))  # decisions 36, 192: fixed reason
        self.assertIn("a temporary copy remains", str(caught.exception))

    def test_F3_16_an_interrupt_while_closing_still_restarts_the_collector(self):
        resumed = []

        def interrupted():
            raise KeyboardInterrupt
        final, code = TOOL["conclude"](report={"ready": True, "collector_quiesced": False}, code=0, error=None,
                                       close=interrupted, resume=lambda: resumed.append(True))
        self.assertEqual([True], resumed)
        self.assertEqual(3, code)
        self.assertIn("KeyboardInterrupt", final["close_error"])


if __name__ == "__main__":
    unittest.main()
