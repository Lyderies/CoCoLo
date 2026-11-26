"""S14b bounded maintenance, summaries and history; synthetic ledgers only."""
import json
import contextlib
import sqlite3
import time
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_learning_store as legacy
from tests.support import REPOSITORY_ROOT
from tests.learning_support import verification_outcome, event_record
from codex_agents import learning_store, run_retention
from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import validate_learning_record

DAY = 86_400_000
START = 1_000_000_000_000


def identity(identifier, observed=START):
    return dict(schema_version=1, record_type='provider_identity', record_id=identifier,
                provider='codex', account_fingerprint='a'*64, logged_in=True,
                auth_method=None, subscription_type=None, plan_type=None,
                client_path='/synthetic/codex', client_sha256='b'*64,
                client_version='1.0.0', observed_at_ms=observed)


def store_product_row(ledger, project, record, source='provider'):
    """Use the product validation/append/auto-link sequence, including host provenance."""
    checked = validate_learning_record(record)
    timestamp = checked['observed_at_ms']
    with ledger._held(), (contextlib.nullcontext() if ledger._connection.in_transaction else ledger._account_transaction()):
        if checked['record_type'] == 'outcome':
            event = event_record('evt_'+checked['record_id'], observed_at_ms=timestamp)
            for field in ('run_id', 'assignment_id', 'model', 'effort', 'role', 'task_slice',
                          'topology', 'configuration_digest', 'coverage', 'usage'):
                event[field] = checked[field]
            event.update(event_code='item_completed', status='completed', item_digest=checked['evidence_digests'][0])
            store_product_row(ledger, project, event, 'host')
        ledger._validate_evidence_links_locked(project, checked)
        return ledger._append_checked_locked(project, checked, producer=source, timestamp=timestamp)


@requirements(why_not_scenario='Direct synthetic-ledger maintenance and SQL assertions; no socket service is opened.')
class RunRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.db = self.ledger._connection
        run_retention.ensure_indexes(self.ledger)  # a consented owner open creates them; a plain open never does

    def append(self, record, source='provider'):
        return store_product_row(self.ledger, self.project, record, source)

    def summaries(self):
        return [json.loads(bytes(row[0])) for row in self.db.execute(
            "SELECT content FROM records WHERE record_type='retention_summary' ORDER BY source_sequence")]

    def sweep(self, **options):
        return self.ledger.sweep_run_retention(now_ms=START+91*DAY, **options)

    @requirements('S14b-1')
    def test_five_year_projection_at_double_peak_includes_every_family(self):
        model = json.loads((REPOSITORY_ROOT.parents[1] / 'docs/requirements/s14b/s14-growth-input.json').read_text())
        # The accepted source measurement: 106-event runs, 387/day peak, 1.745x
        # database/column and .972x planned-backup/database overhead.
        m = model['measured']
        policy = learning_store.load_retention_policy()
        self.append(identity('old'))
        self.append(verification_outcome('projection-outcome'), 'verifier')
        # Before S14b, the automatic collector leaves all non-event records.
        server = learning_store.CollectorServer(self.ledger)
        with mock.patch.object(self.ledger, '_clock_ms', return_value=START+91*DAY):
            server._sweep_cycle()
        bounded = bool(self.summaries()) and self.db.execute(
            "SELECT 1 FROM records WHERE record_id='projection-outcome'").fetchone() is None
        overhead = m['database_to_column_ratio'] * m['planned_backup_to_database_ratio']
        by_table = m['other_families_per_run']['by_table']
        # OP-2 already bounds these finished rows. Count all other metadata and
        # controlled evidence over the entire horizon, and retain recent detail too.
        open_bytes = sum(by_table[t]['column_bytes'] for t in
                         ('host_run_bindings', 'account_open_receipts', 'account_open_requests'))
        detail_bytes = by_table['records']['column_bytes']+by_table['records (outcome)']['column_bytes']
        permanent = m['other_families_per_run']['backup_column_bytes'] - open_bytes - (detail_bytes if bounded else 0)
        runs = 2*model['recorded_peak_runs_per_day']
        horizon = 5*365
        events = runs * (103*policy.event_keep_ms['tool_started']/DAY +
                         3*policy.event_keep_ms['session_started']/DAY) * m['planned_backup_bytes_per_event']
        event_summaries = runs*policy.summary_keep_ms/DAY*m['summary_planned_backup_bytes_per_run']
        # Conservative allowance: one 2 KiB detail summary for every run,
        # even though actual summaries combine multiple records/runs per page.
        summaries = runs*horizon*2048 if bounded else 0
        total = events+event_summaries+overhead*runs*(permanent*horizon+(open_bytes+detail_bytes)*91)+summaries
        self.assertLess(total, 16*1024**3, f'five years at 2x peak: {total/1024**3:.2f} GiB')

    @requirements('S14b-2', 'S14b-4')
    def test_cutoff_summary_counts_and_old_detail_answer(self):
        self.append(identity('old'))
        self.append(identity('young', START+DAY+1))
        self.assertEqual(0, self.ledger.sweep_run_retention(now_ms=START+90*DAY-1))
        self.assertEqual(1, self.ledger.sweep_run_retention(now_ms=START+90*DAY))
        [summary] = self.summaries()
        validate_learning_record(summary)
        self.assertEqual({'provider_identity': 1}, summary['record_counts'])
        self.assertEqual({'provider': 1}, summary['source_counts'])
        self.assertEqual((START, START), (summary['first_observed_at_ms'], summary['last_observed_at_ms']))
        with self.ledger.repository(self.project) as reader:
            old = reader.record('old', 'provider_identity')
            self.assertEqual('retention_summary', old['record_type'])
            self.assertIn('older than 90 days', old['detail'])
            self.assertEqual('young', reader.record('young', 'provider_identity')['record_id'])
        self.assertIsNone(self.db.execute("SELECT expires_at_ms FROM records WHERE record_type='retention_summary'").fetchone()[0])

    @requirements('S14b-4')
    def test_mistyped_content_digest_is_not_found(self):
        with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
            self.ledger.record_by_digest(self.project, 'mistyped-digest', 'outcome')

    @requirements('S14b-4')
    def test_old_detail_resolves_its_own_summary_after_other_batches(self):
        self.append(identity('first'))
        self.assertEqual(1, self.sweep(batch=1))
        first = self.summaries()[0]
        self.append(identity('second', START+1))
        self.sweep(batch=1)
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(first, reader.record('first', 'provider_identity'))
            self.assertEqual(first, reader.record(first['record_id'], 'retention_summary'))
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('never-written', 'provider_identity')
        from codex_agents.run_retention import rebuild_history_index
        self.db.execute('DELETE FROM retention_members')
        rebuild_history_index(self.ledger)
        self.sweep()
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(first, reader.record('first', 'provider_identity'))

    @requirements('S14b-3', 'S14b-4', 'S14b-5')
    def test_reopen_upgrade_rebuild_is_paged_resumable_and_lookup_is_safe(self):
        from codex_agents import run_retention
        from codex_agents.learning_contracts import content_digest
        for i in range(70):
            self.append(identity(f'rebuild-{i}'))
            self.sweep(batch=1)
        expected = self.summaries()[-1]
        store_product_row(self.ledger, self.fixture.project_b_id, identity('other-project-history'))
        self.sweep(batch=1)
        # Epoch-only product upgrade, followed by a second restart mid-rebuild.
        self.db.execute('PRAGMA user_version=16')
        for restart in range(2):
            self.ledger.close()
            self.fixture.ledger = self.ledger = learning_store.LearningLedger(
                self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
            self.db = self.ledger._connection
            self.assertEqual(0, self.db.execute('SELECT count(*) FROM retention_members').fetchone()[0])
            with self.ledger.repository(self.project) as reader:
                for name in ('rebuild-69', 'never-written'):
                    self.assertEqual('history index rebuilding', reader.record(name, 'provider_identity')['detail'])
            from codex_agents.sql_read_budget import SQLReadError
            with mock.patch.object(self.db, 'read_in_current_mode', side_effect=SQLReadError('sql_read_work_limit')):
                with self.ledger.repository(self.project) as reader:
                    self.assertEqual('history index rebuilding', reader.record('rebuild-69', 'provider_identity')['detail'])
                self.assertEqual('history index rebuilding', self.ledger.record_by_digest(
                    self.project, content_digest('anything'), 'outcome')['detail'])
            # Existing live rows remain available while the derived index builds.
            self.append(identity(f'live-{restart}', START+200*DAY))
            with self.ledger.repository(self.project) as reader:
                self.assertEqual(f'live-{restart}', reader.record(f'live-{restart}', 'provider_identity')['record_id'])
            self.sweep()
            self.assertGreater(self.db.execute('SELECT count(*) FROM retention_members').fetchone()[0], 0)
            self.assertLessEqual(self.db.execute('SELECT count(*) FROM retention_members').fetchone()[0], 32)
        # Refusal must preserve the cursor; the next owner maintenance resumes.
        from codex_agents.sql_read_budget import SQLReadError
        checkpoint = self.ledger._history_index_after
        with mock.patch.object(self.db, 'read_call', side_effect=SQLReadError('sql_read_work_limit')):
            self.assertEqual(0, self.sweep())
        self.assertEqual(checkpoint, self.ledger._history_index_after)
        for _ in range(5):
            self.sweep()
        self.assertFalse(self.ledger._history_index_rebuilding)
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(expected, reader.record('rebuild-69', 'provider_identity'))
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                reader.record('never-written', 'provider_identity')
        with self.ledger.repository(self.fixture.project_b_id) as reader:
            self.assertIn('older than 90 days', reader.record('other-project-history', 'provider_identity')['detail'])
        rows = self.db.execute('SELECT length(digest) FROM retention_members').fetchall()
        self.assertTrue(rows)
        self.assertEqual({12}, {r[0] for r in rows})
        ddl = self.db.execute("SELECT sql FROM sqlite_temp_master WHERE name='retention_members'").fetchone()[0]
        self.assertIn('WITHOUT ROWID', ddl)
        n = 774*1825*15
        self.assertLess(n*(n-1)/2**97, 2.84e-15)

    @requirements('S14b-3', 'S14b-4', 'S14b-5')
    def test_exclusive_vacuum_reschedules_rowid_based_history(self):
        self.append(identity('vacuum-old'))
        self.sweep()
        [expected] = self.summaries()
        # The existing product deletion path vacuums the whole database, which
        # may renumber both summary-walk rowids and numeric project references.
        self.ledger.delete_project(self.fixture.project_b_id)
        self.assertTrue(self.ledger._history_index_rebuilding)
        self.assertEqual(('', 0), self.ledger._history_index_after)
        with self.ledger.repository(self.project) as reader:
            self.assertEqual('history index rebuilding', reader.record('vacuum-old', 'provider_identity')['detail'])
        self.sweep()
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(expected, reader.record('vacuum-old', 'provider_identity'))

    @requirements('S14b-4')
    def test_equal_prefixes_never_return_another_members_summary(self):
        from codex_agents import run_retention
        from codex_agents.learning_contracts import content_digest
        # Force both distinct ID and content digest prefixes to collide.
        with mock.patch.object(run_retention, 'digest_prefix', return_value=b'x'*12):
            outcome = verification_outcome('collision-outcome')
            self.append(identity('collision-first'))
            self.sweep(batch=1)
            first = self.summaries()[0]
            self.append(identity('collision-second'))
            self.sweep(batch=1)
            self.append(outcome, 'verifier')
            self.sweep()
            own = self.summaries()[-1]
            with self.ledger.repository(self.project) as reader:
                self.assertEqual(first, reader.record('collision-first', 'provider_identity'))
                with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                    reader.record('collision-second', 'provider_identity')
                with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                    reader.record('collision-foreign', 'provider_identity')
            self.assertEqual(own, self.ledger.record_by_digest(self.project, content_digest(outcome), 'outcome'))
            with self.assertRaisesRegex(learning_store.LedgerError, 'not found'):
                self.ledger.record_by_digest(self.project, content_digest('foreign'), 'outcome')

    @requirements('S14b-1', 'S14b-4', 'S14b-5')
    def test_backup_bytes_exclude_index_and_restored_rows_rebuild_answers(self):
        from codex_agents import project_backup, run_retention
        self.append(identity('backup-old'))
        self.sweep()
        expected = self.summaries()[0]
        schema = self.db.execute('PRAGMA user_version').fetchone()[0]
        planned = project_backup.estimate_bytes(self.db, self.project, schema)
        paths = [self.ledger.state.root / f'index-{i}.sqlite3' for i in range(2)]
        for i, path in enumerate(paths):
            path.touch(mode=0o600)
            project_backup.write(self.db, path, self.project, schema)
            if i == 0:
                self.db.execute('DELETE FROM retention_members')
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        self.assertEqual(planned, project_backup.estimate_bytes(self.db, self.project, schema))
        with contextlib.closing(sqlite3.connect(paths[0])) as restored:
            self.assertIsNone(restored.execute("SELECT 1 FROM sqlite_master WHERE name='retention_members'").fetchone())
            # Restore the actual backup's permanent summary rows into the owner;
            # the derived state is deliberately absent, as on recovery/import.
            columns = ','.join(('project_id', *learning_store._BACKUP_TABLE_COLUMNS['records']))
            rows = restored.execute('SELECT '+columns+' FROM records').fetchall()
        with self.ledger._account_transaction():
            self.db.execute('DELETE FROM records WHERE project_id=?', (self.project,))
            self.db.executemany('INSERT INTO records('+columns+') VALUES('+','.join('?' for _ in rows[0])+')', rows)
        run_retention.rebuild_history_index(self.ledger)
        with self.ledger.repository(self.project) as reader:
            self.assertEqual('history index rebuilding', reader.record('backup-old', 'provider_identity')['detail'])
        self.sweep()
        with self.ledger.repository(self.project) as reader:
            self.assertEqual(expected, reader.record('backup-old', 'provider_identity'))

    @requirements('S14b-3')
    def test_sql_work_is_bounded_even_when_every_row_is_protected(self):
        for i in range(120):
            self.append(identity(f'protected-{i}'))
            self.db.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                            (self.project, f'protected-{i}', f'protected-{i}', 'evidence'))
        from codex_agents import run_retention
        from codex_agents.sql_read_budget import SQLReadLimits
        with mock.patch.object(run_retention, 'RUN_RETENTION_READ_LIMITS', SQLReadLimits(1, 25, 4*1_048_576)):
            self.assertEqual(0, self.sweep())
        self.assertFalse(self.db.in_transaction)
        # No retention admission cap: ordinary writes still work after refusal.
        self.append(identity('new-run', START+92*DAY))

    @requirements('S14b-2', 'S14b-3')
    def test_live_windows_Claude_bootstrap_and_latest_reading_preserve_admission_answers(self):
        from codex_agents.account_service import _ADMISSION_INPUT_SCOPE
        def headroom(name, provider='claude', observed=START, reset=START+DAY):
            return dict(schema_version=1, record_type='provider_headroom', record_id=name,
                        provider=provider, account_fingerprint='a'*64, run_id=None, phase=None,
                        windows=[dict(window_minutes=300, scope=None, used_percent=100.0, resets_at=reset)],
                        status=None, observed_at_ms=observed)
        self.append(headroom('superseded'))
        self.append(headroom('latest', observed=START+1))
        self.append(headroom('still-exhausted', provider='codex', reset=START+200*DAY))
        self.append(headroom('reset', provider='codex'))
        def decision(name, observed):
            return dict(schema_version=1, record_type='admission_decision', record_id=name, run_id='run-'+name,
                        provider='claude', decision='pass', reason_code='admitted', admission_basis='bootstrap',
                        account_fingerprint='a'*64, evidence_digests=['b'*64], observed_at_ms=observed)
        self.append(decision('first-pass', START), 'lifecycle')
        self.append(decision('later-pass', START+1), 'lifecycle')
        def selected():
            return {row[0] for row in self.db.execute('SELECT record_id FROM records r WHERE '+_ADMISSION_INPUT_SCOPE,
                                                     {'p': self.project, 'a': START+91*DAY})}
        before = selected()
        self.assertEqual({'first-pass', 'latest', 'still-exhausted'}, before)
        self.assertEqual(3, self.sweep())
        self.assertEqual(before, selected())

    @requirements('S14b-3')
    def test_failed_fast_continuation_stops_and_batch_ceiling_never_limits_a_run(self):
        self.append(identity('old'))
        from codex_agents.run_retention import RUN_RETENTION_BATCH
        with self.assertRaises(ValueError):
            self.sweep(batch=RUN_RETENTION_BATCH+1)
        self.ledger._run_retention_more = True
        self.db.execute("CREATE TEMP TRIGGER stop_retention BEFORE DELETE ON records "
                        "WHEN OLD.record_id='old' BEGIN SELECT RAISE(ABORT,'stopped'); END")
        server = learning_store.CollectorServer(self.ledger)
        with mock.patch.object(self.ledger, '_clock_ms', return_value=START+91*DAY):
            self.assertIsNone(server._sweep_cycle(holds_only=True))
        self.assertFalse(self.ledger._run_retention_more)
        self.assertEqual([], self.summaries())
        self.assertFalse(self.db.in_transaction)
        self.append(identity('ordinary-work', START+92*DAY))
        self.db.execute('DROP TRIGGER stop_retention')
        self.assertEqual(1, self.sweep())

    @requirements('S14b-2', 'S14b-3')
    def test_past_batch_limit_and_protected_prefix_drain_without_starvation(self):
        for i in range(7):
            self.append(identity(f'old-{i}'))
        for i in range(3):
            self.db.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                            (self.project, f'old-{i}', f'old-{i}', 'evidence'))
        removed = []
        for _ in range(8):
            removed.append(self.sweep(batch=2))
        self.assertLessEqual(max(removed), 2)
        self.assertEqual(4, sum(removed))
        self.assertEqual(4, sum(s['record_count'] for s in self.summaries()))
        self.assertEqual(3, self.db.execute("SELECT COUNT(*) FROM records WHERE record_type='provider_identity'").fetchone()[0])
        self.assertFalse(self.db.in_transaction)

    @requirements('S14b-2', 'S14b-3')
    def test_summary_failure_rolls_back_and_readers_and_transactions_skip(self):
        self.append(identity('old'))
        with self.ledger.repository(self.project):
            self.assertEqual(0, self.sweep())
        self.db.execute('BEGIN')
        try:
            self.assertEqual(0, self.sweep())
        finally:
            self.db.rollback()
        with mock.patch.object(self.ledger, '_append_checked_locked', side_effect=learning_store.LedgerError('refused')):
            with self.assertRaises(learning_store.LedgerError):
                self.sweep()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual([], self.summaries())
        self.assertEqual(1, self.sweep())

    @requirements('S14b-2')
    def test_full_outcomes_and_attested_and_file_backed_rows_stay(self):
        self.append(identity('attested'))
        self.append(identity('file-backed'))
        self.append(verification_outcome('outcome'), 'verifier')
        self.db.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                        (self.project, 'outcome', 'outcome', 'evidence'))
        self.db.execute('INSERT INTO owner_attestations VALUES(?,?,?,?)',
                        (self.project, 'attested', b'not-authority', START))
        self.db.execute('INSERT INTO stored_files VALUES(?,?,?,?,?,?,?)',
                        (self.project, 'f', 'export', 'exports/synthetic', 'f'*64, 'file-backed', START))
        self.assertEqual(0, self.sweep())
        self.assertEqual(4, self.db.execute('SELECT COUNT(*) FROM records WHERE project_id=?', (self.project,)).fetchone()[0])

    @requirements('S14b-2', 'S14b-4')
    def test_unreferenced_outcome_leaves_status_counts_and_digest_lookup_answer(self):
        from codex_agents.learning_contracts import content_digest
        outcome = verification_outcome('old-outcome')
        self.append(outcome, 'verifier')
        self.assertEqual(2, self.sweep())
        [summary] = self.summaries()
        self.assertEqual({'event': 1, 'outcome': 1}, summary['record_counts'])
        self.assertEqual({outcome['status']: 1}, summary['outcome_counts'])
        self.assertEqual(summary, self.ledger.record_by_digest(self.project, content_digest(outcome), 'outcome'))
        self.assertEqual(summary, self.ledger.record_by_digest(self.project, content_digest(outcome).upper(), 'outcome'))

    @requirements('S14b-3', 'S14b-4')
    def test_historical_lookup_budget_failure_does_not_invent_membership(self):
        from codex_agents.sql_read_budget import SQLReadError
        self.append(identity('old'))
        self.sweep()
        with mock.patch.object(self.db, 'read_in_current_mode', side_effect=SQLReadError('sql_read_work_limit')) as bounded:
            with self.ledger.repository(self.project) as reader:
                with self.assertRaises(SQLReadError):
                    reader.record('never-written', 'provider_identity')
        bounded.assert_called_once()

    @requirements('S14b-3')
    def test_plain_open_makes_no_index_and_the_consented_step_enables_retention_with_a_silent_idle(self):
        plain = legacy.LearningStoreTests(methodName='runTest')
        plain.setUp()
        self.addCleanup(plain.tearDown)
        ledger, db = plain.ledger, plain.ledger._connection
        names = ', '.join("'%s'" % n for n in run_retention.RETENTION_INDEX_NAMES)
        # Decision 352 (S14b-6): a fresh schema-18 ledger is created with the five indexes. The rest of this test
        # is for a ledger that lacks them (one made before the step): drop them, as that ledger would be.
        self.assertEqual(5, db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({names})").fetchone()[0])
        for name in run_retention.RETENTION_INDEX_NAMES:
            db.execute(f'DROP INDEX {name}')
        run_retention.probe(ledger)
        self.assertEqual(0, db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({names})").fetchone()[0])
        store_product_row(ledger, plain.project_a_id, identity('old'))
        statements = []
        db.set_trace_callback(statements.append)
        self.assertEqual(0, ledger.sweep_run_retention(now_ms=START+91*DAY))
        db.set_trace_callback(None)
        self.assertEqual([], statements)  # not ready: no statement, so an idle pass stays the cheap read
        ledger.create_retention_indexes()
        self.assertEqual(5, db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({names})").fetchone()[0])
        statements.clear()
        db.set_trace_callback(statements.append)
        self.assertEqual(0, ledger.sweep_run_retention(now_ms=START+89*DAY))
        db.set_trace_callback(None)
        self.assertEqual([], statements)  # nothing can have expired yet: still no statement
        self.assertEqual(1, ledger.sweep_run_retention(now_ms=START+91*DAY))

    @requirements('S14b-2', 'S14b-3')
    def test_protected_tail_reaches_idle_and_does_not_wrap_on_short_pages(self):
        for i in range(70):
            self.append(identity(f'pinned-{i}'))
            self.db.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                            (self.project, f'pinned-{i}', f'pinned-{i}', 'evidence'))
        for _ in range(4):
            self.sweep()
        self.assertFalse(self.ledger._run_retention_more)
        with mock.patch.object(self.db, 'read_call', wraps=self.db.read_call) as reads:
            for i in range(20):
                self.assertEqual(0, self.ledger.sweep_run_retention(now_ms=START+91*DAY+i*60_000))
                self.assertFalse(self.ledger._run_retention_more)
        self.assertLessEqual(reads.call_count, 20)  # at most the indexed empty tail

    @requirements('S14b-2')
    def test_protection_is_checked_inside_write_transaction_and_propagates(self):
        self.append(verification_outcome('atomic-outcome'), 'verifier')
        from codex_agents import run_retention
        original = run_retention._protected_ids
        def checked(*args):
            self.assertTrue(self.db.in_transaction)
            return original(*args)
        with mock.patch.object(run_retention, '_protected_ids', side_effect=checked):
            self.assertEqual(2, self.sweep())
        self.assertEqual({'event': 1, 'outcome': 1}, self.summaries()[0]['record_counts'])

    @requirements('S14b-2', 'S14b-3')
    def test_oversize_linked_group_stays_atomic_without_starving_later_work(self):
        for i in range(17):
            outcome = {**verification_outcome(f'large-{i}'), 'run_id': 'large-run'}
            self.append(outcome, 'verifier')
            if i:
                self.db.execute('INSERT INTO record_links VALUES(?,?,?,?)',
                                (self.project, f'large-{i-1}', f'large-{i}', 'derived_from'))
        self.append(verification_outcome('small'), 'verifier')
        removed = [self.sweep() for _ in range(4)]
        self.assertEqual(2, sum(removed))
        self.assertLessEqual(max(removed), 32)
        self.assertEqual(34, self.db.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','outcome')").fetchone()[0])
        self.assertFalse(self.ledger._run_retention_more)
        self.append(identity('ordinary-work', START+92*DAY))

    @requirements('S14b-2')
    def test_completion_replay_fence_pins_expired_outcome_and_its_event(self):
        outcome = verification_outcome('fenced-outcome')
        self.append(outcome, 'verifier')
        handle, bridge = '1'*64, 'brg_'+'2'*32
        self.db.execute('INSERT INTO launch_claims VALUES(?,?,?,?,?,?)',
                        (self.project, handle, outcome['run_id'], 'claimed', START, bridge))
        self.db.execute('INSERT INTO active_run_capacity(project_id,handle_digest,run_id,bridge_run_id,kind,'
                        'claimed_at_ms,released_at_ms,release_digest) VALUES(?,?,?,?,?,?,?,?)',
                        (self.project, handle, outcome['run_id'], bridge, 'legacy_unknown', START, START+1, 'a'*64))
        from codex_agents.learning_contracts import content_digest
        self.db.execute('INSERT INTO capacity_completions VALUES(?,?,?,?,?,?,?,?)',
                        (self.project, handle, outcome['run_id'], bridge, outcome['record_id'],
                         content_digest(outcome), START+1, 'b'*64))
        self.assertEqual(0, self.sweep())
        self.assertEqual(2, self.db.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('event','outcome')").fetchone()[0])

    @requirements('S14b-2')
    def test_linked_group_rolls_back_together_and_reconsiders_released_protection(self):
        self.append(verification_outcome('group-outcome'), 'verifier')
        self.db.execute('INSERT INTO owner_attestations VALUES(?,?,?,?)',
                        (self.project, 'group-outcome', b'synthetic', START))
        self.assertEqual(0, self.sweep())
        self.db.execute('DELETE FROM owner_attestations WHERE project_id=?', (self.project,))
        self.db.execute("CREATE TEMP TRIGGER stop_group BEFORE DELETE ON records WHEN OLD.record_id='group-outcome' "
                        "BEGIN SELECT RAISE(ABORT,'stop'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sweep()
        self.assertEqual([], self.summaries())
        self.assertEqual(2, self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.db.execute('DROP TRIGGER stop_group')
        self.assertEqual(2, self.sweep())

    @requirements('S14b-4')
    def test_summary_stays_readable_when_live_writer_vocabulary_shrinks(self):
        from codex_agents import learning_contracts as contracts
        self.append(verification_outcome('old-outcome'), 'verifier')
        self.sweep()
        [summary] = self.summaries()
        with mock.patch.object(contracts, 'RETAINABLE_DETAIL_TYPES', frozenset()), \
                mock.patch.object(contracts, 'OUTCOME_STATUSES', set()), \
                mock.patch.object(contracts, 'SOURCES', set()):
            self.assertEqual(summary, validate_learning_record(summary, stored=True))

    @requirements('S14b-2', 'S14b-3')
    def test_released_capacity_history_does_not_exhaust_retention_work_budget(self):
        from codex_agents import run_retention
        from codex_agents.sql_read_budget import SQLReadLimits
        self.db.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<5000) "
                        "INSERT INTO launch_claims SELECT ?,printf('%064x',i),'history-'||i,'legacy_fenced',1,NULL FROM n",
                        (self.project,))
        self.db.execute("INSERT INTO active_run_capacity(project_id,handle_digest,run_id,kind,claimed_at_ms,released_at_ms,release_digest) "
                        "SELECT project_id,handle_digest,run_id,'legacy_unknown',1,2,? FROM launch_claims", ('a'*64,))
        outcome = {**verification_outcome('old-outcome'), 'run_id': 'not-in-history'}
        self.append(outcome, 'verifier')
        with mock.patch.object(run_retention, 'RUN_RETENTION_READ_LIMITS', SQLReadLimits(64, 25, 4*1_048_576)):
            self.assertEqual(2, self.sweep())

    @requirements('S14b-3')
    def test_collector_maintenance_contains_failure_and_is_the_only_automatic_entry(self):
        self.append(identity('old'))
        server = learning_store.CollectorServer(self.ledger)
        with mock.patch.object(self.ledger, '_clock_ms', return_value=START+91*DAY):
            server._sweep_cycle()
        self.assertEqual(1, len(self.summaries()))
        with mock.patch.object(self.ledger, 'sweep_run_retention', side_effect=sqlite3.OperationalError('interrupted')):
            server._sweep_cycle()
        # A failed cleanup does not poison a subsequent ordinary write.
        self.append(identity('next', START+92*DAY))

    @requirements('S14b-4', 'S14b-5')
    def test_summary_contract_is_closed_and_old_vocabulary_remains_readable(self):
        self.append(identity('old'))
        self.sweep()
        [summary] = self.summaries()
        for changes in ({'record_count': 2}, {'record_count': 33}, {'record_counts': {'invented': 1}}, {'record_counts': None},
                        {'source_counts': {'invented': 1}}, {'extra': 1}, {'record_id_types': [[]]},
                        {'first_observed_at_ms': START+1}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_learning_record({**summary, **changes})
        validate_learning_record(identity('old-schema'))
        validate_learning_record(verification_outcome('old-outcome'))
        # Streamed backups must carry the summary with no new table or schema.
        from codex_agents import project_backup
        path = self.ledger.state.root / 'summary.sqlite3'
        schema = self.db.execute('PRAGMA user_version').fetchone()[0]
        path.touch(mode=0o600)
        project_backup.write(self.db, path, self.project, schema)
        project_backup.verify(path, project_id=self.project, schema_version=schema, compare=self.db)


if __name__ == '__main__':
    unittest.main()
