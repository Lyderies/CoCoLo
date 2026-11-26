"""Additive B4t schema, used only by the fenced persistence owners."""
from .collector_contracts import _version_tuple

FLOOR_COLUMNS = {'runtime_floor': ('singleton', 'floor_version')}
FLOOR_SCHEMA = '''CREATE TABLE runtime_floor(
singleton INTEGER PRIMARY KEY CHECK(singleton=1),floor_version TEXT NOT NULL);
'''
LINEAGE_COLUMNS = {'runtime_lineage': ('predecessor_digest', 'predecessor_version', 'successor_digest',
    'successor_version', 'epoch', 'takeover_id', 'recorded_at_ms')}
LEDGER_SCHEMA = FLOOR_SCHEMA + '''CREATE TABLE runtime_lineage(
predecessor_digest TEXT PRIMARY KEY,predecessor_version TEXT NOT NULL,successor_digest TEXT NOT NULL,
successor_version TEXT NOT NULL,epoch INTEGER NOT NULL,takeover_id TEXT NOT NULL,recorded_at_ms INTEGER NOT NULL);
'''
PROJECT_COLUMNS = {**FLOOR_COLUMNS,
    'takeover_record': ('takeover_id', 'predecessor_digest', 'predecessor_version', 'successor_digest',
                        'successor_version', 'state', 'recorded_at_ms', 'runs_listed', 'runs_unlisted'),
    'takeover_runs': ('takeover_id', 'bridge_run_id', 'run_handle', 'attempt'),
    'takeover_obligations': ('kind', 'identity', 'takeover_id', 'original_request_id', 'original_request_digest',
                             'original_runtime_digest')}
PROJECT_SCHEMA = FLOOR_SCHEMA + '''CREATE TABLE takeover_record(
takeover_id TEXT PRIMARY KEY,predecessor_digest TEXT NOT NULL UNIQUE,predecessor_version TEXT NOT NULL,
successor_digest TEXT NOT NULL,successor_version TEXT NOT NULL,
state TEXT NOT NULL CHECK(state IN ('adopting','adopted','complete')),
recorded_at_ms INTEGER NOT NULL,runs_listed INTEGER NOT NULL CHECK(runs_listed BETWEEN 0 AND 64),
runs_unlisted INTEGER NOT NULL CHECK(runs_unlisted>=0));
CREATE TABLE takeover_runs(
takeover_id TEXT NOT NULL,bridge_run_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
PRIMARY KEY(takeover_id,bridge_run_id),FOREIGN KEY(takeover_id) REFERENCES takeover_record(takeover_id));
CREATE TABLE takeover_obligations(
kind TEXT NOT NULL,identity TEXT NOT NULL,takeover_id TEXT NOT NULL,original_request_id TEXT NOT NULL,
original_request_digest TEXT NOT NULL,original_runtime_digest TEXT NOT NULL,PRIMARY KEY(kind,identity),
FOREIGN KEY(takeover_id) REFERENCES takeover_record(takeover_id));
'''


def check_layout(connection, schema, refusal):
    for statement in schema.split(';'):
        if not statement.strip():
            continue
        table = statement.split('(', 1)[0].split()[-1]
        row = connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if (row is None or row[0] != 'table' or ' '.join(row[1].split()) != ' '.join(statement.strip().split())
                or connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                                      (table,)).fetchone() is not None):
            raise refusal()


def check_floor(connection, version, refusal):
    """Read before any acquisition write, including the epoch insert."""
    present = connection.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_floor'").fetchone()
    if present is None:
        return
    check_layout(connection, FLOOR_SCHEMA, refusal)
    if tuple(row[1] for row in connection.execute('PRAGMA table_info(runtime_floor)')) != FLOOR_COLUMNS['runtime_floor']:
        raise refusal()
    rows = connection.execute('SELECT singleton,floor_version FROM runtime_floor LIMIT 2').fetchall()
    current = _version_tuple(version)
    if (len(rows) != 1 or rows[0][0] != 1 or current is None
            or _version_tuple(rows[0][1]) is None or current < _version_tuple(rows[0][1])):
        raise refusal()


def raise_floor(connection, version, refusal, *, initialize=False):
    if initialize:
        connection.execute('INSERT INTO runtime_floor VALUES(1,?)', (version,))
    else:
        check_floor(connection, version, refusal)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_floor'").fetchone() is not None:
            connection.execute('UPDATE runtime_floor SET floor_version=? WHERE singleton=1 AND floor_version<>?',
                               (version, version))


def install(connection, schema):
    for statement in schema.split(';'):
        if statement.strip():
            connection.execute(statement)
