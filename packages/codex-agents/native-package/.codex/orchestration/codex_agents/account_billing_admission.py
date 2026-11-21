"""Original billing-probe admission, before any model worker may be offered.

Only the actual registered signature return is consumed. An admitted request
survives crashes as active/unknown custody; this component cannot release it.
Native authentication, launch containment and confirmed return are separate gates.
"""
from dataclasses import dataclass, replace
import hmac
import os
import threading
import time

from .account_claim_contracts import ClaimError
from .admission_inputs import AdmissionCaptureLimits
from .admission_metadata import API_KEY_VARIABLES, evaluate_admission
from .account_observation import ObservationContext
from .account_open_admission import open_admission_inputs
from .billing_probe_schema import assert_billing_probe_schema
from .billing_probe_contracts import (PROBE_COMPLETION_REASONS, PROBE_STEP_REASONS, ProbeAdmissionError,
    valid_client_version)
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

# Billing this recent is reused instead of probing; an admitted probe reserves its account this long.
PROBE_REUSE_MS = 300_000
PROBE_COOLDOWN_MS = 300_000


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
    deadline_ns: int


@dataclass(frozen=True, repr=False, eq=False)
class ProbeAdmission:
    """Owner-retained original, not JSON/native authority or a launch capability."""
    reservation: _Reservation
    account_fingerprint: str
    admission_record_id: str
    admission_digest: str
    admitted_at_ms: int


@dataclass(frozen=True, repr=False)
class _ExecutionInputs:
    permit_fields: tuple
    reservation: _Reservation
    snapshot: object
    identity: bytes
    admission: bytes
    deadline_ns: int


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
        self._pending, self._signatures, self._permits, self._execution_inputs, self._reserved_inputs = {}, {}, {}, {}, {}
        # O0b RS-A round 2 G3: set (from any thread) once a sweep has committed expired probes; read on the owner.
        self._closed_notice = threading.Event()
        # O0b RS-A round 2 G9: closed originals a live billing job still holds; that job ends them.
        self._closed_live = set()
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
        # The mode is chosen under the lock, with a bounded wait (C12c, C12f).
        return self._ledger._connection.read_in_current_mode(self._limits, operation)

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
                    # O0b RS-A round 2 G3: a replay of a request the sweep closed frees its memory (its row
                    # was committed by an earlier transaction, so this one rolling back cannot undo it).
                    self._forget_closed_contained()
                    return reply
                original = self._pending.get((project, key))
                if original is None:
                    raise ProbeAdmissionError()  # Restart cannot recreate a launchable original.
                if original.context is not context or original.snapshot != snapshot:
                    raise ProbeAdmissionError('probe_conflict')
                self._authorize(context, snapshot, before_commit)
                return original
            # O0b B0-5 (narrowed): only probes in progress count, so finished ones never fill a project
            # for good; their records stay, so a replay still finds its stored answer.
            count = self._one("SELECT COUNT(*) FROM (SELECT 1 FROM account_billing_probe_requests "
                "WHERE project_id=? AND state<>'committed' LIMIT ?)", (project, MAX_PROBE_REQUESTS+1))[0]
            if count >= MAX_PROBE_REQUESTS:
                raise ProbeAdmissionError('probe_full')
            self._forget_closed_contained()
            if (len(self._pending) >= MAX_PENDING_PROBES
                    or sum(p == project for p, _ in self._pending) >= MAX_PENDING_PROJECT_PROBES):
                raise ProbeAdmissionError('probe_full')
            created_ns = time.monotonic_ns()
            now = self._now()
            row = dict(zip(_FIELDS, (project, key, parameters, runtime, caller, now, now+self._timeout,
                'reserved', None, None, None, None)))
            original = _Reservation(project, key, parameters, context, snapshot, before_commit,
                                    now, now+self._timeout, created_ns+self._timeout*1_000_000)
            self._authorize(context, snapshot, before_commit)
            self._ledger._connection.execute('INSERT INTO account_billing_probe_origins VALUES(?,?,?,?,?)',
                (project, key, parameters, now, self._origin_seal(row)))
            self._ledger._connection.execute('INSERT INTO account_billing_probe_requests VALUES('
                +','.join('?' for _ in range(len(_FIELDS)+1))+')', (*row.values(), self._seal(row)))
            self._authorize(context, snapshot, before_commit)
            if self._project(project, context) != root or self._row(project, key) != {**row, 'state_seal': self._seal(row)}:
                raise ProbeAdmissionError('probe_conflict')
        self._pending[(project, key)] = original
        self._reserved_inputs[original] = (self._reservation_fields(original), context, snapshot, before_commit)
        return original

    @staticmethod
    def _reservation_fields(reservation):
        return (reservation.project, reservation.key, reservation.parameters, reservation.created,
                reservation.deadline, reservation.deadline_ns)

    def _original(self, reservation):
        self._guard()
        if (type(reservation) is not _Reservation
                or self._pending.get((reservation.project, reservation.key)) is not reservation):
            raise ProbeAdmissionError('probe_not_authorized')
        captured = self._reserved_inputs.get(reservation)
        if (captured is None or self._reservation_fields(reservation) != captured[0]
                or reservation.context is not captured[1] or reservation.snapshot != captured[2]
                or reservation.authorize is not captured[3]):
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

    def abort_reserved(self, reservation):
        """Drop only original unadmitted memory after its fixed jobs are stopped.

        Durable intent remains reserved and cannot be recreated after this drop.
        This operation never changes or releases an admitted database row.
        """
        self._guard()
        if (type(reservation) is not _Reservation
                or self._pending.get((reservation.project, reservation.key)) is not reservation
                or reservation in self._signatures or reservation in self._permits):
            raise ProbeAdmissionError('probe_conflict')
        row = self._row(reservation.project, reservation.key)
        if row is None or row['state'] != 'reserved':
            raise ProbeAdmissionError('probe_conflict')
        self._pending.pop((reservation.project, reservation.key))
        self._reserved_inputs.pop(reservation)

    def _fresh(self, reservation, snapshot, *, appended=None):
        limits = self._intake._limits
        signer = self._ledger._current_signer_bytes()
        policy = self._intake._views._claim_policy('claude')
        current = self._read(lambda: self._intake._views._capture_admission_rows_locked(reservation.project,
            'claude', policy, AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer,
            snapshot.anchor_ms))
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
            execution_owner = getattr(self, '_execution_owner', None)
            if (execution_owner is not None
                    and execution_owner._prepared_identity(reservation) != canonical_json(identity)):
                raise ProbeAdmissionError('probe_conflict')
            fingerprint = identity['account_fingerprint']
            latest = max((r for r, _ in prepared['billing'] if r['account_fingerprint'] == fingerprint),
                         key=lambda r: r['observed_at_ms'], default=None)
            if latest is not None and 0 <= now-latest['observed_at_ms'] < PROBE_REUSE_MS:
                heads = [r for r, _ in prepared['headroom'] if r['account_fingerprint'] == fingerprint]
                selected = [identity, *heads[-1:], latest]
                final = self._commit_reply(row, 'reused', None, selected, result.prepared.rows)
                appended = None
            else:
                cooldown = any(r.record['record_type'] == 'probe_admission'
                    and r.record['provider'] == 'claude' and r.record['decision'] == 'pass'
                    and r.record['account_fingerprint'] == fingerprint and r.record['observed_at_ms'] > now-PROBE_COOLDOWN_MS
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
                    try:
                        self._read(lambda: self._ledger._check_active_capacity_locked('claude', fingerprint))
                    except ClaimError as error:
                        if error.reason_code != 'claim_busy':
                            raise
                        # Every slot is held: a probe reason, so it is not reported as unavailable.
                        raise ProbeAdmissionError('probe_busy') from None
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
            if (execution_owner is not None
                    and execution_owner._prepared_identity(reservation) != canonical_json(identity)):
                raise ProbeAdmissionError('probe_conflict')
            if ({n: 'present' for n in API_KEY_VARIABLES if os.environ.get(n)} != guard
                    or self._row(reservation.project, reservation.key) != final):
                raise ProbeAdmissionError('probe_conflict')
        self._pool.discard(handle)
        self._signatures.pop(reservation)
        if final['state'] == 'admitted':
            original = ProbeAdmission(reservation, final['account_fingerprint'], final['admission_record_id'],
                final['admission_digest'], now)
            self._permits[reservation] = original
            self._execution_inputs[original] = _ExecutionInputs(
                (original.account_fingerprint, original.admission_record_id, original.admission_digest,
                 original.admitted_at_ms), reservation,
                replace(snapshot, policy=replace(snapshot.policy)), canonical_json(identity),
                canonical_json(appended), reservation.deadline_ns)
            self._original(reservation)
            return original
        self._pending.pop((reservation.project, reservation.key))
        self._reserved_inputs.pop(reservation)
        reply = self._reply(final, known=False)
        self._authorize(reservation.context, reservation.snapshot, reservation.authorize)
        if self._row(reservation.project, reservation.key) != final:
            raise ProbeAdmissionError('probe_conflict')
        return reply

    def settle_unlaunched(self, reservation, reason):
        """Settle an admitted probe that was never offered for execution (0.3.3, C9).

        Only when the execution owner holds no ticket for it, so no model can have started. The row
        becomes a committed denial naming the failed step; its stored admission record stays, so the
        probe cooldown still applies, and a known request reads this reply and never relaunches.
        Settling publishes nothing, so it needs no publication permission, and it works whether or not
        the permit was installed (0.3.3, C9d).
        """
        self._guard()
        execution_owner = getattr(self, '_execution_owner', None)
        if (type(reservation) is not _Reservation or reason not in PROBE_STEP_REASONS or execution_owner is None
                or self._pending.get((reservation.project, reservation.key)) is not reservation):
            raise ProbeAdmissionError('probe_conflict')
        with self._ledger._account_transaction():
            row = self._row(reservation.project, reservation.key)  # Sealed rows only; no callback runs.
            if (row is None or row['state'] != 'admitted' or row['parameters_digest'] != reservation.parameters
                    or row['created_at_ms'] != reservation.created or row['deadline_at_ms'] != reservation.deadline):
                raise ProbeAdmissionError('probe_conflict')
            # Checked last, with nothing reentrant between this and the write.
            if execution_owner.ticketed(reservation):
                raise ProbeAdmissionError('probe_conflict')
            final = self._update(row, state='committed', references_json=canonical_json(
                {'probe': 'denied', 'reason_code': reason, 'records': []}).decode())
            if self._row(reservation.project, reservation.key) != final:
                raise ProbeAdmissionError('probe_conflict')
        permit = self._permits.pop(reservation, None)
        if permit is not None:
            self._execution_inputs.pop(permit, None)
        self._pending.pop((reservation.project, reservation.key), None)
        self._reserved_inputs.pop(reservation, None)
        self._signatures.pop(reservation, None)

    def settle_ended(self, reservation, reason):
        """O0c M10: settle an admitted probe left unknown once no model it launched can still be running.

        As settle_unlaunched, but where a ticket was issued: only when the execution owner proves its
        child gone (`child_ended`: the ticket confirmed cleaned or never started). The row becomes a
        committed denial naming the failed step, so it stops holding the machine's and the account's
        probe capacity and the self-restart; its admission record stays, so the cooldown still applies."""
        self._guard()
        execution_owner = getattr(self, '_execution_owner', None)
        if (type(reservation) is not _Reservation or reason not in PROBE_STEP_REASONS or execution_owner is None
                or self._pending.get((reservation.project, reservation.key)) is not reservation):
            raise ProbeAdmissionError('probe_conflict')
        with self._ledger._account_transaction():
            row = self._row(reservation.project, reservation.key)  # Sealed rows only; no callback runs.
            if (row is None or row['state'] != 'admitted' or row['parameters_digest'] != reservation.parameters
                    or row['created_at_ms'] != reservation.created or row['deadline_at_ms'] != reservation.deadline):
                raise ProbeAdmissionError('probe_conflict')
            # Checked last, with nothing reentrant between this and the write.
            if not execution_owner.child_ended(reservation):
                raise ProbeAdmissionError('probe_conflict')
            final = self._update(row, state='committed', references_json=canonical_json(
                {'probe': 'denied', 'reason_code': reason, 'records': []}).decode())
            if self._row(reservation.project, reservation.key) != final:
                raise ProbeAdmissionError('probe_conflict')
        permit = self._permits.pop(reservation, None)
        if permit is not None:
            self._execution_inputs.pop(permit, None)
        self._pending.pop((reservation.project, reservation.key), None)
        self._reserved_inputs.pop(reservation, None)
        self._signatures.pop(reservation, None)

    def release_committed(self, reservation):
        """Drop the memory of a reservation whose row is already committed (0.3.3, C9d).

        A step after the commit failed before the pending original was released. The durable row is
        final and unchanged; only this owner's memory of it goes.
        """
        self._guard()
        if (type(reservation) is not _Reservation
                or self._pending.get((reservation.project, reservation.key)) is not reservation
                or reservation in self._permits):
            raise ProbeAdmissionError('probe_conflict')
        row = self._row(reservation.project, reservation.key)
        if row is None or row['state'] != 'committed':
            raise ProbeAdmissionError('probe_conflict')
        self._pending.pop((reservation.project, reservation.key))
        self._reserved_inputs.pop(reservation, None)
        self._signatures.pop(reservation, None)

    def _forget_closed(self):
        """Forget the originals the supervisor closed (their rows already committed, by the sweep's expiry),
        with their pooled signature work, so they never fill this owner's memory. Memory goes only after the
        durable commit, so a rolled-back close keeps it. At most MAX_PENDING_PROBES rows read.
        O0b RS-A F6: run when a new reservation arrives. Round 2 G3: also on the owner's step after a committed
        sweep (forget_expired) and on a replay of the closed request. Round 2 G9: an original a live billing job
        still holds is left to that job (noted in _closed_live), which ends it with the stored answer and drops
        its own handle mapping in the same step; known_reply forgets as reserve()'s replay does.
        Round 3: an original's pooled work goes first, and the original and its handle mapping only once that
        has worked, so a pool failure leaves them as the retry target; work the pool cannot release yet (a
        running job, still to confirm its stop) keeps them too. Returns whether nothing was left."""
        jobs = getattr(self, '_billing_jobs', None)
        held = jobs.held_reservations() if jobs is not None else frozenset()
        left = False
        for (project, key), reservation in list(self._pending.items()):
            if reservation in self._permits:
                continue
            row = self._row(project, key)
            if row is None or row['state'] != 'committed':
                continue
            if reservation in held:
                self._closed_live.add(reservation)
                continue
            handle = self._signatures.get(reservation)
            if handle is not None:  # returned work is discarded; queued or running work is cancelled
                if handle in self._pool._returned:
                    self._pool.discard(handle)
                elif self._pool.cancel(handle) != 'released':
                    left = True  # its stop is confirmed later; it is discarded once returned
                    continue
                self._signatures.pop(reservation)
            self._pending.pop((project, key))
            self._reserved_inputs.pop(reservation, None)
        return not left

    def _forget_closed_contained(self):
        """RS-A code round 3 verifier M2: forgetting inside a request (reserve's replay, a new reservation,
        known_reply) must never turn a valid answer into an error. A failure leaves the originals as the
        retry target and sets the notice, so the owner's next step tries again and notes any failure."""
        try:
            if not self._forget_closed():
                self._closed_notice.set()
        except Exception:
            self._closed_notice.set()

    def probes_closed(self):
        """O0b RS-A round 2 G3: told by the ledger after a sweep's transaction has committed expired probes
        (never on rollback). Any thread may call this; only the owner thread touches memory or the pool, here
        at once when the sweep ran on it, else at its next forget_expired() step."""
        self._closed_notice.set()
        if self._pid == os.getpid() and self._pool._thread is threading.current_thread():
            self.forget_expired()

    def forget_expired(self):
        """O0b RS-A round 2 G3: the owner's step, run from each drive with or without a request: after a
        committed sweep, forget the originals it closed and their signature work (see _forget_closed)."""
        self._guard()
        if not self._closed_notice.is_set():
            return
        self._closed_notice.clear()  # cleared first, so a sweep committing meanwhile is seen next step
        try:
            if not self._forget_closed():
                self._closed_notice.set()  # round 3: work still stopping is tried again at the next step
        except BaseException:
            self._closed_notice.set()  # tried again at the next step
            raise

    def known_reply(self, project, runtime, caller, identity, context, *, before_commit):
        """Read a committed original even after a local post-commit fault.

        No pending original is recreated/adopted and no execution is offered.
        Local unknown custody remains charged independently of this read.
        """
        self._guard()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(context) is not ObservationContext):
            raise ProbeAdmissionError('probe_invalid_request')
        snapshot = context.snapshot()
        self._authorize(context, snapshot, before_commit)
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        with self._ledger._account_transaction():
            root = self._project(project, context)
            row = self._row(project, key)
            if row is None or row['state'] != 'committed':
                return None
            parameters = keyed(self._key, b'billing-probe-parameters-v1\0', [runtime, caller, root])
            if row['parameters_digest'] != parameters:
                raise ProbeAdmissionError('probe_conflict')
            reply = self._reply(row, known=True)
            self._authorize(context, snapshot, before_commit)
            if self._project(project, context) != root or self._row(project, key) != row:
                raise ProbeAdmissionError('probe_conflict')
            self._forget_closed_contained()  # O0b RS-A round 2 G9: as reserve()'s replay does
            return reply

    @staticmethod
    def closed_unlaunched(row):
        """O0b RS-A round 2 G9: whether this committed row is the sweep's close of an expired reservation."""
        if row is None or row['state'] != 'committed' or type(row['references_json']) is not str:
            return False
        value = parse_bounded_json_value(row['references_json'].encode(), max_bytes=4096)
        return type(value) is dict and value.get('probe') == 'denied' and value.get(
            'reason_code') == 'probe_unlaunched_expired'

    def _execution(self, permit, *, launching=False):
        """Original owner data only. Native launch authority is checked by its adapter."""
        self._guard()
        if type(permit) is not ProbeAdmission:
            raise ProbeAdmissionError('probe_not_authorized')
        inputs = self._execution_inputs.get(permit)
        if (inputs is None or permit.reservation is not inputs.reservation
                or self._permits.get(inputs.reservation) is not permit
                or (permit.account_fingerprint, permit.admission_record_id, permit.admission_digest,
                    permit.admitted_at_ms) != inputs.permit_fields):
            raise ProbeAdmissionError('probe_not_authorized')
        row = self._original(inputs.reservation)
        if (row['state'] != 'admitted' or (row['account_fingerprint'], row['admission_record_id'],
                row['admission_digest']) != inputs.permit_fields[:3]):
            raise ProbeAdmissionError('probe_conflict')
        admission = parse_bounded_json_value(inputs.admission, max_bytes=MAX_PROBE_REPLY_BYTES)
        actual = self._one('SELECT content,content_digest,source FROM records WHERE project_id=? AND record_id=?',
                          (inputs.reservation.project, permit.admission_record_id))
        if (actual is None or bytes(actual['content']) != inputs.admission or actual['source'] != 'lifecycle'
                or actual['content_digest'] != permit.admission_digest
                or content_digest(admission) != permit.admission_digest):
            raise ProbeAdmissionError('probe_conflict')
        if launching:
            self._fresh(inputs.reservation, inputs.snapshot, appended=admission)
            if time.monotonic_ns() >= inputs.deadline_ns:
                raise ProbeAdmissionError('probe_conflict')
        return inputs, row

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
        completed = type(value) is dict and value.get('probe') == 'completed'
        # CI-4: a completed probe's row also keeps the client version its init frame reported (rows from
        # before it have none).
        if (type(value) is not dict or value.keys() - {'client_version'} != ({'probe', 'reason_code', 'records', 'completion_digest'}
                if completed else {'probe', 'reason_code', 'records'})
                or ('client_version' in value and not (completed and valid_client_version(value['client_version'])))
                or value['probe'] not in {'reused', 'denied', 'completed'} or type(value['records']) is not list
                or (completed and (not matches(value['completion_digest'], _DIGEST)
                    or value['reason_code'] not in {None, *PROBE_COMPLETION_REASONS}))
                or len(value['records']) > 3):
            raise ProbeAdmissionError('probe_conflict')
        now, records = self._now(), []
        for ref in value['records']:
            if type(ref) is not dict or ref.keys() != {'project_id', 'record_id', 'content_digest'}:
                raise ProbeAdmissionError('probe_conflict')
            raw = self._one("SELECT content,content_digest,source,observed_at_ms,expires_at_ms FROM records WHERE project_id=? AND record_id=?",
                (ref['project_id'], ref['record_id']))
            if (raw is None or raw['source'] != 'provider' or raw['content_digest'] != ref['content_digest']
                    or (raw['expires_at_ms'] is not None and now >= raw['expires_at_ms'])
                    or not now-DEFAULT_RETENTION_MS <= raw['observed_at_ms'] <= now):
                raise ProbeAdmissionError()
            record = validate_learning_record(parse_bounded_json_value(bytes(raw['content']), max_bytes=MAX_PROBE_REPLY_BYTES))
            if (record['provider'] != 'claude' or content_digest(record) != ref['content_digest']
                    or record['record_id'] != ref['record_id']):
                raise ProbeAdmissionError('probe_conflict')
            records.append(record)
        reply = {'probe': value['probe'], 'reason_code': value['reason_code'], 'records': records,
            'known': known, 'observed_at_ms': row['created_at_ms']}
        if value.get('client_version') is not None:
            reply['client_version'] = value['client_version']
        if len(canonical_json(reply)) > MAX_PROBE_REPLY_BYTES:
            raise ProbeAdmissionError()
        return reply


def keyed(key, domain, value):
    from .learning_contracts import keyed_digest
    return keyed_digest(key, domain+canonical_json(value))


def close_expired_reserved(ledger, now_ms, limit):
    """O0b RS-A-2: close every `reserved` probe request past its deadline as a committed denial
    (`probe_unlaunched_expired`), inside the caller's account transaction. A reserved request was never
    admitted, so no model could have started for it. Its origin stays; its new state is sealed exactly as
    the probe owner seals it; its register row is released `unlaunched`. Returns how many were closed."""
    if not ledger._connection.in_transaction:
        raise ProbeAdmissionError('probe_conflict')
    key = ledger.state.digest_key()
    rows = [dict(row) for row in ledger._connection.execute(
        "SELECT * FROM account_billing_probe_requests WHERE state='reserved' AND deadline_at_ms<? LIMIT ?",
        (now_ms, limit)).fetchall()]
    names = [n for n in _FIELDS if n not in {'project_id', 'request_key'}]
    for row in rows:
        final = {**{n: row[n] for n in _FIELDS}, 'state': 'committed', 'references_json': canonical_json(
            {'probe': 'denied', 'reason_code': 'probe_unlaunched_expired', 'records': []}).decode()}
        final['state_seal'] = keyed(key, b'billing-probe-state-v1\0', {n: final[n] for n in _FIELDS})
        ledger._connection.execute(
            "UPDATE run_holds SET release_kind='unlaunched' WHERE kind='probe' AND project_id=? AND identity=? "
            "AND state='held'", (row['project_id'], row['request_key']))
        ledger._connection.execute(
            'UPDATE account_billing_probe_requests SET ' + ','.join(n + '=?' for n in (*names, 'state_seal'))
            + " WHERE project_id=? AND request_key=? AND state='reserved'",
            (*(final[n] for n in (*names, 'state_seal')), row['project_id'], row['request_key']))
    return len(rows)
