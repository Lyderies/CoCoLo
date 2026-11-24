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
from .project_history import LEASE_ORCHESTRATION, MAX_LEASE_ROWS, ProjectHistory, _operation
from . import project_holds_schema

MAX_JOURNAL_EVENTS = 65_536
# A closed orchestration is kept this long for reports, then removed with its checkpoints (0.3.4 R6):
# the ledger's own retention (client_runtime.LEARNING_MAX_AGE_MS).
ORCHESTRATION_RETENTION_MS = 90 * 24 * 60 * 60 * 1000
# An open or paused orchestration with no checkpoint and no run registered for this long is abandoned and
# is ended interrupted by the service's cleanup (O0c P5, owner 2026-11-21: 24 hours idle). B6: kept in its register row
# (`due_at_ms`), with the unattended close's back-off (project_holds_schema).
ORCHESTRATION_IDLE_MS = project_holds_schema.ORCHESTRATION_IDLE_MS
# B6 (RS-B-B6-4): at most this many unattended closes are tried per pass (the service asks for this many).
UNATTENDED_CLOSE_BATCH = 4

# B6 review r1 (HIGH retained history): one orchestration's own leases and reservations, through step 21's indexes.
_TARGET_ROWS = {
    'result_leases': ('SELECT rowid FROM result_leases INDEXED BY result_leases_orchestration '
                      f'WHERE project_id=? AND {LEASE_ORCHESTRATION}=?'),
    'result_reservations': ('SELECT rowid FROM result_reservations INDEXED BY result_reservations_orchestration '
                            'WHERE project_id=? AND orchestration_handle=?'),
}


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
            raise HistoryError('history_orchestration_other_scope')  # R8a: not another host class
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

    def _latest_snapshot(self, row):
        """The snapshot of an orchestration's latest checkpoint, verified by its own keyed seal; None
        when it cannot be verified."""
        try:
            view = self._checkpoint(row['orchestration_handle'], row['sequence'])
        except HistoryError:
            return None
        return view['snapshot'] if view['checkpoint_digest'] == row['checkpoint_digest'] else None

    def _open_count(self):
        """Orchestrations not yet closed. B6 (RS-B-B6-8, decisions 185/187): read from the indexed state column,
        written with each checkpoint pointer, so no checkpoint is parsed however many closed ones are kept. Only a
        close sets it `closed`, so a changed or unverifiable row never frees a place."""
        return self._db.execute("SELECT COUNT(*) FROM project_orchestrations INDEXED BY project_orchestrations_state "
                                "WHERE project_id=? AND state='open'", (self._history.project_id,)).fetchone()[0]

    def end_earlier(self, current, limit):
        """End, as interrupted, the open or paused orchestrations an earlier runtime of this same service
        left (0.3.4 R10): same project, namespace, supervisor instance and generation, another runtime
        digest. Nothing could use or close them after an update. The ordinary close does it, under each
        one's own sealed scope, so a run not yet over still keeps it open. Returns how many ended."""
        current = OrchestrationScope(**{name: getattr(current, name) for name in SCOPE_FIELDS})
        if current.project_id != self._history.project_id:
            return 0
        same = {name: getattr(current, name) for name in SCOPE_FIELDS if name != 'runtime_digest'}
        own = self._scope_digest(current)
        # A bounded window from where the last pass stopped; every attempt counts (review 034-r10).
        after = self._history._purge_cursors.get('earlier')
        rows = self._db.execute('SELECT rowid,orchestration_handle,sequence,checkpoint_digest,scope_digest '
                                'FROM project_orchestrations WHERE project_id=? AND scope_digest!=?'
                                + ('' if after is None else ' AND rowid>?') + ' ORDER BY rowid LIMIT ?',
                                (current.project_id, own, *(() if after is None else (after,)), limit * 16)).fetchall()
        if not rows and after is not None:
            self._history._purge_cursors['earlier'] = None
            return self.end_earlier(current, limit)
        ended = tried = 0
        for row in rows:
            if tried >= limit:
                break
            self._history._purge_cursors['earlier'] = row['rowid']
            snapshot = self._latest_snapshot(row)
            if (snapshot is None or snapshot['status'] in ORCHESTRATION_TERMINAL_STATES
                    or any(snapshot['scope'].get(name) != value for name, value in same.items())
                    or snapshot['scope'].get('runtime_digest') == current.runtime_digest):
                continue
            earlier = OrchestrationScope(**snapshot['scope'])
            if self._scope_digest(earlier) != row['scope_digest']:
                continue  # The row does not belong to the scope its checkpoint names: left as it is.
            tried += 1
            # A fresh request id each time: no client can have used it before (review 034-r10).
            request = OrchestrationRequest(1, 'orchestration.close', 'end-earlier-' + secrets.token_hex(16), earlier, {
                'orchestration_handle': row['orchestration_handle'], 'expected_sequence': snapshot['sequence'],
                'status': 'interrupted', 'goal_tokens_used': None, 'goal_time_used_seconds': None})
            try:
                self.apply(request, client_version=snapshot['client_version'], before_commit=lambda: None,
                           _runtime_update=True)
            except HistoryError:
                continue  # A run not yet over, or anything else: it stays open for a later pass.
            ended += 1
        return ended

    _UNATTENDED_SELECT = (
        "SELECT identity FROM project_holds INDEXED BY project_holds_orchestration_check WHERE project_id=? "
        "AND kind='orchestration' AND state='held' AND next_check_ms<=? AND due_at_ms<=? ORDER BY next_check_ms LIMIT ?")

    def _unattended_request(self, handle, sequence, scope):
        """B6 (RS-B-B6-3): the unattended close of (handle, expected sequence), with a request id derived from both
        under the store's key, so a crash-replay is the identical request (answered `known`) and no client can have
        used it before (review 034-r10)."""
        digest = keyed_digest(self._key(), b'project-orchestration-unattended-close-v1\0'
                              + canonical_json({'orchestration_handle': handle, 'expected_sequence': sequence}))
        return OrchestrationRequest(1, 'orchestration.close', 'end-idle-' + digest[:32], OrchestrationScope(**scope), {
            'orchestration_handle': handle, 'expected_sequence': sequence, 'status': 'interrupted',
            'goal_tokens_used': None, 'goal_time_used_seconds': None})

    def _back_off(self, handle, now):
        """B6 (RS-B-B6-4): a due row that was refused waits 10 s, then twice as long each time, at most 5 min."""
        row = self._db.execute("SELECT evidence FROM project_holds WHERE project_id=? AND kind='orchestration' "
                               "AND identity=? AND state='held'", (self._history.project_id, handle)).fetchone()
        if row is None:
            return
        previous = None
        try:
            previous = parse_bounded_json(row[0].encode(), max_bytes=256).get('backoff_ms')
        except (AttributeError, HistoryError, TypeError, ValueError):
            previous = None
        wait = (project_holds_schema.UNATTENDED_BACKOFF_MS if not valid_integer(previous, minimum=1)
                else min(previous * 2, project_holds_schema.UNATTENDED_BACKOFF_MAX_MS))
        with self._history._transaction():
            self._db.execute("UPDATE project_holds SET next_check_ms=?,evidence=? WHERE project_id=? "
                             "AND kind='orchestration' AND identity=? AND state='held'",
                             (now + wait, project_holds_schema.evidence_json(backoff_ms=wait),
                              self._history.project_id, handle))

    def end_idle(self, now=None, limit=UNATTENDED_CLOSE_BATCH):
        """The unattended close (RS-B-B6, O0c P5): end, as interrupted, the open or paused orchestrations with no
        activity for ORCHESTRATION_IDLE_MS. Selected fairly from their register rows by next check (at most `limit`,
        never more than UNATTENDED_CLOSE_BATCH); each is closed by the ordinary close, under its own sealed scope, with
        its eligibility checked inside the close's transaction (no held reservation, nothing unfinished, due). A
        refused one backs off. Returns how many ended.
        B6 review r1 (MED clock): the time is the owner's own effective now, max(wall, persisted floor), read
        without moving the floor (and read again inside each close's transaction); `now` is not used, so a caller's
        clock that went back below the floor can neither refuse nor delay the pass."""
        project = self._history.project_id
        if not valid_integer(limit, minimum=1):
            raise HistoryError('history_invalid_input')
        limit = min(limit, UNATTENDED_CLOSE_BATCH)
        now = self._history._hold_now()
        ended = 0
        for row in self._db.execute(self._UNATTENDED_SELECT, (project, now, now, limit)).fetchall():
            handle = row['identity']
            orchestration = self._db.execute('SELECT orchestration_handle,sequence,checkpoint_digest,scope_digest '
                                             'FROM project_orchestrations WHERE project_id=? AND orchestration_handle=?',
                                             (project, handle)).fetchone()
            snapshot = None if orchestration is None else self._latest_snapshot(orchestration)
            if (snapshot is None or snapshot['status'] in ORCHESTRATION_TERMINAL_STATES
                    or self._scope_digest(OrchestrationScope(**snapshot['scope'])) != orchestration['scope_digest']):
                self._back_off(handle, now)  # It does not verify: left as it is, and asked about again later.
                continue
            request = self._unattended_request(handle, snapshot['sequence'], snapshot['scope'])
            try:
                self.apply(request, client_version=snapshot['client_version'], before_commit=lambda: None,
                           _unattended=True)
            except HistoryError:
                self._back_off(handle, now)  # A run not yet over, or anything else: it stays open for a later pass.
                continue
            ended += 1
        return ended

    def purge_closed(self, now, limit):
        """Remove closed orchestrations past ORCHESTRATION_RETENTION_MS, with their checkpoints and
        requests, once no result row names them (0.3.4 R6). Each pass examines a bounded window from where
        the last one stopped, and reads the leases once (R1g). Returns how many went."""
        project = self._history.project_id
        after = self._history._purge_cursors.get('orchestrations')
        statement = 'SELECT rowid,orchestration_handle,sequence,checkpoint_digest FROM project_orchestrations WHERE project_id=?'
        rows = self._db.execute(statement + ('' if after is None else ' AND rowid>?') + ' ORDER BY rowid LIMIT ?',
                                (project, *(() if after is None else (after,)), limit * 16)).fetchall()
        if not rows and after is not None:
            self._history._purge_cursors['orchestrations'] = None
            return self.purge_closed(now, limit)  # Start again from the first.
        named, purged = None, 0
        for row in rows:
            if purged >= limit:
                break  # The cursor stays on the last row examined; the next pass goes on from there.
            self._history._purge_cursors['orchestrations'] = row['rowid']
            snapshot = self._latest_snapshot(row)
            if (snapshot is None or snapshot['status'] not in ORCHESTRATION_TERMINAL_STATES
                    or snapshot['ended_at_ms'] is None or now <= snapshot['ended_at_ms'] + ORCHESTRATION_RETENTION_MS):
                continue
            if named is None:
                # A lease names its orchestration only inside its sealed binding: read them once per pass.
                named = set()
                for lease in self._db.execute('SELECT * FROM result_leases WHERE project_id=?', (project,)).fetchall():
                    try:
                        named.add(self._history._lease(lease).binding.orchestration_handle)
                    except HistoryError:
                        return purged  # Unreadable: keep every orchestration rather than guess.
            handle = row['orchestration_handle']
            if handle in named or self._db.execute('SELECT 1 FROM result_reservations WHERE project_id=? '
                                                   'AND orchestration_handle=? LIMIT 1', (project, handle)).fetchone():
                continue  # Still named; a later one may still go (re-review 034-security-2).
            with self._history._transaction():
                for table in ('project_orchestration_requests', 'project_orchestration_checkpoints',
                              'project_orchestrations'):
                    self._db.execute(f'DELETE FROM {table} WHERE project_id=? AND orchestration_handle=?',
                                     (project, handle))
            purged += 1
        return purged

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

    def _positions(self, table, handle):
        # Only fixed owner-selected tables, bounded scalar rows before any blob.
        if table not in _TARGET_ROWS:
            raise HistoryError('history_invalid_input')
        if self._history._result_capacity is None:
            raise HistoryError('history_orchestration_unavailable')
        # This bounds the verification work, not results (0.3.4 R1e). B6 review r1: only this orchestration's own
        # rows, found by index, so other orchestrations' kept rows neither count against it nor add work.
        positions = self._db.execute(_TARGET_ROWS[table] + ' LIMIT ?',
                                     (self._history.project_id, handle, MAX_LEASE_ROWS + 1)).fetchall()
        if len(positions) > MAX_LEASE_ROWS:
            raise HistoryError('history_orchestration_unavailable')
        return positions

    def _account_released(self, binding):
        """Whether the account's release of this run is verified in the project store (0.3.4 R7): the
        child is gone and its slot freed, which a timeout, a missing process or a fenced terminal
        does not prove."""
        from .project_release_ack import read_account_release
        row = self._db.execute('SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?',
                               (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
        if row is None:
            return False  # A legacy reservation without a lease has no release evidence here.
        lease = self._history._lease(row)
        if (lease.claim_id != binding.claim_id or lease.binding.orchestration_handle != binding.orchestration_handle
                or lease.binding.run_id != binding.run_id):
            raise HistoryError('history_conflict')  # Not this reservation's lease (review 034-security).
        try:
            read_account_release(self._history, lease.binding)
        except HistoryError as exc:
            if exc.reason_code == 'history_result_not_ready':
                return False
            raise
        return True

    def _settled(self, binding):
        """B4s: whether this reservation's attempt has a settlement row whose closure verifies for its own lease."""
        row = self._db.execute('SELECT * FROM result_leases WHERE project_id=? AND run_handle=? AND attempt=?',
                               (binding.project_id, binding.run_handle, binding.attempt)).fetchone()
        if row is None:
            return False
        lease = self._history._lease(row)
        return lease.claim_id == binding.claim_id and self._history._attempt_settled(lease)

    def _unfinished(self, scope, handle, *, unrecorded=None):
        # A timeout, absent process or accepted terminal does not prove cleanup.
        # B6 review r1 (HIGH retained history): only this orchestration's own leases and reservations (each still
        # verified by its seal), so no project-wide scan; its reservations' consumptions are verified below.
        for position in self._positions('result_leases', handle):
            row = self._db.execute('SELECT * FROM result_leases WHERE rowid=?', (position[0],)).fetchone()
            lease = self._history._lease(row)
            if lease.binding.orchestration_handle == handle:
                if not scope.matches_attempt(lease.binding):
                    raise HistoryError('history_conflict')
                if lease.state in {'reserved', 'claim_pending'}:
                    return True
        for position in self._positions('result_reservations', handle):
            row = self._db.execute('SELECT * FROM result_reservations WHERE rowid=?', (position[0],)).fetchone()
            reservation = self._history._reservation(row)
            binding = reservation.binding
            if binding.orchestration_handle != handle:
                continue
            if not scope.matches_attempt(binding):
                raise HistoryError('history_conflict')
            if self._settled(binding):
                continue  # B4s (RS-B-B4-5): ended by the account's verified settlement, before any consumption check
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
                if exc.reason_code != 'history_not_found':
                    raise
                if not self._account_released(binding):
                    return True
                if unrecorded is not None:
                    unrecorded.append(True)
                continue  # 0.3.4 R7: over, by the account's verified release; reported unrecorded.

            receipt = parse_bounded_json(entry.content)
            if (entry.source != 'host' or receipt.get('record_type') != 'host_result_admission'
                    or receipt.get('binding_digest') != digest or receipt['run_id'] != binding.run_id
                    or receipt['registration_digest'] != grant.registration_digest
                    or receipt['dispatch_result']['record_id'] != reservation.terminal_record_id
                    or receipt['dispatch_result']['result_digest'] != reservation.result_digest):
                raise HistoryError('history_conflict')
            self._history._host_result_receipt(entry)
        return False

    def _unattended_eligible(self, handle, now):
        """B6 (RS-B-B6-2), inside the close's transaction: the register row is held and due, and no reservation of
        the orchestration is held or releasing (read through the held reservation rows, never the tombstones). A held
        lease is `_unfinished`'s (SH-7: a held lease row is exactly a live lease); memory kinds are not checked."""
        project = self._history.project_id
        row = self._db.execute("SELECT due_at_ms FROM project_holds WHERE project_id=? AND kind='orchestration' "
                               "AND identity=? AND state='held'", (project, handle)).fetchone()
        if row is None or not valid_integer(row[0]) or now < row[0]:
            return False
        # B6 review r1: this orchestration's own reservations by index, each with its register row by key.
        return self._db.execute(
            "SELECT 1 FROM result_reservations s INDEXED BY result_reservations_orchestration JOIN project_holds r "
            "ON r.project_id=s.project_id AND r.kind='reservation' AND r.identity=s.claim_id "
            "WHERE s.project_id=? AND s.orchestration_handle=? AND r.state<>'released' LIMIT 1",
            (project, handle)).fetchone() is None

    @_operation
    def apply(self, request, *, client_version, before_commit, _runtime_update=False, _unattended=False):
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
        if _unattended and operation != 'orchestration.close':
            raise HistoryError('history_invalid_input')
        # B6 (RS-B-B6-2): the unattended close judges at max(wall, persisted floor), read without moving the floor,
        # inside the close's own transaction (review r1).
        now = None if _unattended else self._history._result_now()
        with self._history._transaction():
            if _unattended:
                now = self._history._hold_now()
            if operation == 'orchestration.open':
                # Only open or paused ones count (0.3.4 R6); in 0.8.29 every one ever opened did.
                if self._open_count() >= limits.max_orchestrations:
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
                unrecorded = []  # filled by the close's _unfinished check; read by the update-close hold write
                if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                    for name in ('goal_tokens_used', 'goal_time_used_seconds'):
                        value = payload[name]
                        if value is not None:
                            if snapshot[name] is not None and value < snapshot[name]:
                                raise HistoryError('history_orchestration_state')
                            snapshot[name] = value
                if operation == 'orchestration.close':
                    if _unattended and not self._unattended_eligible(handle, now):
                        raise HistoryError('history_orchestration_pending')
                    if self._unfinished(scope, handle, unrecorded=unrecorded):
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
            closed = snapshot['status'] in ORCHESTRATION_TERMINAL_STATES
            if operation == 'orchestration.open':
                self._db.execute('INSERT INTO project_orchestrations(project_id,orchestration_handle,scope_digest,'
                                 "sequence,checkpoint_digest,state) VALUES(?,?,?,?,?,'open')",
                                 (scope.project_id, handle, scope_digest, 1, checkpoint_digest))
                # B6 (RS-B-B6-1): its register row, in the open's own transaction, due 24 h from now.
                project_holds_schema.take_hold(self._db, scope.project_id, 'orchestration', handle, now_ms=now,
                                               due_at_ms=now + ORCHESTRATION_IDLE_MS)
            else:
                self._db.execute('UPDATE project_orchestrations SET sequence=?,checkpoint_digest=?,state=? '
                                 'WHERE project_id=? AND orchestration_handle=?',
                                 (snapshot['sequence'], checkpoint_digest, 'closed' if closed else 'open',
                                  scope.project_id, handle))
                if not closed and self._db.execute(  # B6: activity moves the due time (and asks again at once)
                        "UPDATE project_holds SET due_at_ms=?,next_check_ms=0,evidence=NULL WHERE project_id=? "
                        "AND kind='orchestration' AND identity=? AND state='held'",
                        (now + ORCHESTRATION_IDLE_MS, scope.project_id, handle)).rowcount != 1:
                    raise HistoryError('history_incompatible')
            self._db.execute('INSERT INTO project_orchestration_checkpoints VALUES(?,?,?,?,?)',
                             (scope.project_id, handle, snapshot['sequence'], raw, checkpoint_digest))
            self._db.execute('INSERT INTO project_orchestration_requests VALUES(?,?,?,?,?)',
                             (scope.project_id, request_key, digest, handle, snapshot['sequence']))
            if operation == 'orchestration.close':
                # B6 (RS-B-B6-1): the register row is released in the close's own transaction: `closed` by the
                # primary or by an update (RS-D-2 N1 keeps its classification with it, before result retirement can
                # remove the evidence used by _unfinished; no result content), `interrupted_unattended` unattended.
                if _runtime_update:
                    kind, evidence = 'closed', project_holds_schema.evidence_json(
                        ended_by_update_unrecorded=bool(unrecorded))
                elif _unattended:
                    kind, evidence = 'interrupted_unattended', project_holds_schema.evidence_json(by='unattended')
                else:
                    kind, evidence = 'closed', project_holds_schema.evidence_json(by='primary')
                project_holds_schema.release_hold(self._db, scope.project_id, 'orchestration', handle, kind,
                                                  now_ms=now, receipt=checkpoint_digest, evidence=evidence)
            # Recheck independent current authority after the complete mutation,
            # inside the transaction and immediately before its actual commit.
            before_commit()
        view = {'snapshot': snapshot, 'checkpoint_digest': checkpoint_digest}
        return {'receipt': view, 'current': view, 'known': False}
