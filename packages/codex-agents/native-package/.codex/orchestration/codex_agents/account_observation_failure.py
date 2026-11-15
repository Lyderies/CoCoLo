"""Atomic fixed failure metadata on the original observation account owner."""
from . import account_observation
from .account_observation import AccountObservationOwner, ObservationContext, _FIELDS
from .history_contracts import _DIGEST, _ID, _PROJECT, matches
from .learning_contracts import canonical_json, content_digest, keyed_digest, validate_observation_unavailable
from .observation_failure_contracts import ObservationFailureInput, checked_observation_failure_reply
from .provider_observation_contracts import ObservationError


def _reply(owner, row, request, *, known):
    records = owner._retained_records(row)
    if len(records) != 1:
        raise ObservationError('observation_conflict')
    return checked_observation_failure_reply({**request.payload(), 'run_id': row['run_id'],
        'observed_at_ms': row['observed_at_ms'], 'known': known, 'record': records[0]}, request)


def recheck_observation_failure(owner, project, runtime, caller, identity, request, context, before_publish, result):
    snapshot = context.snapshot()
    owner._authorize(context, snapshot, before_publish)
    key = keyed_digest(owner._key, canonical_json([project, runtime, caller, identity]))
    with owner._ledger._account_transaction():
        root = owner._project(project)
        row = owner._row(project, key)
        parameters = keyed_digest(owner._key, canonical_json([request.payload(), snapshot[1], root]))
        if row is None or row['parameters_digest'] != parameters:
            raise ObservationError('observation_conflict')
        actual = _reply(owner, row, request, known=result['known'])
        owner._authorize(context, snapshot, before_publish)
        if owner._project(project) != root or actual != result:
            raise ObservationError('observation_not_authorized')


def record_observation_failure(owner, project, runtime, caller, identity, request, context, before_publish):
    if (type(owner) is not AccountObservationOwner or not matches(project, _PROJECT)
            or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST) or not matches(identity, _ID)
            or type(request) is not ObservationFailureInput or type(context) is not ObservationContext
            or not callable(before_publish)):
        raise ObservationError('observation_invalid_request')
    request = ObservationFailureInput(**request.payload())
    snapshot = context.snapshot()
    owner._authorize(context, snapshot, before_publish)
    key = keyed_digest(owner._key, canonical_json([project, runtime, caller, identity]))
    with owner._ledger._account_transaction():
        root = owner._project(project)
        parameters = keyed_digest(owner._key, canonical_json([request.payload(), snapshot[1], root]))
        def current():
            owner._authorize(context, snapshot, before_publish)
            if owner._project(project) != root:
                raise ObservationError('observation_not_authorized')
        existing = owner._row(project, key)
        if existing is not None:
            if existing['parameters_digest'] != parameters:
                raise ObservationError('observation_conflict')
            result = _reply(owner, existing, request, known=True)
            current()
            return result
        if key in owner._pending:
            raise ObservationError('observation_conflict')
        count = owner._one('SELECT COUNT(*) FROM (SELECT 1 FROM account_observation_origins '
            'WHERE project_id=? LIMIT ?)', (project, account_observation.MAX_OBSERVATION_REQUESTS + 1))[0]
        if count >= account_observation.MAX_OBSERVATION_REQUESTS:
            raise ObservationError('observation_full')
        run_id, now = owner._run(project, request), owner._now()
        material = {'schema_version': 1, 'record_type': 'observation_unavailable', 'provider': request.provider,
                    'run_id': run_id, 'phase': request.phase, 'reason_code': request.reason_code, 'observed_at_ms': now}
        record = validate_observation_unavailable({**material, 'record_id': 'observation_unavailable:' + content_digest(material)})
        references = canonical_json([{'record_id': record['record_id'], 'content_digest': content_digest(record)}]).decode()
        row = dict(zip(_FIELDS, (project, key, parameters, runtime, caller, request.provider,
            keyed_digest(owner._key, request.run_handle), run_id, request.phase, now, now + owner._timeout,
            'committed', references)))
        current()
        owner._ledger._connection.execute('INSERT INTO account_observation_origins VALUES(?,?,?,?,?)',
                                          (project, key, parameters, now, owner._origin_seal(row)))
        current()
        owner._ledger._append_checked_locked(project, record, producer='provider', timestamp=now)
        current()
        if request.phase == 'before':
            owner._ledger._connection.execute('INSERT INTO launch_observations VALUES(?,?,?,?) '
                'ON CONFLICT(project_id,handle_digest) DO UPDATE SET identity_digest=excluded.identity_digest,'
                'observed_at_ms=excluded.observed_at_ms', (project, row['handle_digest'], owner._generation(row), now))
            current()
        owner._ledger._connection.execute('INSERT INTO account_observation_requests VALUES('
            + ','.join('?' for _ in range(14)) + ')', (*row.values(), owner._seal(row)))
        current()
        if not now <= owner._now() < row['deadline_at_ms'] or owner._run(project, request) != run_id:
            raise ObservationError('observation_not_authorized')
        result = _reply(owner, {**row, 'state_seal': owner._seal(row)}, request, known=False)
        current()
    owner._state.harden_database_files()
    return result
