"""U3: a budgeted read, and a read made on every open, claim or append, does work bounded by live rows, not by
history (ANALYSIS-0.4.0-lifetime-caps.md U3).

A read under a SQL work budget (1 s, or 100,000 callbacks of 256 steps) whose cost grows with history is
refused for good once the history is large enough: a lifetime cap in time instead of count. The standalone measurement uses 1k/100k rows; fast suite cases use 100/2k. Each case
builds the history first (copies of the fixture's own rows, each for an earlier run) and compares the work of
the same product read at 1,000 and at 100,000 history rows: SQLite VM steps of the statements it runs, or the
budget's own callback count, never wall time.

U3-1 an open's preflight (budgeted) finds the run's earlier records by the run, not by reading every
     admission and event of the project.
U3-2 the admission a claim, a dispatch or a result checks is found by its run.
U3-3 an admission's evidence and an appended record's links are found by content digest and by run.
U3-4 the account's recent passing admissions are read from the five-hour window, not from every admission.
U3-5 the project store's terminal conflict checks (result reservation and lease binding) use its unique
     indexes, not every terminal of the project.
U3-6 the admission capture (budgeted) reads only the evidence its rules can keep.
U3-7 a claim's attestation rows, billing waiver and signature capture read the project's attestations only.
U3-8 a registration counts only the cleanup receipts its project can still release.

Integration stage: the original-open ledger and project store fixtures (their set-up starts processes).
"""
from __future__ import annotations

import ast
import re
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT
from tests import test_account_original_open as original_open

from codex_agents.sql_read_budget import SQLReadConnection
from test_framework import requirements

SMALL, LARGE = 100, 2_000
DAY_MS = 24 * 60 * 60 * 1000
# Every record type the fixture's runs wrote to the ledger, copied as history.
HISTORY_TYPES = ('admission_decision', 'dispatch_intent', 'event', 'provider_billing', 'provider_headroom')


def counted(connection, sql, parameters=()):
    """SQLite VM steps of one statement on `connection`."""
    count = [0]

    def tick():
        count[0] += 1
        return 0
    connection.set_progress_handler(tick, 1)
    try:
        connection.execute(sql, parameters).fetchall()
    finally:
        connection.set_progress_handler(None, 0)
    return count[0]


class ReadGrowthTests(unittest.TestCase):
    def setUp(self):
        self.fixture = original_open.OriginalAccountOpenJobTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger = self.fixture.ledger
        self.reference = self.fixture.reference
        self.project = self.reference.base.scope.project_id
        self.store = self.reference.base.store
        self.snapshots = {}
        self.plans = {}
        self.measurements = []
        self.budget_samples = []
        self.enterContext(mock.patch.object(self.ledger, '_clock_ms', return_value=self.ledger._clock_ms()))

    @requirements("U3", why_not_scenario="Direct schema comparison proves additive reopen without a version change.")
    def test_U3_additive_indexes_rebuild_on_reopen_without_changing_rows_or_version(self):
        from codex_agents.learning_store import LearningLedger
        indexes = {'records_run', 'records_type_observed', 'records_foreign_admissions',
                   'account_cleanup_receipts_unreleased', 'records_candidate'}
        connection = self.ledger._connection
        actual = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(indexes <= actual, indexes - actual)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        before = [tuple(row) for row in connection.execute('SELECT * FROM records ORDER BY rowid')]
        for name in indexes:
            connection.execute('DROP INDEX ' + name)
        self.ledger.close()
        reopened = LearningLedger(self.ledger.state, client_capabilities=self.ledger._client_capabilities)
        self.addCleanup(reopened.close)
        db = reopened._connection
        self.assertEqual(version, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM records ORDER BY rowid')])
        actual = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(indexes <= actual)

    # History -----------------------------------------------------------------------------------------------

    def history(self, total, *, claude=False):
        """Copies of the fixture's records until the project holds `total` of them, each for its own earlier
        run (a new run id, record id and content digest) and 30 days old. One statement; the ledger's own
        triggers run, so the admission evidence register grows with them."""
        connection = self.ledger._connection
        with self.ledger._lock, connection:
            have = connection.execute("SELECT COUNT(*) FROM records WHERE project_id=? AND record_id LIKE 'hist:%'",
                                      (self.project,)).fetchone()[0]
            top = connection.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?',
                                     (self.project,)).fetchone()[0]
            marks = ','.join('?' * len(HISTORY_TYPES))
            account = ", '$.provider', 'claude', '$.account_fingerprint', '" + 'c'*64 + "'" if claude else ''
            connection.execute(
                'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?), '
                # Each host event also as an outcome of its run (the fixture's ledger has none).
                "rows AS (SELECT project_id, record_id, record_type, source, observed_at_ms, ingested_at_ms, expires_at_ms, "
                f"content, late FROM records WHERE project_id=? AND record_type IN ({marks}) AND record_id NOT LIKE 'hist:%' AND record_id NOT LIKE 'u3-claude-anchor:%' "
                "UNION ALL SELECT project_id, record_id||':outcome', 'outcome', source, observed_at_ms, ingested_at_ms, "
                "expires_at_ms, content, late FROM records WHERE project_id=? AND record_type='event' AND source='host' "
                "AND record_id NOT LIKE 'hist:%'), "
                'seed AS (SELECT *, ROW_NUMBER() OVER (ORDER BY record_id)-1 AS k, COUNT(*) OVER () AS m FROM rows) '
                "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,ingested_at_ms,expires_at_ms,content,content_digest,late) SELECT s.project_id, 'hist:'||x, s.record_type, s.source, ?+x, "
                's.observed_at_ms-?-x, s.ingested_at_ms-?-x, s.expires_at_ms, '
                f"CAST(json_set(CAST(s.content AS TEXT),'$.run_id','run:hist-'||x{account}) AS BLOB), printf('%064x', x), s.late "
                'FROM n JOIN seed s ON s.k = x % s.m',
                (have + 1, total, self.project, *HISTORY_TYPES, self.project, top, 30 * DAY_MS, 30 * DAY_MS))

    def store_history(self, total):
        """Delivered terminals in the project store until it holds `total`, each with its own record (the
        outbox and the records are kept for good)."""
        connection = self.store._connection
        seed = connection.execute('SELECT * FROM terminal_outbox LIMIT 1').fetchone()
        self.assertIsNotNone(seed)
        have = connection.execute("SELECT COUNT(*) FROM terminal_outbox WHERE claim_id LIKE 'hist-%'").fetchone()[0]
        top = connection.execute('SELECT MAX(source_sequence), MAX(observed_at_ms) FROM records').fetchone()
        delivery = connection.execute('SELECT MAX(delivery_sequence) FROM terminal_outbox').fetchone()[0]
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute(
                'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                "INSERT INTO records SELECT r.project_id, 'hist:'||x, r.record_type, r.source, ?+x, r.observed_at_ms, "
                "r.ingested_at_ms, r.content, printf('%064x', x), r.late FROM n, records r "
                'WHERE r.project_id=? AND r.record_id=?',
                (have + 1, total, top[0], self.project, seed['record_id']))
            connection.execute(
                'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                "INSERT INTO terminal_outbox SELECT project_id, 'hist-'||x, ?+x, 'hrn_hist_'||x, attempt, "
                "printf('%064x', x), binding, 'hist:'||x, record_content_digest, result_digest, printf('%064x', x) "
                'FROM n, terminal_outbox WHERE project_id=? AND claim_id=?',
                (have + 1, total, delivery, self.project, seed['claim_id']))
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def copy_metadata_history(self, table, total, overrides):
        """Synthetic unrelated terminal metadata, preserving current schema columns.

        Only parent identities/seals of the fixture copies are synthetic. These
        rows are never offered as current authorization or decoded as receipts.
        """
        db = self.ledger._connection
        have = getattr(self, '_metadata_counts', {}).get(table, 0)
        columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
        seed = db.execute('SELECT * FROM ' + table + ' LIMIT 1').fetchone()
        self.assertIsNotNone(seed, table)
        expressions = [overrides.get(column, 's.' + column) for column in columns]
        with self.ledger._lock, db:
            db.execute('WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                       'INSERT INTO ' + table + '(' + ','.join(columns) + ') SELECT ' + ','.join(expressions)
                       + ' FROM n CROSS JOIN (SELECT * FROM ' + table + ' LIMIT 1) s', (have+1, total))
        if not hasattr(self, '_metadata_counts'):
            self._metadata_counts = {}
        self._metadata_counts[table] = total

    # Work --------------------------------------------------------------------------------------------------

    def statements(self, action):
        """The statements `action` runs on the ledger connection, with their parameters."""
        seen = []
        real = SQLReadConnection.execute
        ledger = self.ledger._connection

        def execute(connection, sql, parameters=(), /):
            if connection is ledger:
                seen.append((sql, tuple(parameters) if not isinstance(parameters, dict) else dict(parameters)))
            return real(connection, sql, parameters)
        with mock.patch.object(SQLReadConnection, 'execute', execute):
            self.last_action_result = action()
        return seen

    def steps(self, action, matching):
        """VM steps of every SELECT `action` runs whose text matches `matching`, re-run on a read-only
        connection with the same parameters."""
        chosen = [(sql, parameters) for sql, parameters in self.statements(action)
                  if sql.lstrip().upper().startswith(('SELECT', 'WITH')) and re.search(matching, sql)]
        self.assertTrue(chosen, matching)
        # Keep the owner's connection: P9 membership is a TEMP projection.
        return self.measure_queries(self.ledger._connection, chosen)

    def measure_queries(self, connection, queries):
        total = 0
        for sql, parameters in queries:
            key = (sql, repr(parameters))
            plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters)]
            self.plans[key] = plan
            # Production owner limits apply independently of VM instrumentation.
            started = time.monotonic()
            read = lambda: connection.execute(sql, parameters).fetchall()
            if isinstance(connection, SQLReadConnection):
                fetched = connection.read_in_current_mode(self.reference.work, read)
            else:
                fetched = read()  # Project terminal store uses FencedConnection.
            elapsed = time.monotonic()-started
            self.assertLess(elapsed, self.reference.work.max_duration_ms / 1000)
            self.budget_samples.append({'seconds': elapsed, 'query_budget_passed': True})
            rows = [tuple(row) for row in fetched]
            if key in self.snapshots:
                self.assertEqual(self.snapshots[key], rows, 'answer changed as unrelated history grew')
            self.snapshots[key] = rows
            total += counted(connection, sql, parameters)
        return total

    def callbacks(self, action):
        """The budget's callbacks (one per 256 VM steps) of every budgeted read `action` makes."""
        seen = []
        real = SQLReadConnection._restore_read_settings

        def restore(connection, prior, scope, **options):
            seen.append(scope.callbacks)
            return real(connection, prior, scope, **options)
        started = time.monotonic()
        with mock.patch.object(SQLReadConnection, '_restore_read_settings', restore):
            statements = self.statements(action)
        elapsed = time.monotonic() - started
        self.assertTrue(seen)
        queries = [(sql, params) for sql, params in statements
                   if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
        self.measure_queries(self.ledger._connection, queries)
        self.budget_samples.append({'callbacks': sum(seen), 'seconds': elapsed})
        return sum(seen)

    def assertFlat(self, small, large, slack):
        self.measurements.append({'small': small, 'large': large, 'slack': slack})
        self.assertLessEqual(large, 2 * small + slack, f'work grew with history: {small} at {SMALL}, {large} at {LARGE}')

    def a_run(self, kind):
        return self.ledger._connection.execute(
            "SELECT json_extract(CAST(content AS TEXT),'$.run_id') FROM records WHERE project_id=? AND record_type=? "
            "AND record_id NOT LIKE 'hist:%' LIMIT 1", (self.project, kind)).fetchone()[0]

    # Tests -------------------------------------------------------------------------------------------------

    @requirements("U3", why_not_scenario="Synthetic terminal metadata isolates indexed provenance and open reads.")
    def test_U3_10_open_and_provenance_ignore_released_account_history(self):
        identity = {"handle_digest": "printf('e%063x',x)", "run_id": "'run:u3-hist-'||x"}
        def grow(total):
            self.history(total)
            self.copy_metadata_history('host_run_bindings', total, {
                **identity, 'binding_state': "'terminal'", 'consumed_at_ms': 's.created_at_ms'})
            self.copy_metadata_history('launch_claims', total, {
                **identity, 'bridge_run_id': "printf('brg_%032x',x)"})
            self.copy_metadata_history('active_run_capacity', total, {
                **identity, 'bridge_run_id': "printf('brg_%032x',x)", 'released_at_ms': 's.claimed_at_ms',
                'release_digest': "'" + 'f'*64 + "'"})
            # The ordinary source triggers create released run_holds tombstones.
            self.assertEqual(0, self.ledger._connection.execute(
                "SELECT COUNT(*) FROM run_holds WHERE identity LIKE 'e%' AND state<>'released'").fetchone()[0])
        proof_owner = self.reference.base.worker.base
        grow(SMALL)
        small = self.callbacks(proof_owner.proof)
        expected = self.last_action_result
        grow(LARGE)
        large = self.callbacks(proof_owner.proof)
        self.assertEqual(expected, self.last_action_result)
        self.assertFlat(small, large, 8)

    @requirements("U3", why_not_scenario="Project registration reads are measured without constructing irrelevant drivers.")
    def test_U3_11_project_registration_ignores_other_orchestration_history(self):
        db = self.store._connection
        def grow(total):
            have = db.execute("SELECT COUNT(*) FROM project_registered_runs WHERE run_id LIKE 'run:u3-hist-%'").fetchone()[0]
            with db:
                db.execute('WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                    "INSERT INTO project_registered_runs SELECT ?, 'hrn_u3_hist_'||x, 1, 'orc_u3_old_'||x, "
                    "'run:u3-hist-'||x, 'old-service', 1, 'service_stopped', 2 FROM n", (have+1, total, self.project))
        tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents/project_history.py').read_text())
        owner = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'record_registered_run')
        queries = [n.value for n in ast.walk(owner) if isinstance(n, ast.Constant) and type(n.value) is str
                   and n.value.startswith('SELECT')]
        self.assertEqual(2, len(queries))
        binding = self.reference.base.binding
        probes = {'project_id': self.project, 'run_handle': 'hrn_u3_absent', 'attempt': 1,
                  'orchestration_handle': binding.orchestration_handle}
        def work():
            return self.measure_queries(db, [(sql, tuple(probes[key] for key in re.findall(r'(\w+)=\?', sql)))
                                             for sql in queries])
        grow(SMALL)
        small = work()
        grow(LARGE)
        large = work()
        self.assertFlat(small, large, 100)

    @requirements("U3", why_not_scenario="Probe owner SQL is replayed with a fixed live set and retained completion history.")
    def test_U3_12_completed_probe_history_keeps_owner_reads_bounded(self):
        from tests.test_account_billing_admission import AccountBillingAdmissionTests
        probe = AccountBillingAdmissionTests(methodName='runTest')
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        self.ledger, self.project = probe.ledger, probe.fixture.project_id
        probe.reserve()
        def grow(total):
            identity = {'request_key': "printf('e%063x',x)"}
            self.copy_metadata_history('account_billing_probe_origins', total, identity)
            self.copy_metadata_history('account_billing_probe_requests', total, {
                **identity, 'state': "'committed'", 'references_json': "'{}'"})
        grow(SMALL)
        statements = self.statements(lambda: probe.reserve('u3-live'))
        queries = [(sql, parameters) for sql, parameters in statements
                   if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
        self.assertGreater(len(queries), 4)
        small = self.measure_queries(self.ledger._connection, queries)
        grow(LARGE)
        large = self.measure_queries(self.ledger._connection, queries)
        self.assertFlat(small, large, 200)

    @requirements("U3", why_not_scenario="Claude account-skip queries need fixed historical anchor rows and exact VM work.")
    def test_U3_13_claude_first_pass_and_latest_older_readings_skip_account_history(self):
        db = self.ledger._connection
        now = self.ledger._clock_ms()
        import hashlib
        import json
        from codex_agents.learning_contracts import canonical_json
        anchors = set()
        for kind, age in (('admission_decision', 99), ('provider_billing', 2), ('provider_headroom', 2)):
            top = db.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()[0]
            seed = db.execute('SELECT content,source FROM records WHERE project_id=? AND record_type=? LIMIT 1',
                              (self.project, kind)).fetchone()
            record = json.loads(bytes(seed['content']))
            identifier = 'u3-claude-anchor:' + kind
            anchors.add(identifier)
            record.update(record_id=identifier, provider='claude', account_fingerprint='c'*64,
                          observed_at_ms=now-age*DAY_MS)
            content = canonical_json(record)
            with db:
                db.execute(
                    'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                    'ingested_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,0)',
                    (self.project, identifier, kind, seed['source'], top+1, now-age*DAY_MS,
                     now-age*DAY_MS, content, hashlib.sha256(content).hexdigest()))
        from codex_agents.admission_inputs import AdmissionCaptureLimits
        views = self.fixture.jobs._views
        def capture():
            return views.capture_admission_inputs(self.project, 'codex', views._claim_policy('codex'),
                                                  AdmissionCaptureLimits(4096, 2*1_048_576), self.reference.work)
        self.history(SMALL, claude=True)
        small = self.callbacks(capture)
        self.assertTrue(anchors <= {row[2] for row in self.last_action_result.rows})
        self.history(LARGE, claude=True)
        large = self.callbacks(capture)
        self.assertTrue(anchors <= {row[2] for row in self.last_action_result.rows})
        self.assertFlat(small, large, 8)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_1_an_open_preflight_reads_only_this_runs_records_as_history_grows(self):
        """U3-1: the preflight's check that no earlier record names the new run reads by the run. At 0.8.38
        it read every admission decision and event of the project (json_extract on each) inside the open's
        SQL work budget, so a long-lived project was refused for good."""
        self.history(SMALL)
        small = self.callbacks(lambda: self.reference.preflight())
        # Restore the same live set before measuring the same fresh open again.
        self.ledger._connection.execute('DELETE FROM account_open_requests WHERE project_id=? AND run_id=?',
                                        (self.project, self.last_action_result[0].run_id))
        self.history(LARGE)
        large = self.callbacks(lambda: self.reference.preflight())
        self.assertFlat(small, large, 8)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_2_a_runs_admission_is_found_by_its_run_as_history_grows(self):
        """U3-2: the admission a claim, a dispatch intent and a result record check (one per run)."""
        run_id = self.a_run('admission_decision')

        def admission():
            with self.ledger._lock:
                self.ledger._run_admission_locked(self.project, run_id)
        self.history(SMALL)
        small = self.steps(admission, r"record_type='admission_decision'")
        self.history(LARGE)
        large = self.steps(admission, r"record_type='admission_decision'")
        self.assertFlat(small, large, 200)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_3_evidence_and_links_are_found_by_digest_and_run_as_history_grows(self):
        """U3-3: an admission's evidence check and an append's derivation links (by content digest, and a
        feedback's outcomes by run) read only the matching records."""
        def links():
            with self.ledger._lock:
                self.ledger._auto_link_locked(self.project, {
                    'record_type': 'qa_evidence', 'record_id': 'u3-probe', 'evaluation_assignment_digest': 'f' * 64,
                    'case_digest': 'e' * 64, 'host_outcome_digest': 'd' * 64, 'verifier_host_outcome_digest': None})
                self.ledger._auto_link_locked(self.project, {
                    'record_type': 'feedback', 'record_id': 'u3-probe', 'run_id': 'run:absent'})
        self.history(SMALL)
        small = self.steps(links, r'FROM records')
        self.history(LARGE)
        large = self.steps(links, r'FROM records')
        self.assertFlat(small, large, 400)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_4_recent_passing_admissions_are_read_from_their_window_as_history_grows(self):
        """U3-4: the account's admission cap reads the passing admissions of the last five hours, in every
        project, from that window (history is 30 days old here and never counts)."""
        now = self.ledger._clock_ms()

        def recent():
            with self.ledger._lock:
                self.ledger._prior_run_admissions_locked('codex', now)
        self.history(SMALL)
        small = self.steps(recent, r"record_type='admission_decision'")
        self.history(LARGE)
        large = self.steps(recent, r"record_type='admission_decision'")
        self.assertFlat(small, large, 200)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_6_the_admission_capture_reads_only_evidence_that_can_still_count(self):
        """U3-6: the admission capture of an open, a claim or a probe (budgeted) reads the evidence its rules
        keep (the last 5 hours of decisions, the last day of billing and headroom, live headroom windows, and
        Claude's first pass and latest older readings per account), not every decision and reading the ledger
        holds. At 0.8.38 two of its branches read every admission decision and every billing and headroom
        reading of every project, and charged each against the capture's work budget."""
        from codex_agents.admission_inputs import (AdmissionCaptureLimits, MAX_ADMISSION_CAPTURE_BYTES,
                                                   MAX_ADMISSION_CAPTURE_ROWS)
        views = self.fixture.jobs._views
        limits = AdmissionCaptureLimits(MAX_ADMISSION_CAPTURE_ROWS, MAX_ADMISSION_CAPTURE_BYTES)

        def capture():
            return views.capture_admission_inputs(self.project, 'codex', views._claim_policy('codex'), limits,
                                           self.reference.work)
        self.history(SMALL)
        small = self.callbacks(capture)
        self.history(LARGE)
        large = self.callbacks(capture)
        self.assertFlat(small, large, 8)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_7_owner_attestations_are_read_by_type_as_history_grows(self):
        """U3-7: a claim's attestation check, its billing waiver and the signature capture (budgeted) read the
        project's billing attestations, not every record of the project in sequence order."""
        from codex_agents.signature_inputs import (MAX_SIGNATURE_CAPTURE_BYTES, MAX_SIGNATURE_CAPTURE_ROWS,
                                                   SignatureCaptureLimits)
        views = self.fixture.jobs._views
        limits = SignatureCaptureLimits(MAX_SIGNATURE_CAPTURE_ROWS, MAX_SIGNATURE_CAPTURE_BYTES)

        def attestations():
            with self.ledger._lock:
                self.ledger._attestation_rows_locked(self.project, 'codex')
                self.ledger._provider_records_locked(self.project, 'billing_attestation', 'codex')

        def work():
            return (self.steps(attestations, r"billing_attestation"),
                    self.callbacks(lambda: views.capture_signature_inputs(self.project, 'codex', limits,
                                                                          self.reference.work)))
        self.history(SMALL)
        small_steps, small_callbacks = work()
        self.history(LARGE)
        large_steps, large_callbacks = work()
        self.assertFlat(small_steps, large_steps, 200)
        self.assertFlat(small_callbacks, large_callbacks, 8)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_8_a_registration_counts_only_unreleased_cleanup_receipts_as_history_grows(self):
        """U3-8: registering a run's execution (once per claim) counts the project's cleanup receipts it can
        still release against MAX_RELEASE_RESERVATIONS; released ones are kept for good and must not be read."""
        from codex_agents.account_cleanup import live_cleanup_receipts
        connection = self.ledger._connection

        def released(total):
            """Released copies of the fixture's receipt until the project holds `total` (their parents are not
            copied, so foreign keys are off while they are written)."""
            have = connection.execute("SELECT COUNT(*) FROM account_cleanup_receipts WHERE run_id LIKE 'run:hist-%'"
                                      ).fetchone()[0]
            with self.ledger._lock:
                connection.execute('PRAGMA foreign_keys=OFF')
                try:
                    with connection:
                        connection.execute(
                            'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                            "INSERT INTO account_cleanup_receipts SELECT c.project_id, printf('%064x', x), 'run:hist-'||x, "
                            "'brg_hist_'||x, c.registration_digest, c.lease_digest, c.binding_digest, printf('h%063x', x), "
                            'c.terminal_record_id, c.terminal_content_digest, c.result_digest, c.terminal_receipt_digest, '
                            'c.terminal_recorded_at_ms, c.cleanup_digest, 1, c.receipt_digest, c.state_seal '
                            "FROM n, (SELECT * FROM account_cleanup_receipts WHERE project_id=? AND run_id NOT LIKE "
                            "'run:hist-%' LIMIT 1) c", (have + 1, total, self.project))
                finally:
                    connection.execute('PRAGMA foreign_keys=ON')

        def count():
            with self.ledger._lock:
                live_cleanup_receipts(self.ledger, self.project)
        released(SMALL)
        small = self.steps(count, r'account_cleanup_receipts')
        released(LARGE)
        large = self.steps(count, r'account_cleanup_receipts')
        self.assertFlat(small, large, 200)

    @requirements("U3", why_not_scenario="The append-lineage SQL seam allows deterministic work measurement.")
    def test_U3_9_candidate_lineage_looks_up_only_the_named_candidate(self):
        from tests.learning_support import candidate
        from codex_agents.learning_contracts import canonical_json
        connection = self.ledger._connection
        record = candidate()
        content = canonical_json(record)
        def grow(total):
            have = connection.execute("SELECT COUNT(*) FROM records WHERE record_id LIKE 'candidate-hist:%'").fetchone()[0]
            top = connection.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()[0]
            with self.ledger._lock, connection:
                connection.execute(
                    'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                    'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                    'ingested_at_ms,content,content_digest,late) '
                    "SELECT ?, 'candidate-hist:'||x, 'candidate', 'host', ?+x, 1, 1, "
                    "CAST(json_set(CAST(? AS TEXT),'$.candidate_digest',printf('%064x',x)) AS BLOB), "
                    "printf('c%063x',x), 0 FROM n", (have+1, total, self.project, top, content))
        def link():
            with self.ledger._lock:
                self.ledger._auto_link_locked(self.project, {'record_type': 'evaluation_assignment',
                    'record_id': 'u3-absent', 'candidate_digest': 'f'*64, 'case_ids': []})
                target = self.ledger._connection.execute(
                    "SELECT record_id FROM records WHERE project_id=? AND record_type='event' LIMIT 1", (self.project,)).fetchone()[0]
                self.ledger._auto_link_locked(self.project, {'record_type': 'evaluation_assignment',
                    'record_id': target, 'candidate_digest': f'{50:064x}', 'case_ids': []})
        grow(SMALL)
        small = self.steps(link, "record_type='candidate'")
        grow(LARGE)
        large = self.steps(link, "record_type='candidate'")
        self.assertFlat(small, large, 100)

    @requirements("U3", why_not_scenario="SQL query-plan and VM-work assertions require direct access to disposable store connections.")
    def test_U3_5_the_project_stores_terminal_conflict_checks_use_its_unique_indexes(self):
        """U3-5: reserving a result and binding a lease each check that no terminal holds the claim, run attempt
        or claim digest. With the OR inside `project_id=? AND (...)` SQLite read every terminal of the
        project, and terminals are kept for good."""
        queries = terminal_conflict_queries()
        self.assertGreaterEqual(len(queries), 2)
        binding = self.reference.base.binding
        probes = {'project_id': self.project, 'claim_id': 'brg_absent', 'run_handle': 'hrn_absent', 'attempt': 1,
                  'claim_digest': 'c' * 64}

        def work():
            return self.measure_queries(self.store._connection,
                                        [(query, tuple(probes[name] for name in re.findall(r'(\w+)=\?', query)))
                                         for query in queries])
        self.assertTrue(binding.claim_id)
        self.store_history(SMALL)
        small = work()
        self.store_history(LARGE)
        large = work()
        self.assertFlat(small, large, 100)


def terminal_conflict_queries():
    """Every terminal_outbox existence check in the project store that names a claim, attempt or digest."""
    tree = ast.parse((Path(ORCHESTRATION_ROOT) / 'codex_agents' / 'project_history.py').read_text(encoding='utf-8'))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith('SELECT 1 FROM terminal_outbox WHERE') and 'claim_digest=?' in node.value]


if __name__ == '__main__':
    unittest.main()
