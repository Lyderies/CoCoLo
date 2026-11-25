"""Connection-local verified supersession, rebuilt by bounded collector maintenance.

This projection only reduces capture candidates. Every selected signature still
passes through the ordinary worker and exact recapture. Persistent history and
its reference graph are untouched for cited rows. No schema upgrade is needed.
"""
from __future__ import annotations

import hashlib

from .learning_contracts import MAX_RECORD_BYTES, parse_bounded_json


def initialize(connection):
    # TEMP data belongs to this writer connection and is never backed up or
    # accepted from an imported ledger. Store exact inputs, not just record IDs.
    connection.execute(
        'CREATE TEMP TABLE attestation_checks('
        'record_rowid INTEGER PRIMARY KEY,project_id TEXT,record_id TEXT,'
        'source_sequence INTEGER,observed_at_ms INTEGER,content BLOB,content_digest TEXT,'
        'source TEXT,signature BLOB,owner_sequence INTEGER,signer_line BLOB,'
        'owner_reason TEXT,owner_recorded_ms INTEGER,'
        'provider TEXT,account_fingerprint TEXT,valid INTEGER,current INTEGER,superseded INTEGER)')
    connection.execute('CREATE INDEX temp.attestation_checks_group ON attestation_checks('
                       'project_id,provider,account_fingerprint,current,valid,observed_at_ms,source_sequence)')

    connection.execute('CREATE INDEX temp.attestation_checks_active ON attestation_checks('
                       'project_id,provider,account_fingerprint,observed_at_ms,source_sequence) '
                       'WHERE current=1 AND valid=1 AND superseded=0')
    connection.execute('CREATE INDEX temp.attestation_checks_identity ON attestation_checks(project_id,record_id)')
    connection.execute('CREATE INDEX temp.attestation_checks_due ON attestation_checks('
                       'current,superseded,record_rowid)')
    # Materialize attestation membership for both providers, including unchecked rows.
    # All reads start at these indexes, never at retained superseded history.
    connection.execute('CREATE TEMP TABLE capture_candidates('
                       'consumer TEXT,record_rowid INTEGER,project_id TEXT,record_type TEXT,'
                       'source_sequence INTEGER,PRIMARY KEY(consumer,record_rowid)) WITHOUT ROWID')
    connection.execute('CREATE INDEX temp.capture_candidates_signature ON capture_candidates('
                       'consumer,project_id,record_type,source_sequence)')
    connection.execute('CREATE INDEX temp.capture_candidates_record ON capture_candidates(record_rowid)')
    rebuild = (
        'UPDATE attestation_checks SET current=EXISTS('
        'SELECT 1 FROM records r LEFT JOIN owner_attestations a ON '
        'a.project_id=r.project_id AND a.subject_record_id=r.record_id WHERE ' + _matches('r', 'a', 'attestation_checks') + ');'
        'UPDATE attestation_checks SET superseded=(current=1 AND valid=1 AND EXISTS('
        'SELECT 1 FROM attestation_checks v WHERE v.project_id=attestation_checks.project_id '
        'AND v.provider=attestation_checks.provider AND v.account_fingerprint=attestation_checks.account_fingerprint '
        'AND v.current=1 AND v.valid=1 AND '
        '(v.observed_at_ms,v.source_sequence)>(attestation_checks.observed_at_ms,attestation_checks.source_sequence)));'
        'DELETE FROM capture_candidates;'
        + _populate_sql() + ';'
    )
    connection.execute(_populate_sql())
    # These TEMP triggers observe the sole writer connection. Mutations restore
    # conservative membership atomically, including dependants of a changed
    # superseder. Cached exact checks survive a reverted edit, as before.
    connection.execute('CREATE TEMP TRIGGER capture_record_insert AFTER INSERT ON main.records '
                       "WHEN NEW.record_type='billing_attestation' BEGIN "
                       'INSERT INTO capture_candidates SELECT consumer,NEW.rowid,NEW.project_id,'
                       'NEW.record_type,NEW.source_sequence FROM '
                       "(SELECT 'codex' consumer UNION ALL SELECT 'claude'); END")
    connection.execute('CREATE TEMP TRIGGER capture_record_update AFTER UPDATE ON main.records '
                       'WHEN EXISTS(SELECT 1 FROM attestation_checks WHERE record_rowid IN (OLD.rowid,NEW.rowid)) '
                       'BEGIN ' + rebuild + ' END')
    connection.execute('CREATE TEMP TRIGGER capture_unchecked_update AFTER UPDATE ON main.records '
                       'WHEN NOT EXISTS(SELECT 1 FROM attestation_checks WHERE record_rowid IN (OLD.rowid,NEW.rowid)) '
                       'BEGIN DELETE FROM capture_candidates WHERE record_rowid=OLD.rowid;'
                       'INSERT INTO capture_candidates SELECT consumer,NEW.rowid,NEW.project_id,'
                       'NEW.record_type,NEW.source_sequence FROM '
                       "(SELECT 'codex' consumer UNION ALL SELECT 'claude') "
                       "WHERE NEW.record_type='billing_attestation'; END")
    connection.execute('CREATE TEMP TRIGGER capture_record_delete AFTER DELETE ON main.records BEGIN '
                       'DELETE FROM capture_candidates WHERE record_rowid=OLD.rowid;'
                       'DELETE FROM attestation_checks WHERE record_rowid=OLD.rowid; END')
    # Record deletion must restore the surviving predecessor even when there is
    # no signature-delete cascade. Only deleting a current valid group winner
    # needs this indexed repair; ordinary superseded-row sweeps stay bounded.
    connection.execute('CREATE TEMP TRIGGER capture_check_delete AFTER DELETE ON attestation_checks '
                       'WHEN OLD.current=1 AND OLD.valid=1 AND OLD.superseded=0 BEGIN '
                       'UPDATE attestation_checks SET superseded=0 WHERE record_rowid=('
                       'SELECT record_rowid FROM attestation_checks WHERE project_id=OLD.project_id '
                       'AND provider=OLD.provider AND account_fingerprint=OLD.account_fingerprint '
                       'AND current=1 AND valid=1 ORDER BY observed_at_ms DESC,source_sequence DESC LIMIT 1);'
                       'INSERT OR IGNORE INTO capture_candidates '
                       "SELECT provider,record_rowid,project_id,'billing_attestation',source_sequence "
                       'FROM attestation_checks WHERE project_id=OLD.project_id AND provider=OLD.provider '
                       'AND account_fingerprint=OLD.account_fingerprint AND current=1 AND valid=1 '
                       'AND superseded=0; END')
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        reference = 'OLD' if event == 'DELETE' else 'NEW'
        identity = f'(c.project_id={reference}.project_id AND c.record_id={reference}.subject_record_id)'
        if event == 'UPDATE':
            identity += ' OR (c.project_id=OLD.project_id AND c.record_id=OLD.subject_record_id)'
        condition = '(' + identity + ')'
        if event == 'DELETE':
            # A direct signature deletion invalidates even a superseded row.
            # Cascading deletion of an already superseded record needs no
            # group rebuild: its verified successor is still present.
            condition += ' AND (c.superseded=0 OR EXISTS(SELECT 1 FROM records r WHERE r.rowid=c.record_rowid))'
        connection.execute(f'CREATE TEMP TRIGGER capture_signature_{event.lower()} AFTER {event} '
                           'ON main.owner_attestations WHEN EXISTS(SELECT 1 FROM attestation_checks c WHERE '
                           + condition + ') BEGIN ' + rebuild + ' END')
    connection.execute('CREATE TEMP TRIGGER capture_owner_insert AFTER INSERT ON main.owner_key_history BEGIN '
                       + rebuild + ' END')


def _populate_sql():
    return (
        'INSERT INTO capture_candidates SELECT providers.consumer,r.rowid,r.project_id,r.record_type,r.source_sequence '
        "FROM records r CROSS JOIN (SELECT 'codex' consumer UNION ALL SELECT 'claude') providers "
        'LEFT JOIN attestation_checks c ON c.record_rowid=r.rowid '
        "WHERE r.record_type='billing_attestation' AND (c.current IS NOT 1 "
        'OR (c.valid=1 AND c.superseded=0 AND c.provider=providers.consumer))'
    )


def _matches(record, signature, check):
    return (
        f"{record}.record_type='billing_attestation' AND {check}.record_rowid={record}.rowid "
        f'AND {check}.project_id={record}.project_id '
        f'AND {check}.record_id={record}.record_id AND {check}.source_sequence={record}.source_sequence '
        f'AND {check}.observed_at_ms={record}.observed_at_ms AND {check}.content={record}.content '
        f'AND {check}.content_digest={record}.content_digest AND {check}.source={record}.source '
        f'AND {check}.signature IS {signature}.signature '
        f'AND {check}.owner_sequence=(SELECT sequence FROM owner_key_history ORDER BY sequence DESC LIMIT 1) '
        f'AND {check}.signer_line=(SELECT signer_line FROM owner_key_history ORDER BY sequence DESC LIMIT 1) '
        f'AND {check}.owner_reason=(SELECT reason FROM owner_key_history ORDER BY sequence DESC LIMIT 1) '
        f'AND {check}.owner_recorded_ms=(SELECT recorded_at_ms FROM owner_key_history ORDER BY sequence DESC LIMIT 1)'
    )



_ROW_SELECT = (
    'SELECT r.rowid,r.project_id,r.record_id,r.source_sequence,r.observed_at_ms,'
    "CASE WHEN typeof(r.content)='blob' AND length(r.content)<=? THEN r.content END,"
    'r.content_digest,r.source,'
    "CASE WHEN typeof(a.signature)='blob' AND length(a.signature)<=16384 THEN a.signature END "
    'FROM records r LEFT JOIN owner_attestations a '
    'ON a.project_id=r.project_id AND a.subject_record_id=r.record_id '
)


def sweep(ledger, batch, cited_sql):
    """Snapshot bounded work, verify unlocked, then recheck and publish briefly."""
    if type(batch) is not int or batch < 1:
        return 0
    from .learning_engine import OwnerGrantAuthority

    connection = ledger._connection
    with ledger._held():
        if ledger._readers() or connection.in_transaction:
            return 0
        owner = ledger._owner_generation_locked()
        signer = ledger._current_signer_bytes()
        if owner is None or owner[1] != signer:
            return 0
        after = getattr(ledger, '_attestation_sweep_after', 0)
        rows = connection.execute(
            _ROW_SELECT + "WHERE r.record_type='billing_attestation' AND r.rowid>? ORDER BY r.rowid LIMIT ?",
            (MAX_RECORD_BYTES, after, batch)).fetchall()
        pending = []
        for row in rows:
            values = tuple(row)
            previous = connection.execute(
                'SELECT record_rowid,project_id,record_id,source_sequence,observed_at_ms,content,'
                'content_digest,source,signature,owner_sequence,signer_line,owner_reason,owner_recorded_ms '
                'FROM temp.attestation_checks WHERE record_rowid=?', (row[0],)).fetchone()
            if previous is None or tuple(previous) != values + owner:
                pending.append(values)
        ledger._attestation_sweep_after = rows[-1][0] if len(rows) == batch else 0

    verdicts = []
    try:
        authority = OwnerGrantAuthority(ledger.state.allowed_signers_path, signer_line=signer,
                                        raise_verifier_failures=True)
    except Exception:
        authority = None
    for values in pending if authority is not None else ():
        rowid, project, identifier, sequence, observed, content, digest, source, signature = values
        if (type(content) is not bytes or not content or type(sequence) is not int or sequence < 1
                or type(observed) is not int or source != 'owner'
                or hashlib.sha256(content).hexdigest() != digest):
            continue
        try:
            record = parse_bounded_json(content)
            provider, account = record['provider'], record['account_fingerprint']
            if (record['record_type'] != 'billing_attestation' or record['observed_at_ms'] != observed
                    or record['record_id'] != identifier
                    or type(provider) is not str or type(account) is not str):
                continue
            valid = bool(signature) and bool(authority.verify_owner_record(project, record, signature))
        except Exception:
            # Includes TimeoutExpired and other verifier failures. No verdict is
            # recorded, so transient failures remain charged and retryable.
            continue
        verdicts.append((values, provider, account, int(valid)))

    with ledger._held():
        if ledger._readers() or connection.in_transaction:
            return 0
        if ledger._owner_generation_locked() != owner or ledger._current_signer_bytes() != signer:
            return 0
        with ledger._account_transaction():
            for values, provider, account, valid in verdicts:
                rowid, project = values[:2]
                current = connection.execute(_ROW_SELECT + "WHERE r.rowid=? AND r.record_type='billing_attestation'", (MAX_RECORD_BYTES, rowid)).fetchone()
                if current is None or tuple(current) != values:
                    continue
                superseded = valid and connection.execute(
                    'SELECT 1 FROM temp.attestation_checks WHERE project_id=? AND provider=? '
                    'AND account_fingerprint=? AND current=1 AND valid=1 AND superseded=0 '
                    'AND (observed_at_ms,source_sequence)>(?,?) LIMIT 1',
                    (project, provider, account, values[4], values[3])).fetchone() is not None
                connection.execute('INSERT OR REPLACE INTO temp.attestation_checks VALUES('
                                   '?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   values + owner + (provider, account, valid, 1, int(superseded)))
                if valid:
                    older = connection.execute(
                        'SELECT record_rowid FROM temp.attestation_checks WHERE project_id=? AND provider=? '
                        'AND account_fingerprint=? AND current=1 AND valid=1 AND superseded=0 '
                        'AND (observed_at_ms,source_sequence)<(?,?)',
                        (project, provider, account, values[4], values[3])).fetchall()
                    connection.executemany('DELETE FROM temp.capture_candidates WHERE record_rowid=?', older)
                    connection.executemany('UPDATE temp.attestation_checks SET superseded=1 WHERE record_rowid=?', older)
                connection.execute('DELETE FROM temp.capture_candidates WHERE record_rowid=? '
                                   'AND (?=0 OR ?=1 OR consumer<>?)', (rowid, valid, int(superseded), provider))
            # Bound candidates BEFORE checking references. Cited history cannot
            # make one pass walk an unbounded prefix or starve later deletion.
            after = getattr(ledger, '_attestation_due_after', 0)
            due = connection.execute(
                'SELECT record_rowid FROM temp.attestation_checks '
                'WHERE current=1 AND superseded=1 AND record_rowid>? ORDER BY record_rowid LIMIT ?',
                (after, batch)).fetchall()
            removed = 0
            for row in due:
                removed += connection.execute('DELETE FROM records AS r WHERE r.rowid=? AND NOT '
                                              + cited_sql('r'), (row[0],)).rowcount
            ledger._attestation_due_after = due[-1][0] if len(due) == batch else 0
        return removed
