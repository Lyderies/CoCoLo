"""S14b years-scale history lookup; split from test_run_retention so each module keeps its own budget."""
import copy
import json
import time
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_learning_store as legacy
from tests import test_run_retention as retention
from codex_agents import learning_store, run_retention, sql_read_budget
from codex_agents.learning_contracts import canonical_json, content_digest, validate_learning_record
from codex_agents.sql_read_budget import SQLReadError

SUMMARY_SIZES = (20_000, 80_000)  # S13G: ~0.27 and ~1.10 years at ~200 batches/day (was one build of 365,000)
DETAILS = 10_000  # was 50,000 beside 365,000 summaries; the same 1:8 share of detail rows
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
        columns in one batch (the membership index is not written here: the rebuild below replaces it), which only drops the per-record statement overhead that made this test cost
        more than its module budget.
        """
        db = self.db
        sequence = db.execute('SELECT max(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()[0]
        source = learning_store.SOURCE_BY_PRODUCER['lifecycle']
        rows = []
        for i in range(first, first+count):
            summary = dict(template)  # every list field is replaced below; nothing nested is mutated
            summary.update(record_id=f'retsum-history-{i}', record_id_digests=[content_digest(f'history-{i}')])
            checked = validate_learning_record(summary)
            sequence += 1
            rows.append((self.project, checked['record_id'], 'retention_summary', source, sequence,
                         checked['observed_at_ms'], checked['observed_at_ms'], None,
                         canonical_json(checked), content_digest(checked), 0, None))
        db.executemany('INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                       'ingested_at_ms,expires_at_ms,content,content_digest,late,probe_id) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)

    @requirements('S14b-4')
    def test_typed_history_and_years_of_summaries_use_indexed_membership(self):
        self.append(retention.identity('first'))
        self.ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
        first = json.loads(bytes(self.db.execute(
            "SELECT content FROM records WHERE record_type='retention_summary'").fetchone()[0]))
        with self.ledger.repository(self.project) as reader:
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('first', 'outcome')
        # Years of history at about 200 batches/day, each validated; none unlinked. S13G: two sizes (~0.27 and ~1.10 years
        # at 200/day) prove the scale claim by growth (every per-entry measure stays flat as the history quadruples) instead of
        # one 365,000-row build whose insert time grew with machine load.
        with self.ledger._account_transaction():
            for i in range(PRODUCT_APPENDS):
                summary = copy.deepcopy(first)
                summary.update(record_id=f'retsum-history-{i}', record_id_digests=[content_digest(f'history-{i}')])
                self.append(summary, 'lifecycle')
        measured, built = [], PRODUCT_APPENDS
        for total in SUMMARY_SIZES:
            with self.ledger._account_transaction():
                self.bulk_summaries(first, built, total-built)
            built = total
            if not measured:
                # Real detail in the partial index as well as summaries. All rows use product validation.
                with self.ledger._account_transaction():
                    for i in range(DETAILS):
                        self.append(retention.identity(f'index-detail-{i}', retention.START+92*retention.DAY))
            measured.append(self.measure_at(total, first, oracle=total == SUMMARY_SIZES[-1]))
        small, large = measured
        self.scale_measurements = dict(sizes=measured)
        # The growth claim: four times the history costs four times the index, with the same bytes per entry and a
        # rebuild that takes four times the pages (linear, not quadratic).
        self.assertEqual(SUMMARY_SIZES[1]/SUMMARY_SIZES[0], 4)
        self.assertLessEqual(large['membership_bytes_per_entry'], 1.15*small['membership_bytes_per_entry'])
        self.assertLessEqual(large['membership_index_pages'], 4.5*small['membership_index_pages'])
        self.assertGreaterEqual(large['membership_index_pages'], 3.5*small['membership_index_pages'])
        # Work, not calls: read-scope VM work spent by the whole rebuild grows linearly (a rescan per page would be ~16x).
        self.assertGreater(small['rebuild_work_units'], 0)
        self.assertLessEqual(large['rebuild_work_units'], 4*1.25*small['rebuild_work_units'])

    def measure_at(self, total, first, *, oracle):
        """Everything S14b-4 asserts at one history size; returns what it measured.

        The old JSON walk is a regression oracle only where it is clearly over the 25 ms read budget (at 80,000
        summaries it takes ~120 ms; at 20,000 it is near the budget and proves nothing).
        """
        size = self.db.execute('PRAGMA page_size').fetchone()[0] * self.db.execute('PRAGMA page_count').fetchone()[0]
        ddl = (run_retention.RETENTION_INDEX, *run_retention.PROTECTION_INDEXES)
        for sql in ddl:
            self.db.execute('DROP INDEX '+sql.split('EXISTS ')[1].split(' ON ')[0])
        started = time.perf_counter()
        for sql in ddl:
            self.db.execute(sql)
        persistent_seconds = time.perf_counter()-started
        started = time.perf_counter()
        # Work, not calls: the read scope that bounds each rebuild page is called once per 256 SQLite VM steps, so
        # its call count is the VM work the rebuild's reads spend (a per-page rescan shows up here, not in `pages`).
        steps = [0]
        real_progress = sql_read_budget._ReadScope.progress
        def counting_progress(scope):
            steps[0] += 1
            return real_progress(scope)
        with mock.patch.object(sql_read_budget._ReadScope, 'progress', counting_progress):
            run_retention.rebuild_history_index(self.ledger)
            pages = 0
            while self.ledger._history_index_rebuilding:
                self.ledger.sweep_run_retention(now_ms=retention.START+91*retention.DAY)
                pages += 1
        history_seconds = time.perf_counter()-started
        index_bytes = (self.db.execute('PRAGMA temp.page_size').fetchone()[0]
                       * self.db.execute('PRAGMA temp.page_count').fetchone()[0])
        measurements = dict(summary_count=total+1, detail_count=DETAILS, ledger_bytes=size,
                            persistent_index_build_seconds=persistent_seconds,
                            membership_index_build_seconds=history_seconds,
                            membership_index_bytes=index_bytes,
                            membership_bytes_per_entry=index_bytes/(total+1),
                            membership_index_pages=pages, rebuild_work_units=steps[0])
        self.assertLessEqual(index_bytes/(total+1), 40)
        # The reviewed implementation's JSON walk cannot reach the oldest
        # member under the read budget. Keep this as the regression oracle.
        def old_scan():
            return self.db.execute("SELECT content FROM records r WHERE project_id=? AND record_type='retention_summary' "
                                   "AND EXISTS(SELECT 1 FROM json_each(CAST(r.content AS TEXT),'$.record_id_digests') WHERE value=?) "
                                   "ORDER BY source_sequence DESC LIMIT 1", (self.project, content_digest('first'))).fetchone()
        if oracle:
            with self.assertRaises(SQLReadError):
                self.db.read_in_current_mode(run_retention.RUN_RETENTION_READ_LIMITS, old_scan)
        started = time.perf_counter()
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(first, reader.record('first', 'provider_identity'))
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('first', 'outcome')
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('typo-first', 'provider_identity')
        measurements['three_lookup_seconds'] = time.perf_counter()-started
        return measurements

if __name__ == '__main__':
    unittest.main()
