#!/usr/bin/env python3
"""Opt-in U3 first-open index build measurement on a disposable ~300 MiB ledger, outside test discovery.

Run from the repository root with the test interpreter: $PY -B scripts/run_measurement.py scripts/measure_u3_index_build.py
Never opens an owner ledger. The synthetic ledger mixes record types the way a long-lived project does:
event records through the real LearningLedger.append path, and dispatch_intent, provider_usage,
admission_decision, provider_billing/headroom and outcome records copied from rows the real open path wrote
(they are not appendable from outside, so the copies use the ledger's own INSERT and triggers).
The five U3 indexes are dropped, the file is vacuumed, then the real open path rebuilds them. Each
CREATE INDEX is timed separately; file growth is the whole set of five.
"""

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 300, "data_mib": 768, "rss_mib": 2048}  # ~295 MiB ledger plus index and WAL
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from unittest import mock  # noqa: E402

TARGET_BYTES = 295 * 2**20
U3_INDEXES = ('records_run', 'records_candidate', 'records_type_observed', 'records_foreign_admissions',
              'account_cleanup_receipts_unreleased')


def populate(case, growth, support, target):
    connection = case.ledger._connection
    page = connection.execute('PRAGMA page_size').fetchone()[0]
    def size():
        return page * connection.execute('PRAGMA page_count').fetchone()[0]
    started = time.perf_counter()
    seed = connection.execute("SELECT * FROM records WHERE record_type='provider_billing' LIMIT 1").fetchone()
    usage = {'schema_version': 1, 'record_type': 'provider_usage', 'record_id': 'u3-seed-usage', 'provider': 'claude',
             'run_id': 'run-seed', 'phase': 'after', 'requested_model': 'claude-opus-5-5',
             'models': [{'model': 'claude-opus-5-5', 'input_tokens': 10, 'cached_input_tokens': 0,
                         'cache_creation_input_tokens': 0, 'output_tokens': 2, 'reasoning_output_tokens': None}],
             'helper_models': ['claude-haiku-5-5'], 'input_tokens': 10, 'cached_input_tokens': 0,
             'cache_creation_input_tokens': 0, 'output_tokens': 2, 'reasoning_output_tokens': None,
             'observed_at_ms': seed['observed_at_ms']}
    content = json.dumps(usage, sort_keys=True).encode()
    top = connection.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?', (case.project,)).fetchone()[0]
    with case.ledger._lock, connection:
        connection.execute(
            'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,ingested_at_ms,'
            'expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)',
            (case.project, 'u3-seed-usage', 'provider_usage', 'provider', top + 1, seed['observed_at_ms'],
             seed['ingested_at_ms'], seed['expires_at_ms'], content, 'f' * 64))
    growth.HISTORY_TYPES = growth.HISTORY_TYPES + ('provider_usage',)
    total = 0
    while True:
        total += 40_000
        case.history(total)
        size_now = size()
        print(f'population: {connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]} rows, '
              f'{size_now / 2**20:.1f} MiB, {time.perf_counter() - started:.1f} s', flush=True)
        if size_now >= TARGET_BYTES - 35 * 2**20:
            break
        assert total < 1_000_000, 'synthetic ledger did not reach the required scale'
    # Real append path last: history() copies every existing row as a seed, so appended
    # rows must not become seeds (its seed join is quadratic in the seed count).
    appended = time.perf_counter()
    for index in range(20_000):
        record = support.event_record(f'synthetic-{index:08d}', observed_at_ms=index + 1)
        record.update(configuration_digest='a' * 64, tool_digest='b' * 64, item_digest='c' * 64)
        assert case.ledger.append(case.project, record, producer='hook', now_ms=index + 1)['stored']
    append_seconds = time.perf_counter() - appended
    print(f'real appends: 20000 rows, {append_seconds:.1f} s, {size() / 2**20:.1f} MiB', flush=True)
    return append_seconds, time.perf_counter() - started


def measure():
    from tests import test_read_growth as growth
    from tests import learning_support as support
    from codex_agents.learning_store import LearningLedger

    case = growth.ReadGrowthTests('test_U3_2_a_runs_admission_is_found_by_its_run_as_history_grows')
    case.setUp()
    try:
        append_seconds, population_seconds = populate(case, growth, support, TARGET_BYTES)
        connection = case.ledger._connection
        counts = {row[0]: row[1] for row in connection.execute('SELECT record_type, COUNT(*) FROM records GROUP BY 1')}
        for name in U3_INDEXES:
            connection.execute('DROP INDEX ' + name)
        connection.commit()
        connection.execute('VACUUM')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        database = case.ledger.state.database
        before = database.stat().st_size
        state, capabilities = case.ledger.state, case.ledger._client_capabilities
        case.ledger.close()
        execute = type(connection).execute
        ddl = {}

        def timed(conn, sql, parameters=()):
            for name in U3_INDEXES:
                if sql.startswith(f'CREATE INDEX IF NOT EXISTS {name} '):
                    start = time.perf_counter()
                    try:
                        return execute(conn, sql, parameters)
                    finally:
                        ddl.setdefault(name, []).append(time.perf_counter() - start)
            return execute(conn, sql, parameters)

        with mock.patch.object(type(connection), 'execute', timed):
            start = time.perf_counter()
            case.ledger = LearningLedger(state, client_capabilities=capabilities)
            first_open = time.perf_counter() - start
            case.ledger._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after = database.stat().st_size
            case.ledger.close()
            start = time.perf_counter()
            case.ledger = LearningLedger(state, client_capabilities=capabilities)
            reopen = time.perf_counter() - start
        case.fixture.ledger = case.ledger
        print('U3 index measurement: ' + json.dumps({
            'rows': sum(counts.values()), 'rows_by_type': counts,
            'before_bytes': before, 'after_bytes': after, 'growth_bytes': after - before,
            'growth_percent': round(100 * (after - before) / before, 1),
            'population_seconds': round(population_seconds, 1), 'real_append_20k_seconds': round(append_seconds, 1),
            'first_open_seconds': round(first_open, 2), 'reopen_seconds': round(reopen, 3),
            'index_build_seconds': {k: round(v[0], 2) for k, v in ddl.items()},
            'index_build_total_seconds': round(sum(v[0] for v in ddl.values()), 2),
            'second_open_index_ddl_seconds': round(sum(v[1] for v in ddl.values() if len(v) > 1), 3),
        }, sort_keys=True), flush=True)
    finally:
        case.doCleanups()


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / 'packages/codex-agents'), str(root / 'scripts'),
                    str(root / 'packages/codex-agents/native-package/.codex/orchestration')]
    measure()


if __name__ == '__main__':
    main()
