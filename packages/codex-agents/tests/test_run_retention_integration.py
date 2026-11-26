"""S14b real open/claim, owner thread and original-source copy boundaries. No sockets."""
import json
import threading
import time
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_account_original_open as opens
from tests import test_ledger_owner_wiring as wiring
from tests import test_learning_source_transfer as sources
from tests import test_run_retention as retention
from tests.learning_support import verification_outcome
from codex_agents import learning_store, project_backup
from codex_agents.learning_contracts import content_digest


def column_bytes(db, table, mark):
    columns = [r[1] for r in db.execute('PRAGMA table_info('+table+')')]
    expression = '+'.join('coalesce(length(CAST('+c+' AS BLOB)),0)' for c in columns)
    return db.execute('SELECT coalesce(sum('+expression+'),0) FROM '+table+' WHERE rowid>?', (mark,)).fetchone()[0]


@requirements(why_not_scenario='Real account jobs use an in-process transport; no sockets or provider network access.')
class RunRetentionIntegrationTests(unittest.TestCase):
    @requirements('S14b-1', 'S14b-2', 'S14b-5')
    def test_real_open_claim_footprint_retires_to_measured_metadata_and_summary(self):
        fixture = opens.OriginalAccountOpenJobTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger, db = fixture.ledger, fixture.ledger._connection
        project = fixture.reference.base.scope.project_id
        schema = db.execute('PRAGMA user_version').fetchone()[0]
        marks = {table: db.execute('SELECT coalesce(max(rowid),0) FROM '+table).fetchone()[0]
                 for table in learning_store._backup_tables(schema)}
        client, state, launch = fixture.managed_lifecycle()
        claimed = client.claim_launch(launch.run_handle, launch.identity_digest,
                                      request_id=state['scope'].claim_request_id)
        self.assertEqual('claimed', claimed['state'])
        now = ledger._clock_ms()
        outcome = {**verification_outcome('retention-outcome'), 'observed_at_ms': now,
                   'run_id': state['scope'].run_id}
        retention.store_product_row(ledger, project, outcome, 'verifier')
        before = sum(column_bytes(db, table, mark) for table, mark in marks.items())
        old = now+92*retention.DAY
        # Authority remains while the original open and run slot exist.
        dispatch = db.execute("SELECT record_id FROM records WHERE record_type='dispatch_intent' "
                              "AND json_extract(CAST(content AS TEXT),'$.run_id')=?", (state['scope'].run_id,)).fetchone()[0]
        ledger.sweep_run_retention(now_ms=old)
        self.assertIsNotNone(db.execute('SELECT 1 FROM records WHERE record_id=?', (dispatch,)).fetchone())
        self.assertIsNotNone(db.execute('SELECT 1 FROM records WHERE record_id=?', (outcome['record_id'],)).fetchone())
        with mock.patch.object(ledger, '_clock_ms', return_value=old), \
                mock.patch.object(ledger, '_capacity_clock_reference', return_value=1<<62):
            ledger.release_due_account_holds()
        ledger.sweep_account_retention(now_ms=old)
        self.assertIsNone(db.execute('SELECT 1 FROM account_open_receipts WHERE run_id=?',
                                     (state['scope'].run_id,)).fetchone())
        for _ in range(10):
            ledger.sweep_run_retention(now_ms=old)
        self.assertIsNone(db.execute('SELECT 1 FROM records WHERE record_id=?', (dispatch,)).fetchone())
        after = sum(column_bytes(db, table, mark) for table, mark in marks.items())
        self.assertLess(after, before//2, (before, after))
        self.assertIsNone(db.execute("SELECT 1 FROM records WHERE record_type='outcome' AND record_id=?",
                                       (outcome['record_id'],)).fetchone())
        summary = ledger.record_by_digest(project, content_digest(outcome), 'outcome')
        self.assertEqual(1, summary['record_counts']['outcome'])
        self.assertEqual({outcome['status']: 1}, summary['outcome_counts'])
        # Projected bytes include measured permanent columns, actual summaries,
        # recent full footprint, and S14's weighted events and event summaries.
        overhead = 1.745*.972
        runs, horizon = 774, 1825
        event_bytes = runs*(103*7+3*30)*1367 + runs*90*1536
        self.membership_entries_per_run = db.execute('SELECT COUNT(*) FROM retention_members').fetchone()[0]
        # TEMP membership is rebuildable and absent from all backup/image bytes.
        # Reserve 40 bytes/member for the compact live B-tree, reported separately.
        membership_bytes = runs*horizon*self.membership_entries_per_run*40
        projected = overhead*runs*(after*horizon+before*92)+event_bytes
        self.projected_backup_bytes = projected
        self.projected_live_bytes = projected/.972+membership_bytes
        # Isolated restore validates replay/capacity coverage after the real purge.
        path = ledger.state.root/'retired.sqlite3'
        path.touch(mode=0o600)
        project_backup.write(db, path, project, schema)
        project_backup.verify(path, project_id=project, compare=db)
        from codex_agents.learning_source_image import MAX_BACKUP_BYTES
        self.assertEqual(16*1024**3, MAX_BACKUP_BYTES)
        self.assertLess(projected, MAX_BACKUP_BYTES, (before, after, projected/1024**3))
        self.assertGreater(MAX_BACKUP_BYTES-projected, .79*1024**3)
        self.assertEqual(0, db.execute(
            "SELECT count(*) FROM retention_members WHERE length(digest)<>12").fetchone()[0])

    @requirements('S14b-3')
    def test_collector_sweep_sql_runs_on_the_E1b_owner_thread(self):
        fixture = wiring.LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()  # default sockets=False
        owner = fixture.owner
        def seed():
            retention.store_product_row(owner.ledger, fixture.project, retention.identity('old'))
        owner.call(seed)
        with mock.patch.object(owner.ledger, '_clock_ms', return_value=retention.START+91*retention.DAY):
            fixture.server._sweep_cycle()
        total = owner.call(lambda: owner.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='retention_summary'").fetchone()[0])
        self.assertEqual(1, total)
        statements = [(thread, sql) for thread, sql in fixture.sql if 'retsum-' in sql]
        self.assertTrue(statements)
        self.assertTrue(all(thread is fixture.thread for thread, _ in statements))
        self.assertIsNot(threading.current_thread(), fixture.thread)
        # A restored/upgraded connection schedules the same rebuild on E1b's
        # owner; normal lookups have a plain answer until its maintenance turn.
        from codex_agents import run_retention
        owner.call(lambda: run_retention.rebuild_history_index(owner.ledger))
        def lookup():
            with owner.ledger.repository(fixture.project) as reader:
                return reader.record('old', 'provider_identity')
        self.assertEqual('history index rebuilding', owner.call(lookup)['detail'])
        with mock.patch.object(owner.ledger, '_clock_ms', return_value=retention.START+91*retention.DAY):
            fixture.server._sweep_cycle()
        self.assertEqual('retention_summary', owner.call(lookup)['record_type'])
        rebuild_sql = [(thread, sql) for thread, sql in fixture.sql if 'r.rowid>' in sql]
        self.assertTrue(rebuild_sql)
        self.assertTrue(all(thread is fixture.thread for thread, _ in rebuild_sql))
        owner.call(seed)  # ordinary work still runs after maintenance

    @requirements('S14b-3')
    def test_pinned_ledger_reaches_idle_without_delaying_owner_requests(self):
        from codex_agents.ledger_waits import LEDGER_REQUEST_WAIT_MS
        fixture = wiring.LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()
        owner = fixture.owner
        def seed():
            with owner.ledger._account_transaction():
                for i in range(256):
                    identifier = f'pinned-{i}'
                    retention.store_product_row(owner.ledger, fixture.project, retention.identity(identifier))
                    owner.ledger._connection.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                                                     (fixture.project, identifier, identifier, 'evidence'))
        owner.call(seed)
        latencies = []
        with mock.patch.object(owner.ledger, '_clock_ms', return_value=retention.START+91*retention.DAY):
            for _ in range(12):
                handle = owner.offer(fixture.server._sweep_cycle, caller='sweeps')
                started = time.monotonic()
                self.assertEqual(fixture.project, owner.call(lambda: fixture.project))
                latencies.append(time.monotonic()-started)
                self.assertTrue(handle.handled.wait(LEDGER_REQUEST_WAIT_MS/1000))
            self.assertFalse(owner.call(lambda: owner.ledger._run_retention_more))
            for _ in range(5):
                self.assertIsNone(fixture.server._sweep_cycle())
        self.assertLess(max(latencies), LEDGER_REQUEST_WAIT_MS/1000)

    @requirements('S14b-3', 'S14b-4', 'S14b-5')
    def test_import_rebuild_pages_resume_and_preserve_digest_answers(self):
        fixture = sources.LearningSourceTransferTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger = fixture.source
        ledger._connection.execute('INSERT OR IGNORE INTO record_links '
                                   "SELECT project_id,record_id,record_id,'evidence' FROM records")
        for i in range(70):
            retention.store_product_row(ledger, fixture.project, retention.identity(f'import-old-{i}'))
            ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY, batch=1)
        outcome = {**verification_outcome('import-outcome'), 'observed_at_ms': retention.START}
        retention.store_product_row(ledger, fixture.project, outcome, 'verifier')
        ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        expected = ledger.record_by_digest(fixture.project, content_digest(outcome), 'outcome')
        fixture.file_id = 'paged_retention'
        fixture.backup = ledger.create_sanitized_backup(fixture.project, file_id=fixture.file_id,
                                                       expires_at_ms=ledger._clock_ms()+3_600_000)
        fixture.transfer()
        target = fixture.target
        self.assertEqual(32, target.rebuild_learning_history_index())
        self.assertIn('source', target._learning_history_cursors)
        self.assertEqual('history index rebuilding', target.learning_import_history_record(
            'source', content_digest(outcome), 'outcome', by_digest=True)['detail'])
        fixture.reopen()  # TEMP entries and scheduling restart from durable summaries.
        target = fixture.target
        with mock.patch.object(target, '_transaction', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                target.rebuild_learning_history_index()
        self.assertEqual(0, target._learning_history_cursors['source'])
        for _ in range(2):
            self.assertLessEqual(target.rebuild_learning_history_index(), 32)
        # The normal project-owner cleanup path advances the final page too.
        target.expire_results()
        self.assertNotIn('source', target._learning_history_cursors)
        self.assertEqual(expected, target.learning_import_history_record(
            'source', content_digest(outcome), 'outcome', by_digest=True))
        self.assertIsNone(target.learning_import_history_record('source', 'never-written', 'provider_identity'))
        # The same full-digest verification rejects a forced candidate collision.
        from codex_agents import run_retention
        with mock.patch.object(run_retention, 'digest_prefix', return_value=run_retention.digest_prefix(content_digest(outcome))):
            self.assertIsNone(target.learning_import_history_record(
                'source', content_digest('foreign'), 'outcome', by_digest=True))

    @requirements('S14b-4', 'S14b-5')
    def test_summary_survives_original_source_image_and_learning_transfer(self):
        fixture = sources.LearningSourceTransferTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger = fixture.source
        # This source fixture already carries historical provider observations.
        # Keep them explicitly cited so this test isolates the one new detail.
        ledger._connection.execute('INSERT OR IGNORE INTO record_links '
                                   "SELECT project_id,record_id,record_id,'evidence' FROM records")
        retention.store_product_row(ledger, fixture.project, retention.identity('old'))
        ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        fixture.file_id = 'retention_copy'
        fixture.backup = ledger.create_sanitized_backup(fixture.project, file_id=fixture.file_id,
                                                       expires_at_ms=ledger._clock_ms()+3_600_000)
        before_size = fixture.backup.stat().st_size
        ledger._connection.execute('DELETE FROM retention_members')
        fixture.transfer()
        self.assertEqual(before_size, len(fixture.archive_body()))
        self.assertEqual(fixture.backup.read_bytes(), fixture.archive_body())
        target = fixture.target
        self.assertEqual('history index rebuilding', target.learning_import_history_record(
            'source', 'old', 'provider_identity')['detail'])
        target.rebuild_learning_history_index()
        # Rebuild the original owner from the same durable summary as well.
        from codex_agents import run_retention
        run_retention.rebuild_history_index(ledger)
        ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        with ledger.repository(fixture.project) as reader:
            expected = reader.record('old', 'provider_identity')
        self.assertEqual(expected, target.learning_import_history_record('source', 'old', 'provider_identity'))
        fixture.reopen()
        self.assertEqual('history index rebuilding', fixture.target.learning_import_history_record(
            'source', 'old', 'provider_identity')['detail'])
        fixture.target.rebuild_learning_history_index()
        self.assertEqual(expected, fixture.target.learning_import_history_record('source', 'old', 'provider_identity'))
        rows = fixture.target._connection.execute("SELECT content FROM project_learning_records "
                                                  "WHERE record_type='retention_summary'").fetchall()
        self.assertEqual(1, len(rows))
        summary = json.loads(bytes(rows[0][0]))
        self.assertEqual([content_digest('old')], summary['record_id_digests'])
        self.assertIn('older than 90 days', summary['detail'])


if __name__ == '__main__':
    unittest.main()
