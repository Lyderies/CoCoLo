"""Account-owner cleanup journal reserved before registered execution.

Original fields survive missing companions. Migration adds no cleanup fact or
permission, and leaves legacy execution origins and release digests unchanged.
"""
RELEASE_COLUMN = ('cleanup_reservation_digest TEXT REFERENCES account_cleanup_receipts(reservation_digest) '
                  'DEFERRABLE INITIALLY DEFERRED')
RELEASE_SCHEMA = '''CREATE TABLE account_cleanup_receipts(
project_id TEXT NOT NULL,handle_digest TEXT NOT NULL,run_id TEXT NOT NULL,bridge_run_id TEXT NOT NULL,
registration_digest TEXT NOT NULL,lease_digest TEXT NOT NULL,binding_digest TEXT NOT NULL,
reservation_digest TEXT NOT NULL UNIQUE,terminal_record_id TEXT,terminal_content_digest TEXT,
result_digest TEXT,terminal_receipt_digest TEXT,terminal_recorded_at_ms INTEGER,cleanup_digest TEXT,released_at_ms INTEGER,
receipt_digest TEXT,state_seal TEXT NOT NULL,
PRIMARY KEY(project_id,handle_digest),UNIQUE(project_id,run_id),UNIQUE(project_id,bridge_run_id),
FOREIGN KEY(project_id,handle_digest) REFERENCES active_run_capacity(project_id,handle_digest) ON DELETE CASCADE,
FOREIGN KEY(project_id,handle_digest,run_id,bridge_run_id)
REFERENCES launch_claims(project_id,handle_digest,run_id,bridge_run_id))'''
RELEASE_COLUMNS = ('handle_digest', 'run_id', 'bridge_run_id', 'registration_digest', 'lease_digest',
                   'binding_digest', 'reservation_digest', 'terminal_record_id', 'terminal_content_digest',
                   'result_digest', 'terminal_receipt_digest', 'terminal_recorded_at_ms', 'cleanup_digest', 'released_at_ms',
                   'receipt_digest', 'state_seal')


def assert_cleanup_schema(connection):
    from .active_run_capacity import CAPACITY_EXECUTIONS_SCHEMA
    def normalized(sql):
        return ' '.join(sql.replace('IF NOT EXISTS ', '').strip().rstrip(';').split())
    expected = CAPACITY_EXECUTIONS_SCHEMA.replace('    PRIMARY KEY(project_id,handle_digest),',
        ' ' + RELEASE_COLUMN + ',\n    PRIMARY KEY(project_id,handle_digest),')
    for name, sql in (('capacity_executions', expected), ('account_cleanup_receipts', RELEASE_SCHEMA)):
        actual = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if actual is None or type(actual[0]) is not str or normalized(actual[0]) != normalized(sql):
            raise ValueError('account cleanup schema is incompatible')


def install_cleanup_schema(connection):
    if not connection.in_transaction:
        raise ValueError('account cleanup migration requires an owner transaction')
    connection.execute(RELEASE_SCHEMA)
    connection.execute('ALTER TABLE capacity_executions ADD COLUMN ' + RELEASE_COLUMN)
    assert_cleanup_schema(connection)


def check_cleanup_coverage(connection):
    missing = connection.execute('SELECT 1 FROM capacity_executions e LEFT JOIN account_cleanup_receipts c '
        'ON c.project_id=e.project_id AND c.reservation_digest=e.cleanup_reservation_digest '
        'WHERE e.cleanup_reservation_digest IS NOT NULL AND '
        '(c.handle_digest IS NULL OR c.handle_digest<>e.handle_digest OR c.run_id<>e.run_id '
        'OR c.bridge_run_id<>e.bridge_run_id OR c.registration_digest<>e.owner_digest) LIMIT 1').fetchone()
    orphan = connection.execute('SELECT 1 FROM account_cleanup_receipts c LEFT JOIN capacity_executions e '
        'ON e.project_id=c.project_id AND e.handle_digest=c.handle_digest '
        'WHERE e.cleanup_reservation_digest IS NULL OR e.cleanup_reservation_digest<>c.reservation_digest LIMIT 1').fetchone()
    return missing is None and orphan is None
