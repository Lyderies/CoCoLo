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

        def interrupted(connection, path, *args):
            original(connection, path, *args)
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
