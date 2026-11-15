"""Account-owner original-open layout; no client-selected SQL or migration.

The explicit origin survives a missing receipt. Journals intentionally have no
host-row cascade: an orphan refuses recovery instead of reissuing original work.
Only a trusted, drained upgrade may add this layout to an existing schema.
"""

HISTORY_OWNER_COLUMN = "history_owner TEXT NOT NULL DEFAULT 'legacy' CHECK(history_owner IN ('legacy','project'))"
OPEN_RECEIPTS_SCHEMA = '''CREATE TABLE account_open_receipts(
project_id TEXT NOT NULL,handle_digest TEXT NOT NULL,run_id TEXT,request_key TEXT NOT NULL,
scope_digest TEXT NOT NULL,parameters_digest TEXT NOT NULL,content BLOB NOT NULL
CHECK(length(content) BETWEEN 1 AND 16384),content_seal TEXT NOT NULL,
PRIMARY KEY(project_id,handle_digest),UNIQUE(project_id,run_id),UNIQUE(project_id,request_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''
OPEN_REQUESTS_SCHEMA = '''CREATE TABLE account_open_requests(
project_id TEXT NOT NULL,request_key TEXT NOT NULL,original_key TEXT NOT NULL,parameters_digest TEXT NOT NULL,
handle_digest TEXT NOT NULL,run_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','committed')),
opened_at_ms INTEGER NOT NULL CHECK(typeof(opened_at_ms)='integer' AND opened_at_ms>=0),
observation_deadline_at_ms INTEGER NOT NULL CHECK(typeof(observation_deadline_at_ms)='integer'
AND observation_deadline_at_ms>opened_at_ms),receipt_digest TEXT,request_seal TEXT NOT NULL,
CHECK((state='pending' AND receipt_digest IS NULL) OR (state='committed' AND receipt_digest IS NOT NULL)),
PRIMARY KEY(project_id,request_key),UNIQUE(project_id,handle_digest),UNIQUE(project_id,original_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''
OPEN_OBJECTS = (('table', 'account_open_receipts', OPEN_RECEIPTS_SCHEMA),
                ('table', 'account_open_requests', OPEN_REQUESTS_SCHEMA))


def assert_open_schema(connection):
    def normalized(sql):
        return ' '.join(sql.replace('IF NOT EXISTS ', '').strip().rstrip(';').split())
    for kind, name, expected in OPEN_OBJECTS:
        row = connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', (kind, name)).fetchone()
        if row is None or type(row[0]) is not str or normalized(row[0]) != normalized(expected):
            raise ValueError('account original-open schema is incompatible')
    rows = connection.execute('PRAGMA table_info(host_run_bindings)').fetchall()
    origin = [row for row in rows if row[1] == 'history_owner']
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='host_run_bindings'").fetchone()
    if (len(origin) != 1 or origin[0][2:5] != ('TEXT', 1, "'legacy'") or sql is None
            or normalized(HISTORY_OWNER_COLUMN) not in normalized(sql[0])):
        raise ValueError('account original-open origin is incompatible')


def install_open_schema(connection):
    if not connection.in_transaction:
        raise ValueError('account original-open migration requires an owner transaction')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(host_run_bindings)')}
    if 'history_owner' in columns:
        raise ValueError('account original-open origin already exists')
    if any(connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone()
           for _, name, _ in OPEN_OBJECTS):
        raise ValueError('account original-open migration is ambiguous')
    connection.execute('ALTER TABLE host_run_bindings ADD COLUMN '+HISTORY_OWNER_COLUMN)
    for _, _, sql in OPEN_OBJECTS:
        connection.execute(sql)
    assert_open_schema(connection)
