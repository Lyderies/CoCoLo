"""Original billing-probe admission, before any model worker may be offered.

Only the actual registered signature return is consumed. An admitted request
survives crashes as active/unknown custody; this component cannot release it.
Native authentication, launch containment and confirmed return are separate gates.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import hmac
import os
import threading
import time

from .account_claim_contracts import ClaimError
from .admission_inputs import AdmissionCaptureLimits
from . import admission_metadata
from .admission_metadata import API_KEY_VARIABLES, evaluate_admission
from .account_observation import ObservationContext
from .account_open_admission import open_admission_inputs
from .billing_probe_schema import assert_billing_probe_schema
from .billing_probe_contracts import (PROBE_COMPLETION_REASONS, PROBE_STEP_REASONS, ProbeAdmissionError,
    valid_client_version, valid_probe_retry)
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
# O0c P6: an admitted probe no live original of this process holds (its collector restarted) is closed as a denial
# once its deadline plus this grace has passed; no model it launched can still be running by then.
PROBE_ORPHAN_GRACE_MS = 300_000
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
    # Decision 44: the committed row a repeat of a finished request is judged against. A repeat is a new
    # judgment generation of the same request key (its own capture time and deadline); the row (history) is
    # never overwritten by it.
    history: object = None
    # Decision 47: False when this generation re-judges the request's own commit for the job that made it (its
    # reply is that job's own answer, as the first one was); True for a repeat of a request made earlier.
    known: bool = True


@dataclass(frozen=True, repr=False, eq=False)
class ProbeAdmission:
    """Owner-retained original, not JSON/native authority or a launch capability."""
    reservation: _Reservation
    account_fingerprint: str
    admission_record_id: str
    admission_digest: str
    admitted_at_ms: int


@dataclass(frozen=True, repr=False, eq=False)
class Yes:
    """Decision 50 (BR-19): a sealed yes. It is built only by `AccountBillingAdmission._seal_yes`, which only the
    final gate (`_gate`, `_gate_permit`) calls, after every caller callback and with nothing external after it.
    `kind` is 'reply' (a reused or completed answer) or 'permit' (an admitted probe); `gated_at_ms` is the
    ledger time of the gate."""
    kind: str
    gated_at_ms: int


@dataclass(frozen=True, repr=False, eq=False)
class _Judgment:
    """Decision 47 (H1, M1): what one judgment generation answered, kept so that delivery can be checked against
    it. `row` is the committed row the answer is about (a repeat's unchanged history); `digest` is the digest
    of the reply exactly as it was built; `snapshot` and `guard` are the inputs it judged (None for an answer
    that was only bound afterwards: it cannot show that its inputs are unchanged, so a yes of that kind is
    judged again before it is delivered)."""
    reservation: object
    row: dict
    digest: str
    snapshot: object = None
    guard: object = None
    # Decision 50 (BR-18): the time facts the yes stood on, tested again by the gate: the observation time of
    # the identity judged and, for a reuse, of the probe billing reading it cited.
    identity_at: object = None
    reuse_at: object = None
    # 'reply' for a reuse (its billing reading must be inside the reuse age), 'permit' for the completed run of
    # this very generation (its reading was taken by the run itself, so no reuse age applies).
    kind: str = 'reply'
    # What this generation wrote after its snapshot (a completed run's admission and readings); None for a reuse.
    appended: object = None


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
        # Decision 47: the judgment of a finished generation, until the job that owns it takes it (judgment_of).
        self._judged = {}
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

    def _authorize(self, context, snapshot, callback, keys=True):
        # keys=False (admit only): a provider API key is not refused here but judged by the admission, which
        # refuses it with its own reason (provider_api_key_present) before any provider call.
        self._guard()
        if (type(context) is not ObservationContext or context.snapshot() != snapshot
                or not callable(callback) or callback() is not True):
            raise ProbeAdmissionError('probe_not_authorized')
        self._guard()
        if context.snapshot() != snapshot:
            raise ProbeAdmissionError('probe_not_authorized')
        if keys and any(os.environ.get(n) for n in API_KEY_VARIABLES):
            # BR-10 (decision 44): a provider API key is refused by name at every stage, before signature work.
            raise ProbeAdmissionError('provider_api_key_present')

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

    def reserve(self, project, runtime, caller, identity, context, *, before_commit=None, known=True):
        self._guard()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(context) is not ObservationContext):
            raise ProbeAdmissionError('probe_invalid_request')
        snapshot = context.snapshot()
        # BR-10: a provider API key is refused by name before any signature work; a repeat of a finished
        # request answers it as a denial (below), every other request with the named error.
        self._authorize(context, snapshot, before_commit, False)
        keyed_env = any(os.environ.get(n) for n in API_KEY_VARIABLES)
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        with self._ledger._account_transaction():
            root = self._project(project, context)
            parameters = keyed(self._key, b'billing-probe-parameters-v1\0', [runtime, caller, root])
            row = self._row(project, key)
            repeating = (row is not None and row['parameters_digest'] == parameters and row['state'] == 'committed'
                         and self._rejudges(self._value(row)))
            if keyed_env and not repeating:
                raise ProbeAdmissionError('provider_api_key_present')
            if keyed_env:
                return self._refuse_repeat_key(project, key, root, row, context, snapshot, before_commit)
            if row is not None:
                if row['parameters_digest'] != parameters:
                    raise ProbeAdmissionError('probe_conflict')
                if row['state'] == 'committed':
                    if self._rejudges(self._value(row)):
                        return self._reserve_repeat(project, key, parameters, root, row, context, snapshot,
                                                    before_commit, known)
                    reply = self._reply(row, known=True)  # a refusal stays a refusal (BR-6)
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

    def _refuse_unusable(self, reservation, handle):
        """A repeat whose judged inputs cannot be verified at all is refused as probe_replay_stale, recorded as a
        denial (owner decision 44); the history row stays as it is."""
        with self._ledger._account_transaction():
            row = self._original(reservation, False)
            now = self._now()
            decision = {'provider': 'claude', 'decision': 'deny', 'reason_code': 'probe_replay_stale',
                        'admission_basis': None, 'account_fingerprint': None, 'evidence_digests': [],
                        'observed_at_ms': now}
            self._ledger._store_probe_denial_locked(reservation.project, decision, timestamp=now)  # BR-22
            self._authorize(reservation.context, reservation.snapshot, reservation.authorize, False)
            if self._row(reservation.project, reservation.key) != row:
                raise ProbeAdmissionError('probe_conflict')
        self._pool.discard(handle)
        self._signatures.pop(reservation, None)
        self._pending.pop((reservation.project, reservation.key), None)
        self._reserved_inputs.pop(reservation, None)
        return {'probe': 'denied', 'reason_code': 'probe_replay_stale', 'records': [], 'known': reservation.known,
                'observed_at_ms': now}

    def _refuse_repeat_key(self, project, key, root, row, context, snapshot, before_commit):
        """BR-10: a repeat of a finished request with a provider API key in the environment is a denial,
        recorded as one (owner decision 44); the history row stays as it is and no signature work starts."""
        now = self._now()
        decision = {'provider': 'claude', 'decision': 'deny', 'reason_code': 'provider_api_key_present',
                    'admission_basis': None, 'account_fingerprint': None, 'evidence_digests': [],
                    'observed_at_ms': now}
        self._ledger._store_probe_denial_locked(project, decision, timestamp=now)  # BR-22
        self._authorize(context, snapshot, before_commit, False)
        if self._project(project, context) != root or self._row(project, key) != row:
            raise ProbeAdmissionError('probe_conflict')
        return {'probe': 'denied', 'reason_code': 'provider_api_key_present', 'records': [], 'known': True,
                'observed_at_ms': now}

    @staticmethod
    def _rejudges(value):
        """Decision 44 (BR-1, BR-9): a stored success ('reused', or a completed run without a failure reason) is
        never served from storage; a repeat of it is judged again. A denial and a completed run that carries a
        failure reason stay as stored."""
        return value['probe'] == 'reused' or (value['probe'] == 'completed' and value['reason_code'] is None)

    def _reserve_repeat(self, project, key, parameters, root, row, context, snapshot, before_commit, known=True):
        """A new judgment generation for a request whose row is committed. Runs in reserve's transaction and
        writes nothing: the row is history. At most one live generation per request key; the count is bounded
        by the same pending limits as a new request."""
        existing = self._pending.get((project, key))
        # An original whose row is already committed is only its own local custody left over from a fault after
        # the commit (the row is history); it is not a live generation and does not block a new judgment. Its
        # custody stays charged where it is held and ends with the restart (as known_reply's read never touched it).
        if existing is not None and existing.history is not None:
            if existing.history != row or existing.context is not context or existing.snapshot != snapshot:
                raise ProbeAdmissionError('probe_conflict')
            self._authorize(context, snapshot, before_commit)
            return existing
        self._forget_closed_contained()
        if (len(self._pending) >= MAX_PENDING_PROBES
                or sum(p == project for p, _ in self._pending) >= MAX_PENDING_PROJECT_PROBES):
            raise ProbeAdmissionError('probe_full')
        created_ns = time.monotonic_ns()
        now = self._now()
        original = _Reservation(project, key, parameters, context, snapshot, before_commit,
                                now, now+self._timeout, created_ns+self._timeout*1_000_000, row, known)
        self._authorize(context, snapshot, before_commit)
        if self._project(project, context) != root or self._row(project, key) != row:
            raise ProbeAdmissionError('probe_conflict')
        self._pending[(project, key)] = original
        self._reserved_inputs[original] = (self._reservation_fields(original), context, snapshot, before_commit)
        return original

    @staticmethod
    def _reservation_fields(reservation):
        return (reservation.project, reservation.key, reservation.parameters, reservation.created,
                reservation.deadline, reservation.deadline_ns)

    def _original(self, reservation, keys=True):
        self._guard()
        if (type(reservation) is not _Reservation
                or self._pending.get((reservation.project, reservation.key)) is not reservation):
            raise ProbeAdmissionError('probe_not_authorized')
        captured = self._reserved_inputs.get(reservation)
        if (captured is None or self._reservation_fields(reservation) != captured[0]
                or reservation.context is not captured[1] or reservation.snapshot != captured[2]
                or reservation.authorize is not captured[3]):
            raise ProbeAdmissionError('probe_not_authorized')
        self._authorize(reservation.context, reservation.snapshot, reservation.authorize, keys)
        self._project(reservation.project, reservation.context)
        row = self._row(reservation.project, reservation.key)
        if reservation.history is not None:  # a repeat: judged against its unchanged committed row
            if row is None or row != reservation.history or row['parameters_digest'] != reservation.parameters:
                raise ProbeAdmissionError('probe_conflict')
            return row
        if (row is None or row['parameters_digest'] != reservation.parameters
                or row['created_at_ms'] != reservation.created or row['deadline_at_ms'] != reservation.deadline):
            raise ProbeAdmissionError('probe_conflict')
        return row

    def prepare(self, reservation):
        row = self._original(reservation)
        if row['state'] != ('committed' if reservation.history is not None else 'reserved'):
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
        if row is None or row['state'] != ('committed' if reservation.history is not None else 'reserved'):
            raise ProbeAdmissionError('probe_conflict')
        self._pending.pop((reservation.project, reservation.key))
        self._reserved_inputs.pop(reservation)

    def _capture(self, project, anchor_ms=None):
        """The admission inputs at the ledger clock (or, with `anchor_ms`, over that capture's own scope)."""
        limits = self._intake._limits
        signer = self._ledger._current_signer_bytes()
        policy = self._intake._views._claim_policy('claude')
        return self._read(lambda: self._intake._views._capture_admission_rows_locked(project,
            'claude', policy, AdmissionCaptureLimits(limits.rows_per_job, limits.bytes_per_job), signer, anchor_ms))

    def _fresh(self, reservation, snapshot, *, appended=None):
        """`appended`: the record (or records) this generation itself wrote after its snapshot: its admission record
        and, for a completed run, the run's own readings. Each must be in the ledger exactly as written."""
        current = self._capture(reservation.project, snapshot.anchor_ms)
        expected = snapshot
        for record in () if appended is None else (appended if type(appended) is list else [appended]):
            rows = [r for r in current.rows if r[1] == reservation.project and r[2] == record['record_id']]
            originals = [r for r in snapshot.rows if r[1] == reservation.project and r[2] == record['record_id']]
            source = 'lifecycle' if record['record_type'] == 'probe_admission' else 'provider'
            if (len(rows) != 1 or rows[0][4] != record['record_type'] or rows[0][5] != canonical_json(record)
                    or rows[0][6] != content_digest(record) or rows[0][7] != source
                    or (originals and originals != rows)):
                raise ProbeAdmissionError('probe_conflict')
            if not originals:
                expected = replace(expected, rows=tuple(sorted((*expected.rows, rows[0]), key=lambda r: r[0])))
        if (not same_admission_inputs(expected, current)
                or not reservation.created <= current.captured_at_ms < reservation.deadline):
            raise ProbeAdmissionError('probe_conflict')
        return current.captured_at_ms

    @staticmethod
    def _newer_run_restricts(latest, billing, headroom, fingerprint, now, order):
        """R2 / BR-2: shared with the ledger's own reuse path (decision 50, BR-21); see admission_metadata."""
        return admission_metadata.newer_run_restricts(latest, billing, headroom, fingerprint, now, order)

    def admit(self, reservation):
        try:
            return self._admit(reservation)
        except BaseException:
            if type(reservation) is _Reservation and reservation.history is not None:
                self._abandon_repeat(reservation)
            raise

    def _abandon_repeat(self, reservation):
        """BR-12: a failed repeat judgment leaves nothing behind: its generation ends here (its returned signature
        work is discarded) so the next repeat starts a fresh one. Work still running is left to the job layer and
        to the generation's own deadline. Never raises."""
        try:
            if self._pending.get((reservation.project, reservation.key)) is not reservation:
                return
            handle = self._signatures.get(reservation)
            if handle is not None:
                if handle not in self._pool._returned:
                    return
                self._pool.discard(handle)
                self._signatures.pop(reservation, None)
            self._pending.pop((reservation.project, reservation.key), None)
            self._reserved_inputs.pop(reservation, None)
        except Exception:
            pass

    def _admit(self, reservation):
        row = self._original(reservation, False)
        repeat = reservation.history is not None
        if row['state'] == 'admitted':
            self._original(reservation)  # a repeat admit() re-checks the API key; callbacks end here
            original = self._permits.get(reservation)
            inputs = None if original is None else self._execution_inputs.get(original)
            if inputs is None:
                raise ProbeAdmissionError()
            # Decision 50 (BR-19): the stored permit is a yes again only through the gate, against the inputs
            # it was judged on; it is never handed back from memory.
            with ExitStack() as turn, self._ledger._account_transaction():
                self._gate_permit(original, inputs, parse_bounded_json_value(
                    inputs.admission, max_bytes=MAX_PROBE_REPLY_BYTES), stack=turn)
            return original
        handle = self._signatures.get(reservation)
        if handle is None:
            raise ProbeAdmissionError('probe_not_ready')
        result = self._pool.result(handle)
        if repeat and result.reason == 'signature_execution_invalid_output':
            # BR-3: the records a repeat must judge are not usable evidence (the worker refuses them, e.g. a
            # reading stamped in the future): a new attempt is needed.
            return self._refuse_unusable(reservation, handle)
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
        repeat_reply = None
        # Decision 47 (M3): the signer turn is taken last inside the transaction (ledger lock, then turn) and held
        # until the transaction has committed.
        with ExitStack() as signer_turn, self._ledger._account_transaction():
            row = self._original(reservation, False)
            if row['state'] != ('committed' if repeat else 'reserved'):
                raise ProbeAdmissionError('probe_conflict')
            now = self._fresh(reservation, snapshot)
            identities = prepared['identities']
            # BR-3: an identity stamped after now (a clock stepped back) is no evidence of now: only this
            # judgment's own window counts.
            # Decision 47 (M-identity): the job takes an identity observation for every generation, a repeat's too
            # (the local client's login read: no model call, nothing spent), so the reading judged is this
            # generation's own. Its age is measured against this judgment's time, not the reservation's: a reading
            # older than the reuse age at judgment time is no evidence; with none, the repeat is refused below as
            # probe_replay_stale.
            since = now-PROBE_REUSE_MS if repeat else reservation.created
            identity = max((r for r, _ in identities if since <= r['observed_at_ms'] <= now),
                           key=lambda r: r['observed_at_ms'], default=None)
            if identity is None and not repeat:
                raise ProbeAdmissionError()
            execution_owner = getattr(self, '_execution_owner', None)
            if (execution_owner is not None and not repeat
                    and execution_owner._prepared_identity(reservation) != canonical_json(identity)):
                raise ProbeAdmissionError('probe_conflict')
            fingerprint = None if identity is None else identity['account_fingerprint']
            # A probe's own records carry no run; a finished child's records (run_id, phase 'during') are not
            # probe evidence and the reply contract rejects them. Reuse takes the probe's records only, and a
            # newer run record that is more restrictive stops reuse: admission is re-evaluated and the probe
            # launches only if admitted (otherwise a named denial).
            mine = [(r, d) for r, d in prepared['billing'] if identity is not None and r['account_fingerprint'] == fingerprint]
            latest = max((r for r, _ in mine if r.get('run_id') is None and r.get('phase') is None),
                         key=lambda r: r['observed_at_ms'], default=None)
            if latest is not None and (not 0 <= now-latest['observed_at_ms'] < PROBE_REUSE_MS
                    or self._newer_run_restricts(latest, mine, prepared['headroom'], fingerprint, now,
                                              {r.raw[2]: r.raw[0] for r in result.prepared.rows})):
                latest = None
            judged = None
            yes_kind = None  # decision 50: 'reply' / 'permit' when this judgment ends in a yes (gated below)
            history = self._value(row) if repeat else None
            if latest is not None:
                # B1: a reuse skips only the provider call, never the judgment. Paid usage, an API key, the client
                # version and the attestation are judged again on the records at hand; a refusal is the answer.
                judged = evaluate_admission(provider='claude', identities=prepared['identities'],
                    attestations=prepared['attestations'], billing=prepared['billing'], headroom=prepared['headroom'],
                    window_headroom=prepared['headroom'], prior_claude_admissions=prepared['prior_claude_admissions'],
                    environ=guard, now_ms=now, admission_policy=prepared['admission_policy'],
                    cited_digests=prepared['cited_digests'], probe=True,
                    prior_run_admissions=[(f, t) for f, t in prepared['prior_admissions'] if t >= now-18_000_000])
            selected = None
            if latest is not None and judged['decision'] == 'pass':
                # BR-11: the judged inputs are all kept in `prepared`; the reply cites at most these three.
                head = admission_metadata.probe_head(prepared['headroom'], fingerprint, latest)
                selected = [identity, *([] if head is None else [head]), latest]
                if repeat and history['probe'] == 'completed' and len(selected) != 3:
                    selected = None  # a completed run's reply needs all three records; none fresh enough
            if selected is not None and repeat:
                # A repeat keeps the history row as it is; its fresh judgment is the answer, built from the
                # records it judged, at its own time (decision 44, BR-1, BR-9).
                # The reply of an unchanged request stays the same: a judged identity of the same account that the
                # history already cited is cited again (all judged inputs are kept in `prepared`).
                cited = {ref['record_id'] for ref in history['records']}
                same = [r for r, _ in identities if r['record_id'] in cited and r['account_fingerprint'] == fingerprint
                        and reservation.created - PROBE_REUSE_MS <= r['observed_at_ms'] <= now]
                if same:
                    selected = [same[0], *selected[1:]]
                value = {'probe': history['probe'], 'reason_code': None,
                         'records': self._refs(selected, result.prepared.rows)}
                if history.get('client_version') is not None:
                    value['client_version'] = history['client_version']
                try:
                    # L1: `observed_at_ms` of every reply is the time of the request's origin (its row's created_at_ms),
                    # also on a repeat's fresh yes; the time of the judgment is `now`, which freshness above used.
                    repeat_reply = self._build_reply(value, known=reservation.known, observed_at=row['created_at_ms'],
                                                     gone='probe_replay_stale')
                    final, appended, yes_kind = row, None, 'reply'
                except ProbeAdmissionError as error:
                    if error.reason_code != 'probe_replay_stale':
                        raise
                    selected = None  # a judged record is gone, expired or out of retention: a new probe is needed
            elif selected is not None:
                final = self._commit_reply(row, 'reused', None, selected, result.prepared.rows)
                appended, yes_kind = None, 'reply'
            if selected is None:
                cooldown = max((r.record['observed_at_ms'] for r in result.prepared.rows
                    if r.record['record_type'] == 'probe_admission'
                    and r.record['provider'] == 'claude' and r.record['decision'] == 'pass'
                    and r.record['account_fingerprint'] == fingerprint and r.record['observed_at_ms'] > now-PROBE_COOLDOWN_MS
                    ), default=None)
                # Owner decision 40 (a): the real problem is reported first; a cooldown names itself only when
                # nothing else refuses. billing_unverified is the gap the cooling probe itself fills (its bootstrap
                # is spent), so it is not a real problem and the cooldown names itself.
                decision = judged if judged is not None else evaluate_admission(provider='claude',
                    identities=prepared['identities'],
                    attestations=prepared['attestations'], billing=prepared['billing'], headroom=prepared['headroom'],
                    window_headroom=prepared['headroom'], prior_claude_admissions=prepared['prior_claude_admissions'],
                    environ=guard, now_ms=now, admission_policy=prepared['admission_policy'],
                    cited_digests=prepared['cited_digests'], probe=True,
                    prior_run_admissions=[(f, t) for f, t in prepared['prior_admissions'] if t >= now-18_000_000])
                if repeat and (identity is None or decision['decision'] == 'pass'
                               or decision['reason_code'] == 'billing_unverified'):
                    # BR-13: passing would need a new provider call, which a repeat never makes: it is refused by
                    # name and the client asks again with a new request id.
                    decision = {'provider': 'claude', 'decision': 'deny', 'reason_code': 'probe_replay_stale',
                        'admission_basis': None, 'account_fingerprint': fingerprint, 'evidence_digests': [],
                        'observed_at_ms': now}
                elif cooldown is not None and (decision['decision'] == 'pass' or decision['reason_code'] == 'billing_unverified'):
                    decision = {'provider': 'claude', 'decision': 'deny', 'reason_code': 'probe_cooldown',
                        'admission_basis': None, 'account_fingerprint': fingerprint, 'evidence_digests': [],
                        'observed_at_ms': now}
                if decision['decision'] == 'pass':  # never for a repeat (it is refused above)
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
                if decision['decision'] == 'pass':
                    material = {'schema_version': 1, 'record_type': 'probe_admission', **decision}
                    stored = appended = validate_learning_record(
                        {**material, 'record_id': 'probe_admission:'+content_digest(material)})
                    self._ledger._store_probe_admission_locked(reservation.project, decision, timestamp=now)
                else:
                    # BR-22: one denial record per reason and account per window; a refusal asked again inside the
                    # window is answered from the record that stands for it and writes none.
                    stored, is_new = self._ledger._store_probe_denial_locked(reservation.project, decision,
                                                                             timestamp=now)
                    appended = stored if is_new else None
                if repeat:
                    # Recorded as a denial (owner decision 44); the history row stays as it is.
                    repeat_reply = {'probe': 'denied', 'reason_code': decision['reason_code'], 'records': [],
                                    'known': reservation.known, 'observed_at_ms': now}
                    final = row
                else:
                    row = {**row, 'account_fingerprint': fingerprint,
                        'admission_record_id': stored['record_id'], 'admission_digest': content_digest(stored)}
                    if decision['decision'] == 'pass':
                        final, yes_kind = self._update(row, state='admitted'), 'permit'
                    else:
                        final = self._commit_reply(row, 'denied', decision['reason_code'], [], result.prepared.rows,
                            retry_at=admission_metadata.probe_retry_at(cooldown)
                                if decision['reason_code'] == 'probe_cooldown' else None)
            # Decision 50 (BR-16, BR-17): every caller callback of this judgment has run when this one returns; the
            # gate follows it in the same transaction and builds the yes. Nothing the caller supplies runs after it.
            self._authorize(reservation.context, reservation.snapshot, reservation.authorize, False)
            if (execution_owner is not None and not repeat
                    and execution_owner._prepared_identity(reservation) != canonical_json(identity)):
                raise ProbeAdmissionError('probe_conflict')
            identity_at = None if identity is None else identity['observed_at_ms']
            reuse_at = None if latest is None or yes_kind != 'reply' else latest['observed_at_ms']
            yes = self._gate(reservation, snapshot, guard, final, kind=yes_kind, identity_at=identity_at,
                             reuse_at=reuse_at, appended=appended, stack=signer_turn)
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
            if self._row(reservation.project, reservation.key) != final:  # no callback after the gate (BR-17)
                raise ProbeAdmissionError('probe_conflict')
            return original
        self._pending.pop((reservation.project, reservation.key))
        self._reserved_inputs.pop(reservation)
        reply = repeat_reply if repeat else self._reply(final, known=False)
        # Decision 47 (H3), 50 (BR-17): no authorization callback after the gate; delivery asks the caller again
        # and the publication gate checks what that changed.
        if self._row(reservation.project, reservation.key) != final:
            raise ProbeAdmissionError('probe_conflict')
        self._judged[reservation] = _Judgment(reservation, final, content_digest(reply), snapshot, guard,
                                              identity_at, reuse_at)
        return reply

    def _seal_yes(self, kind, now):
        """The one place a yes is built (decision 50, BR-19); only the gate calls it."""
        return Yes(kind, now)

    def _gate(self, reservation, snapshot, guard, row, *, kind, identity_at, reuse_at, appended, stack):
        """Decision 50, the final gate: the only code that builds a yes (`kind` 'reply' or 'permit'); with `kind`
        None the same comparison runs for an answer that is not a yes (a refusal), which builds nothing.

        Runs inside the caller's account transaction, after every caller callback, and runs none itself. It takes
        the owner-signer turn first (BR-15: ledger lock, then turn, held to the commit), then compares everything
        the judgment used with the current state: the rows, policy, signer and owner generation of the judgment's
        own snapshot (not a capture taken later), the API-key environment, the committed row, and, for a yes,
        freshness at the gate's own time: the identity reading's window and the reuse age of the billing reading
        it stands on (BR-18), with the deadline. Any difference raises probe_conflict."""
        self._guard()
        stack.enter_context(self._signer_stable())
        self._project(reservation.project, reservation.context)
        now = self._fresh(reservation, snapshot, appended=appended)  # inputs vs the snapshot, and the deadline
        if {n: 'present' for n in API_KEY_VARIABLES if os.environ.get(n)} != guard:
            raise ProbeAdmissionError('probe_conflict')
        if self._row(reservation.project, reservation.key) != row:
            raise ProbeAdmissionError('probe_conflict')
        if kind is not None:
            repeat = reservation.history is not None
            refusal = admission_metadata.reuse_gate(
                environ=os.environ, process_environ=os.environ, identity_at=identity_at, reuse_at=reuse_at, now_ms=now,
                since_ms=now-PROBE_REUSE_MS if repeat else reservation.created,
                reuse_ms=PROBE_REUSE_MS if kind == 'reply' else None)
            if refusal is not None:
                raise ProbeAdmissionError('provider_api_key_present' if refusal == 'provider_api_key_present'
                                          else 'probe_conflict')
        self._inputs_unchanged(snapshot)  # BR-15: the signer and the policy, under the turn
        return None if kind is None else self._seal_yes(kind, now)

    def _gate_permit(self, permit, inputs, admission, *, stack):
        """The final gate of a stored permit (a repeat admit, a launch): the same comparison against the inputs the
        permit was judged on, with its own admission record, as `_gate` makes. Runs after the caller callbacks of
        its path and none of its own."""
        self._guard()
        stack.enter_context(self._signer_stable())
        reservation = inputs.reservation
        self._project(reservation.project, reservation.context)
        now = self._fresh(reservation, inputs.snapshot, appended=admission)
        if any(os.environ.get(n) for n in API_KEY_VARIABLES):
            raise ProbeAdmissionError('provider_api_key_present')
        row = self._row(reservation.project, reservation.key)
        if (row is None or row['state'] != 'admitted' or (row['account_fingerprint'], row['admission_record_id'],
                row['admission_digest']) != inputs.permit_fields[:3]):
            raise ProbeAdmissionError('probe_conflict')
        identity = parse_bounded_json_value(inputs.identity, max_bytes=MAX_PROBE_REPLY_BYTES)
        refusal = admission_metadata.reuse_gate(
            environ=os.environ, process_environ=os.environ, identity_at=identity['observed_at_ms'], reuse_at=None,
            now_ms=now, since_ms=reservation.created, reuse_ms=None)
        if refusal is not None:
            raise ProbeAdmissionError('provider_api_key_present' if refusal == 'provider_api_key_present'
                                      else 'probe_conflict')
        self._inputs_unchanged(inputs.snapshot)
        return self._seal_yes('permit', now)

    def record_completed(self, inputs, final, reply, identity, records):
        """The judgment of a run this generation completed (called by the execution commit, inside its ledger
        transaction): the inputs the run was admitted on, plus what the run itself wrote (its admission record and
        its readings), so that delivery can show them unchanged instead of judging the fresh run again. A paid
        reading or any other change after this makes the gate fail and the answer is judged again."""
        reservation = inputs.reservation
        self._judged[reservation] = _Judgment(
            reservation, final, content_digest(reply), inputs.snapshot, {}, identity['observed_at_ms'], None,
            'permit', [parse_bounded_json_value(inputs.admission, max_bytes=MAX_PROBE_REPLY_BYTES), *records])

    def judgment_of(self, reservation):
        """The judgment of a finished generation (None when there is none); handed over once (decision 47)."""
        self._guard()
        return self._judged.pop(reservation, None)

    def bind(self, project, runtime, caller, identity, reply):
        """Decision 47 (M1): the exact committed row and reply digest of an answer a judgment did not make (a
        completed run's commit, a stored refusal). It binds delivery to that row and reply, but names no inputs."""
        self._guard()
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        row = self._row(project, key)
        if row is None or row['state'] != 'committed':
            raise ProbeAdmissionError('probe_conflict')
        return _Judgment(None, row, content_digest(reply))

    def gate_current(self, judgment):
        """Decision 50: whether a judgment's yes would still pass the final gate now (the check at take, which
        decides whether a ready yes is judged again). It is the gate itself: every input the judgment used against
        its own snapshot, plus freshness. Runs no callback and never raises for a changed input; a judgment that
        named no inputs never passes."""
        self._guard()
        reservation = judgment.reservation
        if reservation is None or judgment.snapshot is None:
            return False
        try:
            with ExitStack() as turn, self._ledger._account_transaction():
                self._gate(reservation, judgment.snapshot, judgment.guard, judgment.row, kind=judgment.kind,
                           identity_at=judgment.identity_at, reuse_at=judgment.reuse_at,
                           appended=judgment.appended, stack=turn)
            return True
        except ProbeAdmissionError:
            return False

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
            if reservation.history is not None and time.monotonic_ns() < reservation.deadline_ns:
                continue  # a live repeat generation: its row was committed before it began; it ends at its deadline
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

    def committed(self, project, runtime, caller, identity, context):
        """Whether this request's row is already committed (read-only; no judgment, no callback, no reply)."""
        self._guard()
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        with self._ledger._account_transaction():
            row = self._row(project, key)
            return row is not None and row['state'] == 'committed'

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
            value = self._value(row)
            # I1 (decision 44): a stored success is never served; the caller judges it again.
            reply = None if self._rejudges(value) else self._reply(row, known=True)
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
            # Decision 50 (BR-19): a launch is a yes again; the gate follows the callbacks of `_original` above.
            with ExitStack() as turn, self._ledger._account_transaction():
                self._gate_permit(permit, inputs, admission, stack=turn)
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

    def _commit_reply(self, row, probe, reason, selected, parsed_rows, *, retry_at=None):
        refs = self._refs(selected, parsed_rows)
        raw = canonical_json({'probe': probe, 'reason_code': reason, 'records': refs,
            **({'retry_at': retry_at} if retry_at is not None else {})})
        if len(raw) > 4096:
            raise ProbeAdmissionError()
        return self._update(row, state='committed', references_json=raw.decode())

    @staticmethod
    def _value(row):
        """The stored answer of a committed row, checked for shape (a malformed one is probe_conflict)."""
        if type(row['references_json']) is not str:
            raise ProbeAdmissionError('probe_conflict')
        value = parse_bounded_json_value(row['references_json'].encode(), max_bytes=4096)
        completed = type(value) is dict and value.get('probe') == 'completed'
        # CI-4: a completed probe's row also keeps the client version its init frame reported (rows from
        # before it have none).
        if (type(value) is not dict or value.keys() - {'client_version', 'retry_at'} != ({'probe', 'reason_code', 'records', 'completion_digest'}
                if completed else {'probe', 'reason_code', 'records'})
                or ('retry_at' in value and (value.get('reason_code') != 'probe_cooldown'
                    or not valid_probe_retry(value['retry_at'])))
                or ('client_version' in value and not (completed and valid_client_version(value['client_version'])))
                or value['probe'] not in {'reused', 'denied', 'completed'} or type(value['records']) is not list
                or (completed and (not matches(value['completion_digest'], _DIGEST)
                    or value['reason_code'] not in {None, *PROBE_COMPLETION_REASONS}))
                or len(value['records']) > 3):
            raise ProbeAdmissionError('probe_conflict')
        return value

    def _reply(self, row, *, known):
        return self._build_reply(self._value(row), known=known, observed_at=row['created_at_ms'])

    def _build_reply(self, value, *, known, observed_at, gone=None):
        from .learning_store import DEFAULT_RETENTION_MS
        now, records = self._now(), []
        for ref in value['records']:
            if type(ref) is not dict or ref.keys() != {'project_id', 'record_id', 'content_digest'}:
                raise ProbeAdmissionError('probe_conflict')
            raw = self._one("SELECT content,content_digest,source,observed_at_ms,expires_at_ms FROM records WHERE project_id=? AND record_id=?",
                (ref['project_id'], ref['record_id']))
            if (raw is None or raw['source'] != 'provider' or raw['content_digest'] != ref['content_digest']
                    or (raw['expires_at_ms'] is not None and now >= raw['expires_at_ms'])
                    or not now-DEFAULT_RETENTION_MS <= raw['observed_at_ms'] <= now):
                raise ProbeAdmissionError(gone)
            record = validate_learning_record(parse_bounded_json_value(bytes(raw['content']), max_bytes=MAX_PROBE_REPLY_BYTES))
            if (record['provider'] != 'claude' or content_digest(record) != ref['content_digest']
                    or record['record_id'] != ref['record_id']):
                raise ProbeAdmissionError('probe_conflict')
            records.append(record)
        reply = {'probe': value['probe'], 'reason_code': value['reason_code'], 'records': records,
            'known': known, 'observed_at_ms': observed_at}
        if 'retry_at' in value:
            reply['retry_at'] = value['retry_at']
        if value.get('client_version') is not None:
            reply['client_version'] = value['client_version']
        if len(canonical_json(reply)) > MAX_PROBE_REPLY_BYTES:
            raise ProbeAdmissionError()
        return reply

    def verify_committed(self, project, runtime, caller, identity, context, *, before_commit, reply, history=None,
                         judgment=None, gate=True):
        """BR-8 (decision 44): publication checks the exact row the job committed, for integrity: its seal, its
        request binding and the caller's authorization. A repeat's judgment committed nothing: its row must be the
        unchanged history it was judged against. Never a reason for a false conflict when unrelated evidence
        arrived. Raises probe_conflict on any difference.

        Decision 47: with the job's `judgment`, the committed row must be exactly the one that generation answered
        about and the reply exactly the one it built (M1).

        Decision 50: a yes (a reused or completed answer) is also gated: the caller's callback runs first (BR-16, it
        is untrusted), then the one final gate compares everything the judgment used with the current state, against
        the judgment's own snapshot, and tests freshness again. It returns a sealed `Yes`; a yes with no judgment to
        gate it against is refused. `gate=False` (the check at take, which hands the answer on for publication)
        checks integrity only and builds no yes."""
        self._guard()
        if (not matches(project, _PROJECT) or not matches(runtime, _DIGEST) or not matches(caller, _DIGEST)
                or not matches(identity, _ID) or type(context) is not ObservationContext or type(reply) is not dict):
            raise ProbeAdmissionError('probe_invalid_request')
        snapshot = context.snapshot()
        self._authorize(context, snapshot, before_commit)
        key = keyed(self._key, b'billing-probe-request-v1\0', [project, runtime, caller, identity])
        positive = gate and self._rejudges(reply)
        with ExitStack() as turn, self._ledger._account_transaction():
            root = self._project(project, context)
            row = self._row(project, key)
            if (row is None or row['state'] != 'committed'
                    or row['parameters_digest'] != keyed(self._key, b'billing-probe-parameters-v1\0', [runtime, caller, root])):
                raise ProbeAdmissionError('probe_conflict')
            if history is not None and row != history:
                raise ProbeAdmissionError('probe_conflict')
            if judgment is not None:
                if row != judgment.row or content_digest(reply) != judgment.digest:
                    raise ProbeAdmissionError('probe_conflict')
            elif history is None and self._reply(row, known=True) != {**reply, 'known': True}:
                # A refusal answered over a stored success is a repeat's own (it grants nothing, so it is
                # safe to publish whatever the stored success cites).
                if not (reply['probe'] == 'denied' and reply.get('records') == [] and reply.get('known') is True
                        and self._rejudges(self._value(row))):
                    raise ProbeAdmissionError('probe_conflict')
            if self._project(project, context) != root or self._row(project, key) != row:
                raise ProbeAdmissionError('probe_conflict')
            if not positive:
                return True
            if judgment is None or judgment.reservation is None or judgment.snapshot is None:
                raise ProbeAdmissionError('probe_conflict')  # no judgment to gate the yes against
            return self._gate(judgment.reservation, judgment.snapshot, judgment.guard, judgment.row, kind=judgment.kind,
                              identity_at=judgment.identity_at, reuse_at=judgment.reuse_at,
                              appended=judgment.appended, stack=turn)

    @contextmanager
    def _signer_stable(self):
        """BR-15: the owner signer file is written outside the ledger transaction by another process (first
        setup) and under the ledger lock plus this turn by a replacement. Holding the turn while the last
        recheck runs keeps the signer from changing under it; a busy turn fails closed."""
        from .collector_paths import StateError
        try:
            with self._ledger.state._owner_key_turn():
                yield
        except StateError:
            raise ProbeAdmissionError('probe_unavailable') from None

    def _inputs_unchanged(self, snapshot):
        """BR-15: immediately before the commit, the signer and the policy are the ones the judgment used."""
        if (self._ledger._current_signer_bytes() != snapshot.signer
                or self._intake._views._claim_policy('claude') != snapshot.policy):
            raise ProbeAdmissionError('probe_conflict')

    def _refs(self, selected, parsed_rows):
        refs = []
        for record in selected:
            actual = next((r.raw for r in parsed_rows if r.raw[2] == record['record_id']
                and r.raw[6] == content_digest(record) and r.raw[7] == 'provider'), None)
            if actual is None:
                raise ProbeAdmissionError()
            refs.append({'project_id': actual[1], 'record_id': actual[2], 'content_digest': actual[6]})
        return refs


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


def close_orphaned_admitted(ledger, now_ms, limit):
    """O0c P6: close every `admitted` probe request whose deadline plus PROBE_ORPHAN_GRACE_MS has passed and
    that no original in this process holds (a collector restart lost it: nothing could ever settle it), as a
    committed denial (`probe_admitted_expired`) inside the caller's account transaction. Its admission record
    stays, so the cooldown still applies, and a replay reads this reply and never relaunches. An original this
    process holds is never closed by time: only its own proof of an ended child settles it (M10). Returns how
    many were closed."""
    if not ledger._connection.in_transaction:
        raise ProbeAdmissionError('probe_conflict')
    owner = getattr(ledger, '_billing_admission_owner', None)
    held = set(getattr(owner, '_pending', ())) if owner is not None else set()
    key = ledger.state.digest_key()
    rows = [dict(row) for row in ledger._connection.execute(
        "SELECT * FROM account_billing_probe_requests WHERE state='admitted' AND deadline_at_ms<? LIMIT ?",
        (now_ms - PROBE_ORPHAN_GRACE_MS, limit + len(held))).fetchall()]
    names = [n for n in _FIELDS if n not in {'project_id', 'request_key'}]
    closed = 0
    for row in rows:
        if closed >= limit:
            break
        if (row['project_id'], row['request_key']) in held:
            continue
        final = {**{n: row[n] for n in _FIELDS}, 'state': 'committed', 'references_json': canonical_json(
            {'probe': 'denied', 'reason_code': 'probe_admitted_expired', 'records': []}).decode()}
        final['state_seal'] = keyed(key, b'billing-probe-state-v1\0', {n: final[n] for n in _FIELDS})
        ledger._connection.execute(
            "UPDATE run_holds SET release_kind='expired' WHERE kind='probe' AND project_id=? AND identity=? "
            "AND state='held'", (row['project_id'], row['request_key']))
        ledger._connection.execute(
            'UPDATE account_billing_probe_requests SET ' + ','.join(n + '=?' for n in (*names, 'state_seal'))
            + " WHERE project_id=? AND request_key=? AND state='admitted'",
            (*(final[n] for n in (*names, 'state_seal')), row['project_id'], row['request_key']))
        closed += 1
    return closed
