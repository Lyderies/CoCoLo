"""LOCKHOLD: bounded collector batches and indexed reads, measured by rows/VM work.

Integration fixtures generate owner keys with ssh-keygen; no live services or data.
"""
from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest import mock

from tests import test_event_retention as retention
from tests import test_learning_store as legacy
from tests.learning_support import (
    DIGEST_A, DIGEST_B, DIGEST_C, admit_provider, assignment, event_record,
    fake_provider_tools, routing_fields,
)
from codex_agents import learning_store
from codex_agents.learning_contracts import content_digest
from test_framework import requirements


def measured(db, call):
    steps = [0]
    original = db.set_progress_handler

    def tick():
        steps[0] += 1
        return 0
    def install(callback, interval):
        # Preserve the product's own budget and foreign-handler flag. The base
        # SQLite hook measures without making slot health refuse a foreign reader.
        original(callback, interval)
        ticks = [0]

        def combined():
            tick()
            ticks[0] += 1
            if callback is not None and interval > 0 and ticks[0] >= interval:
                ticks[0] = 0
                return callback()
            return 0
        sqlite3.Connection.set_progress_handler(db, combined, 1)
    sqlite3.Connection.set_progress_handler(db, tick, 1)
    with mock.patch.object(db, 'set_progress_handler', side_effect=install):
        try:
            answer = call()
        finally:
            sqlite3.Connection.set_progress_handler(db, None, 0)
    return steps[0], answer


def former_provider_records(ledger, project, kind, provider):
    """Independent former query, including sequence order and owner verification.

    Pin the former sequence-index plan so it also remains a growing negative control.
    """
    rows = ledger._connection.execute(
        'SELECT content,content_digest,source FROM records INDEXED BY sqlite_autoindex_records_2 '
        "WHERE project_id=? AND record_type=? AND json_extract(CAST(content AS TEXT),'$.provider')=? "
        'ORDER BY source_sequence', (project, kind, provider)).fetchall()
    answer = []
    for row in rows:
        record = json.loads(bytes(row['content']))
        if kind == 'billing_attestation' and (
                row['source'] != 'owner' or not ledger._attestation_signed_by_owner_locked(project, record)):
            continue
        answer.append((record, str(row['content_digest'])))
    return answer


class HoldCounter:
    """Count rows removed in each outermost RLock hold, including recursive holds."""
    def __init__(self, lock):
        self.lock, self.depth, self.rows, self.holds = lock, 0, 0, []

    def acquire(self, *args, **kwargs):
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.depth += 1
        return acquired

    def release(self):
        self.depth -= 1
        if self.depth == 0:
            self.holds.append(self.rows)
            self.rows = 0
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


@requirements('LOCKHOLD-1', why_not_scenario='Real ledger batches; direct cycles expose each lock boundary and transaction.')
class LockHoldRetentionTests(unittest.TestCase):
    def setUp(self):
        self.base = retention.EventRetentionTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.ledger, self.db = self.base.ledger, self.base.connection
        self.now = 1_000_000_000_000
        self.base._seed(2501, self.now)
        self.enterContext(mock.patch.object(self.ledger, '_clock_ms', return_value=self.now + 8 * retention.DAY))

    def rows(self):
        return [tuple(row) for row in self.db.execute('SELECT * FROM records ORDER BY project_id,source_sequence')]

    @contextmanager
    def counted_holds(self):
        counter = HoldCounter(self.ledger._lock)
        real = self.ledger._summarise_and_remove

        def remove(groups, now):
            count = real(groups, now)
            self.assertFalse(self.db.in_transaction)
            counter.rows += count
            return count
        with mock.patch.object(self.ledger, '_lock', counter), \
                mock.patch.object(self.ledger, '_summarise_and_remove', side_effect=remove):
            yield counter

    def test_LOCKHOLD_1_backlog_drains_across_bounded_holds_with_identical_final_state(self):
        # Former full drain, with exactly the same 500-row transaction boundaries.
        initial = self.rows()
        while self.ledger.sweep_event_retention() >= learning_store.EVENT_SWEEP_BATCH:
            pass
        expected = self.rows()
        with self.db:
            self.db.execute('DELETE FROM records')
            self.db.executemany('INSERT INTO records VALUES(' + ','.join('?' for _ in initial[0]) + ')', initial)
        server = learning_store.CollectorServer(self.ledger)
        with self.counted_holds() as counter:
            for cycle, remaining in enumerate((2001, 1501, 1001, 501, 1, 0)):
                outcome = server._sweep_cycle(holds_only=cycle > 0)
                self.assertEqual(remaining, len(self.base._raw_ids()))
                self.assertEqual('more' if remaining else None, outcome)
                self.assertFalse(self.db.in_transaction)
                self.assertEqual(0, counter.depth)
        self.assertEqual([500, 500, 500, 500, 500, 1], [n for n in counter.holds if n])
        self.assertEqual(expected, self.rows())
        self.assertEqual(2501, sum(s['event_count'] for s in self.base._summaries()))

    def test_LOCKHOLD_1_failed_batch_rolls_back_and_restarts(self):
        server = learning_store.CollectorServer(self.ledger)
        self.assertEqual('more', server._sweep_cycle())
        before = self.rows()
        self.db.execute("CREATE TEMP TRIGGER stop_batch BEFORE DELETE ON records "
                        "WHEN OLD.record_id='m750' BEGIN SELECT RAISE(ABORT,'stopped'); END")
        try:
            self.assertEqual('more', server._sweep_cycle(holds_only=True))
            self.assertEqual(before, self.rows())
            self.assertFalse(self.db.in_transaction)
        finally:
            self.db.execute('DROP TRIGGER stop_batch')
        # Pending work is in the durable raw rows, so a new collector also resumes it.
        restarted = learning_store.CollectorServer(self.ledger)
        with self.counted_holds() as counter:
            for _ in range(6):
                restarted._sweep_cycle()
        self.assertEqual(set(), self.base._raw_ids())
        self.assertEqual(2501, sum(s['event_count'] for s in self.base._summaries()))
        self.assertTrue(all(n <= 500 for n in counter.holds), counter.holds)

    def test_LOCKHOLD_1_loop_resumes_batches_and_stop_cancels_the_gap(self):
        server = learning_store.CollectorServer(self.ledger)
        clock, waits = [0.0], []

        def wait(seconds):
            self.assertEqual(0, counter.depth, 'batch gap must release the ledger')
            waits.append(seconds)
            clock[0] += seconds
            return len(waits) > 6
        with self.counted_holds() as counter, \
                mock.patch.object(learning_store.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 1.0), \
                mock.patch.object(learning_store, 'SWEEP_IDLE_CHECK_SECONDS', 1.0), \
                mock.patch.object(learning_store, 'SWEEP_BATCH_GAP_SECONDS', 0.01), \
                mock.patch.object(server._sweeps_stop, 'wait', side_effect=wait), \
                mock.patch.object(server, '_run_health_pass'):
            server._sweep_loop()
        self.assertEqual(set(), self.base._raw_ids())
        self.assertEqual([500, 500, 500, 500, 500, 1], [n for n in counter.holds if n])
        self.assertEqual([0.01] * 5, waits[1:6])

        # Stop during the first gap leaves the rest for a later start.
        self.db.execute('DELETE FROM records')
        self.base._seed(1501, self.now)
        server = learning_store.CollectorServer(self.ledger)
        clock[0], waits[:] = 0.0, []

        def stop_wait(seconds):
            waits.append(seconds)
            clock[0] += seconds
            return len(waits) == 2
        with mock.patch.object(learning_store.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', 1.0), \
                mock.patch.object(learning_store, 'SWEEP_IDLE_CHECK_SECONDS', 1.0), \
                mock.patch.object(server._sweeps_stop, 'wait', side_effect=stop_wait), \
                mock.patch.object(server, '_run_health_pass'):
            server._sweep_loop()
        self.assertEqual(1001, len(self.base._raw_ids()))


@requirements('LOCKHOLD-2', why_not_scenario='Real signed evidence and collector dispatch; VM steps isolate database work.')
class LockHoldReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger, self.project = self.fixture.ledger, self.fixture.project_a_id
        self.db = self.ledger._connection
        self.now = self.ledger._clock_ms()
        self.enterContext(mock.patch.object(self.ledger, '_clock_ms', return_value=self.now))
        self.enterContext(mock.patch.object(learning_store.time, 'time_ns', return_value=self.now * 1_000_000))
        self.server = learning_store.CollectorServer(self.ledger)

    def admit(self, provider='codex'):
        tools = self.fixture.project_a.parent / 'lockhold-tools'
        if not tools.exists():
            fake_provider_tools(tools)
        return admit_provider(self.ledger, self.project, self.fixture.owner_private_key,
                              provider, tools=tools, observed_at_ms=self.now)

    def history(self, total):
        if not self.db.execute("SELECT 1 FROM records WHERE record_id='seed'").fetchone():
            self.ledger.append(self.project, event_record('seed', observed_at_ms=self.now), producer='hook', now_ms=self.now)
        have, top = self.db.execute(
            "SELECT COUNT(CASE WHEN record_id LIKE 'hist:%' THEN 1 END), MAX(source_sequence) "
            'FROM records WHERE project_id=?', (self.project,)).fetchone()
        with self.db:
            self.db.execute(
                'WITH RECURSIVE n(x) AS (SELECT ? UNION ALL SELECT x+1 FROM n WHERE x<?) '
                'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                'ingested_at_ms,expires_at_ms,content,content_digest,late) '
                'SELECT project_id, \'hist:\'||x, record_type, source, ?+x, '
                'observed_at_ms, ingested_at_ms, expires_at_ms, content, content_digest, late '
                "FROM n, records WHERE project_id=? AND record_id='seed'",
                (have + 1, total, top, self.project))

    def health(self):
        return self.server._dispatch({'protocol': learning_store.COLLECTOR_PROTOCOL_VERSION,
                                     'op': 'health', 'project_root': str(self.fixture.project_a)})

    def test_LOCKHOLD_2_status_and_project_health_have_flat_vm_work(self):
        self.admit()
        samples = []
        for total in (100, 2100):
            self.history(total)
            status_steps, status = measured(self.db, lambda: self.ledger.provider_attestation_status(self.project))
            health_steps, health = measured(self.db, self.health)
            with mock.patch.object(self.ledger, '_provider_records_locked',
                                   side_effect=lambda *a: former_provider_records(self.ledger, *a)):
                old_steps, old_status = measured(self.db, lambda: self.ledger.provider_attestation_status(self.project))
                old_health = self.health()
            self.assertEqual(old_status, status)
            self.assertEqual(old_health, health)
            self.assertTrue(health['ok'])
            self.assertNotIn('ledger_busy', health)
            self.assertEqual(status, health['provider_attestations'])
            samples.append((status_steps, health_steps, old_steps))
        self.assertGreater(samples[1][2], 5 * samples[0][2], samples)
        for column in (0, 1):
            self.assertLess(samples[1][column], 1.25 * samples[0][column] + 200, samples)

    def test_LOCKHOLD_2_status_answers_match_former_lookup(self):
        def check(codex, claude):
            expected = {'codex': {'state': codex}, 'claude': {'state': claude}}
            self.assertEqual(expected, self.ledger.provider_attestation_status(self.project))
            with mock.patch.object(self.ledger, '_provider_records_locked',
                                   side_effect=lambda *a: former_provider_records(self.ledger, *a)):
                self.assertEqual(expected, self.ledger.provider_attestation_status(self.project))
        check('identity_missing', 'identity_missing')
        self.admit()
        check('present', 'identity_missing')
        self.admit('claude')
        check('present', 'present')
        signatures = list(self.db.execute('SELECT project_id,subject_record_id,signature FROM owner_attestations'))
        self.db.execute("UPDATE owner_attestations SET signature=?", (b'broken',))
        check('missing', 'missing')
        self.db.executemany('UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?',
                            [(r[2], r[0], r[1]) for r in signatures])
        check('present', 'present')
        self.db.execute("DELETE FROM records WHERE project_id=? AND record_type='provider_identity' "
                        "AND json_extract(CAST(content AS TEXT),'$.provider')='claude'", (self.project,))
        check('present', 'identity_missing')

    @requirements('LOCKHOLD-3', why_not_scenario='Actual host opens and their preflight/write-time admission under growing history.')
    def test_LOCKHOLD_3_open_admission_has_flat_vm_work_and_same_decision(self):
        self.admit()
        assigned = assignment()
        task = {'id': 'task-1', 'objective': 'Read one fixture', 'family': 'research',
                'complexity': 'medium', 'risk': 'low', 'meaningful': True, **routing_fields(),
                'deterministic_validation': True, 'independent_parts': 1, 'parallel_safe': False,
                'failed_attempts': 0, 'failure_events': []}
        configuration = {'model': assigned['model'], 'effort': assigned['effort'], 'role': assigned['role'],
                         'topology': 'single', 'max_parallel': 1, 'verification_required': True,
                         'verifier_digest': DIGEST_A, 'bounds_digest': DIGEST_B, 'package_digest': DIGEST_A,
                         'policy_digest': DIGEST_B, 'agent_digest': DIGEST_C, 'skill_digests': [DIGEST_A],
                         'prompt_digest': DIGEST_B, 'client_digest': content_digest(self.fixture.clients['codex-host-v1'])}
        route = {'task_id': task['id'], 'role': assigned['role'], 'model': assigned['model'],
                 'effort': assigned['effort'], 'topology': 'single', 'max_parallel': 1,
                 'verification_required': True, 'execution_configuration': configuration}
        orchestration = self.ledger.open_orchestration(self.project, client_version='codex-host-v1', now_ms=self.now)
        samples, decisions = [], []
        for total in (100, 2100):
            self.history(total)
            expected = self.ledger.evaluate_admission(self.project, provider='codex', environ={}, now_ms=self.now)
            with mock.patch.object(self.ledger, '_provider_records_locked',
                                   side_effect=lambda *a: former_provider_records(self.ledger, *a)):
                old_steps, former = measured(self.db, lambda: self.ledger.evaluate_admission(
                    self.project, provider='codex', environ={}, now_ms=self.now))
            self.assertEqual(former, expected)
            admission_steps = []
            original = self.ledger._evaluate_admission_locked

            def evaluate(*args, **kwargs):
                steps, decision = measured(self.db, lambda: original(*args, **kwargs))
                admission_steps.append(steps)
                self.assertEqual(expected, decision)
                return decision
            with mock.patch.object(self.ledger, '_evaluate_admission_locked', side_effect=evaluate):
                opened = self.ledger.open_host_run(
                    self.project, assignment=assigned, task=task, route=route,
                    execution_configuration=configuration, client_version='codex-host-v1',
                    orchestration_handle=orchestration['orchestration_handle'], now_ms=self.now)
            self.assertTrue(opened['run_handle'])
            self.assertEqual(2, len(admission_steps))  # preflight plus write-time recheck
            decisions.append(expected)
            samples.append((admission_steps, old_steps))
            # Keep the live admission/capacity set fixed across samples.
            self.db.execute("DELETE FROM records WHERE project_id=? AND record_type='admission_decision'", (self.project,))
        self.assertEqual(decisions[0], decisions[1])
        self.assertGreater(samples[1][1], 5 * samples[0][1], samples)
        for small, large in zip(samples[0][0], samples[1][0]):
            self.assertLess(large, 1.25 * small + 200, samples)


if __name__ == '__main__':
    unittest.main()
