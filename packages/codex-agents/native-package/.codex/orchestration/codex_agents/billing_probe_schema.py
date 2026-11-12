"""Original billing-probe intent; an admitted row remains active until confirmed return."""

BILLING_PROBE_ORIGIN_COLUMNS = ('request_key', 'parameters_digest', 'created_at_ms', 'origin_seal')
BILLING_PROBE_COLUMNS = ('request_key', 'parameters_digest', 'runtime_digest', 'caller_scope',
    'created_at_ms', 'deadline_at_ms', 'state', 'account_fingerprint', 'admission_record_id',
    'admission_digest', 'references_json', 'state_seal')

BILLING_PROBE_ORIGIN_SCHEMA = '''CREATE TABLE account_billing_probe_origins(
project_id TEXT NOT NULL,request_key TEXT NOT NULL,parameters_digest TEXT NOT NULL,
created_at_ms INTEGER NOT NULL CHECK(typeof(created_at_ms)='integer' AND created_at_ms>=0),
origin_seal TEXT NOT NULL,PRIMARY KEY(project_id,request_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''

BILLING_PROBE_SCHEMA = '''CREATE TABLE account_billing_probe_requests(
project_id TEXT NOT NULL,request_key TEXT NOT NULL,parameters_digest TEXT NOT NULL,
runtime_digest TEXT NOT NULL,caller_scope TEXT NOT NULL,
created_at_ms INTEGER NOT NULL CHECK(typeof(created_at_ms)='integer' AND created_at_ms>=0),
deadline_at_ms INTEGER NOT NULL CHECK(typeof(deadline_at_ms)='integer' AND deadline_at_ms>created_at_ms),
state TEXT NOT NULL CHECK(state IN ('reserved','admitted','committed')),
account_fingerprint TEXT,admission_record_id TEXT,admission_digest TEXT,references_json TEXT,
state_seal TEXT NOT NULL,
CHECK((state='reserved' AND account_fingerprint IS NULL AND admission_record_id IS NULL
AND admission_digest IS NULL AND references_json IS NULL)
OR (state='admitted' AND account_fingerprint IS NOT NULL AND length(account_fingerprint)=64
AND admission_record_id IS NOT NULL AND admission_digest IS NOT NULL
AND length(admission_digest)=64 AND references_json IS NULL)
OR (state='committed' AND references_json IS NOT NULL AND length(references_json) BETWEEN 1 AND 4096)),
PRIMARY KEY(project_id,request_key),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
FOREIGN KEY(project_id,request_key) REFERENCES account_billing_probe_origins(project_id,request_key)
DEFERRABLE INITIALLY DEFERRED)'''

BILLING_PROBE_OBJECTS = (
    ('table', 'account_billing_probe_origins', BILLING_PROBE_ORIGIN_SCHEMA),
    ('table', 'account_billing_probe_requests', BILLING_PROBE_SCHEMA),
    ('index', 'billing_probe_active_machine', "CREATE INDEX billing_probe_active_machine ON account_billing_probe_requests(state) WHERE state='admitted'"),
    ('index', 'billing_probe_active_account', "CREATE INDEX billing_probe_active_account ON account_billing_probe_requests(account_fingerprint,state) WHERE state='admitted'"),
)


def assert_billing_probe_schema(connection):
    for kind, name, sql in BILLING_PROBE_OBJECTS:
        row = connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', (kind, name)).fetchone()
        if row is None or ' '.join(row[0].split()) != ' '.join(sql.split()):
            raise ValueError('billing probe schema is incompatible')


def install_billing_probe_schema(connection):
    if not connection.in_transaction:
        raise ValueError('billing probe migration requires an owner transaction')
    for _, _, sql in BILLING_PROBE_OBJECTS:
        connection.execute(sql)
    assert_billing_probe_schema(connection)


def check_billing_probe_coverage(connection):
    return connection.execute('SELECT 1 FROM account_billing_probe_origins o '
        'LEFT JOIN account_billing_probe_requests r ON r.project_id=o.project_id AND r.request_key=o.request_key '
        'WHERE r.request_key IS NULL OR r.parameters_digest<>o.parameters_digest OR r.created_at_ms<>o.created_at_ms '
        'UNION ALL SELECT 1 FROM account_billing_probe_requests r LEFT JOIN account_billing_probe_origins o '
        'ON o.project_id=r.project_id AND o.request_key=r.request_key WHERE o.request_key IS NULL LIMIT 1').fetchone() is None
