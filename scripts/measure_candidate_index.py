#!/usr/bin/env python3
"""Opt-in AP3 measurement on a disposable ~300 MiB ledger, outside test discovery.

Run from the repository root: $PY -B scripts/measure_candidate_index.py
Uses the existing isolated account-ledger fixture; never opens an owner ledger.
"""
import json
from pathlib import Path
import sys
import time
from unittest import mock


def measure(self):
    from codex_agents.learning_store import LearningLedger
    from tests.learning_support import event_record

    connection = self.ledger._connection
    connection.execute('DROP INDEX records_project_digest')
    page_size = connection.execute('PRAGMA page_size').fetchone()[0]
    started = time.perf_counter()
    count = 0
    # Match the requested ~300 MiB before the new index exists. A page-count
    # check every 10k writes avoids estimating size from repeated bulk rows.
    while True:
        for _ in range(10_000):
            count += 1
            record = event_record(f'synthetic-{count:08d}', observed_at_ms=count)
            # Normal optional identities keep synthetic records near the
            # requested byte/row scale without artificial payload fields.
            record.update(configuration_digest='a' * 64, tool_digest='b' * 64,
                          item_digest='c' * 64)
            reply = self.ledger.append(
                self.project, record, producer='hook', now_ms=count)
            self.assertTrue(reply['stored'])
            self.assertEqual(count, reply['source_sequence'])
        size = page_size * connection.execute('PRAGMA page_count').fetchone()[0]
        print(f'AP3 synthetic population: {count} rows, {size / 2**20:.1f} MiB, '
              f'{time.perf_counter() - started:.1f} s', flush=True)
        if count >= 200_000 and size >= 295 * 2**20:
            break
        self.assertLess(count, 400_000, 'synthetic fixture did not reach the required scale')
    population_seconds = time.perf_counter() - started
    self.assertEqual(count, connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
    connection.execute('VACUUM')  # reclaim free pages so file growth measures the new index
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before_bytes = self.store.state.database.stat().st_size
    self.assertGreaterEqual(before_bytes, 250 * 2**20)
    self.assertLess(before_bytes, 400 * 2**20)
    self.ledger.close()

    execute = type(connection).execute
    ddl_seconds = []
    def measured_execute(conn, sql, parameters=()):
        if sql.startswith('CREATE INDEX IF NOT EXISTS records_project_digest '):
            start = time.perf_counter()
            try:
                return execute(conn, sql, parameters)
            finally:
                ddl_seconds.append(time.perf_counter() - start)
        return execute(conn, sql, parameters)

    with mock.patch.object(type(connection), 'execute', measured_execute):
        start = time.perf_counter()
        self.ledger = LearningLedger(self.store.state)
        first_open_seconds = time.perf_counter() - start
        self.store.ledger = self.ledger
        self.ledger._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        after_bytes = self.store.state.database.stat().st_size
        self.ledger.close()
        start = time.perf_counter()
        self.ledger = LearningLedger(self.store.state)
        reopen_seconds = time.perf_counter() - start
        self.store.ledger = self.ledger
    self.assertEqual(2, len(ddl_seconds))
    self.assertGreater(ddl_seconds[0], 0)
    self.assertGreater(after_bytes, before_bytes)
    self.assertIsNotNone(self.ledger._connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='records_project_digest'").fetchone())
    self.assertEqual(count, self.ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
    print('AP3 index measurement: ' + json.dumps({
        'records': count, 'before_bytes': before_bytes, 'after_bytes': after_bytes,
        'index_growth_bytes': after_bytes - before_bytes,
        'population_seconds': population_seconds, 'first_open_seconds': first_open_seconds,
        'index_build_seconds': ddl_seconds[0], 'reopen_seconds': reopen_seconds,
        'existing_index_ddl_seconds': ddl_seconds[1],
    }, sort_keys=True), flush=True)



def main():
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / 'packages/codex-agents'), str(root / 'scripts'),
                    str(root / 'packages/codex-agents/native-package/.codex/orchestration')]
    from tests.test_append_lookup import AppendLookupTests

    fixture = AppendLookupTests(methodName='runTest')
    try:
        fixture.setUp()
        measure(fixture)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
