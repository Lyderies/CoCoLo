"""Original project account-release acknowledgement, never native authority."""
import secrets

from .history_contracts import HistoryError, _DIGEST, matches
from .learning_contracts import canonical_json, keyed_digest

ACK_SCHEMA = '''
CREATE TABLE project_cleanup_acknowledgements(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
 receipt BLOB NOT NULL,ack_digest TEXT NOT NULL,journal_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt),
 FOREIGN KEY(project_id,run_handle,attempt) REFERENCES project_cleanup_receipts(project_id,run_handle,attempt));
'''
ACK_COLUMNS = {'project_cleanup_acknowledgements': ('project_id', 'run_handle', 'attempt', 'receipt',
                                                   'ack_digest', 'journal_seal')}
MAX_ACCOUNT_RELEASE_ACK_BYTES = 16_384


def _journal_seal(store, lease, cleanup_digest, ack_digest):
    return keyed_digest(store._result_key, b'project-account-release-ack-journal-v1\0' + canonical_json({
        'reservation_digest': lease.cleanup_reservation_digest, 'cleanup_digest': cleanup_digest,
        'ack_digest': ack_digest, 'binding': lease.binding.as_data()}))


def check_release_ack_companion(store, lease, reservation):
    row = store._connection.execute('SELECT * FROM project_cleanup_acknowledgements '
        'WHERE project_id=? AND run_handle=? AND attempt=?',
        (lease.binding.project_id, lease.binding.run_handle, lease.binding.attempt)).fetchone()
    digest = None if reservation is None else reservation['account_release_digest']
    if digest is None:
        if row is not None:
            raise HistoryError('history_incompatible')
        return None
    if row is None or not matches(digest, _DIGEST) or reservation['cleanup_digest'] is None:
        raise HistoryError('history_incompatible')
    raw = store._stored_blob(row['receipt'], maximum=MAX_ACCOUNT_RELEASE_ACK_BYTES)
    if (row['ack_digest'] != digest or keyed_digest(store._result_key, raw) != digest
            or not matches(row['journal_seal'], _DIGEST)
            or not secrets.compare_digest(row['journal_seal'], _journal_seal(store, lease, reservation['cleanup_digest'], digest))):
        raise HistoryError('history_incompatible')
    return raw


def _verified_ack(store, binding, raw):
    from .account_claim_contracts import ClaimError
    from .cleanup_contracts import verify_cleanup_evidence
    from .project_cleanup import read_cleanup
    from .release_contracts import verify_release_receipt
    cleanup = read_cleanup(store, binding)
    evidence = verify_cleanup_evidence(store._result_key, binding, cleanup)
    try:
        receipt = verify_release_receipt(store._result_key, evidence, keyed_digest(store._result_key, cleanup), raw)
    except ClaimError:
        raise HistoryError('history_invalid_input') from None
    return evidence, receipt


def read_account_release(store, binding):
    lease = store._lease_row(binding)
    reservation = store._connection.execute('SELECT * FROM project_cleanup_reservations '
        'WHERE project_id=? AND run_handle=? AND attempt=?',
        (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
    raw = check_release_ack_companion(store, lease, reservation)
    if raw is None:
        raise HistoryError('history_result_not_ready')
    _verified_ack(store, binding, raw)
    return raw


def _release_obligation(store, lease, ack_digest):
    """B1 (RS-B draft 3 §6): the reservation's obligation is released `completed` in the transaction that records
    the account's verified release, once its result is past `reserved` (a terminal exists). A row some other release
    already ended (a later slice's settlement) is left as it is."""
    from . import project_holds_schema
    row = store._connection.execute(
        "SELECT s.state,h.state AS held FROM result_reservations s JOIN project_holds h ON h.project_id=s.project_id "
        "AND h.kind='reservation' AND h.identity=s.claim_id WHERE s.project_id=? AND s.claim_id=?",
        (lease.binding.project_id, lease.claim_id)).fetchone()
    if row is None:
        raise HistoryError('history_incompatible')
    if row['state'] != 'reserved' and row['held'] != 'released':
        project_holds_schema.release_hold(store._connection, lease.binding.project_id, 'reservation', lease.claim_id,
                                          'completed', now_ms=store._hold_now(), receipt=ack_digest)


def record_account_release(store, binding, raw, *, registry, custody):
    """Persist after actual account settlement; original live custody stays held.

    The trusted frontend must separately authenticate its original account
    peer/current authority. A shared-key receipt is only a binding check.
    """
    from .project_cleanup import _current_cleanup, _state_seal
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ACCOUNT_RELEASE_ACK_BYTES:
        raise HistoryError('history_invalid_input')
    with store._transaction():
        lease = store._lease_row(binding)
        evidence, receipt = _verified_ack(store, binding, raw)
        _current_cleanup(registry, custody, evidence)
        reservation = store._connection.execute('SELECT * FROM project_cleanup_reservations '
            'WHERE project_id=? AND run_handle=? AND attempt=?',
            (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
        existing = check_release_ack_companion(store, lease, reservation)
        if existing is not None:
            _, previous = _verified_ack(store, binding, existing)
            if ({k: v for k, v in previous.items() if k != 'known'}
                    != {k: v for k, v in receipt.items() if k != 'known'}):
                raise HistoryError('history_conflict')
            raw = existing
        else:
            digest = keyed_digest(store._result_key, raw)
            cleanup_digest = reservation['cleanup_digest']
            store._connection.execute('INSERT INTO project_cleanup_acknowledgements VALUES(?,?,?,?,?,?)',
                (binding.project_id, binding.run_handle, binding.attempt, raw, digest,
                 _journal_seal(store, lease, cleanup_digest, digest)))
            _current_cleanup(registry, custody, evidence)
            store._connection.execute('UPDATE project_cleanup_reservations SET account_release_digest=?,state_digest=? '
                'WHERE project_id=? AND run_handle=? AND attempt=? AND account_release_digest IS NULL',
                (digest, _state_seal(store, lease.cleanup_reservation_digest, cleanup_digest, account_release_digest=digest),
                 binding.project_id, binding.run_handle, binding.attempt))
            _current_cleanup(registry, custody, evidence)
            if read_account_release(store, binding) != raw:
                raise HistoryError('history_incompatible')
            _release_obligation(store, lease, digest)
        _current_cleanup(registry, custody, evidence)
    _current_cleanup(registry, custody, evidence)
    return raw
