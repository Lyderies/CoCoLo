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

C2 (behind `container_launch.SETTINGS`, off as shipped): with containers switched
on, the run's container intent is written to the journal before its claim is
sent (no intent, no claim), the admitted claim is recorded on it, and the child
is launched in that container (`container_launch`). Such entries are never
settled by the process rule above: C4's recovery (`container_recovery`) settles
an earlier instance's container runs at start and on each sweep, once the OS
says each container is gone (stopping it first), and an intent never admitted
through the account's fence; podman unreachable settles nothing.

C3: every stop of a run's container goes through the launcher's one stop action;
a stop that did not prove the container gone is retried by the sweep. A run of
this service that failed after its claim was admitted is settled by this service
itself once the OS says its container is gone (`_settle_own_ended`): the account
settles it as lost, its entry is `settled` and its preparation given back, with
no wait for the service to stop. At its own stop the service stops its containers
not yet gone once more (`service_close`) and settles those gone.

O0c M1, M3, M4, M7: every way a run ends gives back every host slot it took. An
execution that provably never launched is clean once its worker returns, so the
normal release frees it; a preparation whose removal did not finish at retirement
is given back and its removal retried. What nothing here can free (a run failed
after its claim was admitted, a child whose cleanup is unconfirmed, a removal or a
monitor slot given up) keeps its slot, since a process may live, and is counted
(`stuck_slots`, shown in learning-status): the host is then replaced once idle, at
once if no slot is left, and the stopping service settles those runs on the
kernel's evidence, or the next one does.

D1 (RS-D D5): nothing a later record, verifier or QA needs is kept here. The run's record context (its assignment,
task, route, the client capability it was prepared with and its host class) is stored with its lease by the project
writer, and the service's authorizers read it back from the store; so a successor service of the same runtime records
a result a stopped one left on disk, and this service may stop when idle. `run.recorded` is only a hint.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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
from . import container_launch
from .container_recovery import ContainerRecovery
from .control_api import ControlScope
from .dispatch_api import dispatch_request_digest
from .execution_grant import ExecutionRegistrationInput, checked_execution_grant
from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, matches
from .host_open_contracts import OpenProvenanceInput
from .hosts import HOST_CLIENTS
from .launch_journal import JournalError, LaunchJournal, tree_state
from .launch_journal import entry_key as launch_journal_key
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
# D1: a released run's dispatch view stays answerable this long (or until its record), for the dispatching command
# that reads it once published. Its record, result read, verifier and QA no longer need the run here at all.
PUBLISHED_VIEW_SECONDS = 120
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
# Retired runs whose status this host still answers as not found (newest kept).
MAX_RETIRED_RUNS = 256
REGISTERED_IDLE_SECONDS = 3600  # Owner decision 90.7 (2026-11-23).
WITHDRAW_RETRY_SECONDS = 1.0  # also the first backoff of a withdrawal that failed (doubled each time)
WITHDRAW_BACKOFF_MAX_SECONDS = 300.0
FAILED_VIEW_SECONDS = 30.0  # a failed run is kept this long for a dispatch that has not yet asked
_RETRYING = object()


def _wall_ms():
    """Wall-clock milliseconds, the clock of a claim's `deadline_at_ms`."""
    return time.time_ns() // 1_000_000


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
# never forgotten, so its slot then stays taken, and the host is replaced once idle (O0c M7).
CLEANUP_RETRY_SECONDS = 1.0
CLEANUP_RETRY_MAX_SECONDS = 60.0
# After this many refused removals a given-back preparation is no longer asked: it
# stays listed (its workspace may still be there), so its place is never counted free.
MAX_CLEANUP_ATTEMPTS = 12
MAX_FORGET_ATTEMPTS = 12
# O10 D4: a result publication that failed while this service runs is retried from the packet
# the writer still holds, after this long, doubling up to the cap, until it commits or the
# run's own deadline (its claim's `deadline_at_ms`) passes. These refusals can never succeed
# on a retry of the same packet, so they stop the run at once.
PUBLISH_RETRY_SECONDS = 1.0
PUBLISH_RETRY_MAX_SECONDS = 60.0
_PUBLISH_REFUSALS = frozenset({'history_invalid_input', 'history_conflict', 'history_result_not_ready'})
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
    'result_capacity_full', 'result_capacity_busy',
    # O0c M1: its child's cleanup could not be confirmed; its slot stays held until its service is replaced.
    'run_cleanup_unconfirmed',
}) | PREPARATION_STAGE_REASONS
_REGISTER_FIELDS = {'purpose', 'orchestration_handle', 'run_id', 'host_client', 'assignment', 'task', 'route',
                    'parent_grant'}
_PREPARE_FIELDS = {'purpose', 'expected_export_commit'}
_ACTIVE = frozenset({'recording_registration', 'reading', 'preparing', 'observing', 'leasing', 'arming', 'recording_open', 'claiming', 'binding',
                     'registering', 'provenance', 'consuming', 'closing', 'settling', 'withdrawing', 'fencing'})
# A run that can never launch closes itself through these, then ends 'failed' and closed.
_CLOSING = frozenset({'closing', 'settling', 'withdrawing', 'fencing'})
# O10 RC-1: the store's capacity refusals, as the run's failure reasons.
_CAPACITY_REASONS = {'history_result_capacity': 'result_capacity_full',
                     'history_result_capacity_busy': 'result_capacity_busy'}
_AFTER = frozenset({'ready', 'reporting', 'terminal', 'acknowledging', 'cleanup', 'releasing', 'retiring', 'released',
                    'cleanup_unconfirmed'})
# O0c M1: a recorded result whose child's cleanup can never be confirmed in this service. It keeps every
# slot (a child may still be alive) and is no longer under way, so the host can be replaced once idle;
# the stopping service settles it on the kernel's evidence, or the next one does.
_HELD = frozenset({'cleanup_unconfirmed'})


@dataclass
class _Run:
    key: tuple
    scope: OrchestrationScope
    host_client: str
    prospective: OpenScopeContext
    run_id: str
    inputs_digest: str
    state: str = 'registered'
    opened_at: float = 0.0
    open_record: object = None
    open_mark: object = None
    withdraw_reason: str | None = None
    withdrawal_fallback: object = None
    withdrawal_attempted: bool = False
    failed_viewed: bool = False  # a dispatch has been told it failed
    duty_done_at: float | None = None
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
    publish_attempts: int = 0  # Failed publications of the run's result retried (O10 D4).
    publish_at: float | None = None  # When the failed publication is retried.
    launch_token: str | None = None  # C2: its container intent's token (containers switched on only).
    # C3: a failed admitted run's own settlement once its container is gone: the account call under
    # way, whether it is done, and the refused ones (with when it is asked again).
    own_settle: object = None
    own_settled: bool = False
    own_settle_failures: int = 0
    own_settle_at: float | None = None


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
    # C2: the container launcher; None while containers are switched off (`container_launch.SETTINGS`).
    _containers = None
    # C4: the recovery of earlier instances' container entries, made on its first use (containers only).
    _recovery = None
    # O0c M7: retired runs' monitor attempts the monitor still refused after MAX_FORGET_ATTEMPTS
    # (rebound, never mutated, so the class default stays empty).
    _stuck_monitor = frozenset()

    def __init__(self, state, project_root, bundle_root, scope, runtime, *, clock=time.monotonic, history_endpoint=None):
        self._state, self._root, self._bundle = state, Path(project_root), Path(bundle_root)
        # This host's own project API, read from the helper thread for a verifier's worker result.
        self._history_endpoint = None if history_endpoint is None else Path(history_endpoint)
        self._scope, self._runtime, self._clock = scope, runtime, clock
        self._runs, self._closed, self._intake = {}, False, []
        # Preparations of runs that failed or were abandoned before launch, until their slot is free;
        # and, for one whose cleanup did not finish, (attempts, when it is tried again).
        self._giving_back, self._cleanup_retry = [], {}
        # Released and retired runs: key -> (host class, orchestration handle); and the
        # bindings whose monitor slots are still to be freed: binding -> (refusals, next try).
        self._retired, self._forgetting = {}, {}
        self._withdraw_request = self._withdraw_future = self._withdraw_entry = None
        self._withdraw_phase, self._next_withdraw, self._withdraw_queue = 'start', 0.0, []
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
        # C2: None while containers are switched off (`container_launch.SETTINGS`, as shipped).
        self._containers = container_launch.launcher_for(scope.project_id, self._journal)
        runtime.pool._registry.container_launch = self._containers
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
                run.withdraw_reason = 'cancelled_before_claim'
                self._fail(run, 'run_abandoned')
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
        """The opening primary reports its result admitted: a hint only (D1).

        Only after its own successful admission does the CLI say so. It lets a
        released run retire at once instead of keeping its dispatch view for
        PUBLISHED_VIEW_SECONDS; a false report can only shorten that, for that
        host class's own run. The outcome's association with the run is written
        by the admission itself, in the project store, so nothing depends on it.
        """
        run = self._runs.get(key)
        if run is None:
            raise HistoryError('history_orchestration_unavailable')
        if run.host_client != host_client:
            raise HistoryError('history_not_authorized')
        if run.inputs_digest != inputs_digest:
            raise HistoryError('history_conflict')
        if value and (value.keys() != {'purpose', 'host_outcome_digest'} or value['purpose'] != 'recorded'
                      or not matches(value['host_outcome_digest'], _DIGEST)):
            raise HistoryError('history_invalid_input')
        run.result_admitted = True

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
        # O0c M11: no dispatch slot left in this host's lifetime: refuse before any claim (exhausted() has the
        # host replaced), never admit a run that would meet dispatch_full.
        if len(self._runs) >= MAX_RUNS or len(self._dispatch._jobs) >= DISPATCH_SLOTS:
            raise HistoryError('history_service_unavailable')
        # D1: a verifier's worker is read from the project store when the run begins (`_begin`), checked there;
        # its open was already authorized against that same stored worker.
        prospective = OpenScopeContext(self._scope, value['orchestration_handle'], value['assignment'], value['task'],
                                       value['route'], value['parent_grant'], None)
        prospective.snapshot()
        run = self._runs[key] = _Run(key, self._scope, host_client, prospective, value['run_id'], inputs_digest)
        run.opened_at = self._clock()
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
        lane = self._runtime.persistence
        try:
            if run.open_record is None:
                run.open_record = lane.open_binding('record_open_binding', run.key[0], run.key[1], run.run_id,
                    run.inputs_digest, run.host_client, instance=lane._service_instance, now_ms=_wall_ms())
            if run.open_record.state == 'queued':
                return True
            if run.open_record.state != 'done':
                if run.open_record.reason == 'history_conflict':
                    # Tombstone/conflict: the first record owns this duty; never withdraw it from memory.
                    run.withdrawal_attempted = True
                return False
            run.reporting = lane.report_registered_run(
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
            run.withdraw_reason = 'refused_before_claim'
            self._fail(run, 'run_failed')  # Registration refused; the open's duty already committed.
            return
        self._begin(run)

    def _begin(self, run):
        digest = run.prospective.task.get('verification_for_outcome') if type(run.prospective.task) is dict else None
        if digest is None:
            self._start_preparation(run, None)
            return
        # A verifier's worker and its result are read first, off the owner thread, from the project store.
        orchestration, host = run.prospective.orchestration_handle, run.host_client

        def read():
            return self._read_worker_result(orchestration, digest, host)

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

    def _read_worker_result(self, orchestration_handle, digest, host_client):
        """Helper thread: the worker's stored originals, terminal and result from this host's own APIs, checked.

        D1: the originals come from the project store (the worker's stored record context, found by its outcome
        digest), so a verifier of a worker another service instance recorded is prepared the same way.
        """
        from .history_api import HistoryClient
        from .host_result_contracts import HostResultContext
        from .result_contracts import MAX_RESULT_BODY_BYTES, checked_result_body
        from .result_read_api import ResultReadClient
        self._call.role, self._call.host_client, self._call.context = 'host', host_client, None
        try:
            endpoint, scope = self._history_endpoint, self._scope
            history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=10)
            current = history.worker_input(digest)
            original = current['worker']
            worker = HostResultContext(**{**original, 'binding': AttemptBinding(**original['binding'])})
            if (worker.snapshot()[1] != original or not scope.matches_attempt(worker.binding)
                    or worker.binding.orchestration_handle != orchestration_handle):
                raise HistoryError('history_conflict')
            reference = current['reference']
            terminal = history.read_record(reference['terminal_record_id'])['record']
            body = ResultReadClient(endpoint, timeout=10).read_result(worker.binding, reference['result_digest'])
            checked_result_body(body, worker.assignment, terminal)
            return worker, parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)
        finally:
            self._call.role = self._call.host_client = self._call.context = None

    @staticmethod
    def _view(run):
        run.failed_viewed = run.failed_viewed or run.state == 'failed'
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
                and run.reporting.state in {'done', 'queued'}):
            # S1-R6: the host open is recorded just before the claim is sent, so this run (most likely)
            # never had one; the store checks its own open rows (R6-10). Reporting only: the closing
            # below is unchanged.
            self._report('failed_before_open', run.key[0], run.key[1],
                         reason if reason in FAILED_BEFORE_OPEN_REASONS else 'run_failed',
                         after=run.reporting)  # sent once its registration commits (round-4 verifier)
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

    def _retry_publication(self, run, reason):
        """A failed publication of the run's result is published again from the packet the writer kept.

        The child is never launched again: only the writer's own publication step is queued again, and
        the history returns the stored delivery for a result it already committed, so there is one
        terminal, outbox and usage record. The run stops when its deadline passes or the refusal can
        never succeed, keeping what the account holds.
        """
        now = self._clock()
        if reason in _PUBLISH_REFUSALS or _wall_ms() >= run.receipt['deadline_at_ms']:
            run.publish_at = None
            self._fail(run, 'run_failed')
            return
        if run.publish_at is None:
            run.publish_attempts += 1
            run.publish_at = now + min(PUBLISH_RETRY_MAX_SECONDS,
                                       PUBLISH_RETRY_SECONDS * 2 ** (run.publish_attempts - 1))
            return
        if now < run.publish_at:
            return
        run.publish_at = None
        try:
            self._runtime.persistence.retry(run.persistence)
        except HistoryError:
            pass  # Still failed: the next step waits longer and asks again.

    def _writer_stopped(self, run):
        """O10 D12: the writer faulted while it held this run's step, so the lane says `unknown`.

        A faulted lane refuses every later call for good (its writer, the history key and the
        history endpoint are gone with it), so no retry can commit, attest or acknowledge anything
        in this service: whether the result committed can only be settled from the store by a
        later service. The run ends now, visibly (`run_unavailable` in its status), instead of
        waiting for ever. It was admitted, so it keeps what the account holds: its lease,
        preparation and journal entry stay, and the store keeps whatever it committed, so nothing
        is published twice or lost; the account's own timed rule, or a later service's lost-run
        settlement, settles its capacity.
        """
        run.publish_at = None
        self._fail(run, 'run_unavailable')

    def _close(self, run):
        """The lease is expired in the history: drop its input, then give the preparation back."""
        if run.receipt is not None or run.binding is not None:
            raise HistoryError('history_conflict')  # An admitted claim keeps everything until its release.
        self._runtime.persistence.retire_denied(run.persistence)
        run.persistence, run.state = None, 'failed'
        if run.launch_token is not None:
            try:
                # Nothing of it was admitted, and now nothing can be: its intent is done with.
                self._journal.remove(run.launch_token)
            except JournalError:
                pass  # Left for the container's recovery, which finds it never admitted.
        self._give_back(run)
        run.closed = run.prepared is None

    def _bind_claim(self, run, receipt):
        """The account admitted the run's claim: record it in the journal, bind it, go on towards launch.

        The journal entry is written before anything can launch the run; if it
        cannot be written the run stops here, keeping what the account holds.
        With containers, the admission is recorded on the run's intent, written
        before its claim was sent.
        """
        run.receipt = receipt
        if run.launch_token is not None:
            self._journal.admitted(run.launch_token, receipt['bridge_run_id'], receipt['deadline_at_ms'])
        else:
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

    @staticmethod
    def _record_inputs(run):
        """D1: what the project writer stores with the run's lease besides its own input: the host class that
        opened it, and the client capability its result is admitted against (from this runtime, as the CLI reads
        it), kept as it was even if the installed one changes later."""
        from .learning_adapters import load_client_capabilities

        capability = load_client_capabilities(
            RUNTIME_ROOT / '.codex' / 'orchestration' / 'config' / 'learning-clients.json')['bridge-v1']
        return {'host_class': run.host_client, 'client_capability': capability}

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
        self._settle_own_ended()
        self._fence_earlier()
        self._expire_results()
        self._send_reports()
        self._withdraw_opens()
        for run in list(self._runs.values()):
            try:
                self._advance(run)
                self._retire_unclaimed(run)
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

    def _withdraw_call(self, value, host_client, request_id):
        from .open_withdrawal import withdrawal_custody
        self._call.role, self._call.host_client, self._call.context = 'project', host_client, withdrawal_custody(value)
        try:
            return AccountClient(self._account, self._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                 timeout=ACCOUNT_TIMEOUT_SECONDS).withdraw_open(value, request_id=request_id)
        finally:
            self._call.role = self._call.host_client = self._call.context = None

    def _retire_unclaimed(self, run):
        """A failed, never-armed run leaves memory only after its duty is durable."""
        if (run.state != 'failed' or run.persistence is not None or run.receipt is not None
                or run.binding is not None or run.claim_sent or run.future is not None
                or run.waiters):  # a waiting dispatch is answered with the failure first
            return
        lane = self._runtime.persistence
        reason = run.withdraw_reason or 'failed_before_claim'
        if run.open_record is None or run.open_record.state == 'queued':
            if run.open_record is None and self._submit_registration(run):
                return
            if run.open_record is not None and run.open_record.state == 'queued':
                return
        if run.open_record is not None and run.open_record.state == 'done':
            try:
                if run.open_mark is None:
                    run.open_mark = lane.open_binding('mark_open_withdrawing', *run.key, reason,
                        project_id=self._scope.project_id, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
                if run.open_mark.state == 'queued':
                    return
                if run.open_mark.state == 'done':
                    self._release_failed(run)
                    return
                if run.open_mark.reason == 'history_conflict':
                    return  # Armed or already ended: do not invent a withdrawal.
                run.open_mark = None
            except HistoryError as error:
                if error.reason_code == 'history_writer_busy':
                    return
        # §7 bounded residual: a stopped/faulted writer permits exactly one memory send.
        if not run.withdrawal_attempted:
            from .open_withdrawal import WithdrawOpenInput, withdrawal_request_id
            value = WithdrawOpenInput(run.key[0], run.key[1], run.run_id, run.inputs_digest, 1, reason)
            run.withdrawal_attempted = True
            run.withdrawal_fallback = self._executor.submit(self._withdraw_call, value, run.host_client,
                                                           withdrawal_request_id(*run.key))
        if run.withdrawal_fallback is not None and not run.withdrawal_fallback.done():
            return
        self._release_failed(run)

    def _release_failed(self, run):
        """The duty is durable (or sent once): give the slot back, and leave the run once it is no longer owed.

        A run nobody can still ask about (idle, or cancelled by its own host) leaves at once; a failed one stays until
        a dispatch has been told, or for a bounded time, so a late prepare reads its failure."""
        self._give_back(run)
        now = self._clock()
        if run.duty_done_at is None:
            run.duty_done_at = now
        if (run.withdraw_reason in {'registered_idle', 'cancelled_before_claim'} or run.failed_viewed
                or now - run.duty_done_at >= FAILED_VIEW_SECONDS):
            run.state = 'released'

    def _withdraw_opens(self):
        """One bounded writer batch and account call at a time; retries use the stored envelope.

        Each due row is tried in turn. A row that fails is deferred with its own backoff (persisted on the row) and
        the next due row goes on, so one refused row never keeps the others waiting. Only the account's own
        `withdraw_stale` is final; any other failure leaves the duty for a later try."""
        from .open_withdrawal import decode_withdraw_open, checked_withdraw_reply
        lane, now = self._runtime.persistence, self._clock()
        if now < self._next_withdraw:
            return
        try:
            if self._withdraw_future is not None:
                if not self._withdraw_future.done():
                    return
                future, self._withdraw_future, entry = self._withdraw_future, None, self._withdraw_entry
                value = decode_withdraw_open(json.loads(entry['evidence'])['payload'])
                try:
                    answer = checked_withdraw_reply(future.result(), value)
                    kind, receipt = 'withdrawn', answer['receipt']
                except AccountError as error:
                    if error.reason_code != 'withdraw_stale':
                        raise  # claim_exists/unavailable/not authorized: held for a later try or the fence path.
                    kind, receipt = 'account_final', None
                self._withdraw_phase = 'released'
                self._withdraw_request = lane.open_binding('release_open_binding', value.run_handle, value.attempt,
                    kind, receipt=receipt, request_digest=entry['request_digest'])
            request = self._withdraw_request
            if request is not None:
                if request.state == 'queued':
                    return
                self._withdraw_request = None
                phase = self._withdraw_phase
                if request.state != 'done' and phase != 'deferred':
                    raise HistoryError(request.reason)
                if phase == 'start':
                    self._withdraw_phase = 'list'
                    self._withdraw_request = lane.open_binding('open_withdrawals', now_ms=_wall_ms())
                    return
                if phase == 'list':
                    self._withdraw_queue = list(request.result or ())
                if self._withdraw_queue:
                    self._send_withdrawal(self._withdraw_queue.pop(0))
                    return
                self._withdraw_phase = 'start'
                self._next_withdraw = now + WITHDRAW_RETRY_SECONDS
                return
            self._withdraw_request = lane.open_binding('withdraw_earlier_open_bindings', lane._service_instance,
                project_id=self._scope.project_id, runtime_digest=COLLECTOR_RUNTIME_DIGEST)
            self._withdraw_phase = 'start'
        except Exception:
            failed = self._withdraw_entry if self._withdraw_phase in {'sending', 'released'} else None
            self._withdraw_request = self._withdraw_future = self._withdraw_entry = None
            self._withdraw_phase = 'start'
            if failed is not None:
                self._defer_withdrawal(failed)
            else:
                self._withdraw_queue = []
                self._next_withdraw = now + WITHDRAW_RETRY_SECONDS

    def _send_withdrawal(self, entry):
        """Recheck one row's complete persisted envelope, then send it; a row that fails is deferred."""
        from .open_withdrawal import decode_withdraw_open
        self._withdraw_entry, self._withdraw_phase = entry, 'sending'
        try:
            evidence = json.loads(entry['evidence'])
            value = decode_withdraw_open(evidence['payload'])
            envelope = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.withdraw_open', entry['request_id'],
                                      evidence['project_id'], evidence['runtime_digest'], value.payload())
            if (envelope.project_id != self._scope.project_id or envelope.runtime_digest != COLLECTOR_RUNTIME_DIGEST
                    or account_request_digest(envelope) != entry['request_digest']):
                raise HistoryError('history_conflict')
            self._withdraw_future = self._executor.submit(self._withdraw_call, value, evidence['host_client'],
                                                          entry['request_id'])
        except Exception:
            self._withdraw_entry = None
            self._defer_withdrawal(entry)

    def _defer_withdrawal(self, entry):
        """Persist the row's next try (doubling backoff) and go on with the rest of the batch."""
        self._withdraw_entry, self._withdraw_phase = None, 'deferred'
        try:
            self._withdraw_request = self._runtime.persistence.open_binding(
                'defer_open_withdrawal', entry['identity'], now_ms=_wall_ms(),
                base_ms=int(WITHDRAW_RETRY_SECONDS * 1000) or 1, max_ms=int(WITHDRAW_BACKOFF_MAX_SECONDS * 1000))
        except Exception:
            self._withdraw_request, self._withdraw_phase = None, 'start'
            self._withdraw_queue = []
            self._next_withdraw = self._clock() + WITHDRAW_RETRY_SECONDS

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
            if run.reporting is None and not self._submit_registration(run):
                run.withdraw_reason = 'refused_before_claim'
                self._fail(run, 'run_failed')
            elif run.reporting is not None and run.reporting.state == 'failed':
                run.withdraw_reason = 'refused_before_claim'
                self._fail(run, 'run_failed')
            elif self._clock() - run.opened_at >= REGISTERED_IDLE_SECONDS:
                run.withdraw_reason = 'registered_idle'
                self._fail(run, 'run_abandoned')
        elif run.state == 'recording_registration':
            self._after_registration(run)
        elif run.state == 'reading':
            read = self._result(run)
            if read is not None:
                worker, worker_result = read
                run.prospective = replace(run.prospective, worker=worker)
                run.prospective.snapshot()
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
                # D1 (RS-D-1): the record context is stored with the lease, before it can be armed.
                run.persistence = lane.prepare_lease(run.lease, run.prepared_inputs.worker,
                                                     record_context=self._record_inputs(run))
            except HistoryError as error:
                if error.reason_code not in _CAPACITY_REASONS:
                    raise
                self._fail(run, _CAPACITY_REASONS[error.reason_code])  # 0.3.4 R3, O10 RC-1: named
                return
            run.state = 'leasing'
        elif run.state == 'leasing':
            snapshot = lane.snapshot(run.persistence)
            state = snapshot['state']
            if state == 'failed' and snapshot['reason_code'] in _CAPACITY_REASONS:
                self._fail(run, _CAPACITY_REASONS[snapshot['reason_code']])  # 0.3.4 R3, O10 RC-1
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
            if self._containers is not None and run.launch_token is None:
                # C2: no intent, no claim. The run's container is on disk before the account is asked.
                try:
                    run.launch_token = self._containers.intent(
                        run_handle=run.key[0], attempt=run.key[1], host_client=run.host_client,
                        lease={n: getattr(run.lease, n) for n in run.lease.__dataclass_fields__},
                        identity_digest=run.identity_digest, claim_request_id=run.claim_request_id,
                        storage_evidence=run.evidence, workspace=run.prepared_inputs.workspace)
                except Exception:
                    self._fail(run, 'run_unavailable')
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
            snapshot = lane.snapshot(run.persistence)
            if snapshot['state'] == 'unknown':
                self._writer_stopped(run)
                return
            if snapshot['state'] == 'failed':
                self._retry_publication(run, snapshot['reason_code'])
                return
            if snapshot['state'] != 'committed':
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
                elif lane.snapshot(run.persistence)['state'] == 'unknown':
                    self._writer_stopped(run)
                return
            try:
                lane.retain_cleanup(run.persistence)
            except HistoryError:
                if self._cleanup_unconfirmed(run):
                    run.state, run.reason = 'cleanup_unconfirmed', 'run_cleanup_unconfirmed'
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
                    self._journal.remove(run.launch_token or run.receipt['bridge_run_id'])
                except JournalError:
                    pass  # A later settlement finds it released and keeps only a tombstone.
            if not run.result_admitted and self._clock() - run.released_at < PUBLISHED_VIEW_SECONDS:
                return  # The capacity is released; its dispatch view stays a while for the dispatching command.
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
                else:
                    # O0c M4: its workspace removal did not finish. It is given back like any other
                    # preparation: its removal is asked again and its slot retired once clean.
                    self._giving_back.append(run.prepared)
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
                        # O0c M7: its slot stays taken; the host is replaced once idle.
                        self._stuck_monitor = self._stuck_monitor | {binding}
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

    def _settle_own_ended(self):
        """Owner step (C3, RS-C-7): settle this service's own failed runs whose container is gone.

        Only the owner thread chooses between a run's normal release and this: a run is eligible
        only once it failed with an admitted claim (so it never enters the normal release) and no
        account call of it is outstanding. Its container entry must say `gone`, which only the
        OS's answer writes; from then on nothing can launch it again (a container starts only from
        an admitted `intent`). The account settles it as lost (`settle_lost_run`, on the sweep's
        helper thread), the entry becomes `settled` and the run's preparation is given back: the
        account no longer holds it. A refused or unreachable settlement is asked again later.
        """
        now = self._clock()
        for run in self._runs.values():
            if (run.own_settled or run.launch_token is None or run.receipt is None or run.state != 'failed'
                    or run.future is not None):
                continue
            if run.own_settle is not None:
                if not run.own_settle.done():
                    continue
                future, run.own_settle = run.own_settle, None
                try:
                    reply = future.result()
                except Exception:
                    run.own_settle_failures += 1
                    run.own_settle_at = now + container_launch.stop_retry_delay(run.own_settle_failures)
                    continue
                try:
                    self._journal.settled(run.launch_token, reply['state'])
                except JournalError:
                    pass  # The account settled it; a later service asks again and gets the same answer.
                self._settled_own(run)
                continue
            if run.own_settle_at is not None and now < run.own_settle_at:
                continue
            try:
                entry = self._journal.entry(run.launch_token)
            except JournalError:
                continue
            if entry is None or entry['state'] == 'settled':
                if entry is not None:
                    self._settled_own(run)
                continue
            if entry['state'] != 'gone' or entry['instance'] != self._journal.instance:
                continue
            run.own_settle = self._sweeper.submit(self._settle_entry, entry)

    def _settled_own(self, run):
        """The account no longer holds the run: its preparation goes back, as a run never admitted's does."""
        run.own_settled, run.own_settle_at = True, None
        if run.prepared is None:
            return
        try:
            self._preparation.discard(run.prepared)
        except Exception:
            return  # Bound to an execution, or the lane is gone: it stays.
        self._giving_back.append(run.prepared)
        run.prepared = None

    def _settle_lost(self, own):
        """Settle every run of a stopped instance whose whole tree is provably gone; return what is known.

        Only an instance whose lock is free can have no thread left that might
        launch or own its child; with `own`, this instance's threads have
        stopped too. Nothing is ever signalled; an entry that cannot be read is
        left as it is, and its capacity to the account's timed rule.

        C3: the live sweep first retries the container stops that did not prove the container
        gone. A container entry of this instance is settled here only when `own` (its service is
        stopping): one not yet gone is stopped once more (`service_close`) and settled once the OS
        says it is gone. C4: the live sweep hands every container entry of a stopped instance, and
        each one this instance took over from one, to the container recovery (`container_recovery`),
        which also stops orphan containers; at this service's own stop only the ones it took over,
        and no orphan sweep.
        """
        if self._containers is not None and not own:
            try:
                self._containers.retry_stops()
            except Exception:
                pass  # Asked again on the next sweep.
        recovery = self._container_recovery()
        journal, lost, stopped, recovering = self._journal, {}, {}, []
        for entry in journal.entries():
            if entry['state'] is None:
                continue
            handle, host_client = entry['run_handle'], entry['host_client']
            if entry['state'] == 'settled':
                lost[handle] = (host_client, 'released')
                continue
            instance = entry['instance']
            adopted = entry.get('version') == 2 and recovery is not None and recovery.owns(entry['token'])
            if adopted:
                pass
            elif instance == journal.instance:
                if not own:
                    continue
            elif stopped.setdefault(instance, journal.instance_stopped(instance)) is not True:
                continue
            lost[handle] = (host_client, 'held')
            if entry.get('version') == 2:
                if adopted or (instance != journal.instance and not own):
                    if recovery is not None:
                        recovering.append(entry)
                    continue
                if instance != journal.instance or not self._own_container_gone(entry):
                    continue
            elif tree_state(entry) != 'gone':
                continue
            try:
                reply = self._settle_entry(entry)
            except Exception:
                continue  # The account is unreachable or refused: asked again on the next sweep.
            self._remove_workspace(entry['workspace'])
            try:
                journal.settled(launch_journal_key(entry), reply['state'])
            except JournalError:
                continue
            lost[handle] = (host_client, 'released')
        if recovery is not None:
            try:
                recovered = recovery.recover(recovering, orphans=not own)
            except Exception:
                recovered = {}  # Asked again on the next sweep.
            for entry in recovering:
                if entry['token'] in recovered:
                    lost[entry['run_handle']] = (entry['host_client'], 'released')
        journal.prune()
        for instance in journal.instances():
            if instance != journal.instance:
                journal.forget_instance(instance)
        return lost

    def _container_recovery(self):
        """C4: this service's container recovery, or None while containers are switched off."""
        if self._containers is None:
            return None
        if self._recovery is None:
            self._recovery = ContainerRecovery(
                self._containers, self._journal, fence=lambda entry: self._fence_entry(entry),
                settle=lambda entry: self._settle_entry(entry),
                remove_workspace=lambda workspace: self._remove_workspace(workspace), clock=self._clock)
        return self._recovery

    def _own_container_gone(self, entry):
        """C3, at this service's own stop: whether its run's container is gone, stopping it once more
        (`service_close`, at once) when it was started and not yet proven gone."""
        if entry['state'] == 'gone':
            return True
        if (entry['state'] not in {'started', 'stopping', 'stop_failed'} or self._containers is None
                or entry['container_id'] is None):
            return False  # Never started (an intent): its recovery fences the claim (C4).
        started = container_launch.StartedContainer(entry['token'], entry['container_name'], entry['container_id'],
                                                    entry['cgroup_path'] or '')
        try:
            return self._containers.stop(started, 'service_close', now=True) is True
        except Exception:
            return False

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

    def _fence_entry(self, entry):
        """Helper thread (C4): the account's fence for a recovered intent's own claim request, with the run's
        custody: it closes the request for good, or returns the receipt of the claim it admitted after all."""
        lease = ResultLeaseBinding(**entry['lease'])
        if lease.project_id != self._scope.project_id or entry['host_client'] not in HOST_CLIENTS:
            raise HistoryError('history_conflict')
        evidence = bytes.fromhex(entry['storage_evidence'])
        fence = FenceClaimInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                parse_bounded_json(evidence)['lease']['lease_digest'])
        custody = canonical_json({'purpose': 'run', 'lease': entry['lease'], 'storage_evidence': entry['storage_evidence'],
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        call = self._call
        call.role, call.host_client, call.context = 'project', entry['host_client'], custody
        try:
            client = AccountClient(self._account, self._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                   timeout=SETTLE_TIMEOUT_SECONDS)
            return client.fence_claim(
                fence, request_id='recover-' + hashlib.sha256(entry['token'].encode()).hexdigest()[:32])
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

    def settle_on_exit(self, *, settle=True):
        """After every thread of this host stopped: settle its own runs, then let later hosts settle the rest.

        An in-flight sweep and account call are waited for first, so nothing of
        this host writes its journal or reaches the account after its lock is gone.
        Without `settle` (O10 D13: its result writer faulted) it settles nothing and
        only frees its journal lock, so the next host settles its runs.
        """
        self._sweeper.shutdown(wait=True)
        self._executor.shutdown(wait=True)
        if not settle:
            if self._containers is not None:
                self._containers.close()
            self._journal.release()
            return
        install_enrollment(self._preamble, owner=self._owner_name)
        try:
            self._settle_lost(True)
        except Exception:
            pass
        finally:
            install_enrollment(None, owner=self._owner_name)
            if self._containers is not None:
                self._containers.close()
        self._journal.release()

    @staticmethod
    def _id(run):
        return hashlib.sha256(canonical_json([run.key[0], run.key[1], run.inputs_digest])).hexdigest()[:32]

    def busy(self):
        return any(run.state not in {'registered', 'failed', 'released'} | _HELD for run in self._runs.values())

    def _cleanup_unconfirmed(self, run):
        """O0c M1: whether the run's execution can never be confirmed clean here: its worker returned
        and left the execution unknown, which nothing in this service asks again (process mode; a
        container's later proof of its end is C3's, so a container run keeps waiting)."""
        if run.launch_token is not None or run.prepared_inputs is None:
            return False
        try:
            supervisor = self._runtime.pool.supervisor(run.prepared_inputs.worker)
            view = None if supervisor is None else supervisor.snapshot()
        except Exception:
            return False
        return view is not None and view['state'] == 'unknown' and view['worker_returned'] is True

    def _stranded(self, run):
        """O0c M1, M3: a run that holds a host slot nothing in this service will free.

        A failed run that keeps its preparation or its result entry and waits for no answer (its claim
        was admitted, or its fence was given up), or one whose child's cleanup is unconfirmed. A
        container run is C3's to settle first (`_settle_own_ended`); once settled it counts here too.
        """
        if run.state in _HELD:
            return True
        if (run.state != 'failed' or run.closed or run.future is not None or run.fence_at is not None
                or (run.launch_token is not None and not run.own_settled)):
            return False
        return run.persistence is not None or run.prepared is not None

    def stuck_slots(self):
        """O0c M1, M3, M4, M7: host slots held by what nothing in this service will free: stranded runs,
        given-back preparations whose removal was asked MAX_CLEANUP_ATTEMPTS times, and monitor attempts
        given up. Each is held until the host is replaced: never freed while a process may live."""
        preparations = sum(1 for handle in self._giving_back
                           if self._cleanup_retry.get(handle, (0, 0.0))[0] >= MAX_CLEANUP_ATTEMPTS)
        return sum(1 for run in self._runs.values() if self._stranded(run)) + preparations + len(self._stuck_monitor)

    def replace_when_idle(self):
        """O0c M3: something here holds a slot it will never free, so a fresh host replaces this one
        once no run is under way and no client has called for the idle period."""
        return not self.busy() and self.stuck_slots() > 0

    def exhausted(self):
        """No room for another run in this host's lifetime; a fresh host has it all."""
        # O0c M3: with every run slot stuck nothing can run here, so it is replaced at once when idle.
        return not self.busy() and (len(self._runs) >= MAX_RUNS or len(self._dispatch._jobs) >= DISPATCH_SLOTS
                                    or self.stuck_slots() >= PREPARATION_SLOTS)

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
