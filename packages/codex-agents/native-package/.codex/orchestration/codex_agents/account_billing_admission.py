"""Original billing-probe admission, before any model worker may be offered.

Only the actual registered signature return is consumed. An admitted request
survives crashes as active/unknown custody; this component cannot release it.
Native authentication, launch containment and confirmed return are separate gates.
"""
from dataclasses import dataclass, replace
import hmac
import os

from .admission_inputs import AdmissionCaptureLimits
from .admission_metadata import API_KEY_VARIABLES, evaluate_admission
from .account_observation import ObservationContext
from .account_open_admission import open_admission_inputs
from .billing_probe_schema import assert_billing_probe_schema
from .claim_admission import PreparedClaimAdmission, admission_signature_rows, same_admission_inputs
from .history_contracts import _DIGEST, _ID, _PROJECT, matches, valid_integer
from .learning_contracts import canonical_json, content_digest, parse_bounded_json_value, validate_learning_record
from .signature_worker_pool import SignatureWorkerPool
from .sql_read_budget import SQLReadLimits

MAX_PROBE_REQUESTS = 4096
MAX_PENDING_PROBES = 64
MAX_PENDING_PROJECT_PROBES = 8
MAX_PROBE_REPLY_BYTES = 12_288
_FIELDS = ('project_id', 'request_key', 'parameters_digest', 'runtime_digest', 'caller_scope',
    'created_at_ms', 'deadline_at_ms', 'state', 'account_fingerprint', 'admission_record_id',
    'admission_digest', 'references_json')


class ProbeAdmissionError(RuntimeError):
    def __init__(self, reason='probe_unavailable'):
        self.reason_code = reason if type(reason) is str and reason in {
            'probe_invalid_request', 'probe_not_authorized', 'probe_unavailable', 'probe_conflict',
            'probe_full', 'probe_busy', 'probe_not_ready'} else 'probe_unavailable'
        super().__init__(self.reason_code)


@dataclass(frozen=True, repr=False, eq=False)
class _Reservation:
    project: str
    key: str
    parameters: str
    context: ObservationContext
    snapshot: tuple
    authorize: object
    created: int
    deadline: int


@dataclass(frozen=True, repr=False, eq=False)
class ProbeAdmission:
    """Owner-retained original, not JSON/native authority or a launch capability."""
    reservation: _Reservation
    account_fingerprint: str
    admission_record_id: str
    admission_digest: str
    admitted_at_ms: int


class AccountBillingAdmission:
    def __init__(self, views, pool, work_limits, *, timeout_ms=60_000):
        from .account_service import AccountOwnerViews
        if (type(views) is not AccountOwnerViews or type(pool) is not SignatureWorkerPool
                or type(work_limits) is not SQLReadLimits or not valid_integer(timeout_ms, minimum=1, maximum=60_000)):
            raise ProbeAdmissionError('probe_invalid_request')
        views._guard()
        pool._owner()
        ledger = views._ledger
        if (getattr(ledger, '_billing_admission_owner', None) is not None
                or getattr(ledger, '_signature_job_intake', None) is not pool._intake
                or pool._intake._views._ledger is not ledger
                or getattr(pool._intake, '_signature_worker_pool', None) is not pool):
            raise ProbeAdmissionError()
        assert_billing_probe_schema(ledger._connection)
        self._views, self._ledger, self._pool, self._intake = views, ledger, pool, pool._intake
        self._pid, self._key, self._timeout = os.getpid(), ledger.state.digest_key(), timeout_ms
        self._limits = SQLReadLimits(work_limits.max_callbacks, work_limits.max_duration_ms, work_limits.max_value_bytes)
        self._pending, self._signatures, self._permits = {}, {}, {}
        ledger._billing_admission_owner = self

    def _guard(self):
        self._views._guard()
        self._pool._owner()
        if (self._pid != os.getpid() or self._views._ledger is not self._ledger
                or getattr(self._ledger, '_billing_admission_owner', None) is not self
                or self._pool._intake is not self._intake
                or not hmac.compare_digest(self._key, self._ledger.state.digest_key())):
            raise ProbeAdmissionError('probe_not_authorized')

    def _read(self, operation):
        connection = self._ledger._connection
        run = connection.transaction_read_call if connection.in_transaction else connection.read_call
        return run(self._limits, operation)

    def _one(self, sql, parameters=()):
        return self._read(lambda: self._ledger._connection.execute(sql, parameters).fetchone())

    def _authorize(self, context, snapshot, callback):
        self._guard()
        if (type(context) is not ObservationContext or context.snapshot() != snapshot
                or not callable(callback) or callback() is not True):
            raise ProbeAdmissionError('probe_not_authorized')
        self._guard()
        if context.snapshot() != snapshot or any(os.environ.get(n) for n in API_KEY_VARIABLES):
            raise ProbeAdmissionError('probe_not_authorized')

    def _project(self, project, context):
        row = self._one('SELECT canonical_root,status FROM projects WHERE project_id=?', (project,))
        if row is None or row['status'] != 'active' or row['canonical_root'] != context.root:
            raise ProbeAdmissionError('probe_not_authorized')
        return row['canonical_root']

    def _seal(self, row):
        return keyed(self._key, b'billing-probe-state-v1\0', {n: row[n] for n in _FIELDS})

    def _origin_seal(self, row):
        return keyed(self._key, b'billing-probe-origin-v1\0',
            {n: row[n] for n in ('project_id', 'request_key', 'parameters_digest', 'created_at_ms')})

    def _row(self, project, key):
        row = self._one('SELECT * FROM account_billing_probe_requests WHERE project_id=? AND request_key=?', (project, key))
        origin = self._one('SELECT * FROM account_billing_probe_origins WHERE project_id=? AND request_key=?', (project, key))
        if row is None and origin is None:
            return None
        if row is None or origin is None:
            raise ProbeAdmissionError('probe_conflict')
        row = dict(row)
        if (type(row['state_seal']) is not str or not hmac.compare_digest(row['state_seal'], self._seal(row))
                or type(origin['origin_seal']) is not str
                or not hmac.compare_digest(origin['origin_seal'], self._origin_seal(row))
                or origin['parameters_digest'] != row['parameters_digest'] or origin['created_at_ms'] != row['created_at_ms']):
            raise ProbeAdmissionError('probe_conflict')
        return row

    def _now(self):
        now = self._ledger._clock_ms()
        if not valid_integer(now, maximum=(1 << 63)-1-self._timeout):
            raise ProbeAdmissionError()
        return now

    def reserve(self, project, runtime, caller, identity, context, *, before_commit=None):
        self._guard()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(context) is not ObservationContext):
            raise ProbeAdmissionError('probe_invalid_request')
        snapshot = context.snapshot()
        self._authorize(context, snapshot, before_commit)
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        with self._ledger._account_transaction():
            root = self._project(project, context)
            parameters = keyed(self._key, b'billing-probe-parameters-v1\0', [runtime, caller, root])
            row = self._row(project, key)
            if row is not None:
                if row['parameters_digest'] != parameters:
                    raise ProbeAdmissionError('probe_conflict')
                if row['state'] == 'committed':
                    reply = self._reply(row, known=True)
                    self._authorize(context, snapshot, before_commit)
                    if self._project(project, context) != root or self._row(project, key) != row:
                        raise ProbeAdmissionError('probe_conflict')
                    return reply
                original = self._pending.get((project, key))
                if original is None:
                    raise ProbeAdmissionError()  # Restart cannot recreate a launchable original.
                if original.context is not context or original.snapshot != snapshot:
                    raise ProbeAdmissionError('probe_conflict')
                self._authorize(context, snapshot, before_commit)
                return original
            count = self._one('SELECT COUNT(*) FROM (SELECT 1 FROM account_billing_probe_origins '
                'WHERE project_id=? LIMIT ?)', (project, MAX_PROBE_REQUESTS+1))[0]
            if count >= MAX_PROBE_REQUESTS:
                raise ProbeAdmissionError('probe_full')
            if (len(self._pending) >= MAX_PENDING_PROBES
                    or sum(p == project for p, _ in self._pending) >= MAX_PENDING_PROJECT_PROBES):
                raise ProbeAdmissionError('probe_full')
            now = self._now()
            row = dict(zip(_FIELDS, (project, key, parameters, runtime, caller, now, now+self._timeout,
                'reserved', None, None, None, None)))
            original = _Reservation(project, key, parameters, context, snapshot, before_commit, now, now+self._timeout)
            self._authorize(context, snapshot, before_commit)
            self._ledger._connection.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                (project, key, parameters, now, self._origin_seal(row)))
            self._ledger._connection.execute('INSERT INTO account_billing_probe_requests VALUES('
                +','.join('?' for _ in range(len(_FIELDS)+1))+')', (*row.values(), self._seal(row)))
            self._authorize(context, snapshot, before_commit)
            if self._project(project, context) != root or self._row(project, key) != {**row, 'state_seal': self._seal(row)}:
                raise ProbeAdmissionError('probe_conflict')
        self._pending[(project, key)] = original
        return original

    def _original(self, reservation):
        self._guard()
        if (type(reservation) is not _Reservation
                or self._pending.get((reservation.project, reservation.key)) is not reservation):
            raise ProbeAdmissionError('probe_not_authorized')
        self._authorize(reservation.context, reservation.snapshot, reservation.authorize)
        self._project(reservation.project, reservation.context)
        row = self._row(reservation.project, reservation.key)
        if (row is None or row['parameters_digest'] != reservation.parameters
                or row['created_at_ms'] != reservation.created or row['deadline_at_ms'] != reservation.deadline):
            raise ProbeAdmissionError('probe_conflict')
        return row

    def prepare(self, reservation):
        row = self._original(reservation)
        if row['state'] != 'reserved':
            raise ProbeAdmissionError('probe_conflict')
        if not reservation.created <= self._now() < reservation.deadline:
            raise ProbeAdmissionError('probe_conflict')
        if reservation not in self._signatures:
            self._signatures[reservation] = self._intake.capture_admission(reservation.project, 'claude', self._limits)
        return self._signatures[reservation]

    def _fresh(self, reservation, snapshot, *, appended=None):
        limits = self._intake._limits
        signer = self._ledger._current_signer_bytes()
        policy = self._intake._views._claim_policy('claude')
        current = self._read(lambda: self._intake._views._capture_admission_rows_locked(reservation.project,
            'claude', policy, AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer))
        expected = snapshot
        if appended is not None:
            rows = [r for r in current.rows if r[1] == reservation.project and r[2] == appended['record_id']]
            originals = [r for r in snapshot.rows if r[1] == reservation.project and r[2] == appended['record_id']]
            if (len(rows) != 1 or rows[0][4] != 'probe_admission' or rows[0][5] != canonical_json(appended)
                    or rows[0][6] != content_digest(appended) or rows[0][7] != 'lifecycle'
                    or (originals and originals != rows)):
                raise ProbeAdmissionError('probe_conflict')
            if not originals:
                expected = replace(snapshot, rows=tuple(sorted((*snapshot.rows, rows[0]), key=lambda r: r[0])))
        if (not same_admission_inputs(expected, current)
                or not reservation.created <= current.captured_at_ms < reservation.deadline):
            raise ProbeAdmissionError('probe_conflict')
        return current.captured_at_ms

    def admit(self, reservation):
        row = self._original(reservation)
        if row['state'] == 'admitted':
            original = self._permits.get(reservation)
            if original is None:
                raise ProbeAdmissionError()
            return original
        handle = self._signatures.get(reservation)
        if handle is None:
            raise ProbeAdmissionError('probe_not_ready')
        result = self._pool.result(handle)
        inputs = self._intake.returned_inputs(handle)
        snapshot, _ = self._intake.returned_admission(handle)
        if (result.reason != 'signature_execution_completed' or type(result.prepared) is not PreparedClaimAdmission
                or result.prepared.snapshot is not snapshot or snapshot.project_id != reservation.project
                or snapshot.provider != 'claude' or inputs.provider != 'claude'
                or inputs.origin is not self._ledger._attestation_origin
                or snapshot.origin is not self._intake._views._admission_capture_origin
                or inputs.rows != admission_signature_rows(snapshot)
                or snapshot.owner != inputs.owner or snapshot.signer != inputs.signer):
            raise ProbeAdmissionError()
        verified = frozenset(inputs.rows[index][0] for index in result.indices)
        prepared = open_admission_inputs(result.prepared, verified)
        guard = {n: 'present' for n in API_KEY_VARIABLES if os.environ.get(n)}
        with self._ledger._account_transaction():
            row = self._original(reservation)
            if row['state'] != 'reserved':
                raise ProbeAdmissionError('probe_conflict')
            now = self._fresh(reservation, snapshot)
            identities = prepared['identities']
            identity = max((r for r, _ in identities), key=lambda r: r['observed_at_ms'], default=None)
            if identity is None or not reservation.created <= identity['observed_at_ms'] <= now:
                raise ProbeAdmissionError()
            fingerprint = identity['account_fingerprint']
            latest = max((r for r, _ in prepared['billing'] if r['account_fingerprint'] == fingerprint),
                         key=lambda r: r['observed_at_ms'], default=None)
            if latest is not None and 0 <= now-latest['observed_at_ms'] < 300_000:
                heads = [r for r, _ in prepared['headroom'] if r['account_fingerprint'] == fingerprint]
                selected = [identity, *heads[-1:], latest]
                final = self._commit_reply(row, 'reused', None, selected, result.prepared.rows)
                appended = None
            else:
                cooldown = any(r.record['record_type'] == 'probe_admission'
                    and r.record['provider'] == 'claude' and r.record['decision'] == 'pass'
                    and r.record['account_fingerprint'] == fingerprint and r.record['observed_at_ms'] > now-300_000
                    for r in result.prepared.rows)
                decision = ({'provider': 'claude', 'decision': 'deny', 'reason_code': 'probe_cooldown',
                    'admission_basis': None, 'account_fingerprint': fingerprint, 'evidence_digests': [], 'observed_at_ms': now}
                    if cooldown else evaluate_admission(provider='claude', identities=prepared['identities'],
                        attestations=prepared['attestations'], billing=prepared['billing'], headroom=prepared['headroom'],
                        window_headroom=prepared['headroom'], prior_claude_admissions=prepared['prior_claude_admissions'],
                        environ=guard, now_ms=now, admission_policy=prepared['admission_policy'],
                        cited_digests=prepared['cited_digests'], probe=True,
                        prior_run_admissions=[(f, t) for f, t in prepared['prior_admissions'] if t >= now-18_000_000]))
                if decision['decision'] == 'pass':
                    self._read(lambda: self._ledger._check_active_capacity_locked('claude', fingerprint))
                    if self._one("SELECT 1 FROM account_billing_probe_requests INDEXED BY billing_probe_active_account "
                            "WHERE state='admitted' AND account_fingerprint=? LIMIT 1", (fingerprint,)) is not None:
                        raise ProbeAdmissionError('probe_busy')
                material = {'schema_version': 1, 'record_type': 'probe_admission', **decision}
                appended = validate_learning_record({**material, 'record_id': 'probe_admission:'+content_digest(material)})
                self._ledger._store_probe_admission_locked(reservation.project, decision, timestamp=now)
                row = {**row, 'account_fingerprint': fingerprint,
                    'admission_record_id': appended['record_id'], 'admission_digest': content_digest(appended)}
                if decision['decision'] == 'pass':
                    final = self._update(row, state='admitted')
                else:
                    final = self._commit_reply(row, 'denied', decision['reason_code'], [], result.prepared.rows)
            self._authorize(reservation.context, reservation.snapshot, reservation.authorize)
            self._fresh(reservation, snapshot, appended=appended)
            if ({n: 'present' for n in API_KEY_VARIABLES if os.environ.get(n)} != guard
                    or self._row(reservation.project, reservation.key) != final):
                raise ProbeAdmissionError('probe_conflict')
        self._pool.discard(handle)
        self._signatures.pop(reservation)
        if final['state'] == 'admitted':
            original = ProbeAdmission(reservation, final['account_fingerprint'], final['admission_record_id'],
                final['admission_digest'], now)
            self._permits[reservation] = original
            self._original(reservation)
            return original
        self._pending.pop((reservation.project, reservation.key))
        reply = self._reply(final, known=False)
        self._authorize(reservation.context, reservation.snapshot, reservation.authorize)
        if self._row(reservation.project, reservation.key) != final:
            raise ProbeAdmissionError('probe_conflict')
        return reply

    def _update(self, row, **changes):
        final = {**row, **changes}
        final['state_seal'] = self._seal(final)
        names = [n for n in _FIELDS if n not in {'project_id', 'request_key'}]
        self._ledger._connection.execute('UPDATE account_billing_probe_requests SET '
            +','.join(n+'=?' for n in (*names, 'state_seal'))+' WHERE project_id=? AND request_key=?',
            (*(final[n] for n in (*names, 'state_seal')), row['project_id'], row['request_key']))
        return final

    def _commit_reply(self, row, probe, reason, selected, parsed_rows):
        refs = []
        for record in selected:
            actual = next((r.raw for r in parsed_rows if r.raw[2] == record['record_id']
                and r.raw[6] == content_digest(record) and r.raw[7] == 'provider'), None)
            if actual is None:
                raise ProbeAdmissionError()
            refs.append({'project_id': actual[1], 'record_id': actual[2], 'content_digest': actual[6]})
        raw = canonical_json({'probe': probe, 'reason_code': reason, 'records': refs})
        if len(raw) > 4096:
            raise ProbeAdmissionError()
        return self._update(row, state='committed', references_json=raw.decode())

    def _reply(self, row, *, known):
        from .learning_store import DEFAULT_RETENTION_MS
        value = parse_bounded_json_value(row['references_json'].encode(), max_bytes=4096)
        if (type(value) is not dict or value.keys() != {'probe', 'reason_code', 'records'}
                or value['probe'] not in {'reused', 'denied'} or type(value['records']) is not list
                or len(value['records']) > 3):
            raise ProbeAdmissionError('probe_conflict')
        now, records = self._now(), []
        for ref in value['records']:
            if type(ref) is not dict or ref.keys() != {'project_id', 'record_id', 'content_digest'}:
                raise ProbeAdmissionError('probe_conflict')
            raw = self._one("SELECT content,content_digest,source,observed_at_ms FROM records WHERE project_id=? AND record_id=?",
                (ref['project_id'], ref['record_id']))
            if (raw is None or raw['source'] != 'provider' or raw['content_digest'] != ref['content_digest']
                    or not now-DEFAULT_RETENTION_MS <= raw['observed_at_ms'] <= now):
                raise ProbeAdmissionError()
            record = validate_learning_record(parse_bounded_json_value(bytes(raw['content']), max_bytes=MAX_PROBE_REPLY_BYTES))
            if (record['provider'] != 'claude' or content_digest(record) != ref['content_digest']
                    or record['record_id'] != ref['record_id']):
                raise ProbeAdmissionError('probe_conflict')
            records.append(record)
        reply = {'probe': value['probe'], 'reason_code': value['reason_code'], 'records': records,
            'known': known, 'observed_at_ms': row['created_at_ms']}
        if len(canonical_json(reply)) > MAX_PROBE_REPLY_BYTES:
            raise ProbeAdmissionError()
        return reply


def keyed(key, domain, value):
    from .learning_contracts import keyed_digest
    return keyed_digest(key, domain+canonical_json(value))
