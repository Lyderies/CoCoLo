"""Project-writer cleanup journal; metadata never authenticates a native peer.

Every new lease reserves its bounded cleanup slot before account admission.
Legacy leases are explicitly unreserved; migration cannot invent cleanup facts.
Native custody and closed-descendant containment remain separate requirements.
"""
import secrets
from dataclasses import replace

from .history_contracts import HistoryError, _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest

MAX_CLEANUP_RESERVATIONS = 4096
MAX_CLEANUP_EVIDENCE_BYTES = 16_384

CLEANUP_SCHEMA_V9 = """
CREATE TABLE project_cleanup_reservations(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
 binding BLOB NOT NULL,lease_digest TEXT NOT NULL,reservation_digest TEXT NOT NULL,
 cleanup_digest TEXT,state_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt),UNIQUE(project_id,reservation_digest),
 FOREIGN KEY(project_id,run_handle,attempt) REFERENCES result_leases(project_id,run_handle,attempt));
CREATE TABLE project_cleanup_receipts(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
 claim_id TEXT NOT NULL,evidence BLOB NOT NULL,cleanup_digest TEXT NOT NULL,journal_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt),UNIQUE(project_id,claim_id),
 FOREIGN KEY(project_id,run_handle,attempt) REFERENCES project_cleanup_reservations(project_id,run_handle,attempt));
"""
CLEANUP_SCHEMA = CLEANUP_SCHEMA_V9.replace('cleanup_digest TEXT,state_digest TEXT NOT NULL,',
    'cleanup_digest TEXT,state_digest TEXT NOT NULL, account_release_digest TEXT,')
CLEANUP_COLUMNS = {
    'project_cleanup_reservations': ('project_id', 'run_handle', 'attempt', 'binding', 'lease_digest',
                                     'reservation_digest', 'cleanup_digest', 'state_digest', 'account_release_digest'),
    'project_cleanup_receipts': ('project_id', 'run_handle', 'attempt', 'claim_id', 'evidence',
                                'cleanup_digest', 'journal_seal'),
}


def _reservation_digest(store, lease):
    return keyed_digest(store._result_key, canonical_json({
        'domain': 'project-cleanup-reservation-v1', 'binding': lease.binding.as_data(),
        'lease_digest': lease.lease_digest,
    }))


def _state_seal(store, reservation_digest, cleanup_digest, *, account_release_digest=None):
    material = {
        'domain': ('project-cleanup-reservation-state-v1' if account_release_digest is None
                   else 'project-cleanup-reservation-state-v2'),
        'reservation_digest': reservation_digest, 'cleanup_digest': cleanup_digest,
    }
    if account_release_digest is not None:
        material['account_release_digest'] = account_release_digest
    return keyed_digest(store._result_key, canonical_json(material))


def _journal_seal(store, lease, claim_id, digest):
    return keyed_digest(store._result_key, canonical_json({
        'domain': 'project-cleanup-journal-v1', 'reservation_digest': lease.cleanup_reservation_digest,
        'project_id': lease.binding.project_id, 'run_handle': lease.binding.run_handle,
        'attempt': lease.binding.attempt, 'claim_id': claim_id, 'cleanup_digest': digest,
    }))


def reserve_cleanup(store, lease):
    """Inside the original lease transaction, never as a post-launch retry."""
    count = store._connection.execute('SELECT COUNT(*) FROM project_cleanup_reservations').fetchone()[0]
    if count >= MAX_CLEANUP_RESERVATIONS:
        raise HistoryError('history_result_capacity')
    digest = _reservation_digest(store, lease)
    store._connection.execute('INSERT INTO project_cleanup_reservations VALUES(?,?,?,?,?,?,NULL,?,NULL)',
        (lease.binding.project_id, lease.binding.run_handle, lease.binding.attempt,
         canonical_json(lease.binding.as_data()), lease.lease_digest, digest, _state_seal(store, digest, None)))
    return replace(lease, cleanup_reservation_digest=digest)


def check_cleanup_reservation(store, lease):
    """Verify both original companions; absence cannot permit reminting."""
    identity = (lease.binding.project_id, lease.binding.run_handle, lease.binding.attempt)
    row = store._connection.execute('SELECT * FROM project_cleanup_reservations '
        'WHERE project_id=? AND run_handle=? AND attempt=?', identity).fetchone()
    receipt = store._connection.execute('SELECT * FROM project_cleanup_receipts '
        'WHERE project_id=? AND run_handle=? AND attempt=?', identity).fetchone()
    if lease.cleanup_reservation_digest is None:
        if row is not None or receipt is not None:
            raise HistoryError('history_incompatible')
        from .project_release_ack import check_release_ack_companion
        check_release_ack_companion(store, lease, None)
        return None
    if row is None:
        raise HistoryError('history_incompatible')
    digest = row['cleanup_digest']
    if (store._stored_blob(row['binding'], maximum=4096) != canonical_json(lease.binding.as_data())
            or row['lease_digest'] != lease.lease_digest
            or row['reservation_digest'] != lease.cleanup_reservation_digest
            or row['reservation_digest'] != _reservation_digest(store, lease)
            or not matches(row['state_digest'], _DIGEST)
            or not secrets.compare_digest(row['state_digest'], _state_seal(store, row['reservation_digest'], digest,
                account_release_digest=row['account_release_digest']))
            or (digest is not None and not matches(digest, _DIGEST))
            or (digest is None) != (receipt is None)):
        raise HistoryError('history_incompatible')
    if receipt is not None:
        raw = store._stored_blob(receipt['evidence'], maximum=MAX_CLEANUP_EVIDENCE_BYTES)
        if (lease.state != 'bound' or receipt['claim_id'] != lease.claim_id
                or receipt['cleanup_digest'] != digest or keyed_digest(store._result_key, raw) != digest
                or not matches(receipt['journal_seal'], _DIGEST)
                or not secrets.compare_digest(receipt['journal_seal'], _journal_seal(store, lease, lease.claim_id, digest))):
            raise HistoryError('history_incompatible')
    from .project_release_ack import check_release_ack_companion
    check_release_ack_companion(store, lease, row)
    return row


def _original_material(store, binding):
    from .cleanup_contracts import CleanupEvidence
    from .terminal_delivery import terminal_receipt, verify_terminal_evidence

    lease = store._lease_row(binding)
    if lease.state != 'bound' or lease.cleanup_reservation_digest is None:
        raise HistoryError('history_result_not_ready')
    attempt_row = store._connection.execute('SELECT binding FROM result_reservations '
        'WHERE project_id=? AND claim_id=?', (binding.project_id, lease.claim_id)).fetchone()
    if attempt_row is None:
        raise HistoryError('history_incompatible')
    from .history_contracts import AttemptBinding
    from .learning_contracts import parse_bounded_json
    attempt = AttemptBinding(**parse_bounded_json(store._stored_blob(attempt_row['binding'], maximum=4096)))
    evidence = verify_terminal_evidence(store._result_key, binding, store.attest_terminal_delivery(attempt))
    # This new cleanup protocol never downgrades a missing managed original to
    # the explicit pre-journal fixture/legacy execution path. Both original
    # provenance and its independent host-open journal remain indispensable.
    store._execution_provenance(evidence.grant)
    consumed = store._connection.execute('SELECT * FROM project_execution_consumptions '
        'WHERE project_id=? AND claim_id=?', (binding.project_id, lease.claim_id)).fetchone()
    provenance = store.record(store._execution_provenance_id(evidence.grant))
    if (consumed is None or consumed['registration_digest'] != evidence.grant.registration_digest
            or not valid_integer(consumed['consumed_at_ms'], minimum=evidence.grant.claimed_at_ms,
                                 maximum=evidence.grant.deadline_at_ms - 1)
            or provenance.observed_at_ms != consumed['consumed_at_ms']
            or provenance.ingested_at_ms != consumed['consumed_at_ms']):
        raise HistoryError('history_incompatible')
    row = store._connection.execute('SELECT receipt_digest FROM terminal_outbox '
        'WHERE project_id=? AND claim_id=?', (binding.project_id, lease.claim_id)).fetchone()
    expected = terminal_receipt(store._result_key, evidence.grant, evidence.delivery, known=False)['receipt_digest']
    if row is None or row['receipt_digest'] != expected:
        raise HistoryError('history_result_not_ready')
    return lease, CleanupEvidence(binding, evidence.grant, evidence.delivery, expected)


def _current_cleanup(registry, custody, value):
    from .owned_execution import ExecutionCustodyError, OwnedExecutionKey, OwnedExecutionRegistry
    if type(registry) is not OwnedExecutionRegistry:
        raise HistoryError('history_not_authorized')
    try:
        snapshot = registry.cleanup_snapshot(custody)
        grant = value.grant
        if (snapshot['key'] != OwnedExecutionKey(value.binding.project_id, grant.handle_digest,
                grant.binding.run_id, grant.binding.claim_id) or snapshot['deadline_ns'] != grant.deadline_ns):
            raise ExecutionCustodyError()
    except ExecutionCustodyError:
        raise HistoryError('history_not_authorized') from None


def read_cleanup(store, binding):
    """Recover the same original bytes, never a new cleanup assertion/window."""
    from .cleanup_contracts import CleanupError, verify_cleanup_evidence
    lease, value = _original_material(store, binding)
    row = store._connection.execute('SELECT evidence FROM project_cleanup_receipts '
        'WHERE project_id=? AND run_handle=? AND attempt=?',
        (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
    if row is None:
        raise HistoryError('history_result_not_ready')
    raw = store._stored_blob(row['evidence'], maximum=MAX_CLEANUP_EVIDENCE_BYTES)
    try:
        actual = verify_cleanup_evidence(store._result_key, binding, raw)
    except CleanupError:
        raise HistoryError('history_incompatible') from None
    if actual != value or lease.claim_id != actual.grant.binding.claim_id:
        raise HistoryError('history_incompatible')
    return raw


def issue_cleanup(store, binding, *, registry, custody):
    """Issue once from the original live returned worker, on its chosen writer.

    No registry lock spans SQL. Check custody after each write and before every
    successful return; a lost response recovers only the durable original.
    This still supplies no native peer/profile or closed-descendant proof.
    """
    from .cleanup_contracts import encode_cleanup_evidence
    with store._transaction():
        lease, value = _original_material(store, binding)
        _current_cleanup(registry, custody, value)
        existing = store._connection.execute('SELECT 1 FROM project_cleanup_receipts '
            'WHERE project_id=? AND run_handle=? AND attempt=?',
            (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
        if existing is not None:
            raw = read_cleanup(store, binding)
        else:
            raw = encode_cleanup_evidence(store._result_key, value)
            digest = keyed_digest(store._result_key, raw)
            store._connection.execute('INSERT INTO project_cleanup_receipts VALUES(?,?,?,?,?,?,?)',
                (binding.project_id, binding.run_handle, binding.attempt, lease.claim_id, raw, digest,
                 _journal_seal(store, lease, lease.claim_id, digest)))
            _current_cleanup(registry, custody, value)
            store._connection.execute('UPDATE project_cleanup_reservations SET cleanup_digest=?,state_digest=? '
                'WHERE project_id=? AND run_handle=? AND attempt=? AND cleanup_digest IS NULL',
                (digest, _state_seal(store, lease.cleanup_reservation_digest, digest),
                 binding.project_id, binding.run_handle, binding.attempt))
            _current_cleanup(registry, custody, value)
            if read_cleanup(store, binding) != raw:
                raise HistoryError('history_incompatible')
        _current_cleanup(registry, custody, value)
    _current_cleanup(registry, custody, value)
    return raw
