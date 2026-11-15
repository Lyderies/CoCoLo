"""Durable original observation requests; pending never means safe to retry."""
OBSERVATION_COLUMNS = (
    'request_key', 'parameters_digest', 'runtime_digest', 'caller_scope', 'provider',
    'handle_digest', 'run_id', 'phase', 'observed_at_ms', 'deadline_at_ms', 'state', 'references_json', 'state_seal',
)
OBSERVATION_ORIGIN_COLUMNS = ('request_key', 'parameters_digest', 'observed_at_ms', 'origin_seal')
OBSERVATION_ORIGIN_SCHEMA = '''CREATE TABLE account_observation_origins(
project_id TEXT NOT NULL,request_key TEXT NOT NULL,parameters_digest TEXT NOT NULL,
observed_at_ms INTEGER NOT NULL CHECK(typeof(observed_at_ms)='integer' AND observed_at_ms>=0),
origin_seal TEXT NOT NULL,PRIMARY KEY(project_id,request_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''
OBSERVATION_SCHEMA = '''CREATE TABLE account_observation_requests(
project_id TEXT NOT NULL,request_key TEXT NOT NULL,parameters_digest TEXT NOT NULL,
runtime_digest TEXT NOT NULL,caller_scope TEXT NOT NULL,provider TEXT NOT NULL CHECK(provider IN ('codex','claude')),
handle_digest TEXT,run_id TEXT,phase TEXT CHECK(phase IN ('before','after')),
observed_at_ms INTEGER NOT NULL CHECK(typeof(observed_at_ms)='integer' AND observed_at_ms>=0),
deadline_at_ms INTEGER NOT NULL CHECK(typeof(deadline_at_ms)='integer' AND deadline_at_ms>observed_at_ms),
state TEXT NOT NULL CHECK(state IN ('pending','committed')),references_json TEXT,
state_seal TEXT NOT NULL,
CHECK((handle_digest IS NULL AND run_id IS NULL AND phase IS NULL)
OR (handle_digest IS NOT NULL AND run_id IS NOT NULL AND phase IS NOT NULL)),
CHECK((state='pending' AND references_json IS NULL) OR
(state='committed' AND references_json IS NOT NULL AND length(references_json) BETWEEN 1 AND 4096)),
PRIMARY KEY(project_id,request_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
FOREIGN KEY(project_id,request_key) REFERENCES account_observation_origins(project_id,request_key)
DEFERRABLE INITIALLY DEFERRED)'''


def assert_observation_schema(connection):
    for name, sql in (('account_observation_origins', OBSERVATION_ORIGIN_SCHEMA),
                      ('account_observation_requests', OBSERVATION_SCHEMA)):
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row is None or ' '.join(row[0].split()) != ' '.join(sql.split()):
            raise ValueError('account observation schema is incompatible')


def install_observation_schema(connection):
    if not connection.in_transaction:
        raise ValueError('account observation migration requires an owner transaction')
    connection.execute(OBSERVATION_ORIGIN_SCHEMA)
    connection.execute(OBSERVATION_SCHEMA)
    assert_observation_schema(connection)


def check_observation_coverage(connection):
    return connection.execute('SELECT 1 FROM account_observation_origins o LEFT JOIN account_observation_requests r '
        'ON r.project_id=o.project_id AND r.request_key=o.request_key WHERE r.request_key IS NULL OR '
        'r.parameters_digest<>o.parameters_digest OR r.observed_at_ms<>o.observed_at_ms UNION ALL '
        'SELECT 1 FROM account_observation_requests r LEFT JOIN account_observation_origins o '
        'ON o.project_id=r.project_id AND o.request_key=r.request_key WHERE o.request_key IS NULL LIMIT 1').fetchone() is None
