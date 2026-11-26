#!/usr/bin/env python3
"""S14c small large-ledger growth profile (measurement only; no product change). Never reads an owner ledger.

Run ONE size per invocation, only through the runner, from the repository root with the test interpreter:

    $PY -B scripts/run_measurement.py scripts/measure_s14c.py -- --size-mib 64 --output docs/requirements/s14c/size-64.json

Build. A disposable fixture ledger (the admission tests' fixture) takes ONE real open + claim through the product
write path (signature worker and all, ~0.3 s). Doing that thousands of times would cost minutes, so the rows that
one real run wrote (11 tables; admission_evidence and run_holds come from the product's own triggers) are cloned with fresh run-specific
identifiers (every 64-hex / brg_ token that no earlier row uses) and the recorded times moved back 9 days plus 1/774 day per
run (774 runs/day is the S14 mix; a long-lived ledger is almost all older than the 7-day replay window), in batches, until the database file reaches the requested size. Clones are
unreleased claimed runs (the only state the real run leaves it in); the rows are real product rows, the volume is
copied. Record contents are copied as-is except identifiers (content digests recomputed).

Measured per size (each timed op is interrupted after OP_LIMIT seconds and then reported as "not finished"):
  custody   runtime_lineage.live_ledger_custody (the two queries separately, plans, vm steps, cProfile)
  open      LearningLedger(...) reopening the file through product code (it runs the same custody purge)
  lookups   per-run queries keyed by handle (lost-run lookup, withdrawal control) and DENIED_OPENS maintenance
  others    consented 17 -> 18 upgrade on a copy (when it fits the disk cap), idle retention passes, status reads
"""
from __future__ import annotations

# Enforced by scripts/run_measurement.py (S14d, decision 371): run this script only through the runner.
MEASUREMENT_BUDGET = {"wall_seconds": 300, "data_mib": 1024, "rss_mib": 3072}

import argparse
import cProfile
import hashlib
import io
import json
import os
import pstats
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/codex-agents'))
sys.path.insert(0, str(ROOT / 'scripts'))

RUNS_PER_DAY = 774
DAY_MS = 86_400_000
OP_LIMIT = 45          # seconds; a timed query is interrupted after this
TOKEN = re.compile(r'(?:brg_[0-9a-f]{32}|[0-9a-f]{64})')
RUN_TABLES = ('sessions', 'run_provenance', 'records', 'host_run_bindings', 'launch_claims', 'launch_inputs',
              'launch_observations', 'launch_receipts', 'active_run_capacity', 'account_open_receipts',
              'account_open_requests')  # admission_evidence and run_holds are filled by the product's own triggers

# The two queries of runtime_lineage.live_ledger_custody (runtime_lineage.py:139-147), verbatim.
Q1 = ('SELECT runtime_digest FROM active_run_capacity '
      'WHERE runtime_digest IS NOT NULL AND (released_at_ms IS NULL OR released_at_ms>=?)')
Q2 = ('SELECT i.runtime_digest FROM launch_inputs i JOIN host_run_bindings h '
      'ON h.project_id=i.project_id AND h.handle_digest=i.handle_digest '
      'WHERE h.expires_at_ms>=? OR EXISTS(SELECT 1 FROM run_holds r '
      "WHERE r.project_id=i.project_id AND r.identity=i.handle_digest AND r.state<>'released')")
# Candidate fix shape (reported, not applied): the same lookup pinned to the primary key (kind is known).
Q2_KIND = Q2.replace("r.identity=i.handle_digest AND r.state<>'released'",
                     "r.kind='binding' AND r.identity=i.handle_digest AND r.state<>'released'")
LOST_RUN = ("SELECT kind,state,release_kind,receipt,evidence FROM run_holds WHERE project_id=? AND identity=? "
            "AND kind IN ('run_slot','binding')")
WITHDRAW = "SELECT state,release_kind,generation,receipt,evidence FROM run_holds WHERE project_id=? AND kind='binding' AND identity=?"


def timed(connection, sql, params=(), limit=OP_LIMIT, count_steps=True):
    """(seconds, rows, vm_steps_in_100k, finished). The query is interrupted after `limit` seconds."""
    steps = [0]
    if count_steps:
        connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100_000)
    timer = threading.Timer(limit, connection.interrupt)
    started = time.perf_counter()
    timer.start()
    rows, finished = 0, True
    try:
        for _ in connection.execute(sql, params):
            rows += 1
    except sqlite3.OperationalError as error:
        if 'interrupt' not in str(error):
            raise
        finished = False
    finally:
        timer.cancel()
        connection.set_progress_handler(None, 0)
    return round(time.perf_counter() - started, 4), rows, steps[0], finished


def plan(connection, sql, params):
    return [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql, params)]


class Cloner:
    """Turns the rows one real run wrote into a stream of fresh, valid-looking runs."""

    def __init__(self, db, before):
        self.db = db
        self.tables = {}
        shared = set()
        # tokens that existed before the real run are shared state (runtime digest, accounts, project), not run ids
        for table, mark in before.items():
            for row in db.execute(f'SELECT * FROM "{table}" WHERE rowid<=?', (mark,)):
                for value in row:
                    shared.update(TOKEN.findall(value if isinstance(value, str) else
                                                bytes(value).decode('latin1') if isinstance(value, (bytes, memoryview)) else ''))
        self.mapping_tokens = []
        for table in RUN_TABLES:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            rows = [tuple(r) for r in db.execute(f'SELECT * FROM "{table}" WHERE rowid>?', (before[table],))]
            self.tables[table] = (columns, rows)
            for row in rows:
                for value in row:
                    text = value if isinstance(value, str) else bytes(value).decode('latin1') if isinstance(value, (bytes, memoryview)) else ''
                    self.mapping_tokens.extend(t for t in TOKEN.findall(text) if t not in shared)
        for columns, rows in self.tables.values():  # content-addressed record ids are always run-specific
            if 'record_id' in columns:
                for row in rows:
                    self.mapping_tokens.extend(TOKEN.findall(row[columns.index('record_id')]))
        self.mapping_tokens = sorted(set(self.mapping_tokens))
        stamps = [v for _, rows in self.tables.values() for row in rows for v in row
                  if type(v) is int and v > 1_000_000_000_000]
        self.stamp = max(set(stamps), key=stamps.count)
        self.sequence = db.execute('SELECT MAX(source_sequence) FROM records').fetchone()[0]
        self.plans = {}
        for table, (columns, rows) in self.tables.items():
            plan_rows = []
            for row in rows:
                cells = []
                for name, value in zip(columns, row):
                    if isinstance(value, str) and (TOKEN.search(value) or name in ('request_id', 'request_key', 'record_id')):
                        cells.append(('text', value))
                    elif isinstance(value, (bytes, memoryview)):
                        cells.append(('blob', bytes(value)))
                    elif type(value) is int and name.endswith('_ms') and value > 1_000_000_000_000:
                        cells.append(('ms', value))
                    elif name == 'source_sequence':
                        cells.append(('seq', value))
                    else:
                        cells.append(('const', value))
                plan_rows.append(cells)
            self.plans[table] = plan_rows
        self.digest_checked = {}
        records = self.tables['records']
        for row in records[1]:
            r = dict(zip(records[0], row))
            self.digest_checked[r['record_id']] = hashlib.sha256(bytes(r['content'])).hexdigest() == r['content_digest']
        self.sql = {t: f'INSERT INTO "{t}"({",".join(chr(34) + c + chr(34) for c in cols)}) VALUES({",".join("?" * len(cols))})'
                    for t, (cols, _) in self.tables.items()}

    def rows_for(self, k):
        remap = {}
        for token in self.mapping_tokens:
            new = hashlib.sha256(f'{token}/{k}'.encode()).hexdigest()
            remap[token] = 'brg_' + new[:32] if token.startswith('brg_') else new
        suffix = f'-c{k}'
        shift = 9 * DAY_MS + (k * DAY_MS) // RUNS_PER_DAY  # older than the 7-day open-request window
        sub = lambda m: remap[m.group(0)] if m.group(0) in remap else m.group(0)
        sub_bytes = lambda m: remap[m.group(0).decode()].encode() if m.group(0).decode() in remap else m.group(0)
        blob_token = re.compile(TOKEN.pattern.encode())
        out = {}
        for table, plan_rows in self.plans.items():
            columns = self.tables[table][0]
            made = []
            for cells in plan_rows:
                row = []
                for (kind, value), name in zip(cells, columns):
                    if kind == 'text':
                        text = TOKEN.sub(sub, value)
                        row.append(text + suffix if name in ('request_id', 'request_key', 'record_id') and text == value else text)
                    elif kind == 'blob':
                        row.append(blob_token.sub(sub_bytes, value))
                    elif kind == 'ms':
                        row.append(value - shift)
                    elif kind == 'seq':
                        self.sequence += 1
                        row.append(self.sequence)
                    else:
                        row.append(value)
                if table == 'records':
                    r = dict(zip(columns, row))
                    row[columns.index('content_digest')] = hashlib.sha256(bytes(r['content'])).hexdigest()
                made.append(tuple(row))
            out[table] = made
        return out

    def insert(self, k_from, k_to):
        db = self.db
        batches = {t: [] for t in RUN_TABLES}
        for k in range(k_from, k_to):
            for table, rows in self.rows_for(k).items():
                batches[table].extend(rows)
        for table in RUN_TABLES:
            db.executemany(self.sql[table], batches[table])


def disk_bytes(root):
    total = 0
    for base, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def top(profile, count=8):
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).sort_stats('cumulative')
    rows = []
    for (path, line, name), (_, calls, own, cum, _) in list(stats.stats.items()):
        rows.append((cum, f'{Path(path).name}:{line}:{name}', calls, round(own, 4)))
    rows.sort(reverse=True)
    return [{'where': w, 'calls': c, 'own_seconds': o, 'cumulative_seconds': round(cum, 4)} for cum, w, c, o in rows[:count]]


def profiled(function, limit=OP_LIMIT):
    """(seconds, result, profile top, finished); a call that overruns is reported at its limit (its thread is left to end)."""
    profile = cProfile.Profile()
    box = {}

    def target():
        profile.enable()
        try:
            box['result'] = function()
        except Exception as error:  # reported, not raised
            box['error'] = f'{type(error).__name__}: {error}'
        finally:
            profile.disable()
    thread = threading.Thread(target=target, daemon=True)
    started = time.perf_counter()
    thread.start()
    thread.join(limit)
    seconds = round(time.perf_counter() - started, 3)
    if thread.is_alive():
        return seconds, None, [], False
    return seconds, box.get('result', box.get('error')), top(profile), True


def status_profile(ledger, project):
    """S14h: cProfile of one `account_holds_status` plus the held-row counts that its time should scale with."""
    import cProfile
    import io
    import pstats
    db = ledger._connection
    counts = {kind: db.execute("SELECT COUNT(*) FROM run_holds WHERE project_id=? AND kind=? AND state='held'", (project, kind)).fetchone()[0]
              for kind in ('run_slot', 'binding', 'probe')}
    counts['run_holds_total'] = db.execute('SELECT COUNT(*) FROM run_holds').fetchone()[0]
    profile = cProfile.Profile()
    started = time.perf_counter()
    profile.enable()
    ledger.account_holds_status(project)
    profile.disable()
    seconds = round(time.perf_counter() - started, 4)
    out = io.StringIO()
    pstats.Stats(profile, stream=out).sort_stats('cumulative').print_stats(14)
    text = [line.rstrip() for line in out.getvalue().splitlines() if line.strip()][-16:]
    return {'seconds': seconds, 'held_rows': counts, 'top_cumulative': text}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--size-mib', type=int, required=True, help='target database file size')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--no-upgrade', action='store_true', help='skip the 17 -> 18 copy (when a copy would pass the disk cap)')
    args = parser.parse_args()
    started_all = time.monotonic()
    from tests import test_account_original_open as opens
    from codex_agents.learning_store import LearningLedger
    from codex_agents import runtime_lineage
    report = {'target_mib': args.size_mib}
    fixture = opens.OriginalAccountOpenJobTests(methodName='runTest')
    fixture.setUp()
    ledger = fixture.ledger
    db = ledger._connection
    before = {t: db.execute(f'SELECT COALESCE(MAX(rowid),0) FROM "{t}"').fetchone()[0] for t in RUN_TABLES}
    t0 = time.perf_counter()
    client, state, launch = fixture.managed_lifecycle()
    claim = client.claim_launch(launch.run_handle, launch.identity_digest, request_id=state['scope'].claim_request_id)
    report['real_run_seconds'] = round(time.perf_counter() - t0, 3)
    assert claim['state'] == 'claimed', claim
    project = fixture.reference.base.scope.project_id
    root = Path(os.path.realpath(os.environ['MEASUREMENT_OUTPUT_DIR']))
    # ---- build
    t0 = time.perf_counter()
    cloner = Cloner(db, before)
    now = cloner.stamp + 1000
    db.commit()
    runs, path = 1, ledger.state.root
    base_bytes = disk_bytes(path)
    target = args.size_mib * 2**20
    batch = 100
    while True:
        db.execute('BEGIN')
        cloner.insert(runs, runs + batch)
        db.commit()
        runs += batch
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')  # the file size, not the log, is the target
        size = disk_bytes(path)
        if size >= target:
            break
        per_run = max(1.0, (size - base_bytes) / runs)
        batch = max(200, min(4000, int((target - size) / per_run * 0.9) or 200))
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    report['build_seconds'] = round(time.perf_counter() - t0, 1)
    page = db.execute('PRAGMA page_size').fetchone()[0]
    report['db_bytes'] = page * db.execute('PRAGMA page_count').fetchone()[0]
    report['file_bytes'] = disk_bytes(path)
    report['runs'] = runs
    report['bytes_per_run'] = round((report['db_bytes'] - base_bytes) / runs)
    report['rows'] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                      for t in ('launch_inputs', 'host_run_bindings', 'active_run_capacity', 'run_holds', 'records')}
    report['clone_valid'] = {'unique_handles': db.execute('SELECT COUNT(DISTINCT handle_digest) FROM launch_inputs').fetchone()[0]}
    # ---- custody queries (separately, so the slow one is named), plans, steps
    cutoff = now - 7 * DAY_MS
    custody = {}
    for name, sql in (('Q1_active_run_capacity', Q1), ('Q2_launch_inputs_bindings_holds', Q2), ('Q2_with_kind_candidate', Q2_KIND)):
        seconds, rows, steps, finished = timed(db, sql, (cutoff,))
        custody[name] = {'seconds': seconds, 'rows': rows, 'vm_steps_100k': steps, 'finished': finished,
                         'plan': plan(db, sql, (cutoff,))}
    seconds, result, prof, finished = profiled(lambda: runtime_lineage.live_ledger_custody(db, now))
    custody['live_ledger_custody'] = {'seconds': seconds, 'finished': finished, 'digests': None if result is None else len(result),
                                      'profile_top': prof}
    if finished:  # S14e: best of three warm runs (a small ledger's single run is noise-sized)
        best = []
        for _ in range(3):
            started = time.perf_counter()
            runtime_lineage.live_ledger_custody(db, now)
            best.append(time.perf_counter() - started)
        custody['live_ledger_custody']['best_of_3_seconds'] = round(min(best), 5)
    report['custody'] = custody
    # ---- S14f: the owner-thread sweep's paged custody. One lineage edge on a runtime the history uses is added
    # inside a transaction and rolled back; every pass of one cycle is timed (the owner is held for one pass).
    digest = db.execute('SELECT runtime_digest FROM active_run_capacity WHERE runtime_digest IS NOT NULL LIMIT 1').fetchone()
    if digest is not None and not db.in_transaction:
        db.execute('BEGIN')
        try:
            db.execute('INSERT INTO runtime_lineage VALUES(?,?,?,?,1,?,1)', (digest[0], '0.0.1', '0' * 64, '0.0.2', '1' * 32))
            # S14F-5: the whole owner cycle (edges, history, walk, deletes) as the sweep runs it; deletes stay in
            # this rolled-back transaction (nullcontext, so nothing commits).
            import contextlib
            scan, passes, removed, started = runtime_lineage.CustodyScan(), [], 0, time.perf_counter()
            while scan.cycles == 0 and len(passes) <= 100000:
                tick = time.perf_counter()
                removed += scan.advance(db, now, 'f' * 64, transaction=contextlib.nullcontext)
                passes.append(time.perf_counter() - tick)
            report['paged_custody'] = {'passes': len(passes), 'worst_pass_seconds': round(max(passes), 5),
                                       'total_seconds': round(time.perf_counter() - started, 4), 'rows_per_pass': runtime_lineage.CUSTODY_PASS_ROWS,
                                       'edges_removed': removed}
        finally:
            db.execute('ROLLBACK')
    # ---- per-run lookups (sampled handles), plans
    handles = [r[0] for r in db.execute('SELECT handle_digest FROM launch_inputs ORDER BY rowid LIMIT 1 OFFSET ?', (runs // 2,))]
    handles += [r[0] for r in db.execute('SELECT handle_digest FROM launch_inputs ORDER BY rowid DESC LIMIT 5')]
    lookups = {}
    for name, sql in (('lost_run_lookup_kind_in', LOST_RUN), ('withdrawal_control_kind_eq', WITHDRAW)):
        runs_s = [timed(db, sql, (project, h), count_steps=True) for h in handles[:6]]
        lookups[name] = {'median_seconds': sorted(r[0] for r in runs_s)[len(runs_s) // 2], 'max_seconds': max(r[0] for r in runs_s),
                         'vm_steps_100k_max': max(r[2] for r in runs_s), 'plan': plan(db, sql, (project, handles[0]))}
    from codex_agents.account_original_open import DENIED_OPENS
    params = {'deadline': now - 7 * DAY_MS, 'limit': 32}
    seconds, rows, steps, finished = timed(db, DENIED_OPENS, params)
    lookups['denied_opens_maintenance'] = {'seconds': seconds, 'rows': rows, 'vm_steps_100k': steps, 'finished': finished,
                                          'plan': plan(db, DENIED_OPENS, params)}
    report['lookups'] = lookups
    # ---- status read and idle retention (on the open fixture ledger)
    started = time.perf_counter()
    for _ in range(5):
        status = ledger.account_holds_status(project)
    report['status_read_seconds'] = round((time.perf_counter() - started) / 5, 4)
    report['status_profile'] = status_profile(ledger, project)  # S14h: where the status read spends its time
    started = time.perf_counter()
    lineage = runtime_lineage.lineage_status(db)
    report['lineage_status_seconds'] = round(time.perf_counter() - started, 4)
    seconds, result, prof, finished = profiled(lambda: ledger.sweep_run_retention(now_ms=now))
    report['idle_run_retention'] = {'seconds': seconds, 'finished': finished, 'result': result if finished else None}
    seconds, result, prof, finished = profiled(lambda: ledger.sweep_account_retention(now_ms=now))
    report['idle_account_retention'] = {'seconds': seconds, 'finished': finished, 'result': result if finished else None,
                                        'profile_top': prof}
    # ---- product open of the file (runs the custody purge in __init__)
    state_obj, clients = ledger.state, ledger._client_capabilities
    ledger.close()
    holder = {}

    def reopen():
        holder['ledger'] = LearningLedger(state_obj, client_capabilities=clients)
        return 'opened'
    seconds, result, prof, finished = profiled(reopen, limit=60)
    report['product_open'] = {'seconds': seconds, 'finished': finished, 'result': result, 'profile_top': prof}
    if finished and 'ledger' in holder:
        holder['ledger']._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        holder['ledger'].close()
    # ---- consented 17 -> 18 upgrade on a copy (the disk cap decides whether a copy fits)
    if not args.no_upgrade and report['file_bytes'] * 2 + 64 * 2**20 <= 1024 * 2**20 and report['db_bytes'] * 2 < 700 * 2**20 and finished and 'ledger' in holder:
        report['upgrade_17_18'] = upgrade_copy(clients, path, root, LearningLedger)
    else:
        report['upgrade_17_18'] = {'skipped': 'open did not finish or a copy would pass the 1 GiB disk cap'}
    # ---- the same custody queries once the holds are settled (the usual state of an old run): released holds.
    # Run last: it leaves the register disagreeing with the capacity rows, so nothing may open this file for upgrade after it.
    if finished:
        again = LearningLedger(state_obj, client_capabilities=clients)
        report['custody_released_holds'] = released_variant(again._connection, cutoff, now, runtime_lineage)
        again.close()
    report['total_seconds'] = round(time.monotonic() - started_all, 1)
    text = json.dumps(report, indent=1)
    if args.output:
        args.output.write_text(text + '\n')
    print(text)
    sys.stdout.flush()
    os._exit(0)  # leave any abandoned timed thread behind; the runner deletes the data


def released_variant(db, cutoff, now, runtime_lineage):
    try:
        db.execute('BEGIN')
        db.execute("UPDATE run_holds SET state='released',release_kind='expired',released_at_ms=? WHERE state<>'released'", (now,))
        db.commit()
    except sqlite3.Error as error:
        db.rollback()
        return {'error': f'{type(error).__name__}: {error}'}
    out = {}
    for name, sql in (('Q2_launch_inputs_bindings_holds', Q2), ('Q2_with_kind_candidate', Q2_KIND)):
        seconds, rows, steps, finished = timed(db, sql, (cutoff,))
        out[name] = {'seconds': seconds, 'rows': rows, 'vm_steps_100k': steps, 'finished': finished}
    return out


def upgrade_copy(clients, path, root, LearningLedger):
    """Copy the state, put it back at schema 17 with the hold tables absent, and open it as the consented owner."""
    copy = root / 'upgrade-copy'
    shutil.copytree(path.parent, copy, symlinks=True)
    from codex_agents.learning_store import LearningState
    state = LearningState(copy / path.name, create=False)
    connection = sqlite3.connect(state.root / 'learning.sqlite3')
    from codex_agents.run_retention import RETENTION_INDEX_NAMES
    try:
        # What a schema-17 ledger lacks: the B4t authority tables and the S14b retention indexes.
        for table in ('runtime_floor', 'runtime_lineage', 'runtime_takeover'):
            connection.execute(f'DROP TABLE IF EXISTS {table}')
        for name in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%retention%'").fetchall():
            connection.execute(f'DROP INDEX IF EXISTS "{name[0]}"')
        connection.execute('PRAGMA user_version=17')
        connection.commit()
    finally:
        connection.close()
    holder = {}

    def run():
        holder['ledger'] = LearningLedger(state, client_capabilities=clients, allow_schema_upgrade=True)
        return holder['ledger']._connection.execute('PRAGMA user_version').fetchone()[0]
    seconds, result, prof, finished = profiled(run, limit=120)
    out = {'seconds': seconds, 'finished': finished, 'result': result, 'profile_top': prof}
    if finished and 'ledger' in holder:
        holder['ledger'].close()
    shutil.rmtree(copy, ignore_errors=True)
    return out


if __name__ == '__main__':
    main()
