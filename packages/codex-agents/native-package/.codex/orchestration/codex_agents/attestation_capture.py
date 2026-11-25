"""Connection-local verified supersession, rebuilt by bounded collector maintenance.

This projection only reduces capture candidates. Every selected signature still
passes through the ordinary worker and exact recapture. Persistent history and
its reference graph are untouched for cited rows. No schema upgrade is needed.
"""
from __future__ import annotations

import hashlib

from .contracts import ContractError
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
        'provider TEXT,account_fingerprint TEXT,valid INTEGER)')
    connection.execute('CREATE INDEX temp.attestation_checks_group ON attestation_checks('
                       'project_id,provider,account_fingerprint,valid,observed_at_ms,source_sequence)')


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


def superseded_sql(record='r', signature='a'):
    """Only an exact checked row and an exact verified later row can supersede."""
    return (
        'EXISTS(SELECT 1 FROM temp.attestation_checks c WHERE '
        + _matches(record, signature, 'c') + ' AND c.valid=1 AND EXISTS('
        'SELECT 1 FROM temp.attestation_checks v JOIN records n ON n.rowid=v.record_rowid '
        'LEFT JOIN owner_attestations s ON s.project_id=n.project_id AND s.subject_record_id=n.record_id '
        'WHERE v.project_id=c.project_id AND v.provider=c.provider '
        'AND v.account_fingerprint=c.account_fingerprint AND v.valid=1 '
        'AND (v.observed_at_ms,v.source_sequence)>(c.observed_at_ms,c.source_sequence) AND '
        + _matches('n', 's', 'v') + '))'
    )


def candidate_sql(record='r', signature='a'):
    return (
        'NOT EXISTS(SELECT 1 FROM temp.attestation_checks c WHERE '
        + _matches(record, signature, 'c') + ' AND c.valid=0) AND NOT '
        + superseded_sql(record, signature)
    )


def provider_sql(record='r', parameter=':provider'):
    # Only exact checked foreign-provider rows can be excluded before bounded
    # header reads. Unknown or changed inputs remain conservatively charged.
    return (
        'NOT EXISTS(SELECT 1 FROM temp.attestation_checks c WHERE '
        + _matches(record, 'a', 'c') + f' AND c.provider<>{parameter})'
    )


def sweep(ledger, batch, cited_sql):
    """Inspect at most batch rows and delete at most batch uncited superseded rows.

    A sequence cursor moves past cited/invalid rows too, so they cannot starve
    later history. A completed pass starts again to notice edits to older rows.
    """
    if type(batch) is not int or batch < 1:
        return 0
    from .learning_engine import OwnerGrantAuthority

    connection = ledger._connection
    owner = ledger._owner_generation_locked()
    signer = ledger._current_signer_bytes()
    if owner is None or owner[1] != signer:
        return 0
    after = getattr(ledger, '_attestation_sweep_after', 0)
    rows = connection.execute(
        'SELECT r.rowid,r.project_id,r.record_id,r.source_sequence,r.observed_at_ms,'
        'CASE WHEN typeof(r.content)=\'blob\' AND length(r.content)<=? THEN r.content END,'
        'r.content_digest,r.source,'
        'CASE WHEN typeof(a.signature)=\'blob\' AND length(a.signature)<=16384 THEN a.signature END '
        'FROM records r LEFT JOIN owner_attestations a '
        'ON a.project_id=r.project_id AND a.subject_record_id=r.record_id '
        'WHERE r.record_type=\'billing_attestation\' AND r.rowid>? ORDER BY r.rowid LIMIT ?',
        (MAX_RECORD_BYTES, after, batch)).fetchall()
    authority = OwnerGrantAuthority(ledger.state.allowed_signers_path, signer_line=signer)
    with ledger._account_transaction():
        for row in rows:
            values = tuple(row)
            rowid, project, identifier, sequence, observed, content, digest, source, signature = values
            if (type(content) is not bytes or not content or type(sequence) is not int or sequence < 1
                    or type(observed) is not int or source != 'owner'
                    or hashlib.sha256(content).hexdigest() != digest):
                continue  # Incompatible metadata must still reach capture's checks.
            previous = connection.execute(
                'SELECT record_rowid,project_id,record_id,source_sequence,observed_at_ms,content,'
                'content_digest,source,signature,owner_sequence,signer_line,owner_reason,owner_recorded_ms,valid '
                'FROM temp.attestation_checks '
                'WHERE record_rowid=?', (rowid,)).fetchone()
            checked = values + owner
            if previous is not None and tuple(previous[:-1]) == checked and previous[-1] == 1:
                continue
            try:
                record = parse_bounded_json(content)
                provider, account = record['provider'], record['account_fingerprint']
                if (record['record_type'] != 'billing_attestation' or record['observed_at_ms'] != observed
                        or record['record_id'] != identifier):
                    continue
                valid = bool(signature) and authority.verify_owner_record(project, record, signature)
            except (ContractError, TypeError, ValueError, KeyError, OSError):
                continue
            connection.execute('INSERT OR REPLACE INTO temp.attestation_checks VALUES('
                               '?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', checked + (provider, account, int(valid)))
        due = connection.execute(
            'SELECT r.project_id,r.record_id,r.rowid FROM records r LEFT JOIN owner_attestations a '
            'ON a.project_id=r.project_id AND a.subject_record_id=r.record_id '
            "WHERE r.record_type='billing_attestation' AND r.source='owner' AND NOT "
            + cited_sql('r') + ' AND ' + superseded_sql() + ' ORDER BY r.rowid LIMIT ?', (batch,)).fetchall()
        connection.executemany('DELETE FROM records WHERE project_id=? AND record_id=?',
                               [(row[0], row[1]) for row in due])
        connection.execute('DELETE FROM temp.attestation_checks WHERE record_rowid IN ('
                           'SELECT c.record_rowid FROM temp.attestation_checks c WHERE NOT EXISTS('
                           'SELECT 1 FROM records r WHERE r.rowid=c.record_rowid) LIMIT ?)', (batch,))
    ledger._attestation_sweep_after = rows[-1][0] if len(rows) == batch else 0
    return len(due)
