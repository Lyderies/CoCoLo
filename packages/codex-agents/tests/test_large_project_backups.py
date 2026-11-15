"""A busy project's history is backed up, upgraded and moved without holding it in memory.

Release 0.3.0 built each project backup, and the learning image read from it, as
one document in memory, so a project with 192,000 learning records (312 MB) could
neither be upgraded nor moved. These tests give a project several megabytes of
real records and measure the Python memory each step allocates while it runs: a
streamed step stays at a few batches, an in-memory one is many times the data.
"""

import hashlib
import json
import time
import tracemalloc
import unittest
import unittest.mock
from pathlib import Path

from tests import test_learning_source_image_legacy as legacy
from tests.learning_support import event_record
from tests.test_account_launch_claims import downgrade_ledger_to_six
from codex_agents import learning_source_transfer as transfer
from codex_agents import learning_store
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.sql_read_budget import SQLReadLimits

RECORDS = 16_000  # About 11 MB of records, each a valid event of about 700 bytes.
# A streamed step holds a few batches whatever the project's size (about 2.5 MB
# here); release 0.3.0 held about twelve times the records (137 MB for these).
BOUND = 8 * 1_048_576


def add_events(ledger, project_id, count):
    """`count` valid event records, written directly so the fixture stays quick."""
    connection = ledger._connection
    start = connection.execute("SELECT COALESCE(MAX(source_sequence),0) FROM records WHERE project_id=?",
                               (project_id,)).fetchone()[0]
    rows = []
    for number in range(count):
        record = event_record(f"bulk-{number:06d}", observed_at_ms=number + 1)
        record["model"] = f"model-{number:06d}-" + "m" * 100
        content = canonical_json(record)
        rows.append((project_id, record["record_id"], "event", "hook", start + number + 1, number + 1,
                     number + 1, None, content, content_digest(record), 0))
    with ledger._account_transaction():
        connection.executemany(
            "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
            "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    return sum(len(row[8]) for row in rows)


def peak_bytes(action):
    tracemalloc.start()
    try:
        result = action()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class LargeProjectBackupTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def test_an_upgrade_backs_up_and_checks_a_large_project_in_bounded_memory(self):
        size = add_events(self.source, self.project, RECORDS)
        state = self.source.state
        downgrade_ledger_to_six(self.source)
        self.source.close()

        def upgrade():
            return learning_store.LearningLedger(state, client_capabilities=self.fixture.clients,
                                                 allow_schema_upgrade=True, backup_before_upgrade=True)

        self.source, peak = peak_bytes(upgrade)
        self.fixture.ledger = self.source
        self.assertIsNone(learning_store.ledger_schema_behind(state))
        [backup] = list(state.backups.glob(f"{self.project}/upgrade_schema6_*.sqlite3"))
        self.assertGreater(backup.stat().st_size, size)
        self.assertLess(peak, BOUND, f"the upgrade held {peak:,} bytes for {size:,} bytes of records")

    def test_the_learning_move_reads_and_stages_a_large_project_in_bounded_memory(self):
        size = add_events(self.source, self.project, RECORDS)
        backup = self.source.create_sanitized_backup(
            self.project, file_id="large_image", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)

        def move():
            return transfer.stage_source(self.source, self.target, self.project, "large_image",
                                         import_id="large", work_limits=SQLReadLimits(100_000, 1000, 2 * 1_048_576))

        result, peak = peak_bytes(move)
        self.assertEqual(RECORDS + 23, dict(result["counts"])["records"])
        self.assertEqual(RECORDS + 23, self.target._connection.execute(
            "SELECT COUNT(*) FROM project_learning_records").fetchone()[0])
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), result["source"]["manifest_digest"])
        self.assertLess(peak, BOUND, f"the move held {peak:,} bytes for {size:,} bytes of records")
        # Every record arrived exactly as the ledger holds it.
        imported = dict(self.target._connection.execute(
            "SELECT record_id,content FROM project_learning_records WHERE record_id LIKE 'bulk-%'").fetchall())
        self.assertEqual(RECORDS, len(imported))
        self.assertEqual("bulk-000042", json.loads(imported["bulk-000042"])["record_id"])

    def test_a_backup_registers_only_after_it_is_complete(self):
        # A write that stops part-way leaves no registered or half-written backup behind.
        add_events(self.source, self.project, 500)
        from codex_agents import project_backup
        original = project_backup.write

        def interrupted(connection, path, *args, **options):
            original(connection, path, *args, **options)
            raise KeyboardInterrupt

        project_backup.write = interrupted
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.source.create_sanitized_backup(
                    self.project, file_id="stopped", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        finally:
            project_backup.write = original
        partition = Path(self.source.state.backups) / self.project
        self.assertEqual([], [path.name for path in partition.iterdir() if "stopped" in path.name])
        self.assertIsNone(self.source._connection.execute(
            "SELECT 1 FROM stored_files WHERE file_id='stopped'").fetchone())


class BackupPlanTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def test_the_plan_is_never_smaller_than_the_file(self):
        add_events(self.source, self.project, 3000)
        for number in range(2000):  # Key-heavy rows: indexes copy their keys.
            self.source._connection.execute("INSERT INTO record_links VALUES(?,?,?,?)", (
                self.project, f"bulk-{number:06d}", f"bulk-{number + 1:06d}", "evidence")).close()
        for number in range(500):  # Text whose bytes outnumber its characters.
            self.source._connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (
                self.project, f"{number:04d}" + "\u00e9" * 200, "hook", 1)).close()
        planned = learning_store.project_backup_bytes(
            self.source._connection, self.project, learning_store.LEDGER_SCHEMA_VERSION)
        path = self.source.create_sanitized_backup(
            self.project, file_id="planned", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        self.assertGreaterEqual(planned, path.stat().st_size)

    def test_a_file_over_the_limit_is_refused_and_removed_even_when_the_plan_fitted(self):
        add_events(self.source, self.project, 500)
        with unittest.mock.patch.object(learning_store, "project_backup_bytes", return_value=1), \
                unittest.mock.patch.object(learning_store, "MAX_PROJECT_BACKUP_BYTES", 4096):
            with self.assertRaises(learning_store.LedgerCapacityError):
                self.source.create_sanitized_backup(
                    self.project, file_id="too_big", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        partition = Path(self.source.state.backups) / self.project
        self.assertEqual([], [p.name for p in partition.iterdir() if "too_big" in p.name])
        self.assertIsNone(self.source._connection.execute(
            "SELECT 1 FROM stored_files WHERE file_id='too_big'").fetchone())

    def test_a_partial_file_left_by_a_stopped_write_is_cleared_before_the_next(self):
        partition = Path(self.source.state.backups) / self.project
        partition.mkdir(mode=0o700, parents=True, exist_ok=True)
        stale = partition / ".migration_123.partial"
        stale.write_bytes(b"x" * 1024)
        stale.chmod(0o600)
        self.source.create_sanitized_backup(
            self.project, file_id="after_stop", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        self.assertFalse(stale.exists())


class BackupLimitWhileWritingTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def test_the_limit_stops_a_write_before_the_file_is_complete(self):
        add_events(self.source, self.project, 3000)
        from codex_agents import project_backup
        written = []
        real = project_backup.write

        def observed(connection, path, *args, **options):
            try:
                return real(connection, path, *args, **options)
            finally:
                written.append(path.stat().st_size if path.exists() else 0)

        with unittest.mock.patch.object(learning_store, "project_backup_bytes", return_value=1), \
                unittest.mock.patch.object(learning_store, "MAX_PROJECT_BACKUP_BYTES", 256 * 1024), \
                unittest.mock.patch.object(project_backup, "write", side_effect=observed):
            with self.assertRaises(learning_store.LedgerCapacityError):
                self.source.create_sanitized_backup(
                    self.project, file_id="stopped_early", expires_at_ms=time.time_ns() // 1_000_000 + 3_600_000)
        # Stopped within a batch or so of the limit, not after writing the whole partition (about 3 MB).
        self.assertLess(written[0], 1_048_576)

    def test_an_upgrade_that_stops_part_way_leaves_no_backups_behind(self):
        from tests.test_account_launch_claims import downgrade_ledger_to_six
        later_root = Path(self.fixture.temporary.name) / "second-project"
        later_root.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(later_root)], check=True)
        self.source.register_project(later_root, now_ms=1)
        state = self.source.state
        downgrade_ledger_to_six(self.source)
        self.source.close()
        written = []
        real = learning_store.LearningLedger._write_streamed_backup

        def second_fails(ledger, project_id, *args, **options):
            if written:
                raise learning_store.LedgerCapacityError(projects=2, needed_bytes=10, limit_bytes=1)
            path = real(ledger, project_id, *args, **options)
            written.append(path)
            return path

        with unittest.mock.patch.object(learning_store.LearningLedger, "_write_streamed_backup", autospec=True,
                                        side_effect=second_fails):
            with self.assertRaises(learning_store.LedgerCapacityError):
                learning_store.LearningLedger(state, client_capabilities=self.fixture.clients,
                                              allow_schema_upgrade=True, backup_before_upgrade=True)
        self.assertEqual(1, len(written))
        self.assertFalse(written[0].exists())
        import sqlite3
        kept = sqlite3.connect(state.root / "learning.sqlite3")
        try:
            self.assertEqual(0, kept.execute(
                "SELECT COUNT(*) FROM stored_files WHERE file_id LIKE 'upgrade_schema6_%'").fetchone()[0])
            self.assertEqual(6, kept.execute("PRAGMA user_version").fetchone()[0])
        finally:
            kept.close()
        self.source = learning_store.LearningLedger(state, client_capabilities=self.fixture.clients,
                                                    allow_schema_upgrade=True, backup_before_upgrade=True)
        self.fixture.ledger = self.source
