"""Account-owner original cleanup settlement, never native authentication.

The trusted server adapter must authenticate the original project peer/current
epoch, permissions and closed-descendant containment before supplying evidence.
No model/JSON flag, MAC, PID or reconstructed remote ticket can do that work.
"""
import secrets

from .account_claim_contracts import ClaimError, LaunchClaimInput
from .account_cleanup_schema import RELEASE_COLUMNS
from .cleanup_contracts import CleanupError, verify_cleanup_evidence
from .history_contracts import _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest
from .owned_execution import OwnedExecutionKey
from .release_contracts import release_receipt
from .terminal_delivery import terminal_receipt

MAX_RELEASE_RESERVATIONS = 4096
_IDENTITY = ('project_id', 'handle_digest', 'run_id', 'bridge_run_id', 'registration_digest',
             'lease_digest', 'binding_digest')
_TERMINAL = ('terminal_record_id', 'terminal_content_digest', 'result_digest',
             'terminal_receipt_digest', 'terminal_recorded_at_ms')
_RELEASE = ('cleanup_digest', 'released_at_ms', 'receipt_digest')


def _parameters(ledger, binding, grant):
    return dict(project_id=binding.project_id, handle_digest=grant.handle_digest, run_id=binding.run_id,
        bridge_run_id=grant.binding.claim_id, registration_digest=grant.registration_digest,
        lease_digest=grant.lease_digest, binding_digest=keyed_digest(ledger.state.digest_key(), canonical_json(binding.as_data())))


def _reservation_digest(key, parameters):
    return keyed_digest(key, b'account-cleanup-reservation-v1\0' + canonical_json(parameters))


def _seal(key, row):
    return keyed_digest(key, b'account-cleanup-journal-v1\0' + canonical_json({
        n: row[n] for n in ('project_id', *RELEASE_COLUMNS) if n != 'state_seal'}))


def reserve_cleanup(ledger, binding, grant):
    if not ledger._connection.in_transaction:
        raise ClaimError('claim_cleanup_unavailable')
    if ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts WHERE project_id=?',
                                  (binding.project_id,)).fetchone()[0] >= MAX_RELEASE_RESERVATIONS:
        raise ClaimError('claim_cleanup_unavailable')
    key = ledger.state.digest_key()
    parameters = _parameters(ledger, binding, grant)
    row = {**parameters, 'reservation_digest': _reservation_digest(key, parameters),
           **{n: None for n in (*_TERMINAL, *_RELEASE)}}
    row['state_seal'] = _seal(key, row)
    fields = ('project_id', *RELEASE_COLUMNS)
    ledger._connection.execute('INSERT INTO account_cleanup_receipts VALUES(' + ','.join('?' for _ in fields) + ')',
                               tuple(row[n] for n in fields))
    return row['reservation_digest']


def _checked_journal(ledger, row):
    if row is None:
        raise ClaimError('claim_cleanup_unavailable')
    value = dict(row)
    key = ledger.state.digest_key()
    parameters = {n: value[n] for n in _IDENTITY}
    terminal = tuple(value[n] for n in _TERMINAL)
    release = tuple(value[n] for n in _RELEASE)
    if (any(not matches(value[n], _DIGEST) for n in ('handle_digest', 'registration_digest', 'lease_digest',
                                                     'binding_digest', 'reservation_digest', 'state_seal'))
            or value['reservation_digest'] != _reservation_digest(key, parameters)
            or not secrets.compare_digest(value['state_seal'], _seal(key, value))
            or not (all(v is None for v in terminal) or (
                type(value['terminal_record_id']) is str
                and value['terminal_record_id'].startswith('dispatch_result:')
                and matches(value['terminal_record_id'][16:], _DIGEST)
                and all(matches(value[n], _DIGEST) for n in _TERMINAL[1:4])
                and valid_integer(value['terminal_recorded_at_ms'])))
            or not (all(v is None for v in release) or (
                all(v is not None for v in terminal)
                and matches(value['cleanup_digest'], _DIGEST) and matches(value['receipt_digest'], _DIGEST)
                and valid_integer(value['released_at_ms'], minimum=value['terminal_recorded_at_ms'])))):
        raise ClaimError('claim_cleanup_unavailable')
    return value


def original_journal(ledger, binding, grant):
    row = ledger._connection.execute('SELECT * FROM account_cleanup_receipts WHERE project_id=? AND handle_digest=?',
                                     (binding.project_id, grant.handle_digest)).fetchone()
    value = _checked_journal(ledger, row)
    execution = ledger._connection.execute('SELECT * FROM capacity_executions WHERE project_id=? AND handle_digest=?',
                                           (binding.project_id, grant.handle_digest)).fetchone()
    if (any(value[n] != v for n, v in _parameters(ledger, binding, grant).items())
            or execution is None or execution['cleanup_reservation_digest'] != value['reservation_digest']
            or execution['owner_digest'] != grant.registration_digest or execution['run_id'] != binding.run_id
            or execution['bridge_run_id'] != grant.binding.claim_id):
        raise ClaimError('claim_cleanup_unavailable')
    return value


def _write_journal(ledger, value):
    fields = (*_TERMINAL, *_RELEASE)
    key = ledger.state.digest_key()
    ledger._connection.execute('UPDATE account_cleanup_receipts SET ' + ','.join(n+'=?' for n in (*fields, 'state_seal'))
        + ' WHERE project_id=? AND handle_digest=?',
        (*[value[n] for n in fields], _seal(key, value), value['project_id'], value['handle_digest']))


def record_terminal(ledger, binding, grant, delivery, *, known, recorded_at_ms=None):
    execution = ledger._connection.execute('SELECT cleanup_reservation_digest FROM capacity_executions '
        'WHERE project_id=? AND handle_digest=?', (binding.project_id, grant.handle_digest)).fetchone()
    if execution is None or execution['cleanup_reservation_digest'] is None:
        return  # Explicit old account-local/schema10 execution, never a new release grant.
    value = original_journal(ledger, binding, grant)
    material = dict(terminal_record_id=delivery.record_id, terminal_content_digest=delivery.record_content_digest,
        result_digest=delivery.result_digest,
        terminal_receipt_digest=terminal_receipt(ledger.state.digest_key(), grant, delivery, known=False)['receipt_digest'])
    if value['terminal_record_id'] is not None:
        if any(value[n] != v for n, v in material.items()):
            raise ClaimError('claim_cleanup_conflict')
        return
    if known or not valid_integer(recorded_at_ms, minimum=grant.claimed_at_ms):
        raise ClaimError('claim_cleanup_unavailable')
    _write_journal(ledger, {**value, **material, 'terminal_recorded_at_ms': recorded_at_ms})


def project_release_digest(ledger, row, journal, timestamp, cleanup_digest):
    material = {n: row[n] for n in ('project_id', 'handle_digest', 'run_id', 'bridge_run_id', 'provider',
        'account_fingerprint', 'runtime_digest', 'policy_digest', 'claimed_at_ms', 'deadline_at_ms',
        'terminal_record_id', 'terminal_content_digest', 'clock_reference_ns', 'execution_deadline_ns',
        'clock_owner_digest', 'execution_owner_digest')}
    return keyed_digest(ledger.state.digest_key(), b'active-capacity-project-release-v1\0' + canonical_json({
        **material, 'reservation_digest': journal['reservation_digest'], 'result_digest': journal['result_digest'],
        'terminal_receipt_digest': journal['terminal_receipt_digest'], 'cleanup_digest': cleanup_digest,
        'released_at_ms': timestamp}))


def checked_completion_digest(ledger, row, timestamp):
    """None selects genuine legacy v1, never repairs a missing new companion."""
    if row['cleanup_reservation_digest'] is None:
        return None
    raw = ledger._connection.execute('SELECT * FROM account_cleanup_receipts WHERE project_id=? AND handle_digest=?',
                                     (row['project_id'], row['handle_digest'])).fetchone()
    journal = _checked_journal(ledger, raw)
    if (journal['reservation_digest'] != row['cleanup_reservation_digest']
            or journal['registration_digest'] != row['execution_owner_digest']
            or any(journal[n] != row[n] for n in ('project_id', 'handle_digest', 'run_id', 'bridge_run_id',
                                                 'terminal_record_id', 'terminal_content_digest'))
            or journal['released_at_ms'] != timestamp or journal['cleanup_digest'] is None
            or journal['receipt_digest'] != row['receipt_digest']):
        raise ClaimError('claim_cleanup_unavailable')
    return project_release_digest(ledger, row, journal, timestamp, journal['cleanup_digest'])


def release_capacity(ledger, project_id, *, runtime_digest, caller_scope, submission, storage_binding,
                     storage_evidence, terminal_evidence, cleanup_evidence, before_commit):
    """Original server-held proof only; caller authentication is external."""
    from .claim_storage import verify_bound_storage_evidence
    from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest
    from .terminal_delivery import checked_terminal_receipt

    if not callable(before_commit):
        raise ClaimError('claim_cleanup_invalid')
    try:
        evidence = verify_cleanup_evidence(ledger.state.digest_key(), storage_binding, cleanup_evidence)
    except CleanupError:
        raise ClaimError('claim_cleanup_invalid') from None
    lease = verify_bound_storage_evidence(ledger.state.digest_key(), storage_binding, storage_evidence)
    if lease.cleanup_reservation_digest is None or lease.lease_digest != evidence.grant.lease_digest:
        raise ClaimError('claim_cleanup_invalid')
    # This path can only read an existing original terminal. It cannot turn a
    # missing completion into a fresh acceptance while handling a release.
    accepted = ledger._accept_project_terminal(project_id, runtime_digest=runtime_digest, caller_scope=caller_scope,
        submission=submission, storage_binding=storage_binding, storage_evidence=storage_evidence,
        terminal_evidence=terminal_evidence, require_known=True)
    checked_terminal_receipt(accepted, submission)
    if accepted['receipt_digest'] != evidence.terminal_receipt_digest:
        raise ClaimError('claim_cleanup_invalid')
    grant, delivery, registration = evidence.grant, evidence.delivery, submission.registration
    if (grant.registration_digest != submission.registration_digest or delivery.record_id != submission.terminal_record_id
            or delivery.record_content_digest != submission.terminal_content_digest or delivery.result_digest != submission.result_digest):
        raise ClaimError('claim_cleanup_invalid')
    launch = LaunchClaimInput(registration.run_handle, registration.identity_digest)
    original = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.claim_launch', registration.claim_request_id,
        project_id, runtime_digest, launch.payload())
    if storage_binding.claim_request_digest != account_request_digest(original):
        raise ClaimError('claim_cleanup_invalid')
    context, known_claim, _, _ = ledger._claim_preflight(project_id, runtime_digest=runtime_digest,
        caller_scope=caller_scope, request_id=registration.claim_request_id, launch=launch, storage_binding=storage_binding)
    if known_claim is None or known_claim['receipt_digest'] != registration.claim_digest:
        raise ClaimError('claim_cleanup_unavailable')
    key = OwnedExecutionKey(project_id, grant.handle_digest, grant.binding.run_id, grant.binding.claim_id)
    digest = keyed_digest(ledger.state.digest_key(), cleanup_evidence)
    with ledger._account_transaction():
        host = ledger._connection.execute('SELECT * FROM host_run_bindings WHERE project_id=? AND handle_digest=?',
                                          (project_id, key.handle_digest)).fetchone()
        host_snapshot = None if host is None else tuple(host)
        first = ledger._capacity_completion_locked(key)
        identity = {n: first[n] for n in first.keys() if n not in {
            'released_at_ms', 'release_digest', 'completion_released_at_ms', 'receipt_digest'}}
        journal = original_journal(ledger, storage_binding, grant)
        if (journal['terminal_receipt_digest'] != evidence.terminal_receipt_digest
                or journal['result_digest'] != delivery.result_digest):
            raise ClaimError('claim_cleanup_invalid')
        known = first['released_at_ms'] is not None
        timestamp = first['released_at_ms'] if known else ledger._clock_ms()
        if not valid_integer(timestamp, minimum=max(grant.claimed_at_ms, journal['terminal_recorded_at_ms'])):
            raise ClaimError('claim_cleanup_unavailable')
        receipt_digest = project_release_digest(ledger, first, journal, timestamp, digest)

        def check_original():
            before_commit()
            current = ledger._known_claim_receipt_locked(project_id, context.scope, context.request, context.parameters)
            if current is None or current['receipt_digest'] != registration.claim_digest:
                raise ClaimError('claim_cleanup_unavailable')
            ledger._claim_original_orchestration_digest(storage_binding, 'claim_cleanup_unavailable')
            actual_host = ledger._connection.execute('SELECT * FROM host_run_bindings WHERE project_id=? AND handle_digest=?',
                                                     (project_id, key.handle_digest)).fetchone()
            row = ledger._capacity_completion_locked(key)
            if (actual_host is None or tuple(actual_host) != host_snapshot
                    or any(row[n] != v for n, v in identity.items())):
                raise ClaimError('claim_cleanup_unavailable')
            return row

        check_original()
        if known:
            if (journal['cleanup_digest'] != digest or journal['receipt_digest'] != receipt_digest
                    or first['receipt_digest'] != receipt_digest):
                raise ClaimError('claim_cleanup_conflict')
        else:
            if journal['cleanup_digest'] is not None:
                raise ClaimError('claim_cleanup_unavailable')
            ledger._connection.execute('UPDATE capacity_completions SET released_at_ms=?,receipt_digest=? '
                'WHERE project_id=? AND handle_digest=?', (timestamp, receipt_digest, project_id, key.handle_digest))
            before_commit()
            ledger._connection.execute('UPDATE active_run_capacity SET released_at_ms=?,release_digest=? '
                'WHERE project_id=? AND handle_digest=?', (timestamp, receipt_digest, project_id, key.handle_digest))
            before_commit()
            _write_journal(ledger, {**journal, 'cleanup_digest': digest, 'released_at_ms': timestamp,
                                    'receipt_digest': receipt_digest})
            before_commit()
        final = check_original()
        fresh = original_journal(ledger, storage_binding, grant)
        if (fresh['cleanup_digest'] != digest or fresh['receipt_digest'] != receipt_digest
                or checked_completion_digest(ledger, final, timestamp) != receipt_digest):
            raise ClaimError('claim_cleanup_unavailable')
    before_commit()
    return release_receipt(ledger.state.digest_key(), evidence, digest, timestamp, receipt_digest, known=known)
