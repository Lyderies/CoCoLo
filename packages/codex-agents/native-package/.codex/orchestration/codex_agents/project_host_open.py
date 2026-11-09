"""Protected project host/verifier-open journal; fixed writer only.

The account receipt describes original facts. This journal records them before
execution consumption; it cannot admit a run, claim, launch, rearm or release it.
Native enrollment/input custody/current permission remain separate obligations.
"""
import hashlib
import secrets

from .history_contracts import HistoryError, _DIGEST, matches
from .host_open_contracts import HostOpenContext, OpenProvenanceInput, decode_open_provenance_input, verify_open_provenance
from .learning_contracts import canonical_json, keyed_digest, validate_learning_record
from .result_contracts import ResultLeaseBinding


def record_host_open(history, binding, request, provenance, *, assignment, task, route, before_commit):
    if type(request) is not OpenProvenanceInput or not callable(before_commit):
        raise HistoryError('history_invalid_input')
    if history._result_key is None or history._result_capacity is None:
        raise HistoryError('history_result_unavailable')
    _, inputs = HostOpenContext(binding, assignment, task, route).snapshot()
    binding = history._lease_binding(ResultLeaseBinding(**inputs.pop('binding')))
    try:
        request = decode_open_provenance_input(request.payload())
    except Exception:
        raise HistoryError('history_invalid_input') from None
    lease = history._lease_row(binding)
    invalid = False
    try:
        proof = verify_open_provenance(history._result_key, request, lease, provenance, **inputs)
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_conflict')
    identity = 'host_open_provenance:' + keyed_digest(history._result_key,
        b'project-original-host-open-v1\0' + canonical_json(binding.as_data()))
    record = validate_learning_record({'schema_version': 1, 'record_type': 'host_open_provenance',
        'record_id': identity, 'run_id': binding.run_id, 'provenance': proof,
        'observed_at_ms': proof['provenance']['opened_at_ms']})
    encoded = canonical_json(record)
    binding_raw = canonical_json(binding.as_data())
    digest = hashlib.sha256(encoded).hexdigest()
    def journal_seal(entry):
        return keyed_digest(history._result_key, b'project-host-open-journal-v1\0' + canonical_json({
            'binding': binding.as_data(), 'record_id': identity, 'record_content_digest': digest,
            'source_sequence': entry.source_sequence, 'ingested_at_ms': entry.ingested_at_ms, 'late': entry.late}))

    def known():
        row = history._connection.execute('SELECT * FROM project_host_opens '
            'WHERE project_id=? AND run_handle=? AND attempt=?',
            (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
        try:
            entry = history.record(identity)
        except HistoryError as error:
            if error.reason_code == 'history_not_found' and row is None:
                return None
            raise HistoryError('history_incompatible') from None
        if row is None:
            raise HistoryError('history_incompatible')
        if (row['binding'] != binding_raw or row['record_id'] != identity
                or row['record_content_digest'] != digest or row['source_sequence'] != entry.source_sequence
                or row['ingested_at_ms'] != entry.ingested_at_ms or row['late'] != int(entry.late)
                or not matches(row['journal_seal'], _DIGEST)
                or not secrets.compare_digest(row['journal_seal'], journal_seal(entry))):
            raise HistoryError('history_conflict')
        if entry.source != 'host' or entry.content != encoded:
            raise HistoryError('history_conflict')
        return entry

    prior = known()
    before_commit()  # Current native authority, outside any SQL transaction.
    history._require_owner()
    if prior is not None:
        current = known()
        if current is None:
            # A removed original is not permission to recreate the journal.
            raise HistoryError('history_not_found')
        return current
    # Persist trusted monotonic time outside the fallible write transaction.
    # Known metadata reads above do not refresh the original window or clock.
    now = history._result_now()
    with history._transaction():
        current = known()
        if current is not None:
            return current
        current_lease = history._lease_row(binding)
        if current_lease != lease:
            raise HistoryError('history_conflict')
        if (lease.state not in {'claim_pending', 'bound'}
                or not max(lease.created_at_ms, record['observed_at_ms']) <= now < min(
                    lease.reserved_until_ms, proof['provenance']['observation_deadline_at_ms'])):
            raise HistoryError('history_result_not_ready')
        if history._connection.execute('SELECT 1 FROM project_orchestrations '
                'WHERE project_id=? AND orchestration_handle=?',
                (binding.project_id, binding.orchestration_handle)).fetchone() is None:
            raise HistoryError('history_orchestration_unavailable')
        history._orchestration_owner().active_for(binding)
        if lease.state == 'bound':
            consumed = history._connection.execute('SELECT 1 FROM project_execution_consumptions '
                'WHERE project_id=? AND claim_id=?', (binding.project_id, lease.claim_id)).fetchone()
            result = history._connection.execute('SELECT state,prepared_at_ms FROM result_reservations '
                'WHERE project_id=? AND claim_id=?', (binding.project_id, lease.claim_id)).fetchone()
            if consumed is not None or result is None or result['state'] != 'reserved' or result['prepared_at_ms'] is not None:
                raise HistoryError('history_execution_not_ready')
        elif history._connection.execute('SELECT 1 FROM result_reservations '
                'WHERE project_id=? AND run_handle=? AND attempt=?',
                (binding.project_id, binding.run_handle, binding.attempt)).fetchone() is not None:
            raise HistoryError('history_conflict')
        entry = history._append_locked(record, encoded, 'host', now)
        history._connection.execute('INSERT INTO project_host_opens VALUES(?,?,?,?,?,?,?,?,?,?)',
            (binding.project_id, binding.run_handle, binding.attempt, binding_raw, identity, digest,
             entry.source_sequence, entry.ingested_at_ms, int(entry.late), journal_seal(entry)))
        return entry
