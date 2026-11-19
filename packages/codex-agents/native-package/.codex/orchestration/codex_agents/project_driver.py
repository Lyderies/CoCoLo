"""Per-run custody for one project host: from an admitted open to a released slot.

The checked CLI registers each admitted bridged run with its project host and,
at dispatch, asks the host to prepare it. The driver then runs the original
sequence the process fixtures exercised: committed-export preparation, a
before-launch provider observation, the result lease, the account's open
provenance, the launch claim, registration, run provenance and grant
consumption. Only then can `dispatch.submit` name the run. After the result is
published it submits the terminal, acknowledges it, prepares cleanup custody,
asks the account to release the capacity and retires local custody.

Every step runs on the host's owner thread, except account calls, which run on
one helper thread and are collected on the next step. A failed step stops that
run where it is: nothing is relaunched, and an uncertain account reply leaves
the capacity charged. Only a refused claim fence is asked again, for a bounded
number of tries (`_fence_later`). A run that provably can never launch closes
by itself: a claim the account denied is closed through the account's denial
closure; any other armed run without an admitted claim is fenced by the account
(`account.fence_claim`), which either returns the admitted claim (and closes
nothing) or makes sure it never will be; and a lease never armed is released by
the history. Its input and preparation are then given back.

A run whose service stopped or crashed while it held an admitted claim is
settled by the next service of the project, by itself: each admitted claim
has a durable entry in the project's launch journal (`launch_journal`) that
records whether the run ever spawned, the child's and its watchdog's pids and
birth times. Once the earlier service instance has provably stopped and the
kernel shows nothing of that run still running, this driver asks the account
to settle it (`account.settle_lost_run`): it ends abandoned, with an unknown
outcome, and no longer counts against the limits. A service that stops
gracefully settles its own runs this way after its threads have stopped.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from threading import Lock, local
import time

from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountClient, AccountError, AccountRequest, account_request_digest
from .account_claim_contracts import CLAIM_DENIAL_CODES, LaunchClaimInput
from .run_failure_reasons import FAILED_BEFORE_OPEN_REASONS
from .claim_denial import CloseDeniedInput, FenceClaimInput
from .preparation_reasons import PREPARATION_STAGE_REASONS
from .codex_preparation import (
    MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES, PreparationContext, PreparationError, PreparationLimits,
    preparation_payload,
)
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .control_api import ControlScope
from .dispatch_api import dispatch_request_digest
from .execution_grant import ExecutionRegistrationInput, checked_execution_grant
from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, matches
from .host_open_contracts import OpenProvenanceInput
from .hosts import HOST_CLIENTS
from .launch_journal import JournalError, LaunchJournal, tree_state
from .learning_contracts import canonical_json, content_digest, parse_bounded_json
from .lost_run_contracts import SettleLostInput
from .open_scope_contracts import OpenScopeContext, open_inputs_digest
from .orchestration_api import OrchestrationScope
from .prepared_dispatch import DispatchAuthorization, PreparedDispatchContext
from .rate_limit_contracts import RateLimitError, bounded_rate_limit_info, merged_restriction
from .result_contracts import LEASE_SCOPE_FIELDS, ResultLeaseBinding
from .run_provenance import RunProvenanceInput
from .service_enrollment import (
    account_endpoint_path, enrollment_preamble, proof_directory, read_capability, withdraw_proof,
)
from .service_transport import install_enrollment
from .terminal_delivery import TerminalSubmissionInput

MAX_RUNS = 16
MAX_INTAKE = 32  # Driver connections whose one line has not yet arrived.
INTAKE_SECONDS = 2.0  # Absolute time for that line.
DISPATCH_SLOTS = 64  # Lifetime dispatch slots of one host, the dispatch service's maximum.
# Local custody of a released run waits for its primary to record the result,
# or this long; dispatch status and the result read need that custody.
RECORD_GRACE_SECONDS = 3_600
# Runs this host may have prepared at once: as many as the project may run at once, so one run
# waiting for its record (or one that failed) never turns the next away.
PREPARATION_SLOTS = 3  # project_service.MAX_ACTIVE_ATTEMPTS
MAX_REQUEST_BYTES = 4_096
WAIT_SECONDS = 5.0
ACCOUNT_TIMEOUT_SECONDS = 60
ACCOUNT_RETRY_SECONDS = 600  # How long an unreachable account is waited for, per call.
ACCOUNT_RETRY_INTERVAL = 0.5
RATE_LIMIT_TIMEOUT_SECONDS = 10  # One event; the child's stream waits for it.
MAX_PENDING_RATE_LIMITS = 64  # The account keeps at most this many per run.
MAX_RUN_RATE_LIMIT_EVENTS = 1_000
# Recorded workers whose originals a verifier open may still bind, per host (newest kept).
MAX_RETAINED_WORKERS = 64
# Retired runs whose status this host still answers as not found (newest kept).
MAX_RETIRED_RUNS = 256
_RETRYING = object()
# A fence the account refused (anything but unreachable), or one that could not be
# built, is asked again after this long, doubling up to the cap, at most this many
# times. Then the run gives its preparation back and keeps its lease; the account's
# own timed rule covers its capacity.
FENCE_RETRY_SECONDS = 1.0
FENCE_RETRY_MAX_SECONDS = 300.0
MAX_FENCE_ATTEMPTS = 10
# A failure report is asked again only for these (R6-8); any other refusal can never succeed.
_REPORT_RETRIED = frozenset({'history_writer_busy', 'history_service_unavailable', 'history_unavailable'})
# O10 E1: the account (or its service) stopped or could not take the call; asked again while this
# service runs, never counted as a refusal.
FENCE_OUTAGES = frozenset({'account_unavailable', 'account_closed_without_reply', 'service_refused_busy',
                           'service_refused_unavailable', 'service_refused_late'})
# A given-back preparation whose cleanup did not finish, and a retired run whose
# monitor slot was refused, are tried again after this long, doubling up to the cap.
# A refused forget is tried at most MAX_FORGET_ATTEMPTS times: a live attempt is
# never forgotten, so its slot then stays taken for this host's lifetime.
CLEANUP_RETRY_SECONDS = 1.0
CLEANUP_RETRY_MAX_SECONDS = 60.0
# After this many refused removals a given-back preparation is no longer asked: it
# stays listed (its workspace may still be there), so its place is never counted free.
MAX_CLEANUP_ATTEMPTS = 12
MAX_FORGET_ATTEMPTS = 12
# How often the journal is checked for runs of stopped earlier services, and how long
# one settlement request may take.
SWEEP_SECONDS = 1.0
EXPIRY_SECONDS = 10.0  # Result cleanup; at least once a minute (0.3.4 R2).
SETTLE_TIMEOUT_SECONDS = 10
# The runtime this code runs from, as for preparation: the client capabilities a
# result is admitted against come from here, as the CLI reads them from its runtime.
RUNTIME_ROOT = Path(__file__).resolve().parents[3]
REASON_CODES = frozenset({
    'run_invalid_request', 'run_not_found', 'run_conflict', 'run_full', 'run_failed', 'run_unavailable', 'run_abandoned',
    'run_closed', 'run_settling',
    # The client the account observed before launch is not the one prepared.
    'run_client_changed',
    # An account or collector call for the run failed without a reason of its own.
    'run_account_call_failed',
    # A run of an earlier, stopped service: settled (abandoned, outcome unknown, capacity no
    # longer counted), or not yet, because something of it may still run.
    'run_lost', 'run_lost_held',
    # The project's result store has no free place (0.3.4 R3): close finished orchestrations.
    'result_capacity_full',
}) | PREPARATION_STAGE_REASONS
_REGISTER_FIELDS = {'purpose', 'orchestration_handle', 'run_id', 'host_client', 'assignment', 'task', 'route',
                    'parent_grant'}
_PREPARE_FIELDS = {'purpose', 'expected_export_commit'}
_ACTIVE = frozenset({'recording_registration', 'reading', 'preparing', 'observing', 'leasing', 'arming', 'recording_open', 'claiming', 'binding',
                     'registering', 'provenance', 'consuming', 'closing', 'settling', 'withdrawing', 'fencing'})
# A run that can never launch closes itself through these, then ends 'failed' and closed.
_CLOSING = frozenset({'closing', 'settling', 'withdrawing', 'fencing'})
_AFTER = frozenset({'ready', 'reporting', 'terminal', 'acknowledging', 'cleanup', 'releasing', 'retiring', 'released'})


@dataclass
class _Run:
    key: tuple
    scope: OrchestrationScope
    host_client: str
    prospective: OpenScopeContext
    run_id: str
    inputs_digest: str
    state: str = 'registered'
    reporting: object = None  # S1-R6: its reporting registration on the writer (polled)
    reason: str | None = None
    preparation: object = None
    prepared: object = None
    prepared_inputs: object = None
    identity_digest: str | None = None
    claim_request_id: str | None = None
    lease: ResultLeaseBinding | None = None
    persistence: object = None
    evidence: bytes | None = None
    receipt: dict | None = None
    binding: AttemptBinding | None = None
    registration: ExecutionRegistrationInput | None = None
    registered: dict | None = None
    context: PreparedDispatchContext | None = None
    submission: TerminalSubmissionInput | None = None
    terminal_evidence: bytes | None = None
    future: object = None
    call: object = None
    retry_at: float | None = None
    retry_until: float = 0.0
    waiters: list = field(default_factory=list)
    dispatch_tokens: dict = field(default_factory=dict)
    submit_token: tuple | None = None
    result_admitted: bool = False
    commit: str | None = None
    released_at: float | None = None
    recorder: object = None
    claim_sent: bool = False  # Set before the claim is first handed to the account; never cleared.
    withdrawing: bool = False  # The lease's release is queued on the writer.
    denied: bool = False  # The account holds a denied receipt for the run's claim: it can never be admitted.
    closed: bool = False  # Failed and holding nothing: no lease, no preparation, nothing to reconcile.
    fence_attempts: int = 0  # Fences the account refused or that could not be sent.
    fence_at: float | None = None  # When a refused fence is asked again.


class _Call(local):
    role = host_client = context = None


class _RateLimitRecorder:
    """A Claude child's rate-limit events, sent from its worker thread to the account owner.

    `run_claude` calls this in place of the legacy ledger. Each event is reduced
    to the fields the account reads and sent as one request with its own ID; any
    failure reaches the child as a contract error, which stops it. An event the
    account did not acknowledge stays pending here, and the driver delivers it
    before the run's terminal, so a restriction is never lost while the run's
    capacity is still charged. The account records a lost `during` reading itself
    when it refuses an event.
    """

    def __init__(self, driver, run):
        self._driver, self._project, self._host = driver, run.scope.project_id, run.host_client
        self._custody = driver._custody(run, storage=run.evidence)
        self._prefix, self._count = 'rate-' + driver._id(run) + '-', 0
        self._lock = Lock()
        # Read by the driver after the worker returns. Events beyond the pending
        # limit are folded into one event at least as restrictive as all of them.
        self.pending, self.overflow = [], None

    def request_id(self):
        with self._lock:
            self._count += 1
            return self._prefix + str(self._count)

    def record_claude_rate_limit_event(self, project_id, *, run_handle, rate_limit_info):
        if project_id != self._project:
            raise RateLimitError('rate limit event was not recorded')
        facts = bounded_rate_limit_info(rate_limit_info)
        request_id = self.request_id()
        if self._count > MAX_RUN_RATE_LIMIT_EVENTS:
            # Past the cap the event is not sent now, but it is never dropped: it
            # waits for delivery before the terminal like any undelivered event.
            self._hold(run_handle, facts, request_id)
            raise RateLimitError('rate limit event was not recorded')
        call = self._driver._call
        call.role, call.host_client, call.context = 'project', self._host, self._custody
        try:
            client = AccountClient(self._driver._account, self._project, COLLECTOR_RUNTIME_DIGEST,
                                   timeout=RATE_LIMIT_TIMEOUT_SECONDS)
            reply = client.record_rate_limit_event(run_handle, facts, request_id=request_id)
        except AccountError:
            # Whatever the failure, the driver tries this exact request again
            # before the terminal; a refusal then stops the run, capacity charged.
            self._hold(run_handle, facts, request_id)
            raise RateLimitError('rate limit event was not recorded') from None
        finally:
            call.role = call.host_client = call.context = None
        # The shape run_claude reads: one record per kept event, the first one
        # carrying the stop the account derived.
        stop = {'paid_usage_observed': {'record_type': 'provider_billing', 'paid_usage_usable': True,
                                        'is_using_overage': None},
                'provider_rejected': {'record_type': 'provider_headroom', 'status': 'rejected'}}.get(reply['stop'])
        kept = [{'record_type': 'provider_headroom', 'status': 'allowed'} for _ in range(reply['kept'])]
        return [stop, *kept[1:]] if stop is not None else kept

    def _hold(self, run_handle, facts, request_id):
        with self._lock:
            if len(self.pending) < MAX_PENDING_RATE_LIMITS:
                self.pending.append((run_handle, facts, request_id))
            else:
                folded = [facts] if self.overflow is None else [self.overflow[1], facts]
                self.overflow = (run_handle, merged_restriction(folded), self._prefix + 'merged')

    def record_observation_unavailable(self, project_id, **_):
        return None  # The account already recorded it when it refused the event.


class RunDriver:
    def __init__(self, state, project_root, bundle_root, scope, runtime, *, clock=time.monotonic, history_endpoint=None):
        self._state, self._root, self._bundle = state, Path(project_root), Path(bundle_root)
        # This host's own project API, read from the helper thread for a verifier's worker result.
        self._history_endpoint = None if history_endpoint is None else Path(history_endpoint)
        self._scope, self._runtime, self._clock = scope, runtime, clock
        self._runs, self._closed, self._intake = {}, False, []
        # Preparations of runs that failed or were abandoned before launch, until their slot is free;
        # and, for one whose cleanup did not finish, (attempts, when it is tried again).
        self._giving_back, self._cleanup_retry = [], {}
        # host outcome digest -> (host class, HostResultContext).
        self._workers = {}
        # Released and retired runs: key -> (host class, orchestration handle); and the
        # bindings whose monitor slots are still to be freed: binding -> (refusals, next try).
        self._retired, self._forgetting = {}, {}
        self._call = _Call()
        self._account = account_endpoint_path(state)
        self._preparation = runtime.bind_preparation(
            PreparationLimits(PREPARATION_SLOTS, PREPARATION_SLOTS * MAX_PREPARATION_BYTES,
                              PREPARATION_SLOTS * MAX_PREPARATION_EXPORT_BYTES),
            authorizer=lambda context: any(run.preparation is context for run in self._runs.values()))
        self._dispatch = runtime.bind_dispatch(max_requests=DISPATCH_SLOTS, steps_per_cycle=1,
                                               authorizer=self._authorize_dispatch)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='project-driver-account')
        # Durable launch evidence for this project's admitted claims; every child this
        # host's pool creates is recorded there first.
        try:
            self._journal = LaunchJournal(Path(state.root) / 'projects' / scope.project_id / 'launches')
        except JournalError:
            raise HistoryError('history_service_unavailable') from None
        runtime.pool._registry.launch_journal = self._journal
        # Runs of earlier service instances, by run handle: (host class, 'released' or 'held').
        self._lost, self._sweep, self._next_sweep = {}, None, 0.0
        self._expiry, self._next_expiry = None, 0.0  # The first cleanup runs at start (0.3.4 R2).
        # Leases an earlier service left armed with no admitted claim, fenced here (0.3.3, C3).
        self._earlier, self._earlier_fences, self._fenced = None, {}, {}
        # S1-R6: reporting writes for the orchestration report, in order, each retried a few times.
        self._reports = []
        self._earlier_failures, self._earlier_at, self._earlier_listed = 0, 0.0, False
        self._sweeper = ThreadPoolExecutor(max_workers=1, thread_name_prefix='project-driver-sweep')
        self._owner_name = 'project-driver-%d-%d' % (os.getpid(), id(self))
        install_enrollment(self._preamble, owner=self._owner_name)
        self._enrolled = None  # Set by the host: token -> its enrolled facts, for dispatch requests.

    # Enrollment for this host's own account calls, one helper thread at a time.
    def _preamble(self, socket_path, message):
        call = self._call
        if call.role is None:
            return None
        if call.role == 'host' and self._history_endpoint is not None and Path(socket_path) == self._history_endpoint:
            # The helper thread reading this host's own project API as the run's host class.
            key, proofs = read_capability(self._state, self._root, 'host'), proof_directory(socket_path)
            line = enrollment_preamble(key, 'host', call.host_client, self._scope.supervisor_generation, message, proofs)
            return line, lambda: withdraw_proof(proofs, line)
        if Path(socket_path) != self._account:
            return None
        key, proofs = read_capability(self._state, None, 'account'), proof_directory(socket_path)
        line = enrollment_preamble(key, call.role, call.host_client, 1, message, proofs,
                                   subject=str(self._root), context=call.context)
        return line, lambda: withdraw_proof(proofs, line)

    def _account_call(self, run, role, method, *args, custody=None, **options):
        project, host = run.scope.project_id, run.host_client

        def call():
            self._call.role, self._call.host_client, self._call.context = role, host, custody
            try:
                client = AccountClient(self._account, project, COLLECTOR_RUNTIME_DIGEST, timeout=ACCOUNT_TIMEOUT_SECONDS)
                return getattr(client, method)(*args, **options)
            finally:
                self._call.role = self._call.host_client = self._call.context = None

        run.call, run.retry_at, run.retry_until = call, None, self._clock() + ACCOUNT_RETRY_SECONDS
        run.future = self._executor.submit(call)

    def _custody(self, run, *, storage=None, terminal=None, cleanup=None):
        return canonical_json({'purpose': 'run', 'lease': {n: getattr(run.lease, n) for n in run.lease.__dataclass_fields__},
            'storage_evidence': None if storage is None else storage.hex(),
            'terminal_evidence': None if terminal is None else terminal.hex(),
            'cleanup_evidence': None if cleanup is None else cleanup.hex()})

    # Requests from the enrolled CLI on the driver endpoint.
    def handle(self, connection, enrolled):
        """Take an attached connection; its one line is read later, without waiting on it."""
        if len(self._intake) >= MAX_INTAKE or enrolled.role != 'host':
            connection.close()
            return
        try:
            connection.setblocking(False)
        except OSError:
            connection.close()
            return
        self._intake.append([connection, enrolled, bytearray(), self._clock() + INTAKE_SECONDS])

    def _read_intake(self):
        """Owner step: read what has arrived, never block, and drop late or oversized senders."""
        now, remaining = self._clock(), []
        for item in self._intake:
            connection, enrolled, raw, deadline = item
            try:
                while b'\n' not in raw:
                    chunk = connection.recv(MAX_REQUEST_BYTES + 1 - len(raw))
                    if not chunk or len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                        raise ValueError()
                    raw.extend(chunk)
            except (BlockingIOError, InterruptedError):
                if now >= deadline:
                    connection.close()  # Bytes trickled in slowly never extend the deadline.
                else:
                    remaining.append(item)
                continue
            except Exception:
                connection.close()
                continue
            self._request(connection, enrolled, bytes(raw))
        self._intake = remaining

    def _request(self, connection, enrolled, raw):
        try:
            line, rest = raw.split(b'\n', 1)
            if rest or hashlib.sha256(line).hexdigest() != enrolled.request_digest:
                raise ValueError()
            request = json.loads(line)
            if (type(request) is not dict or request.keys() != {'protocol', 'operation', 'run_handle', 'attempt', 'inputs_digest'}
                    or request['protocol'] != 1 or request['operation'] not in {'run.register', 'run.prepare', 'run.recorded', 'run.describe', 'run.abandon'}
                    or not matches(request['run_handle'], _ID) or type(request['attempt']) is not int
                    or not matches(request['inputs_digest'], _DIGEST)):
                raise ValueError()
            context = parse_bounded_json(enrolled.context or b'{}', max_bytes=300_000)
        except Exception:
            connection.close()
            return
        key = (request['run_handle'], request['attempt'])
        if request['operation'] in {'run.describe', 'run.abandon'}:
            # Commands that know only a run handle: which host class holds it, or
            # dropping one that never took a lease.
            held = [run for run in self._runs.values() if run.key[0] == request['run_handle']]
            lost = self._lost.get(request['run_handle'])
            fenced = self._fenced.get(request['run_handle'])
            settling = not self._earlier_listed or any(
                entry['binding'].run_handle == request['run_handle'] for entry in self._earlier_fences.values())
            if not held and fenced is not None:
                # An earlier service's run this one fenced: it never launched and holds nothing.
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_not_authorized'}
                            if fenced != enrolled.host_client else {'state': 'failed', 'reason_code': 'run_closed'})
            elif not held and lost is None and settling:
                # This service may still be closing it, an earlier service's run (C3d). It answers for
                # the run, as for one it closed, so a command never looks for it elsewhere.
                self._reply(connection, {'state': 'failed', 'reason_code': 'run_settling'})
            elif not held and lost is not None:
                # A run of an earlier service of this project; it is never started again here.
                if lost[0] != enrolled.host_client:
                    self._reply(connection, {'state': 'refused', 'reason_code': 'history_not_authorized'})
                else:
                    self._reply(connection, {'state': 'failed',
                                             'reason_code': 'run_lost' if lost[1] == 'released' else 'run_lost_held'})
            elif not held:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_orchestration_unavailable'})
            elif held[0].host_client != enrolled.host_client:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_not_authorized'})
            elif request['operation'] == 'run.describe':
                self._reply(connection, self._view(held[0]))
            elif held[0].closed:
                # It already closed by itself; dropping it changes nothing anywhere.
                del self._runs[held[0].key]
                self._reply(connection, {'state': 'failed', 'reason_code': 'run_closed'})
            elif (held[0].lease is not None or held[0].state not in {'registered', 'recording_registration', 'failed'}
                  or held[0].future is not None):
                # A leased or claimed run holds custody and maybe capacity; that needs reconciliation.
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_conflict'})
            else:
                run = held[0]
                if run.state != 'failed' and run.reporting is not None:
                    # R6-18/R6-20: reported only once its registration has committed.
                    self._report('failed_before_open', run.key[0], run.key[1], 'run_failed', after=run.reporting)
                for waiting, _ in run.waiters:  # R6-21: a prepare still waiting is answered
                    self._reply(waiting, {'state': 'failed', 'reason_code': 'run_abandoned'})
                run.waiters = []
                self._give_back(run)
                del self._runs[run.key]
                self._reply(connection, {'state': 'failed', 'reason_code': 'run_abandoned'})
            return
        try:
            if request['operation'] == 'run.register':
                self._register(key, request['inputs_digest'], context, enrolled.host_client)
            elif request['operation'] == 'run.prepare':
                self._prepare(key, request['inputs_digest'], context, enrolled.host_client)
            else:
                self._recorded(key, request['inputs_digest'], context, enrolled.host_client)
        except HistoryError as error:
            self._reply(connection, {'state': 'refused', 'reason_code': error.reason_code})
            return
        run = self._runs[key]
        if request['operation'] != 'run.prepare' or run.state in _AFTER or run.state == 'failed':
            self._reply(connection, self._view(run))
        else:
            run.waiters.append((connection, self._clock() + WAIT_SECONDS))

    def _recorded(self, key, inputs_digest, value, host_client):
        """The opening primary reports its result admitted; custody may then retire.

        Only after its own successful admission does the CLI say so. A false
        report can only shorten custody of that host class's own released run.
        With the outcome's digest, the run's originals are kept so a verifier of
        that outcome can be opened later; project history still checks them
        against the run's consumed grant, so a wrong digest binds nothing.
        """
        run = self._runs.get(key)
        if run is None:
            raise HistoryError('history_orchestration_unavailable')
        if run.host_client != host_client:
            raise HistoryError('history_not_authorized')
        if run.inputs_digest != inputs_digest:
            raise HistoryError('history_conflict')
        if value:
            if (value.keys() != {'purpose', 'host_outcome_digest'} or value['purpose'] != 'recorded'
                    or not matches(value['host_outcome_digest'], _DIGEST)):
                raise HistoryError('history_invalid_input')
            original = self.result_context(run.binding, host_client) if run.binding is not None else None
            if original is not None:
                self._workers.pop(value['host_outcome_digest'], None)
                self._workers[value['host_outcome_digest']] = (host_client, original)
                while len(self._workers) > MAX_RETAINED_WORKERS:
                    self._workers.pop(next(iter(self._workers)))
        run.result_admitted = True

    def worker_context(self, orchestration_handle, host_outcome_digest, host_client):
        """A recorded worker's originals for its verifier, only for the host class that ran it.

        Kept in memory: after a host restart a verifier of an earlier worker is
        refused, never opened without its exact worker.
        """
        held = self._workers.get(host_outcome_digest)
        if held is None or held[0] != host_client or (
                orchestration_handle is not None and held[1].binding.orchestration_handle != orchestration_handle):
            return None
        return held[1]

    def retained_context(self, binding, host_client):
        """The kept originals of exactly this recorded run, for that host class's QA."""
        return next((held[1] for held in self._workers.values()
                     if held[0] == host_client and held[1].binding == binding), None)

    def retains_worker(self, binding, host_client):
        """Whether this exact binding is a recorded worker kept for that host class's verifier."""
        return self.retained_context(binding, host_client) is not None

    def _register(self, key, inputs_digest, value, host_client):
        # The host class is the enrolled one; the context may only repeat it.
        if (value.keys() != _REGISTER_FIELDS or value['purpose'] != 'register' or host_client not in HOST_CLIENTS
                or value['host_client'] != host_client):
            raise HistoryError('history_invalid_input')
        if open_inputs_digest(value['assignment'], value['task'], value['route'], value['parent_grant']) != inputs_digest:
            raise HistoryError('history_conflict')
        if value['assignment'].get('attempt') != key[1]:
            raise HistoryError('history_conflict')
        known = self._runs.get(key)
        if known is not None:
            if known.host_client != host_client:
                raise HistoryError('history_not_authorized')
            if known.inputs_digest != inputs_digest or known.run_id != value['run_id']:
                raise HistoryError('history_conflict')
            return
        if len(self._runs) >= MAX_RUNS:
            raise HistoryError('history_service_unavailable')
        worker = None
        digest = value['task'].get('verification_for_outcome') if type(value['task']) is dict else None
        if digest is not None:
            worker = self.worker_context(value['orchestration_handle'], digest, host_client)
            if worker is None:
                raise HistoryError('history_orchestration_unavailable')
        prospective = OpenScopeContext(self._scope, value['orchestration_handle'], value['assignment'], value['task'],
                                       value['route'], value['parent_grant'], worker)
        prospective.snapshot()
        run = self._runs[key] = _Run(key, self._scope, host_client, prospective, value['run_id'], inputs_digest)
        self._submit_registration(run)

    def _prepare(self, key, inputs_digest, value, host_client):
        run = self._runs.get(key)
        if run is None:
            raise HistoryError('history_orchestration_unavailable')
        if run.host_client != host_client:
            raise HistoryError('history_not_authorized')  # Only the host class that opened it.
        if run.inputs_digest != inputs_digest:
            raise HistoryError('history_conflict')
        if run.state != 'registered':
            return  # Already under way: report its state, never start it twice.
        if value.keys() != _PREPARE_FIELDS or value['purpose'] != 'prepare':
            raise HistoryError('history_invalid_input')
        run.commit = value['expected_export_commit']
        # S1-R6-6: preparation starts only once its registration has committed.
        run.state = 'recording_registration'
        self._after_registration(run)

    def _submit_registration(self, run):
        """Hand the run's registration to the writer; True unless the writer is closed or faulted."""
        try:
            run.reporting = self._runtime.persistence.report_registered_run(
                run.prospective.orchestration_handle, run.key[0], run.run_id, run.key[1])
        except HistoryError as error:
            run.reporting = None
            return error.reason_code == 'history_writer_busy'  # No room yet: asked again next step (R6-15).
        return True

    def _after_registration(self, run):
        if run.reporting is None and not self._submit_registration(run):
            self._fail(run, 'run_failed')  # The writer is closed or faulted (R6-16).
            return
        request = run.reporting
        if request is None or request.state == 'queued':
            return
        if request.state != 'done':
            self._fail(run, 'run_failed')  # Not written: refused before preparation, nothing else held.
            return
        self._begin(run)

    def _begin(self, run):
        if run.prospective.worker is None:
            self._start_preparation(run, None)
            return
        # A verifier's worker result is read first, off the owner thread.
        worker, digest, host = run.prospective.worker, run.prospective.task['verification_for_outcome'], run.host_client

        def read():
            return self._read_worker_result(worker, digest, host)

        run.call, run.retry_at, run.retry_until = read, None, self._clock()
        run.future = self._executor.submit(read)
        run.state = 'reading'

    def _start_preparation(self, run, worker_result):
        run.preparation = PreparationContext(preparation_payload(run.prospective, self._root, self._bundle,
            {'PATH': os.defpath}, expected_export_commit=run.commit, worker_result=worker_result))
        try:
            run.prepared = self._preparation.submit(run.preparation)
        except PreparationError as error:
            # Say why the submission was refused: no room, busy, not authorized, invalid.
            self._fail(run, error.reason_code if error.reason_code in PREPARATION_STAGE_REASONS else 'run_unavailable')
            return
        run.state = 'preparing'

    def _read_worker_result(self, worker, digest, host_client):
        """Helper thread: the worker's terminal and result from this host's own APIs, checked."""
        from .history_api import HistoryClient
        from .result_contracts import MAX_RESULT_BODY_BYTES, checked_result_body
        from .result_read_api import ResultReadClient
        self._call.role, self._call.host_client, self._call.context = 'host', host_client, None
        try:
            endpoint, scope = self._history_endpoint, self._scope
            history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=10)
            current = history.worker_input(digest)
            if current['worker'] != worker.snapshot()[1]:
                raise HistoryError('history_conflict')
            reference = current['reference']
            terminal = history.read_record(reference['terminal_record_id'])['record']
            body = ResultReadClient(endpoint, timeout=10).read_result(worker.binding, reference['result_digest'])
            checked_result_body(body, worker.assignment, terminal)
            return parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)
        finally:
            self._call.role = self._call.host_client = self._call.context = None

    @staticmethod
    def _view(run):
        return {'state': 'failed' if run.state == 'failed' else ('ready' if run.state in _AFTER else 'preparing'),
                'reason_code': run.reason}

    @staticmethod
    def _reply(connection, value):
        """One small reply without waiting: a peer not reading it simply loses it."""
        try:
            connection.setblocking(False)
            connection.send(canonical_json({'protocol': 1, **value}) + b'\n')
        except OSError:
            pass
        finally:
            connection.close()

    def _report(self, kind, *arguments, after=None):
        """Queue one failure report (S1-R6); it never holds up the run, and a failed write is asked again
        while the service runs (R6-8). `after`: a registration still being written; the report is sent
        once it commits and dropped if it does not (R6-20)."""
        self._reports.append([kind, arguments, None, after])
        self._send_reports()  # at once when there is room, so a report read next comes after it

    def _send_reports(self):
        """Owner step: hand queued failure reports to the writer while it has reporting room; drop each
        once written, and ask again for one whose write failed."""
        lane = self._runtime.persistence
        for entry in list(self._reports):
            after = entry[3]
            if after is not None:
                if after.state == 'queued':
                    continue
                if after.state != 'done':
                    self._reports.remove(entry)  # Its registration was never written: nothing to report.
                    continue
                entry[3] = None
            request = entry[2]
            if request is not None and (request.state == 'done' or (
                    request.state == 'failed' and request.reason not in _REPORT_RETRIED)):
                self._reports.remove(entry)  # Written, or never can be (no registration, a conflict).
            elif request is None or request.state == 'failed':
                try:
                    entry[2] = lane.report_failed_before_open(*entry[1])
                except HistoryError:
                    return  # No room now, or closing: asked again next step.

    def _fail(self, run, reason):
        """Stop a run; one that can provably never launch then closes by itself.

        A run that took no lease held only its preparation, given back here. A
        run with an armed lease and no admitted claim asks the account to fence
        its claim request (`fencing`): whatever happened to the claim, the
        account either returns the receipt it holds or makes sure it can never
        be admitted, and only its closure frees the lease. A lease that was never
        armed (no claim possible) is released through the history (`withdrawing`).
        A run whose claim was admitted keeps everything. If closing itself fails,
        the run keeps its first reason and everything not proven free; its
        preparation is given back only when no claim of it can ever be admitted.
        """
        if run.state in _CLOSING:
            run.state = 'failed'
            if not run.claim_sent or run.denied:
                self._give_back(run)
            else:
                self._fence_later(run)  # Its claim may still be admitted: only the account can say.
            return
        run.state, run.reason = 'failed', reason
        if (not run.claim_sent and run.receipt is None and run.binding is None and run.reporting is not None
                and run.reporting.state == 'done'):
            # S1-R6: the host open is recorded just before the claim is sent, so this run (most likely)
            # never had one; the store checks its own open rows (R6-10). Reporting only: the closing
            # below is unchanged.
            self._report('failed_before_open', run.key[0], run.key[1],
                         reason if reason in FAILED_BEFORE_OPEN_REASONS else 'run_failed')
        if run.persistence is None:
            self._give_back(run)
            run.closed = run.prepared is None and run.lease is None
        elif run.receipt is not None or run.binding is not None:
            return  # Admitted: local custody stays with what the account holds.
        elif run.claim_sent:
            self._fence(run)
        else:
            run.state = 'withdrawing'

    def _fence(self, run):
        """The account settles the claim request for good; retried while it is unreachable."""
        try:
            fence = FenceClaimInput(run.key[0], run.identity_digest, run.claim_request_id,
                                    self._runtime.persistence.lease(run.persistence).lease_digest)
        except Exception:
            run.state = 'failed'
            if run.claim_sent and not run.denied:
                self._fence_later(run)  # Nothing to fence it with yet: it keeps what it holds.
            else:
                self._give_back(run)  # No claim of it was ever sent, so none can be admitted.
            return
        self._account_call(run, 'project', 'fence_claim', fence, request_id='fence-' + self._id(run),
                           custody=self._custody(run, storage=run.evidence))
        run.retry_until = math.inf  # Nothing is freed without the account's answer.
        run.state = 'fencing'

    def _fence_later(self, run):
        """A claimed run whose fence was refused asks again later, less and less often.

        Nothing is freed without the account's answer, so it keeps its lease and
        preparation meanwhile, and its status reads failed. After
        MAX_FENCE_ATTEMPTS it stops asking and gives its preparation back, so it
        no longer holds one of this host's preparation slots: a failed run is
        never bound or launched here, so the preparation can never be used. Its
        lease stays, and the account's own timed rule settles its capacity.
        """
        run.fence_attempts += 1
        if run.fence_attempts >= MAX_FENCE_ATTEMPTS:
            run.fence_at = None
            self._give_back(run)
            return
        delay = min(FENCE_RETRY_MAX_SECONDS, FENCE_RETRY_SECONDS * 2 ** (run.fence_attempts - 1))
        run.fence_at = self._clock() + delay

    def _close(self, run):
        """The lease is expired in the history: drop its input, then give the preparation back."""
        if run.receipt is not None or run.binding is not None:
            raise HistoryError('history_conflict')  # An admitted claim keeps everything until its release.
        self._runtime.persistence.retire_denied(run.persistence)
        run.persistence, run.state = None, 'failed'
        self._give_back(run)
        run.closed = run.prepared is None

    def _bind_claim(self, run, receipt):
        """The account admitted the run's claim: record it in the journal, bind it, go on towards launch.

        The journal entry is written before anything can launch the run; if it
        cannot be written the run stops here, keeping what the account holds.
        """
        run.receipt = receipt
        self._journal.claimed(receipt['bridge_run_id'], run_handle=run.key[0], attempt=run.key[1],
                              host_client=run.host_client,
                              lease={n: getattr(run.lease, n) for n in run.lease.__dataclass_fields__},
                              identity_digest=run.identity_digest, claim_request_id=run.claim_request_id,
                              storage_evidence=run.evidence, workspace=run.prepared_inputs.workspace)
        run.binding = AttemptBinding(**{n: getattr(run.lease, n) for n in LEASE_SCOPE_FIELDS},
                                     claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
        self._runtime.persistence.bind_claim(run.persistence, run.binding, hard_deadline_at_ms=receipt['deadline_at_ms'])
        run.state = 'binding'

    def _give_back(self, run):
        """A run that stopped before the account admitted its claim gives its preparation
        back. Once a claim is admitted (or the run bound to an execution) the preparation
        stays, so local custody always matches what the account holds for the run."""
        if run.prepared is None or run.receipt is not None:
            return
        try:
            self._preparation.discard(run.prepared)
        except Exception:
            return  # Bound to an execution, or the lane is gone: nothing is freed here.
        self._giving_back.append(run.prepared)
        run.prepared = None

    def _retire_given_back(self):
        for handle in list(self._giving_back):
            try:
                state = self._preparation.snapshot(handle)['state']
                if state in {'cleaned', 'failed'}:
                    self._preparation.retire(handle)
                    self._giving_back.remove(handle)
                    self._cleanup_retry.pop(handle, None)
                elif state == 'unknown':
                    # Its workspace may still be there: kept, and its removal asked for again later.
                    self._retry_cleanup(handle)
            except Exception:
                pass  # Still cleaning up; asked again on the next step.

    def _retry_cleanup(self, handle):
        attempts, when = self._cleanup_retry.get(handle, (0, self._clock()))
        if self._clock() < when or attempts >= MAX_CLEANUP_ATTEMPTS:
            return
        attempts += 1
        self._cleanup_retry[handle] = (attempts, self._clock() + min(
            CLEANUP_RETRY_MAX_SECONDS, CLEANUP_RETRY_SECONDS * 2 ** (attempts - 1)))
        self._preparation.retry_cleanup(handle)

    # The dispatch service names only runs this driver has made ready.
    def _authorize_dispatch(self, request, token):
        """A ready run's dispatch, for the host class that holds it.

        The service rechecks a running job with its submit's token after that
        connection has closed; the driver keeps honouring exactly that request
        while it holds the run, and never another request on that token.
        """
        run = self._runs.get((request.run_handle, request.attempt))
        if run is None or run.context is None or run.state not in _AFTER - {'released'}:
            return None
        digest = dispatch_request_digest(request)
        # The submit's own token is pinned for as long as the driver holds the run;
        # status tokens are short-lived and bounded, and never displace it.
        if run.submit_token != (token, digest) and run.dispatch_tokens.get(token) != digest:
            enrolled = None if self._enrolled is None else self._enrolled(token)
            if (enrolled is None or enrolled.role != 'host' or digest != enrolled.request_digest
                    or run.host_client != enrolled.host_client):
                return None
            if request.operation == 'dispatch.submit' and run.submit_token is None:
                run.submit_token = (token, digest)
            else:
                if len(run.dispatch_tokens) >= 256:
                    run.dispatch_tokens.pop(next(iter(run.dispatch_tokens)))
                run.dispatch_tokens[token] = digest
        return DispatchAuthorization(self._control_scope(), digest, run.context,
                                     frozenset({'dispatch.submit', 'dispatch.status'}))

    def _control_scope(self):
        return ControlScope(**self._scope.as_data())

    def result_context(self, binding, host_client):
        """The run's originals for admitting its host result, for the host class that holds it."""
        from .host_result_contracts import HostResultContext
        from .learning_adapters import load_client_capabilities

        if not self.owns_binding(binding, host_client):
            return None
        run = self._runs[(binding.run_handle, binding.attempt)]
        capability = load_client_capabilities(
            RUNTIME_ROOT / '.codex' / 'orchestration' / 'config' / 'learning-clients.json')['bridge-v1']
        original = run.prospective
        return HostResultContext(run.binding, original.assignment, original.task, original.route, capability)

    def owns_binding(self, binding, host_client):
        """Whether this exact claimed run is one this driver holds for that host class."""
        run = self._runs.get((binding.run_handle, binding.attempt))
        return run is not None and run.binding == binding and run.host_client == host_client

    def owns(self, target, host_client):
        """Whether this run exists here for that host class; status and cancel stay with it.

        A run this host released and retired still belongs to its host class, so
        its status answers that the attempt is no longer tracked.
        """
        key = (target.run_handle, target.attempt)
        run = self._runs.get(key)
        if run is None:
            return self._retired.get(key) == (host_client, target.orchestration_handle)
        return run.host_client == host_client and run.prospective.orchestration_handle == target.orchestration_handle

    # Owner-thread state machine.
    def step(self):
        if self._closed:
            return
        self._read_intake()
        self._retire_given_back()
        self._forget_retired()
        self._sweep_lost()
        self._fence_earlier()
        self._expire_results()
        self._send_reports()
        for run in list(self._runs.values()):
            try:
                self._advance(run)
            except Exception:
                if run.state != 'failed':
                    self._fail(run, 'run_failed')
            if run.waiters and (run.state in _AFTER or run.state == 'failed'):
                for connection, _ in run.waiters:
                    self._reply(connection, self._view(run))
                run.waiters = []
            if run.state == 'released' and not run.waiters:
                del self._runs[run.key]  # Recorded and released; nothing here is owed to it.
            else:
                now = self._clock()
                for connection, deadline in [w for w in run.waiters if now >= w[1]]:
                    self._reply(connection, self._view(run))
                run.waiters = [w for w in run.waiters if now < w[1]]

    def _result(self, run):
        """The finished account call's reply, None while it runs; failures stop the run.

        An unreachable account (its collector restarting for maintenance, say) is
        retried with the same request: every driver call has a fixed request ID,
        so the owner either answers it for the first time or returns the reply it
        already committed. Nothing else is retried.
        """
        if run.retry_at is not None:
            if self._clock() < run.retry_at:
                return None
            run.retry_at, run.future = None, self._executor.submit(run.call)
            return None
        if run.future is None or not run.future.done():
            return None
        future, run.future = run.future, None
        try:
            return future.result()
        except AccountError as error:
            if error.reason_code == 'account_unavailable' and self._clock() < run.retry_until:
                # The call stays outstanding (a sentinel, not a future) until its retry.
                run.future, run.retry_at = _RETRYING, self._clock() + ACCOUNT_RETRY_INTERVAL
                return None
            self._fail(run, error.reason_code)
        except Exception:
            # An account or collector call failed without a fixed reason of its own.
            self._fail(run, 'run_account_call_failed')
        return None

    def _advance(self, run):
        lane, prep = self._runtime.persistence, self._preparation
        if run.state == 'failed':
            if run.fence_at is not None and self._clock() >= run.fence_at:
                run.fence_at = None
                self._fence(run)  # A refused fence, asked again (see `_fence_later`).
        elif run.state == 'registered':
            if run.reporting is None:
                self._submit_registration(run)  # R6-15: whether or not a prepare has come
        elif run.state == 'recording_registration':
            self._after_registration(run)
        elif run.state == 'reading':
            worker_result = self._result(run)
            if worker_result is not None:
                self._start_preparation(run, worker_result)
        elif run.state == 'preparing':
            snapshot = prep.snapshot(run.prepared)
            if snapshot['state'] in {'failed', 'unknown', 'cleaned'}:
                # Say which stage of preparation stopped it, when it is one.
                reason = snapshot.get('reason_code')
                self._fail(run, reason if reason in PREPARATION_STAGE_REASONS else 'run_unavailable')
            elif snapshot['state'] == 'ready':
                run.prepared_inputs = prep.prepared(run.prepared)
                run.state = 'observing'
                self._account_call(run, 'account', 'observe_provider', run.prospective.route['provider'],
                                   request_id='before-' + self._id(run),
                                   run_handle=run.key[0], phase='before')
        elif run.state == 'observing':
            reply = self._result(run)
            if reply is None:
                return
            identity = [r for r in reply['records'] if r.get('record_type') == 'provider_identity']
            # The client the account just observed must be the one prepared for launch.
            if (len(identity) != 1 or (identity[0].get('client_path'), identity[0].get('client_sha256'))
                    != tuple(run.prepared_inputs.client_identity)):
                self._fail(run, 'run_client_changed')
                return
            run.identity_digest = content_digest(identity[0])
            run.claim_request_id = 'claim-' + self._id(run)
            claim = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.claim_launch', run.claim_request_id,
                run.scope.project_id, COLLECTOR_RUNTIME_DIGEST, LaunchClaimInput(run.key[0], run.identity_digest).payload())
            run.lease = ResultLeaseBinding(**run.scope.as_data(), orchestration_handle=run.prospective.orchestration_handle,
                run_handle=run.key[0], run_id=run.run_id, attempt=run.key[1],
                claim_request_id=run.claim_request_id, claim_request_digest=account_request_digest(claim))
            try:
                run.persistence = lane.prepare_lease(run.lease, run.prepared_inputs.worker)
            except HistoryError as error:
                if error.reason_code != 'history_result_capacity':
                    raise
                self._fail(run, 'result_capacity_full')  # 0.3.4 R3: named, never the generic run_failed.
                return
            run.state = 'leasing'
        elif run.state == 'leasing':
            snapshot = lane.snapshot(run.persistence)
            state = snapshot['state']
            if state == 'failed' and snapshot['reason_code'] == 'history_result_capacity':
                self._fail(run, 'result_capacity_full')  # 0.3.4 R3
                return
            if state != 'preparing':
                lane.arm(run.persistence)
                run.state = 'arming'
        elif run.state == 'arming':
            if lane.snapshot(run.persistence)['state'] == 'arming':
                return
            run.evidence = lane.storage_evidence(run.persistence)
            self._account_call(run, 'project', 'open_provenance',
                OpenProvenanceInput(run.key[0], run.prospective.assignment['assignment_id'],
                                    run.prospective.task.get('verification_for_outcome')),
                request_id='open-' + self._id(run), custody=self._custody(run, storage=run.evidence))
            run.state = 'recording_open'
        elif run.state == 'recording_open':
            if run.future is not None:
                original = self._result(run)
                if original is None:
                    return
                lane.record_host_open(run.persistence, canonical_json(original))
                return
            state = lane.snapshot(run.persistence)['state']
            if state == 'recording_host_open':
                return
            if state != 'claim_pending':
                self._fail(run, 'run_failed')
                return
            run.claim_sent = True
            self._account_call(run, 'account', 'claim_launch', run.key[0], run.identity_digest,
                               request_id=run.claim_request_id, custody=self._custody(run, storage=run.evidence))
            run.state = 'claiming'
        elif run.state == 'claiming':
            receipt = self._result(run)
            if receipt is None:
                return
            if receipt.get('state') != 'claimed':
                reason = receipt.get('reason_code')
                if receipt.get('state') != 'denied' or reason not in CLAIM_DENIAL_CODES:
                    self._fail(run, 'run_failed')
                    return
                # The account refused it for good: it fences the run against any later
                # claim and returns the closure that lets the history expire the lease.
                run.reason, run.denied = reason, True
                self._account_call(run, 'project', 'close_denied_attempt',
                    CloseDeniedInput(run.key[0], run.identity_digest, run.claim_request_id, receipt['receipt_digest'],
                                     lane.lease(run.persistence).lease_digest),
                    request_id='close-' + self._id(run), custody=self._custody(run, storage=run.evidence))
                run.state = 'closing'
                return
            self._bind_claim(run, receipt)
        elif run.state == 'closing':
            closure = self._result(run)
            if closure is not None:
                lane.settle_denial(run.persistence, canonical_json(closure))
                run.state = 'settling'
        elif run.state == 'settling':
            state = lane.snapshot(run.persistence)['state']
            if state == 'denial_settling':
                return
            if state != 'denied':
                self._fail(run, run.reason)
                return
            self._close(run)
        elif run.state == 'fencing':
            reply = self._result(run)
            if reply is None:
                return
            if reply['receipt']['state'] == 'claimed':
                # Admitted after all (its reply was lost): it never launched and still holds its
                # lease and preparation, so it goes on exactly as if that reply had arrived. Its
                # dispatch is still waiting, and its capacity is then released the normal way.
                self._bind_claim(run, reply['receipt'])
                return
            run.denied = True
            lane.settle_denial(run.persistence, canonical_json(reply['closure']))
            run.state = 'settling'
        elif run.state == 'withdrawing':
            state = lane.snapshot(run.persistence)['state']
            if not run.withdrawing:
                if state in {'preparing', 'arming', 'recording_host_open'}:
                    return  # The writer finishes what it started first.
                if run.evidence is not None:
                    self._fence(run)  # Armed: the account closes the request, and the run's binding with it.
                    return
                lane.withdraw_unsent(run.persistence)
                run.withdrawing = True
            elif state == 'withdrawn':
                self._close(run)
            elif state != 'withdrawing':
                self._fail(run, run.reason)
        elif run.state == 'binding':
            if lane.snapshot(run.persistence)['state'] == 'binding_claim':
                return
            bound = lane.bound_storage_evidence(run.persistence)
            receipt = run.receipt
            run.registration = ExecutionRegistrationInput(run.key[0], receipt['bridge_run_id'], receipt['receipt_digest'],
                                                          run.claim_request_id, run.identity_digest)
            self._account_call(run, 'project', 'register_execution', run.key[0], receipt['bridge_run_id'],
                receipt['receipt_digest'], run.claim_request_id, run.identity_digest,
                request_id='register-' + self._id(run), custody=self._custody(run, storage=bound))
            run.evidence = bound
            run.state = 'registering'
        elif run.state == 'registering':
            registered = self._result(run)
            if registered is None:
                return
            run.registered = registered
            self._account_call(run, 'project', 'run_provenance', RunProvenanceInput(run.registration,
                registered['grant']['registration_digest'], run.prospective.assignment['assignment_id']),
                request_id='provenance-' + self._id(run), custody=self._custody(run, storage=run.evidence))
            run.state = 'provenance'
        elif run.state == 'provenance':
            provenance = self._result(run)
            if provenance is None:
                return
            grant = checked_execution_grant(run.registered['grant'])
            self._runtime.monitor.register(run.binding, hard_deadline_ns=grant.deadline_ns)
            lane.consume_execution(run.persistence, grant.encode(), provenance=canonical_json(provenance))
            run.state = 'consuming'
        elif run.state == 'consuming':
            if lane.snapshot(run.persistence)['state'] == 'consuming_execution':
                return
            inputs = run.prepared_inputs
            if run.prospective.route['provider'] == 'claude':
                run.recorder = _RateLimitRecorder(self, run)
                self._runtime.pool.attach_recorder(inputs.worker, run.recorder)
            run.context = PreparedDispatchContext(run.binding, run.persistence, inputs.worker,
                                                  lane.prepared_inputs_digest(run.persistence), inputs.export_info)
            run.state = 'ready'
        elif run.state == 'ready':
            if lane.snapshot(run.persistence)['state'] not in {'committed'}:
                return
            run.state = 'reporting'
            self._advance(run)
        elif run.state == 'reporting':
            # A rate-limit event the account never acknowledged is delivered now,
            # retried while the account is unreachable, before the terminal; a
            # refusal or an overflow stops the run with its capacity still charged.
            recorder = run.recorder
            if recorder is not None and run.future is not None:
                if self._result(run) is None:
                    return
                recorder.pending.pop(0)
            if recorder is not None and not recorder.pending and recorder.overflow is not None:
                recorder.pending.append(recorder.overflow)  # Delivered last, once.
                recorder.overflow = None
            if recorder is not None and recorder.pending:
                handle, facts, request_id = recorder.pending[0]
                self._account_call(run, 'project', 'record_rate_limit_event', handle, facts, request_id=request_id,
                                   custody=self._custody(run, storage=run.evidence))
                # The restriction is retried for as long as this run holds its capacity.
                run.retry_until = math.inf
                return
            delivery = lane.delivery(run.persistence)
            run.terminal_evidence = lane.terminal_evidence(run.persistence)
            run.submission = TerminalSubmissionInput(run.registration, run.registered['grant']['registration_digest'],
                delivery.record_id, delivery.record_content_digest, delivery.result_digest)
            self._account_call(run, 'project', 'submit_terminal', run.submission, request_id='terminal-' + self._id(run),
                custody=self._custody(run, storage=run.evidence, terminal=run.terminal_evidence))
            run.state = 'terminal'
        elif run.state == 'terminal':
            receipt = self._result(run)
            if receipt is None:
                return
            lane.acknowledge_terminal(run.persistence, canonical_json(receipt))
            run.state = 'acknowledging'
        elif run.state == 'acknowledging':
            if lane.snapshot(run.persistence)['state'] != 'acknowledged':
                if lane.snapshot(run.persistence)['state'] == 'failed':
                    self._fail(run, 'run_failed')
                return
            try:
                lane.retain_cleanup(run.persistence)
            except HistoryError:
                return  # The tracked tree is not yet confirmed clean.
            lane.prepare_cleanup(run.persistence)
            run.state = 'cleanup'
        elif run.state == 'cleanup':
            state = lane.snapshot(run.persistence)['state']
            if state == 'preparing_cleanup':
                return
            if state != 'cleanup_ready':
                self._fail(run, 'run_failed')
                return
            self._account_call(run, 'project', 'release_capacity', run.submission, request_id='release-' + self._id(run),
                custody=self._custody(run, storage=run.evidence, terminal=run.terminal_evidence,
                                      cleanup=lane.cleanup_evidence(run.persistence)))
            run.state = 'releasing'
        elif run.state == 'releasing':
            if run.future is not None:
                reply = self._result(run)
                if reply is None:
                    return
                lane.acknowledge_release(run.persistence, canonical_json(reply))
                return
            state = lane.snapshot(run.persistence)['state']
            if state == 'acknowledging_release':
                return
            if state != 'release_acknowledged':
                self._fail(run, 'run_failed')
                return
            if run.released_at is None:
                run.released_at = self._clock()
                try:
                    # Its capacity is released: nothing is left for a later service to settle.
                    self._journal.remove(run.receipt['bridge_run_id'])
                except JournalError:
                    pass  # A later settlement finds it released and keeps only a tombstone.
            if not run.result_admitted and self._clock() - run.released_at < RECORD_GRACE_SECONDS:
                return  # The capacity is released; local custody waits for the primary's record.
            lane.finish_release(run.persistence)
            lane.retire(run.persistence)
            self._runtime.owner.retire(run.binding, self._runtime.pool, run.prepared_inputs.worker)
            self._preparation.discard(run.prepared)
            run.state = 'retiring'
        elif run.state == 'retiring':
            state = self._preparation.snapshot(run.prepared)['state']
            if state in {'cleaned', 'unknown'}:
                if state == 'cleaned':
                    self._preparation.retire(run.prepared)
                try:
                    # Its tombstone keeps the slot, so the same submit can never launch again.
                    self._dispatch.retire_released(run.context)
                except Exception:
                    pass
                # Nothing holds its monitor entry any more: the lane, the pool and the
                # dispatch job are retired. Its slot is freed (see `_forget_retired`).
                self._retired.pop(run.key, None)
                self._retired[run.key] = (run.host_client, run.prospective.orchestration_handle)
                while len(self._retired) > MAX_RETIRED_RUNS:
                    self._retired.pop(next(iter(self._retired)))
                self._forgetting[run.binding] = (0, self._clock())
                run.state = 'released'

    def _forget_retired(self):
        """Free the monitor slots of retired runs.

        One the monitor refuses (not yet seen cleaned, say) is tried again later,
        less and less often, at most MAX_FORGET_ATTEMPTS times; a live attempt is
        never forgotten, so its slot then stays taken.
        """
        from .project_monitor import ProjectMonitorError
        now = self._clock()
        for binding, (refusals, when) in list(self._forgetting.items()):
            if now < when:
                continue
            try:
                self._runtime.owner.forget(binding)
            except Exception as error:
                if not (isinstance(error, ProjectMonitorError) and error.reason_code == 'monitor_attempt_not_found'):
                    refusals += 1
                    if refusals >= MAX_FORGET_ATTEMPTS:
                        del self._forgetting[binding]
                    else:
                        self._forgetting[binding] = (refusals, now + min(
                            CLEANUP_RETRY_MAX_SECONDS, CLEANUP_RETRY_SECONDS * 2 ** (refusals - 1)))
                    continue
            del self._forgetting[binding]

    # Leases an earlier service left armed: its claim may have been sent and refused, or never sent.
    def _fence_earlier(self):
        """Owner step: fence each lease an earlier service of this project left armed (0.3.3, C3).

        Such a run's claim was never admitted here, so nothing of it ran; but its lease held one of
        the project's result places for good, because only the service that held it could fence it.
        The account closes its claim request (taking the identity from the run's launch observation)
        unless it already admitted it, and the closure expires the lease. A claim it admitted is
        left to the lost-run settlement. A failed history request is asked again, keeping any
        closure already received (C3a); a refusal at most MAX_FENCE_ATTEMPTS times, an unreachable
        account while this service runs (C3c); both with backoff.
        """
        lane, now = self._runtime.persistence, self._clock()
        if not self._earlier_listed:
            if self._earlier is None:
                if now < self._earlier_at:
                    return
                try:
                    self._earlier = lane.earlier_leases()
                except HistoryError:
                    self._earlier_later(now)
                return
            if self._earlier.state == 'queued':
                return
            if self._earlier.state == 'failed':
                self._earlier = None
                self._earlier_later(now)
                return
            for binding, evidence, host_client in self._earlier.result:
                if (host_client in HOST_CLIENTS and binding.claim_request_id not in self._earlier_fences
                        and binding.run_handle not in self._fenced):
                    self._earlier_fences[binding.claim_request_id] = {
                        'binding': binding, 'evidence': evidence, 'host_client': host_client, 'future': None,
                        'closure': None, 'settling': None, 'refusals': 0, 'outages': 0, 'at': 0.0}
            skipped, self._earlier = self._earlier.skipped, None
            if skipped:
                self._earlier_later(now)  # O10 E2: some could not be read yet; listed again later.
            else:
                self._earlier_listed = True
        for key, entry in list(self._earlier_fences.items()):
            if entry['settling'] is not None:
                state = entry['settling'].state
                if state == 'done':
                    self._fenced[entry['binding'].run_handle] = entry['host_client']
                    del self._earlier_fences[key]
                elif state == 'failed':
                    entry['settling'], entry['outages'] = None, entry['outages'] + 1
                    entry['at'] = now + self._earlier_delay(entry['outages'])
                continue
            if now < entry['at']:
                continue
            if entry['closure'] is not None:
                try:
                    entry['settling'] = lane.settle_earlier_lease(entry['binding'], entry['closure'])
                except HistoryError:
                    entry['outages'] += 1
                    entry['at'] = now + self._earlier_delay(entry['outages'])
                continue
            if entry['future'] is None:
                entry['future'] = self._executor.submit(self._fence_earlier_call, entry)
                continue
            if not entry['future'].done():
                continue
            future, entry['future'] = entry['future'], None
            try:
                reply = future.result()
            except AccountError as error:
                if error.reason_code in FENCE_OUTAGES:
                    entry['outages'] += 1
                    entry['at'] = now + self._earlier_delay(entry['outages'])
                else:
                    entry['refusals'] += 1
                    if entry['refusals'] >= MAX_FENCE_ATTEMPTS:
                        del self._earlier_fences[key]  # Left armed, as before; the next service asks again.
                    else:
                        entry['at'] = now + self._earlier_delay(entry['refusals'])
                continue
            except Exception:
                entry['refusals'] += 1
                if entry['refusals'] >= MAX_FENCE_ATTEMPTS:
                    del self._earlier_fences[key]
                else:
                    entry['at'] = now + self._earlier_delay(entry['refusals'])
                continue
            if reply['receipt']['state'] == 'claimed':
                del self._earlier_fences[key]  # Admitted after all: the lost-run settlement's to settle.
                continue
            entry['closure'] = canonical_json(reply['closure'])  # Kept until its lease is expired.

    @staticmethod
    def _earlier_delay(attempts):
        return min(FENCE_RETRY_MAX_SECONDS, FENCE_RETRY_SECONDS * 2 ** max(0, attempts - 1))

    def _earlier_later(self, now):
        """The earlier leases could not be listed: asked again later, less and less often."""
        self._earlier_failures += 1
        self._earlier_at = now + self._earlier_delay(self._earlier_failures)

    def _fence_earlier_call(self, entry):
        """Helper thread: the account's fence for an earlier service's lease, sent with its custody."""
        binding = entry['binding']
        fence = FenceClaimInput(binding.run_handle, None, binding.claim_request_id,
                                parse_bounded_json(entry['evidence'])['lease']['lease_digest'])
        custody = canonical_json({'purpose': 'run', 'lease': binding.as_data(), 'storage_evidence': entry['evidence'].hex(),
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        call = self._call
        call.role, call.host_client, call.context = 'project', entry['host_client'], custody
        try:
            client = AccountClient(self._account, self._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                   timeout=ACCOUNT_TIMEOUT_SECONDS)
            return client.fence_claim(
                fence, request_id='earlier-' + hashlib.sha256(binding.claim_request_id.encode()).hexdigest()[:32])
        finally:
            call.role = call.host_client = call.context = None

    def _expire_results(self):
        """Owner step: retire results past their retention or whose orchestration closed, and remove
        finished rows, at start and then every EXPIRY_SECONDS (0.3.4 R2). In 0.8.29 nothing ran this,
        so a project's results were never retired and its store filled for good."""
        if self._expiry is not None:
            if self._expiry.state == 'queued':
                return
            self._expiry = None  # Done or failed: a failure stays charged and is tried again later.
        if self._clock() < self._next_expiry:
            return
        self._next_expiry = self._clock() + EXPIRY_SECONDS
        try:
            self._expiry = self._runtime.persistence.expire_results()
        except HistoryError:
            pass  # The writer is closing.

    # Runs of earlier service instances of this project.
    def _sweep_lost(self):
        """Owner step: collect the last sweep, and start the next one on its own helper thread."""
        if self._sweep is not None:
            if not self._sweep.done():
                return
            try:
                self._lost = self._sweep.result()
            except Exception:
                pass
            self._sweep, self._next_sweep = None, self._clock() + SWEEP_SECONDS
        if self._clock() >= self._next_sweep:
            self._sweep = self._sweeper.submit(self._settle_lost, False)

    def _settle_lost(self, own):
        """Settle every run of a stopped instance whose whole tree is provably gone; return what is known.

        Only an instance whose lock is free can have no thread left that might
        launch or own its child; with `own`, this instance's threads have
        stopped too. Nothing is ever signalled; an entry that cannot be read is
        left as it is, and its capacity to the account's timed rule.
        """
        journal, lost, stopped = self._journal, {}, {}
        for entry in journal.entries():
            if entry['state'] is None:
                continue
            handle, host_client = entry['run_handle'], entry['host_client']
            if entry['state'] == 'settled':
                lost[handle] = (host_client, 'released')
                continue
            instance = entry['instance']
            if instance == journal.instance:
                if not own:
                    continue
            elif stopped.setdefault(instance, journal.instance_stopped(instance)) is not True:
                continue
            lost[handle] = (host_client, 'held')
            if tree_state(entry) != 'gone':
                continue
            try:
                reply = self._settle_entry(entry)
            except Exception:
                continue  # The account is unreachable or refused: asked again on the next sweep.
            self._remove_workspace(entry['workspace'])
            try:
                journal.settled(entry['bridge_run_id'], reply['state'])
            except JournalError:
                continue
            lost[handle] = (host_client, 'released')
        journal.prune()
        for instance in journal.instances():
            if instance != journal.instance:
                journal.forget_instance(instance)
        return lost

    def _settle_entry(self, entry):
        """Helper thread: the account's settlement of this run, sent with the run's own custody."""
        lease = ResultLeaseBinding(**entry['lease'])
        if lease.project_id != self._scope.project_id or entry['host_client'] not in HOST_CLIENTS:
            raise HistoryError('history_conflict')
        evidence = bytes.fromhex(entry['storage_evidence'])
        settle = SettleLostInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                 entry['bridge_run_id'], parse_bounded_json(evidence)['lease']['lease_digest'])
        custody = canonical_json({'purpose': 'run', 'lease': entry['lease'], 'storage_evidence': entry['storage_evidence'],
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        call = self._call
        call.role, call.host_client, call.context = 'project', entry['host_client'], custody
        try:
            client = AccountClient(self._account, self._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                   timeout=SETTLE_TIMEOUT_SECONDS)
            return client.settle_lost_run(
                settle, request_id='lost-' + hashlib.sha256(entry['bridge_run_id'].encode()).hexdigest()[:32])
        finally:
            call.role = call.host_client = call.context = None

    @staticmethod
    def _remove_workspace(workspace):
        """The settled run's private workspace, only if it is one of the host scratch folder's."""
        if workspace is None:
            return
        from .bridge import SCRATCH_CONTAINER, scratch_base
        from .host_paths import remove_private_directory
        try:
            # Resolved on both sides: no component of either path may be a symlink.
            container = Path(os.path.realpath(scratch_base())) / SCRATCH_CONTAINER
            path = Path(workspace)
            if (os.path.realpath(container) != str(container) or os.path.realpath(path) != str(path)
                    or path.parent != container or not path.name.startswith('prepare-')):
                return
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
                remove_private_directory(path)
        except Exception:
            pass

    def settle_on_exit(self):
        """After every thread of this host stopped: settle its own runs, then let later hosts settle the rest.

        An in-flight sweep and account call are waited for first, so nothing of
        this host writes its journal or reaches the account after its lock is gone.
        """
        self._sweeper.shutdown(wait=True)
        self._executor.shutdown(wait=True)
        install_enrollment(self._preamble, owner=self._owner_name)
        try:
            self._settle_lost(True)
        except Exception:
            pass
        finally:
            install_enrollment(None, owner=self._owner_name)
        self._journal.release()

    @staticmethod
    def _id(run):
        return hashlib.sha256(canonical_json([run.key[0], run.key[1], run.inputs_digest])).hexdigest()[:32]

    def busy(self):
        return any(run.state not in {'registered', 'failed', 'released'} for run in self._runs.values())

    def retaining(self):
        """Whether recorded runs' originals are kept here for their verifiers and QA."""
        return bool(self._workers)

    def exhausted(self):
        """No room for another run in this host's lifetime; a fresh host has it all."""
        return not self.busy() and (len(self._runs) >= MAX_RUNS or len(self._dispatch._jobs) >= DISPATCH_SLOTS)

    def close(self):
        if self._closed:
            return
        self._closed = True
        install_enrollment(None, owner=self._owner_name)
        for connection, *_ in self._intake:
            connection.close()
        self._intake = []
        for run in self._runs.values():
            for connection, _ in run.waiters:
                connection.close()
            run.waiters = []
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._sweeper.shutdown(wait=False, cancel_futures=True)
