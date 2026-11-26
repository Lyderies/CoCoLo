"""S14b years-scale history lookup; split from test_run_retention so each module keeps its own budget."""
import copy
import json
import time
import unittest

from test_framework import requirements
from tests import test_learning_store as legacy
from tests import test_run_retention as retention
from codex_agents import learning_store, run_retention
from codex_agents.learning_contracts import canonical_json, content_digest, validate_learning_record
from codex_agents.sql_read_budget import SQLReadError

SUMMARIES = 365_000
PRODUCT_APPENDS = 2_000


@requirements(why_not_scenario='Direct synthetic-ledger scale measurement and SQL assertions; no socket service is opened.')
class RunRetentionScaleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.db = self.ledger._connection
        run_retention.ensure_indexes(self.ledger)  # a consented owner open creates them; a plain open never does

    def append(self, record, source='provider'):
        return retention.store_product_row(self.ledger, self.project, record, source)

    def bulk_summaries(self, template, first, count):
        """Every row is validated by the product contract and indexed by the product member function.

        The first PRODUCT_APPENDS go through the whole product append; the rest are written with the same
        columns in one batch, which only drops the per-record statement overhead that made this test cost
        more than its module budget.
        """
        db = self.db
        project_ref = db.execute('SELECT rowid FROM projects WHERE project_id=?', (self.project,)).fetchone()[0]
        sequence = db.execute('SELECT max(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()[0]
        source = learning_store.SOURCE_BY_PRODUCER['lifecycle']
        rows, members = [], []
        for i in range(first, first+count):
            summary = dict(template)  # every list field is replaced below; nothing nested is mutated
            summary.update(record_id=f'retsum-history-{i}', record_id_digests=[content_digest(f'history-{i}')])
            checked = validate_learning_record(summary)
            sequence += 1
            rows.append((self.project, checked['record_id'], 'retention_summary', source, sequence,
                         checked['observed_at_ms'], checked['observed_at_ms'], None,
                         canonical_json(checked), content_digest(checked), 0, None))
            members.extend(run_retention._summary_members(project_ref, sequence, checked))
        db.executemany('INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                       'ingested_at_ms,expires_at_ms,content,content_digest,late,probe_id) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        db.executemany('INSERT OR IGNORE INTO retention_members VALUES(?,?,?,?,?)', members)

    @requirements('S14b-4')
    def test_typed_history_and_years_of_summaries_use_indexed_membership(self):
        self.append(retention.identity('first'))
        self.ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        first = json.loads(bytes(self.db.execute(
            "SELECT content FROM records WHERE record_type='retention_summary'").fetchone()[0]))
        with self.ledger.repository(self.project) as reader:
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('first', 'outcome')
        # Five years at about 200 batches/day, each validated; none unlinked.
        with self.ledger._account_transaction():
            for i in range(PRODUCT_APPENDS):
                summary = copy.deepcopy(first)
                summary.update(record_id=f'retsum-history-{i}', record_id_digests=[content_digest(f'history-{i}')])
                self.append(summary, 'lifecycle')
            self.bulk_summaries(first, PRODUCT_APPENDS, SUMMARIES-PRODUCT_APPENDS)
        # Build indexes against a years-sized ledger, with real detail in the
        # partial index as well as summaries. All rows use product validation.
        with self.ledger._account_transaction():
            for i in range(50_000):
                self.append(retention.identity(f'index-detail-{i}', retention.START+92*retention.DAY))
        size = self.db.execute('PRAGMA page_size').fetchone()[0] * self.db.execute('PRAGMA page_count').fetchone()[0]
        ddl = (run_retention.RETENTION_INDEX, *run_retention.PROTECTION_INDEXES)
        for sql in ddl:
            self.db.execute('DROP INDEX '+sql.split('EXISTS ')[1].split(' ON ')[0])
        started = time.perf_counter()
        for sql in ddl:
            self.db.execute(sql)
        persistent_seconds = time.perf_counter()-started
        started = time.perf_counter()
        run_retention.rebuild_history_index(self.ledger)
        pages = 0
        while self.ledger._history_index_rebuilding:
            self.ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
            pages += 1
        history_seconds = time.perf_counter()-started
        index_bytes = (self.db.execute('PRAGMA temp.page_size').fetchone()[0]
                       * self.db.execute('PRAGMA temp.page_count').fetchone()[0])
        self.scale_measurements = dict(summary_count=SUMMARIES+1, detail_count=50_000, ledger_bytes=size,
                                       persistent_index_build_seconds=persistent_seconds,
                                       membership_index_build_seconds=history_seconds,
                                       membership_index_bytes=index_bytes,
                                       membership_bytes_per_entry=index_bytes/(SUMMARIES+1),
                                       membership_index_pages=pages)
        self.assertLessEqual(index_bytes/(SUMMARIES+1), 40)
        # The reviewed implementation's JSON walk cannot reach the oldest
        # member under the read budget. Keep this as the regression oracle.
        def old_scan():
            return self.db.execute("SELECT content FROM records r WHERE project_id=? AND record_type='retention_summary' "
                                   "AND EXISTS(SELECT 1 FROM json_each(CAST(r.content AS TEXT),'$.record_id_digests') WHERE value=?) "
                                   "ORDER BY source_sequence DESC LIMIT 1", (self.project, content_digest('first'))).fetchone()
        with self.assertRaises(SQLReadError):
            self.db.read_in_current_mode(run_retention.RUN_RETENTION_READ_LIMITS, old_scan)
        started = time.perf_counter()
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(first, reader.record('first', 'provider_identity'))
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('first', 'outcome')
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('typo-first', 'provider_identity')
        self.scale_measurements['three_lookup_seconds'] = time.perf_counter()-started


if __name__ == '__main__':
    unittest.main()
