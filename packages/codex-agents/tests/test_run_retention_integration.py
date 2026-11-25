"""S14b real open/claim, owner thread and original-source copy boundaries. No sockets."""
import json
import threading
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
        outcome = {**verification_outcome('retention-outcome'), 'observed_at_ms': now}
        ledger._append_checked_locked(project, outcome, producer='verifier', timestamp=now)
        before = sum(column_bytes(db, table, mark) for table, mark in marks.items())
        old = now+92*retention.DAY
        # Authority remains while the original open and run slot exist.
        dispatch = db.execute("SELECT record_id FROM records WHERE record_type='dispatch_intent' "
                              "AND json_extract(CAST(content AS TEXT),'$.run_id')=?", (state['scope'].run_id,)).fetchone()[0]
        ledger.sweep_run_retention(now_ms=old)
        self.assertIsNotNone(db.execute('SELECT 1 FROM records WHERE record_id=?', (dispatch,)).fetchone())
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
        projected = overhead*runs*(after*horizon+before*92)+event_bytes
        self.assertLess(projected, 16*1024**3, (before, after, projected/1024**3))
        # Isolated restore validates replay/capacity coverage after the real purge.
        path = ledger.state.root/'retired.sqlite3'
        path.touch(mode=0o600)
        project_backup.write(db, path, project, schema)
        project_backup.verify(path, project_id=project, compare=db)

    @requirements('S14b-3')
    def test_collector_sweep_sql_runs_on_the_E1b_owner_thread(self):
        fixture = wiring.LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()  # default sockets=False
        owner = fixture.owner
        def seed():
            owner.ledger._append_checked_locked(fixture.project, retention.identity('old'),
                                                producer='provider', timestamp=retention.START)
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
        owner.call(seed)  # ordinary work still runs after maintenance

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
        ledger._append_checked_locked(fixture.project, retention.identity('old'),
                                      producer='provider', timestamp=retention.START)
        ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        fixture.file_id = 'retention_copy'
        fixture.backup = ledger.create_sanitized_backup(fixture.project, file_id=fixture.file_id,
                                                       expires_at_ms=ledger._clock_ms()+3_600_000)
        fixture.transfer()
        rows = fixture.target._connection.execute("SELECT content FROM project_learning_records "
                                                  "WHERE record_type='retention_summary'").fetchall()
        self.assertEqual(1, len(rows))
        summary = json.loads(bytes(rows[0][0]))
        self.assertEqual([content_digest('old')], summary['record_id_digests'])
        self.assertIn('older than 90 days', summary['detail'])


if __name__ == '__main__':
    unittest.main()
