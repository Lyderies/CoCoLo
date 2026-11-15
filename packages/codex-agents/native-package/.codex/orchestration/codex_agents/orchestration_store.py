"""Project connection-owner lifecycle journal; no native enrollment or launch.

This borrows exactly the history owner's connection. It never creates an account
ledger, imports a writable account orchestration or treats a JSON state as proof.
"""
from copy import deepcopy
import secrets

from .execution_grant import MAX_EXECUTION_GRANT_BYTES, verify_execution_grant
from .history_contracts import HistoryError, _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest, external_id, parse_bounded_json
from .orchestration_api import (
    MAX_ORCHESTRATION_CHECKPOINT_BYTES, OrchestrationScope, OrchestrationRequest,
    checked_orchestration_view, decode_orchestration_request, encode_orchestration_request,
    orchestration_request_digest, HOST_CLIENT_VERSIONS, ORCHESTRATION_TERMINAL_STATES, SCOPE_FIELDS,
)
from .project_history import ProjectHistory, _operation

MAX_JOURNAL_EVENTS = 65_536

class OrchestrationOwner:
    def __init__(self, history):
        if not isinstance(history, ProjectHistory):
            raise HistoryError('history_wrong_owner')
        self._history = history

    def _require_owner(self):
        self._history._require_owner()

    @property
    def _db(self):
        return self._history._connection

    def _key(self):
        key = self._history._result_key
        if key is None:
            raise HistoryError('history_orchestration_unavailable')
        return key

    def _scope_digest(self, scope):
        return keyed_digest(self._key(), b'project-orchestration-scope-v1\0' + canonical_json(scope.as_data()))

    def _checkpoint(self, handle, sequence, *, previous=None, expected_request=None):
        if not valid_integer(sequence, minimum=1, maximum=MAX_JOURNAL_EVENTS):
            raise HistoryError('history_incompatible')
        row = self._db.execute('SELECT content,checkpoint_digest FROM project_orchestration_checkpoints '
                               'WHERE project_id=? AND orchestration_handle=? AND sequence=?',
                               (self._history.project_id, handle, sequence)).fetchone()
        if row is None:
            raise HistoryError('history_incompatible')
        raw = self._history._stored_blob(row['content'], maximum=MAX_ORCHESTRATION_CHECKPOINT_BYTES)
        data = parse_bounded_json(raw, max_bytes=MAX_ORCHESTRATION_CHECKPOINT_BYTES)
        if (type(data) is not dict or data.keys() != {'snapshot', 'previous_digest', 'request_key', 'request_digest'}
                or canonical_json(data) != raw
                or not matches(data['request_key'], _DIGEST) or not matches(data['request_digest'], _DIGEST)
                or (data['previous_digest'] is not None and not matches(data['previous_digest'], _DIGEST))):
            raise HistoryError('history_incompatible')
        digest = keyed_digest(self._key(), b'project-orchestration-checkpoint-v1\0' + raw)
        if not matches(row['checkpoint_digest'], _DIGEST) or not secrets.compare_digest(row['checkpoint_digest'], digest):
            raise HistoryError('history_incompatible')
        try:
            view = checked_orchestration_view({'snapshot': data['snapshot'], 'checkpoint_digest': digest})
        except HistoryError:
            raise HistoryError('history_incompatible') from None
        snapshot = view['snapshot']
        if (snapshot['orchestration_handle'] != handle or snapshot['scope']['project_id'] != self._history.project_id
                or snapshot['sequence'] != sequence
                or snapshot['orchestration_id'] != external_id(self._key(), 'orc', handle)
                or (sequence == 1) != (data['previous_digest'] is None)):
            raise HistoryError('history_incompatible')
        if expected_request is not None and (data['request_key'], data['request_digest']) != expected_request:
            raise HistoryError('history_conflict')
        if previous is not None:
            prior = previous['snapshot']
            immutable = ('scope', 'orchestration_handle', 'orchestration_id', 'client_version', 'started_at_ms')
            if (data['previous_digest'] != previous['checkpoint_digest'] or prior['sequence'] + 1 != sequence
                    or any(canonical_json(prior[name]) != canonical_json(snapshot[name]) for name in immutable)
                    or prior['status'] in ORCHESTRATION_TERMINAL_STATES
                    or snapshot['observed_at_ms'] < prior['observed_at_ms']
                    or any(prior[name] is not None and (snapshot[name] is None or snapshot[name] < prior[name])
                           for name in ('goal_tokens_used', 'goal_time_used_seconds'))):
                raise HistoryError('history_incompatible')
        return view

    def _current(self, scope, handle, *, missing_reference=False):
        row = self._db.execute('SELECT * FROM project_orchestrations WHERE project_id=? AND orchestration_handle=?',
                               (self._history.project_id, handle)).fetchone()
        if row is None:
            if missing_reference and self._history._allow_legacy_orchestration_reference:
                return None
            raise HistoryError('history_orchestration_unavailable')
        if row['scope_digest'] != self._scope_digest(scope):
            raise HistoryError('history_not_authorized')
        if not valid_integer(row['sequence'], minimum=1, maximum=MAX_JOURNAL_EVENTS):
            raise HistoryError('history_incompatible')
        latest = self._db.execute('SELECT sequence FROM project_orchestration_checkpoints '
                                  'WHERE project_id=? AND orchestration_handle=? ORDER BY sequence DESC LIMIT 1',
                                  (self._history.project_id, handle)).fetchone()
        if latest is None or latest['sequence'] != row['sequence']:
            raise HistoryError('history_incompatible')
        # Constant retained memory and a fixed ceiling, even if policy changes.
        # The latest pointer cannot hide a deleted/corrupt predecessor.
        view = None
        for sequence in range(1, row['sequence'] + 1):
            view = self._checkpoint(handle, sequence, previous=view)
        if view['snapshot']['scope'] != scope.as_data() or view['checkpoint_digest'] != row['checkpoint_digest']:
            raise HistoryError('history_incompatible')
        return view

    @_operation
    def active_for(self, binding):
        scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
        view = self._current(scope, binding.orchestration_handle, missing_reference=True)
        if view is not None:
            if self._history._orchestration_limits is None:
                raise HistoryError('history_orchestration_unavailable')
            if view['snapshot']['status'] != 'active':
                raise HistoryError('history_orchestration_state')

    @_operation
    def closed_for(self, binding):
        scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
        view = self._current(scope, binding.orchestration_handle, missing_reference=True)
        return view is not None and view['snapshot']['status'] in ORCHESTRATION_TERMINAL_STATES

    @_operation
    def metadata_for(self, binding):
        """Validate the actual owned journal without authorizing fresh work."""
        scope = OrchestrationScope(**{name: getattr(binding, name) for name in SCOPE_FIELDS})
        return self._current(scope, binding.orchestration_handle)

    def _positions(self, table):
        # Only fixed owner-selected tables, bounded scalar rows before any blob.
        if table not in {'result_leases', 'result_reservations', 'project_execution_consumptions'}:
            raise HistoryError('history_invalid_input')
        capacity = self._history._result_capacity
        if capacity is None:
            raise HistoryError('history_orchestration_unavailable')
        positions = self._db.execute('SELECT rowid FROM ' + table + ' WHERE project_id=? LIMIT ?',
                                     (self._history.project_id, capacity.max_entries + 1)).fetchall()
        if len(positions) > capacity.max_entries:
            raise HistoryError('history_orchestration_unavailable')
        return positions

    def _unfinished(self, scope, handle):
        # A timeout, absent process or accepted terminal does not prove cleanup.
        for position in self._positions('result_leases'):
            row = self._db.execute('SELECT * FROM result_leases WHERE rowid=?', (position[0],)).fetchone()
            lease = self._history._lease(row)
            if lease.binding.orchestration_handle == handle:
                if not scope.matches_attempt(lease.binding):
                    raise HistoryError('history_conflict')
                if lease.state in {'reserved', 'claim_pending'}:
                    return True
        for position in self._positions('project_execution_consumptions'):
            row = self._db.execute('SELECT claim_id FROM project_execution_consumptions WHERE rowid=?', (position[0],)).fetchone()
            if self._db.execute('SELECT 1 FROM result_reservations WHERE project_id=? AND claim_id=?',
                                (scope.project_id, row['claim_id'])).fetchone() is None:
                raise HistoryError('history_incompatible')
        for position in self._positions('result_reservations'):
            row = self._db.execute('SELECT * FROM result_reservations WHERE rowid=?', (position[0],)).fetchone()
            reservation = self._history._reservation(row)
            binding = reservation.binding
            if binding.orchestration_handle != handle:
                continue
            if not scope.matches_attempt(binding):
                raise HistoryError('history_conflict')
            if reservation.state not in {'committed', 'expired'} or reservation.terminal_record_id is None:
                return True
            consumed = self._db.execute('SELECT * FROM project_execution_consumptions WHERE project_id=? AND claim_id=?',
                                        (scope.project_id, binding.claim_id)).fetchone()
            if consumed is None:
                return True
            grant = verify_execution_grant(self._key(), binding,
                                            self._history._stored_blob(consumed['grant'], maximum=MAX_EXECUTION_GRANT_BYTES))
            if (consumed['registration_digest'] != grant.registration_digest
                    or not valid_integer(consumed['consumed_at_ms'], minimum=grant.claimed_at_ms,
                                         maximum=grant.deadline_at_ms - 1)
                    or reservation.assignment_digest != grant.assignment_digest
                    or reservation.hard_deadline_at_ms != grant.deadline_at_ms):
                raise HistoryError('history_incompatible')
            self._history._execution_provenance(grant)
            digest = keyed_digest(self._key(), b'project-host-result-v1\0' + canonical_json(binding.as_data()))
            try:
                entry = self._history.record('host_result_admission:' + digest)
            except HistoryError as exc:
                if exc.reason_code == 'history_not_found':
                    return True
                raise
            receipt = parse_bounded_json(entry.content)
            if (entry.source != 'host' or receipt.get('record_type') != 'host_result_admission'
                    or receipt.get('binding_digest') != digest or receipt['run_id'] != binding.run_id
                    or receipt['registration_digest'] != grant.registration_digest
                    or receipt['dispatch_result']['record_id'] != reservation.terminal_record_id
                    or receipt['dispatch_result']['result_digest'] != reservation.result_digest):
                raise HistoryError('history_conflict')
            self._history._host_result_receipt(entry)
        return False

    @_operation
    def apply(self, request, *, client_version, before_commit):
        if type(request) is not OrchestrationRequest or client_version not in HOST_CLIENT_VERSIONS or not callable(before_commit):
            raise HistoryError('history_invalid_input')
        request = decode_orchestration_request(encode_orchestration_request(request))
        scope = request.scope
        if scope.project_id != self._history.project_id:
            raise HistoryError('history_not_authorized')
        digest = orchestration_request_digest(request)
        scope_digest = self._scope_digest(scope)
        request_key = keyed_digest(self._key(), canonical_json({'scope_digest': scope_digest, 'request_id': request.request_id}))
        payload, operation = request.payload, request.operation
        if operation == 'orchestration.status':
            current = self._current(scope, payload['orchestration_handle'])
            if current['snapshot']['client_version'] != client_version:
                raise HistoryError('history_not_authorized')
            before_commit()
            return {'receipt': None, 'current': current, 'known': False}
        known = self._db.execute('SELECT * FROM project_orchestration_requests WHERE project_id=? AND request_key=?',
                                 (scope.project_id, request_key)).fetchone()
        if known is not None:
            if known['request_digest'] != digest:
                raise HistoryError('history_conflict')
            receipt = self._checkpoint(known['orchestration_handle'], known['sequence'],
                                        expected_request=(request_key, digest))
            current = self._current(scope, known['orchestration_handle'])
            if current['snapshot']['client_version'] != client_version:
                raise HistoryError('history_not_authorized')
            before_commit()
            return {'receipt': receipt, 'current': current, 'known': True}
        limits = self._history._orchestration_limits
        if limits is None:
            raise HistoryError('history_orchestration_unavailable')
        now = self._history._result_now()
        with self._history._transaction():
            if operation == 'orchestration.open':
                count = self._db.execute('SELECT COUNT(*) FROM project_orchestrations WHERE project_id=?', (scope.project_id,)).fetchone()[0]
                if count >= limits.max_orchestrations:
                    raise HistoryError('history_orchestration_full')
                handle = 'orc_' + secrets.token_hex(24)
                snapshot = {'scope': scope.as_data(), 'orchestration_handle': handle,
                            'orchestration_id': external_id(self._key(), 'orc', handle), 'client_version': client_version,
                            'status': 'active', 'sequence': 1, 'started_at_ms': now, 'observed_at_ms': now,
                            'ended_at_ms': None, 'goal_tokens_used': None, 'goal_time_used_seconds': None}
                previous_digest = None
            else:
                handle = payload['orchestration_handle']
                previous = self._current(scope, handle)
                snapshot = deepcopy(previous['snapshot'])
                if (snapshot['sequence'] != payload['expected_sequence'] or snapshot['client_version'] != client_version):
                    raise HistoryError('history_conflict')
                if snapshot['status'] in ORCHESTRATION_TERMINAL_STATES:
                    raise HistoryError('history_orchestration_state')
                if now < snapshot['observed_at_ms']:
                    raise HistoryError('history_orchestration_state')
                ceiling = limits.max_events if operation == 'orchestration.close' else limits.max_events - 1
                if snapshot['sequence'] >= ceiling:
                    raise HistoryError('history_orchestration_full')
                if operation in {'orchestration.pause', 'orchestration.resume'}:
                    expected = 'active' if operation == 'orchestration.pause' else 'paused'
                    if snapshot['status'] != expected:
                        raise HistoryError('history_orchestration_state')
                    snapshot['status'] = 'paused' if expected == 'active' else 'active'
                if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                    for name in ('goal_tokens_used', 'goal_time_used_seconds'):
                        value = payload[name]
                        if value is not None:
                            if snapshot[name] is not None and value < snapshot[name]:
                                raise HistoryError('history_orchestration_state')
                            snapshot[name] = value
                if operation == 'orchestration.close':
                    if self._unfinished(scope, handle):
                        raise HistoryError('history_orchestration_pending')
                    snapshot['status'], snapshot['ended_at_ms'] = payload['status'], now
                snapshot['sequence'] += 1
                snapshot['observed_at_ms'] = now
                previous_digest = previous['checkpoint_digest']
            raw = canonical_json({'snapshot': snapshot, 'previous_digest': previous_digest,
                                  'request_key': request_key, 'request_digest': digest})
            if len(raw) > MAX_ORCHESTRATION_CHECKPOINT_BYTES:
                raise HistoryError('history_orchestration_unavailable')
            checkpoint_digest = keyed_digest(self._key(), b'project-orchestration-checkpoint-v1\0' + raw)
            checked_orchestration_view({'snapshot': snapshot, 'checkpoint_digest': checkpoint_digest})
            if operation == 'orchestration.open':
                self._db.execute('INSERT INTO project_orchestrations VALUES(?,?,?,?,?)',
                                 (scope.project_id, handle, scope_digest, 1, checkpoint_digest))
            else:
                self._db.execute('UPDATE project_orchestrations SET sequence=?,checkpoint_digest=? '
                                 'WHERE project_id=? AND orchestration_handle=?',
                                 (snapshot['sequence'], checkpoint_digest, scope.project_id, handle))
            self._db.execute('INSERT INTO project_orchestration_checkpoints VALUES(?,?,?,?,?)',
                             (scope.project_id, handle, snapshot['sequence'], raw, checkpoint_digest))
            self._db.execute('INSERT INTO project_orchestration_requests VALUES(?,?,?,?,?)',
                             (scope.project_id, request_key, digest, handle, snapshot['sequence']))
            # Recheck independent current authority after the complete mutation,
            # inside the transaction and immediately before its actual commit.
            before_commit()
        view = {'snapshot': snapshot, 'checkpoint_digest': checkpoint_digest}
        return {'receipt': view, 'current': view, 'known': False}
