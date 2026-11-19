"""Original account-owner observation journal and fixed non-spending probe.

Only a captured immutable payload crosses to a worker, never a ledger/connection.
Native enrollment/current publication remain separate mandatory gates. Context
objects and callbacks are trusted adapter inputs, not evidence of those gates.
"""
from dataclasses import dataclass, field
import hmac
import os
from pathlib import Path

from .admission_metadata import api_keys_present
from .history_contracts import _DIGEST, _ID, _PROJECT, matches, valid_integer
from .learning_contracts import canonical_json, content_digest, keyed_digest, parse_bounded_json, parse_bounded_json_value
from .provider_observation_contracts import (
    MAX_OBSERVATION_REPLY_BYTES, ObservationError, ObservationInput, checked_observation_reply,
)
from .sql_read_budget import SQLReadLimits

# Counts requests not yet removed. A finished one is removed OBSERVATION_REPLAY_MS after its deadline
# (0.3.4 R6), so this holds about a day of requests, not every one ever made.
MAX_OBSERVATION_REQUESTS = 4096
# How long after its deadline a request is kept, so a caller that lost the reply can still ask again.
OBSERVATION_REPLAY_MS = 24 * 60 * 60 * 1000
_FIELDS = ('project_id', 'request_key', 'parameters_digest', 'runtime_digest', 'caller_scope', 'provider',
           'handle_digest', 'run_id', 'phase', 'observed_at_ms', 'deadline_at_ms', 'state', 'references_json')


@dataclass(frozen=True, repr=False)
class ObservationContext:
    root: str
    _origin: object = field(default_factory=object, init=False)
    _pid: int = field(default_factory=os.getpid, init=False)

    def snapshot(self):
        if (self._pid != os.getpid() or type(self.root) is not str or not 0 < len(self.root) <= 4096
                or '\0' in self.root or not Path(self.root).is_absolute() or '..' in Path(self.root).parts):
            raise ObservationError('observation_not_authorized')
        return self._origin, self.root


@dataclass(frozen=True, repr=False)
class _CapturedProbe:
    provider: str
    observed_at_ms: int
    run_id: str | None
    phase: str | None
    root: str
    state_root: str
    untrusted: tuple[str, ...]
    key: bytes
    language: str


@dataclass(frozen=True, repr=False)
class ProbeCompletion:
    capture: _CapturedProbe
    records: bytes | None
    reason: str | None


@dataclass(repr=False)
class _Reservation:
    row: dict
    request: ObservationInput
    context: ObservationContext
    snapshot: tuple
    before_commit: object
    capture: _CapturedProbe


def run_observation_probe(capture):
    """Fixed worker entry. Normalized metadata only; no database or model work."""
    if type(capture) is not _CapturedProbe:
        raise ObservationError('observation_invalid_request')
    from .host_paths import private_directory, remove_private_directory
    from . import providers

    temporary = None
    try:
        if api_keys_present(os.environ):
            raise ObservationError('observation_not_authorized')
        temporary = private_directory(Path(capture.state_root), 'scratch', 'observation-')
        options = dict(digest_key=capture.key, observed_at_ms=capture.observed_at_ms,
                       tmpdir=temporary, environ={'LANG': capture.language},
                       untrusted=tuple(Path(root) for root in capture.untrusted))
        records = (providers.observe_codex(Path(capture.root), run_id=capture.run_id, phase=capture.phase, **options)
                   if capture.provider == 'codex' else providers.observe_claude(**options))
        raw = canonical_json(records)
        if len(raw) > MAX_OBSERVATION_REPLY_BYTES:
            raise ObservationError('observation_invalid_response')
        return ProbeCompletion(capture, raw, None)
    except Exception:
        return ProbeCompletion(capture, None, 'observation_unavailable')
    finally:
        if temporary is not None:
            remove_private_directory(temporary)


class AccountObservationOwner:
    """One original account owner. Reserve before probe, commit on owner return.

    Persistent pending requests never authorize a restart/retry. Known committed
    replies reference retained original records, so the journal cannot extend
    their privacy window. A known reply neither refreshes launch observations
    nor constitutes launch eligibility. Only the future authenticated API may
    expose this owner-local operation.
    """
    def __init__(self, views, work_limits, *, timeout_ms=60_000):
        from .account_service import AccountOwnerViews

        if (type(views) is not AccountOwnerViews or type(work_limits) is not SQLReadLimits
                or type(timeout_ms) is not int or not 1 <= timeout_ms <= 60_000):
            raise ObservationError('observation_invalid_request')
        views._guard()
        if getattr(views._ledger, '_observation_owner', None) is not None:
            raise ObservationError('observation_unavailable')
        self._views, self._ledger = views, views._ledger
        self._state, self._pid = self._ledger.state, os.getpid()
        self._key = self._state.digest_key()
        self._limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms, work_limits.max_value_bytes)
        self._timeout = timeout_ms
        self._pending = {}
        self._ledger._observation_owner = self

    def _guard(self):
        self._views._guard()
        if (self._pid != os.getpid() or self._views._ledger is not self._ledger
                or self._ledger.state is not self._state
                or self._ledger._observation_owner is not self
                or not hmac.compare_digest(self._key, self._state.digest_key())):
            raise ObservationError('observation_not_authorized')

    def _authorize(self, context, snapshot, callback):
        self._guard()
        if context.snapshot() != snapshot or callback() is not True:
            raise ObservationError('observation_not_authorized')
        self._guard()
        if context.snapshot() != snapshot or api_keys_present(os.environ):
            raise ObservationError('observation_not_authorized')

    def _seal(self, row):
        return keyed_digest(self._key, b'account-observation-v1\0' + canonical_json({name: row[name] for name in _FIELDS}))

    def _read(self, operation):
        # The mode is chosen under the lock, with a bounded wait (C12c, C12f).
        return self._ledger._connection.read_in_current_mode(self._limits, operation)

    def _one(self, sql, parameters=()):
        # Only fixed internal owner SELECTs call this helper; no wire SQL/API.
        return self._read(lambda: self._ledger._connection.execute(sql, parameters).fetchone())

    def _row(self, project, key):
        row = self._one('SELECT * FROM account_observation_requests WHERE project_id=? AND request_key=?', (project, key))
        origin = self._one('SELECT * FROM account_observation_origins WHERE project_id=? AND request_key=?', (project, key))
        if row is None and origin is None:
            return None
        if row is None or origin is None:
            raise ObservationError('observation_conflict')
        row = dict(row)
        expected_origin = self._origin_seal(row)
        if (type(row['state_seal']) is not str or not hmac.compare_digest(row['state_seal'], self._seal(row))
                or type(origin['origin_seal']) is not str or not hmac.compare_digest(origin['origin_seal'], expected_origin)
                or origin['parameters_digest'] != row['parameters_digest'] or origin['observed_at_ms'] != row['observed_at_ms']):
            raise ObservationError('observation_conflict')
        return row

    def _origin_seal(self, row):
        return keyed_digest(self._key, b'account-observation-origin-v1\0' + canonical_json(
            {name: row[name] for name in ('project_id', 'request_key', 'parameters_digest', 'observed_at_ms')}))

    def _now(self):
        now = self._ledger._clock_ms()
        if not valid_integer(now, maximum=(1 << 63) - 1 - self._timeout):
            raise ObservationError('observation_unavailable')
        return now

    def _project(self, project):
        row = self._one('SELECT canonical_root,status FROM projects WHERE project_id=?', (project,))
        if (row is None or row['status'] != 'active' or type(row['canonical_root']) is not str
                or not 0 < len(row['canonical_root']) <= 4096 or '\0' in row['canonical_root']
                or not Path(row['canonical_root']).is_absolute()):
            raise ObservationError('observation_not_authorized')
        return row['canonical_root']

    def _run(self, project, request):
        from .learning_store import LedgerError

        try:
            return (None if request.run_handle is None else self._read(lambda:
                    self._ledger._open_run_for_provider_locked(project, request.run_handle, request.provider)))
        except LedgerError:
            raise ObservationError('observation_not_authorized') from None

    def _generation(self, row):
        return keyed_digest(self._key, 'account-observation-pending-v1:' + row['request_key'])

    def _retained_records(self, row):
        if row['state'] != 'committed':
            raise ObservationError('observation_unavailable')
        references = parse_bounded_json_value(row['references_json'].encode(), max_bytes=4096)
        if type(references) is not list or not 1 <= len(references) <= 8:
            raise ObservationError('observation_conflict')
        now, records = self._now(), []
        for reference in references:
            if type(reference) is not dict or reference.keys() != {'record_id', 'content_digest'}:
                raise ObservationError('observation_conflict')
            record = self._one(
                'SELECT content,content_digest,source,expires_at_ms FROM records WHERE project_id=? AND record_id=?',
                (row['project_id'], reference['record_id']))
            if (record is None or record['source'] != 'provider' or record['content_digest'] != reference['content_digest']
                    or (record['expires_at_ms'] is not None and now >= record['expires_at_ms'])
                    or now < row['observed_at_ms']):
                raise ObservationError('observation_unavailable')
            if type(record['content']) is not bytes or len(record['content']) > MAX_OBSERVATION_REPLY_BYTES:
                raise ObservationError('observation_conflict')
            value = parse_bounded_json(record['content'], max_bytes=MAX_OBSERVATION_REPLY_BYTES)
            if content_digest(value) != reference['content_digest']:
                raise ObservationError('observation_conflict')
            records.append(value)
        return records

    def _reply(self, row, request, *, known):
        records = self._retained_records(row)
        return checked_observation_reply({**request.payload(), 'run_id': row['run_id'],
            'observed_at_ms': row['observed_at_ms'], 'known': known, 'records': records}, request)

    def billing_waivers(self, project, records):
        """Current display projection from the same owner rule admission uses."""
        self._guard()
        self._project(project)
        if type(records) is not list or not 1 <= len(records) <= 8:
            raise ObservationError('observation_invalid_response')
        result = {}
        for record in records:
            if record.get('record_type') == 'provider_billing':
                waived = self._read(lambda: self._ledger.billing_waived_by_attestation(project, record))
                if type(waived) is not bool:
                    raise ObservationError('observation_unavailable')
                result[record['record_id']] = waived
        self._guard()
        return result

    def reserve(self, project, runtime, caller, identity, request, context, before_commit):
        self._guard()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(request) is not ObservationInput
                or type(context) is not ObservationContext or not callable(before_commit)):
            raise ObservationError('observation_invalid_request')
        request = ObservationInput(**request.payload())
        snapshot = context.snapshot()
        self._authorize(context, snapshot, before_commit)
        key = keyed_digest(self._key, canonical_json([project, runtime, caller, identity]))
        with self._ledger._account_transaction():
            root = self._project(project)
            parameters = keyed_digest(self._key, canonical_json([request.payload(), snapshot[1], root]))
            def current():
                self._authorize(context, snapshot, before_commit)
                if self._project(project) != root:
                    raise ObservationError('observation_not_authorized')
            existing = self._row(project, key)
            if existing is not None:
                if existing['parameters_digest'] != parameters:
                    raise ObservationError('observation_conflict')
                current()
                if key in self._pending:
                    pending = self._pending[key]
                    if pending.context is not context or pending.snapshot != snapshot:
                        raise ObservationError('observation_conflict')
                    return pending
                return self._reply(existing, request, known=True)
            if key in self._pending:
                raise ObservationError('observation_conflict')
            count = self._one('SELECT COUNT(*) FROM (SELECT 1 FROM account_observation_origins '
                'WHERE project_id=? LIMIT ?)', (project, MAX_OBSERVATION_REQUESTS + 1))[0]
            if count >= MAX_OBSERVATION_REQUESTS:
                raise ObservationError('observation_full')
            run_id, now = self._run(project, request), self._now()
            language = os.environ.get('LANG') or 'C.UTF-8'
            if type(language) is not str or len(language) > 128 or len(str(self._state.root)) > 4096:
                raise ObservationError('observation_unavailable')
            row = dict(zip(_FIELDS, (project, key, parameters, runtime, caller, request.provider,
                None if request.run_handle is None else keyed_digest(self._key, request.run_handle),
                run_id, request.phase, now, now + self._timeout, 'pending', None)))
            capture = _CapturedProbe(request.provider, now, run_id, request.phase, snapshot[1], str(self._state.root),
                                     (root,), self._key, language)
            reservation = _Reservation(row, request, context, snapshot, before_commit, capture)
            current()
            self._ledger._connection.execute('INSERT INTO account_observation_origins VALUES(?,?,?,?,?)',
                (project, key, parameters, now, self._origin_seal(row)))
            current()
            self._ledger._connection.execute('INSERT INTO account_observation_requests VALUES(' + ','.join('?' for _ in range(14)) + ')',
                                               (*row.values(), self._seal(row)))
            if request.phase == 'before':
                self._ledger._connection.execute('INSERT INTO launch_observations VALUES(?,?,?,?) '
                    'ON CONFLICT(project_id,handle_digest) DO UPDATE SET identity_digest=excluded.identity_digest,'
                    'observed_at_ms=excluded.observed_at_ms', (project, row['handle_digest'], self._generation(row), now))
            current()
        self._pending[key] = reservation
        return reservation

    def abort(self, reservation):
        self._guard()
        if type(reservation) is not _Reservation or self._pending.get(reservation.row['request_key']) is not reservation:
            raise ObservationError('observation_not_authorized')
        del self._pending[reservation.row['request_key']]

    def recheck_reply(self, key, request, context, snapshot, before_publish, result):
        """Re-read retained originals before delivery, never reserve or reprobe."""
        self._authorize(context, snapshot, before_publish)
        project, runtime, caller, identity = key
        request_key = keyed_digest(self._key, canonical_json([project, runtime, caller, identity]))
        with self._ledger._account_transaction():
            root = self._project(project)
            row = self._row(project, request_key)
            parameters = keyed_digest(self._key, canonical_json([request.payload(), snapshot[1], root]))
            if row is None or row['parameters_digest'] != parameters:
                raise ObservationError('observation_conflict')
            actual = self._reply(row, request, known=result['known'])
            self._authorize(context, snapshot, before_publish)
            if self._project(project) != root or actual != result:
                raise ObservationError('observation_not_authorized')

    def complete(self, reservation, completion):
        self._guard()
        if (type(reservation) is not _Reservation or self._pending.get(reservation.row['request_key']) is not reservation
                or type(completion) is not ProbeCompletion or completion.capture is not reservation.capture):
            raise ObservationError('observation_not_authorized')
        try:
            self._authorize(reservation.context, reservation.snapshot, reservation.before_commit)
            if completion.reason is not None or type(completion.records) is not bytes:
                raise ObservationError('observation_unavailable')
            records = parse_bounded_json_value(completion.records, max_bytes=MAX_OBSERVATION_REPLY_BYTES)
            request, original = reservation.request, reservation.row
            reply = checked_observation_reply({**request.payload(), 'run_id': original['run_id'],
                'observed_at_ms': original['observed_at_ms'], 'known': False, 'records': records}, request)
            with self._ledger._account_transaction():
                def current():
                    self._authorize(reservation.context, reservation.snapshot, reservation.before_commit)
                    if (self._project(original['project_id']),) != reservation.capture.untrusted:
                        raise ObservationError('observation_not_authorized')
                current()
                if (self._row(original['project_id'], original['request_key']) != {**original, 'state_seal': self._seal(original)}
                        or self._run(original['project_id'], request) != original['run_id']
                        or not original['observed_at_ms'] <= self._now() < original['deadline_at_ms']):
                    raise ObservationError('observation_conflict')
                if request.phase == 'before':
                    marker = self._one('SELECT identity_digest,observed_at_ms FROM launch_observations '
                        'WHERE project_id=? AND handle_digest=?', (original['project_id'], original['handle_digest']))
                    if marker is None or tuple(marker) != (self._generation(original), original['observed_at_ms']):
                        raise ObservationError('observation_conflict')
                references = []
                for record in records:
                    current()
                    self._ledger._append_checked_locked(original['project_id'], record, producer='provider',
                                                         timestamp=original['observed_at_ms'])
                    current()
                    references.append({'record_id': record['record_id'], 'content_digest': content_digest(record)})
                if request.phase == 'before':
                    identity = next(record for record in records if record['record_type'] == 'provider_identity')
                    self._ledger._connection.execute('UPDATE launch_observations SET identity_digest=? '
                        'WHERE project_id=? AND handle_digest=?', (content_digest(identity), original['project_id'], original['handle_digest']))
                    current()
                committed = {**original, 'state': 'committed', 'references_json': canonical_json(references).decode()}
                self._ledger._connection.execute('UPDATE account_observation_requests SET state=?,references_json=?,state_seal=? '
                    'WHERE project_id=? AND request_key=?', ('committed', committed['references_json'], self._seal(committed),
                                                            original['project_id'], original['request_key']))
                current()
            self._state.harden_database_files()
            return reply
        finally:
            self.abort(reservation)


def purge_old_observations(ledger, now, limit):
    """Remove observation requests more than OBSERVATION_REPLAY_MS past their deadline, each with its
    origin in the same transaction (one without the other is damage). Bounded; returns how many went.
    The caller holds the ledger lock outside any transaction."""
    cutoff = now - OBSERVATION_REPLAY_MS
    rows = ledger._connection.execute('SELECT project_id,request_key FROM account_observation_requests '
                                      'WHERE deadline_at_ms<? LIMIT ?', (cutoff, limit)).fetchall()
    if rows:
        with ledger._account_transaction():
            for project, key in rows:
                for table in ('account_observation_requests', 'account_observation_origins'):
                    ledger._connection.execute(f'DELETE FROM {table} WHERE project_id=? AND request_key=?',
                                               (project, key))
    return len(rows)
