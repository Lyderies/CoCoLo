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
after its claim was admitted, a child whose cleanup is unconfirmed, or a
monitor slot given up) keeps its slot, since a process may live, and is counted
(`stuck_slots`, shown in learning-status): the host is then replaced once idle, at
once if no slot is left, and the stopping service settles those runs on the
kernel's evidence, or the next one does. Decision 309: pending preparation folder
cleanup keeps retrying with capped backoff and defers automatic replacement until
observed removal and retirement, retaining its slot and memory holds meanwhile.

D1 (RS-D D5): nothing a later record, verifier or QA needs is kept here. The run's record context (its assignment,
task, route, the client capability it was prepared with and its host class) is stored with its lease by the project
writer, and the service's authorizers read it back from the store; so a successor service of the same runtime records
a result a stopped one left on disk, and this service may stop when idle. `run.recorded` is only a hint.
"""
from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from threading import Lock, local
import time
import types

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
from . import codex_preparation, container_launch, health_resources, user_settings
from .container_recovery import ContainerRecovery
from .recovery_report import EXPIRY_SECONDS, RECOVERY_REASONS
from .control_api import ControlScope
from .dispatch_api import dispatch_request_digest
from .execution_grant import ExecutionRegistrationInput, checked_execution_grant
from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, matches
from .project_holds_schema import attempt_identity
from .host_open_contracts import OpenProvenanceInput
from .hosts import HOST_CLIENTS
from .launch_journal import JournalError, LaunchJournal, new_process_token, tree_state
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
# UH-3 (decision 367): runs this service learnt the takeover left unverified; the durable record stays the authority.
MAX_UNVERIFIED_KNOWN = 64
# B4s confirming check: `settled` journal entries checked against the project store, remembered at most this many
# (past it only those of the current pass stay; a forgotten one is checked once more, harmlessly).
MAX_SETTLED_CHECKED = 1024


def _backoff(base, cap, attempts):
    """`base` doubled per attempt up to `cap`; the exponent is capped before multiplying (no float overflow)."""
    return min(cap, base * 2 ** min(max(0, attempts), 32))
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
# Decision 309: preparation removal keeps being retried while this host owns it.
# Save a capped delay, never an unbounded attempt count/exponent.
MAX_FORGET_ATTEMPTS = 12
# PREPCLEAN-4: recorded folder removals read from the store at one time (the capacity report's run-slot bound).
MAX_PENDING_REMOVALS = 64
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
_ACTIVE = frozenset({'recording_registration', 'reading', 'taking_preparation', 'preparing', 'observing', 'leasing', 'arming', 'recording_open', 'claiming', 'binding',
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
    hand_over_due: bool = False  # Exhausted local fences; retry the durable handoff until it commits.
    publish_attempts: int = 0  # Failed publications of the run's result retried (O10 D4).
    publish_at: float | None = None  # When the failed publication is retried.
    launch_token: str | None = None  # C2: its container intent's token (containers switched on only).
    intent_token: str | None = None  # B4s r1 HIGH 3: its process intent's token (process mode only).
    # C3: a failed admitted run's own settlement once its container is gone: the account call under
    # way, whether it is done, and the refused ones (with when it is asked again).
    own_settle: object = None
    own_settled: bool = False
    own_settle_failures: int = 0
    own_settle_at: float | None = None
    own_reply: dict | None = None  # B4s r1: the account's answer, kept until the workspace is confirmed gone
    own_workspace: str | None = None


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


# run.check (S10): may a new run open, asked before the account admits it.
_OPERATIONS = frozenset({'run.check', 'run.register', 'run.prepare', 'run.recorded', 'run.describe', 'run.abandon'})


class RunDriver:
    _memory = None
    # PREPCLEAN-4: read-only until __init__ gives each driver its own table of inherited folder removals.
    _removals = types.MappingProxyType({})
    _removal_pool = None
    # C2: the container launcher; None while containers are switched off (`container_launch.SETTINGS`).
    _containers = None
    # C4: the recovery of earlier instances' container entries, made on its first use (containers only).
    _recovery = None
    # B4s r1 (HIGH 3): `settled` journal entries checked against the project store (once each, by this service), and
    # those whose recovery failed (failures, next try), kept from the prune until recovered. Replaced, never mutated.
    _settled_checked = frozenset()
    _settled_retry = types.MappingProxyType({})
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
        # PREPCLEAN-4: folder removals an earlier service recorded and left to this one: identity -> [folder, pause,
        # deadline, running removal]; and the request that reads them, whether it ran, whether it filled its bound,
        # and the removals whose proof is still being recorded.
        self._removals, self._removals_asked, self._removals_loaded, self._removals_full = {}, None, False, False
        self._removal_proofs = {}
        self._removal_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='project-driver-removal')
        # Released and retired runs: key -> (host class, orchestration handle); and the
        # bindings whose monitor slots are still to be freed: binding -> (refusals, next try).
        self._retired, self._forgetting = {}, {}
        # RS-B-4 final fix (W5): failed runs memory released before their view window ended, by key:
        # (host class, inputs digest, failure reason, answered until). Bounded by MAX_RETIRED_RUNS.
        self._failed_views = {}
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
        # HM-2a: every preparation folder names this instance (whose lock it holds while it runs) as its owner.
        # Without the marker a leftover folder is only reported, never removed, so a refusal here costs nothing.
        with contextlib.suppress(PreparationError):
            self._preparation.set_owner(Path(state.root), scope.project_id, self._journal.instance)
        from .memory_holds import DriverMemory
        self._memory = DriverMemory(self)
        self._memory_start = runtime.persistence.configure_memory(self._journal)
        # RS-B-4 final fix: run.register requests that arrived while that start-up recovery is still queued,
        # answered once it is done (or failed): (connection, enrolled, request, context, answer-by time).
        self._register_waiting = []
        runtime.pool._registry.launch_journal = self._journal
        # C2: None while containers are switched off (`container_launch.SETTINGS`, as shipped).
        self._containers = container_launch.launcher_for(scope.project_id, self._journal)
        runtime.pool._registry.container_launch = self._containers
        # Runs of earlier service instances, by run handle: (host class, 'released' or 'held').
        self._lost, self._sweep, self._next_sweep = {}, None, 0.0
        # LRS-1: set once the first sweep was collected; until then the earlier runs are not known yet.
        self._swept = False
        # LRS-7: when start-up began, and the last failure code of each start-up check still failing.
        self._warm_started, self._warm_failures = clock(), {}
        self._expiry, self._next_expiry = None, 0.0  # The first cleanup runs at start (0.3.4 R2).
        # Leases an earlier service left armed with no admitted claim, fenced here (0.3.3, C3).
        self._earlier, self._earlier_fences, self._fenced = None, {}, {}
        # B4s r1 HIGH 3: the receipts of earlier leases the account admitted after all (`_fence_earlier`), by claim
        # request id; read by the sweep for the process intents it settles. Replaced whole, never mutated.
        self._earlier_admitted = {}
        # B4s confirming check (HIGH 3, second stop): outstanding process intents the sweep could not decide (no
        # decision of this service about their claim), by claim request id: (binding, evidence, host client); the
        # owner thread fences each once (`_fence_earlier`), with the identical request. Replaced whole by the sweep.
        self._intent_fences, self._intent_asked = {}, set()
        # S1-R6: reporting writes for the orchestration report, in order, each retried a few times.
        self._reports = []
        self._earlier_failures, self._earlier_at, self._earlier_listed = 0, 0.0, False
        self._sweeper = ThreadPoolExecutor(max_workers=1, thread_name_prefix='project-driver-sweep')
        self._exiting = False  # D4: set at this service's own exit, when its result writer has stopped
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
                    or request['protocol'] != 1 or request['operation'] not in _OPERATIONS
                    or not matches(request['run_handle'], _ID) or type(request['attempt']) is not int
                    or not matches(request['inputs_digest'], _DIGEST)):
                raise ValueError()
            context = parse_bounded_json(enrolled.context or b'{}', max_bytes=300_000)
        except Exception:
            connection.close()
            return
        if (request['operation'] == 'run.register' and self._memory is not None
                and self._memory_start.state not in {'done', 'failed'}):
            # RS-B-4 final fix: an open made just after the service started waits for the start-up memory
            # recovery instead of being refused (the CLI does not retry its register); bounded by WAIT_SECONDS,
            # the driver's bound on any waiting request, inside the CLI's own register timeout.
            if len(self._register_waiting) >= MAX_INTAKE:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_service_unavailable'})
            else:
                self._register_waiting.append((connection, enrolled, request, context, self._clock() + WAIT_SECONDS))
            return
        self._answer(connection, enrolled, request, context)

    def _answer(self, connection, enrolled, request, context):
        key = (request['run_handle'], request['attempt'])
        if request['operation'] == 'run.check':
            # S10 review r1: asked by the opening CLI before the account admits anything (no binding, provenance,
            # receipt or run state yet). Its run handle names the orchestration; the account gives the run's later.
            self._reply(connection, self._check_new_run(request['run_handle'], request['attempt'], request['inputs_digest'],
                                                        enrolled.host_client))
            return
        if request['operation'] == 'run.prepare':
            failed = self._late_failure(key, request['inputs_digest'], enrolled.host_client)
            if failed is not None:
                self._reply(connection, failed)
                return
        if request['operation'] in {'run.describe', 'run.abandon'}:
            # Commands that know only a run handle: which host class holds it, or
            # dropping one that never took a lease.
            held = [run for run in self._runs.values() if run.key[0] == request['run_handle']]
            if not held:
                self._reply(connection, self._unheld_reply(request['run_handle'], enrolled.host_client))
            elif held[0].host_client != enrolled.host_client:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_not_authorized'})
            elif request['operation'] == 'run.describe':
                self._reply(connection, self._view(held[0]))
            elif held[0].closed:
                # It already closed by itself; dropping it changes nothing anywhere.
                if self._memory is None:
                    del self._runs[held[0].key]
                elif held[0].claim_sent or held[0].duty_done_at is not None:
                    self._memory.drop(held[0])  # Cleanup and its durable release may still be pending.
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
                self._resource_preflight(key)
                self._register(key, request['inputs_digest'], context, enrolled.host_client)
            elif request['operation'] == 'run.prepare':
                self._prepare(key, request['inputs_digest'], context, enrolled.host_client)
            else:
                self._recorded(key, request['inputs_digest'], context, enrolled.host_client)
        except HistoryError as error:
            # S10: the one refusal that carries its plain reason (folder, free, needed, how to change the limit).
            extra = {'message': str(error)} if error.reason_code == 'resource_low' else {}
            self._reply(connection, {'state': 'refused', 'reason_code': error.reason_code, **extra})
            return
        run = self._runs[key]
        if request['operation'] != 'run.prepare' or run.state in _AFTER or run.state == 'failed':
            self._reply(connection, self._view(run))
        else:
            run.waiters.append((connection, self._clock() + WAIT_SECONDS))

    def _release_registers(self):
        """Owner step (RS-B-4 final fix): answer the registers waiting on the start-up memory recovery.

        Once it is done they register as any other; once it failed, `_register` answers as an unavailable
        service did before memory holds (refused history_service_unavailable) and asks for the recovery again.
        One still waiting at its bound is refused the same way."""
        if self._memory is None or not self._register_waiting:
            return
        waiting, now, self._register_waiting = self._register_waiting, self._clock(), []
        for item in waiting:
            connection, enrolled, request, context, until = item
            if self._memory_start.state in {'done', 'failed'}:
                self._answer(connection, enrolled, request, context)
            elif now >= until:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_service_unavailable'})
            else:
                self._register_waiting.append(item)

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

    def _check_new_run(self, orchestration_handle, attempt, inputs_digest, host_client):
        """S10 HM-6: may a new run be opened? An identical retry of a run this host registered is not probed again
        (the account replays its open); anything else is a new run and is refused on a low or unreadable disk.
        Identical means the identity admission uses (review r2c): orchestration, attempt, inputs and host class."""
        if any(run.inputs_digest == inputs_digest and run.host_client == host_client
               and run.prospective.orchestration_handle == orchestration_handle and run.key[1] == attempt
               for run in self._runs.values()):
            return {'state': 'ready', 'reason_code': None}
        stop = health_resources.refusal(self._root, self._state.root, user_settings.load(self._state.root))
        if stop is not None:
            return {'state': 'refused', 'reason_code': 'resource_low', 'message': stop.text}
        return {'state': 'ready', 'reason_code': None}

    def _resource_preflight(self, key):
        """S10 HM-6, the backstop to `run.check` (the disk can fill between the account's admission and this
        registration): a NEW run is refused, before any run state exists, when the workspace or state folder disk is low or
        unreadable. A retry of a known registration is not checked again. Its own code, `resource_low`; the plain
        reason (folder, free, needed, how to change it) is the message, and the health check shows it in status."""
        if key in self._runs:
            return
        stop = health_resources.refusal(self._root, self._state.root, user_settings.load(self._state.root))
        if stop is not None:
            raise HistoryError('resource_low', message=stop.text)

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
        if self._memory is not None and self._memory_start.state != 'done':
            if self._memory_start.state == 'failed':
                self._memory_start = self._runtime.persistence.memory_hold(
                    'recover', self._journal.directory, self._journal.instance)
            raise HistoryError('history_service_unavailable')
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
            run.failed_viewed = True
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
        if self._memory is not None:
            self._memory.prepare(run)
            return
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
            if self._memory is not None:
                run.hand_over_due = True
                self._memory.hand_over(run)
            else:
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
        self._forget_intent(run)
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
            self._forget_intent(run)  # Its `claimed` entry now holds everything the intent did.
        run.binding = AttemptBinding(**{n: getattr(run.lease, n) for n in LEASE_SCOPE_FIELDS},
                                     claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
        self._runtime.persistence.bind_claim(run.persistence, run.binding, hard_deadline_at_ms=receipt['deadline_at_ms'])
        run.state = 'binding'

    def _forget_intent(self, run):
        """B4s r1 HIGH 3: the run's process intent is done with (its admission is in its `claimed` entry, or its
        claim was closed unadmitted). One left behind is fenced by a later service, which finds that out."""
        if run.intent_token is None:
            return
        try:
            self._journal.remove(run.intent_token)
        except JournalError:
            return
        run.intent_token = None

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
                    if self._memory is not None:
                        self._memory.retired_preparation(handle)
                    self._giving_back.remove(handle)
                    self._cleanup_retry.pop(handle, None)
                elif state == 'unknown':
                    # Its workspace may still be there: kept, and its removal asked for again later.
                    if self._memory is not None:
                        self._memory.unknown_cleanup(handle)
                    self._retry_cleanup(handle)
            except Exception:
                pass  # Still cleaning up; asked again on the next step.

    def _retry_cleanup(self, handle):
        now = self._clock()
        pause, when = self._cleanup_retry.get(handle, (0.0, now))
        if now < when:
            return
        pause = min(CLEANUP_RETRY_MAX_SECONDS, max(CLEANUP_RETRY_SECONDS, pause * 2))
        self._cleanup_retry[handle] = (pause, now + pause)
        self._preparation.retry_cleanup(handle)

    def _step_removals(self):
        """PREPCLEAN-4 (decision 309): carry on the folder removals an earlier service left recorded.

        Once the start-up checks are done (`warm_up` ready), the store's pending records of other instances are read (at most
        MAX_PENDING_REMOVALS; read again when that many were found and all are done). Each is retried off this
        thread with the same capped backoff as an in-process one, no attempt count kept, and recorded as removed
        only after the folder is observed absent. Until then it is a pending cleanup, shown as the warning."""
        # Not before the start-up checks are done: a removal never competes with settling earlier runs.
        if self._memory is None or self.warm_up()[0] != 'ready':
            return
        asked = self._removals_asked
        if asked is not None and asked.state != 'queued':
            self._removals_asked = None
            if asked.state == 'done':
                found = asked.result or ()
                for identity, workspace in found:
                    if identity not in self._removals and identity not in self._removal_proofs:
                        self._removals[identity] = [workspace, 0.0, self._clock(), None]
                self._removals_loaded, self._removals_full = True, len(found) >= MAX_PENDING_REMOVALS
        if self._removals_asked is None and (not self._removals_loaded or (self._removals_full and not self._removals)):
            try:
                self._removals_asked = self._runtime.persistence.memory_hold('pending_removals', self._journal.instance)
            except HistoryError:
                pass
        now = self._clock()
        for identity, entry in list(self._removals.items()):
            if entry[3] is None and now >= entry[2]:
                entry[1] = min(CLEANUP_RETRY_MAX_SECONDS, max(CLEANUP_RETRY_SECONDS, entry[1] * 2))
                entry[2] = now + entry[1]
                try:
                    entry[3] = self._removal_pool.submit(codex_preparation.remove_recorded_workspace, entry[0])
                except RuntimeError:
                    continue  # The pool is closed: this service is stopping, and the record stays for the next.
            if entry[3] is not None and entry[3].done():
                future, entry[3] = entry[3], None
                try:
                    proven = future.result() is True
                except BaseException:
                    proven = False
                if proven:
                    del self._removals[identity]
                    self._removal_proofs[identity] = None
        for identity, request in list(self._removal_proofs.items()):
            if request is not None and request.state == 'done':
                del self._removal_proofs[identity]
            elif request is None or request.state == 'failed':
                try:
                    self._removal_proofs[identity] = self._runtime.persistence.memory_hold('removal_proven', identity)
                except HistoryError:
                    self._removal_proofs[identity] = None

    def unlocated_cleanup(self):
        """Given-back preparations whose removal is unconfirmed and whose folder this service never learned."""
        count = 0
        for handle in self._giving_back:
            try:
                count += self._preparation.unlocated(handle) is True
            except Exception:
                pass
        return count

    def _pending_workspaces(self):
        """Identity -> folder of each preparation this service still holds whose removal it could not observe."""
        found = {}
        for handle, (key, _reason) in self._memory.preparations.items():
            try:
                workspace = self._preparation.workspace_of(handle)
            except Exception:
                continue
            if isinstance(workspace, (str, Path)):
                found[attempt_identity(*key)] = str(workspace)
        return found

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
        if self._memory is not None:
            self._release_registers()
            if self._memory_start.state == 'failed':
                try:
                    self._memory_start = self._runtime.persistence.memory_hold(
                        'recover', self._journal.directory, self._journal.instance)
                except HistoryError:
                    pass
            self._memory.step()
        self._read_intake()
        self._retire_given_back()
        self._step_removals()
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
                if self._memory is None:
                    del self._runs[run.key]
                else:
                    self._memory.drop(run)
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
        if (run.closed is True or run.withdraw_reason in {
                'registered_idle', 'cancelled_before_claim', 'refused_before_claim'} or run.failed_viewed
                or now - run.duty_done_at >= FAILED_VIEW_SECONDS):
            run.state = 'released'
            if ((run.closed is True or run.withdraw_reason == 'refused_before_claim')
                    and now - run.duty_done_at < FAILED_VIEW_SECONDS):
                # RS-B-4 final fix (W5): memory lets these go at once (M7), but a late prepare still reads the
                # failure it would have read while the run was kept (FAILED_VIEW_SECONDS).
                self._failed_views.pop(run.key, None)
                self._failed_views[run.key] = (run.host_client, run.inputs_digest, run.reason,
                                               run.duty_done_at + FAILED_VIEW_SECONDS)
                while len(self._failed_views) > MAX_RETIRED_RUNS:
                    self._failed_views.pop(next(iter(self._failed_views)))

    def _late_failure(self, key, inputs_digest, host_client):
        """The failure a late prepare reads for a failed run already released (W5), or None."""
        now = self._clock()
        for known in [k for k, view in self._failed_views.items() if now >= view[3]]:
            del self._failed_views[known]
        view = self._failed_views.get(key)
        run = self._runs.get(key)
        if (view is None or (run is not None and run.state != 'released')
                or view[0] != host_client or view[1] != inputs_digest):
            return None
        return {'state': 'failed', 'reason_code': view[2]}

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
            # The failed row stays `releasing` with its envelope and its old schedule, so the next list has it again;
            # the rest of the batch is not dropped with it.
            self._withdraw_request, self._withdraw_phase = None, 'start'
            if self._withdraw_queue:
                self._send_withdrawal(self._withdraw_queue.pop(0))
                return
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
            if self._memory is not None and run.hand_over_due:
                self._memory.hand_over(run)
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
                run.failed_viewed = True
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
                # B4s r1 (RS-B-B4-9, RC-3): the store refused the lease for result space, so nothing of it was
                # stored; the job's input goes and the run fails as one that never took a lease, so its open
                # binding is marked for withdrawal at once (`_retire_unclaimed`), not left held for 24 h.
                try:
                    lane.retire_refused(run.persistence)
                    run.persistence = None
                except HistoryError:
                    pass  # Not provably refused: it keeps its job, as before.
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
            if self._containers is None and run.intent_token is None:
                # B4s r1 HIGH 3 (decision 146): no intent, no claim, in process mode too. What a later service
                # needs to fence the claim and settle the run is on disk before the account is asked.
                run.intent_token = new_process_token()
                try:
                    self._journal.process_intent(
                        run.intent_token, run_handle=run.key[0], attempt=run.key[1], host_client=run.host_client,
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
            run.persistence = None
            self._runtime.owner.retire(run.binding, self._runtime.pool, run.prepared_inputs.worker)
            self._preparation.discard(run.prepared)
            run.state = 'retiring'
        elif run.state == 'retiring':
            state = self._preparation.snapshot(run.prepared)['state']
            if state in {'cleaned', 'unknown'}:
                if state == 'cleaned':
                    self._preparation.retire(run.prepared)
                    if self._memory is not None:
                        self._memory.retired_preparation(run.prepared)
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
                except HistoryError as error:
                    self._earlier_later(now, error.reason_code)
                return
            if self._earlier.state == 'queued':
                return
            if self._earlier.state == 'failed':
                self._earlier = None
                self._earlier_later(now, 'listing_failed')
                return
            for binding, evidence, host_client in self._earlier.result:
                if (host_client in HOST_CLIENTS and binding.claim_request_id not in self._earlier_fences
                        and binding.run_handle not in self._fenced):
                    self._earlier_fences[binding.claim_request_id] = {
                        'binding': binding, 'evidence': evidence, 'host_client': host_client, 'future': None,
                        'closure': None, 'settling': None, 'refusals': 0, 'outages': 0, 'at': 0.0}
            skipped, self._earlier = self._earlier.skipped, None
            if skipped:
                self._earlier_later(now, 'listing_incomplete')  # O10 E2: some could not be read yet; listed again later.
            else:
                self._earlier_listed = True
                self._warm_failures.pop('listing', None)
        else:
            # B4s confirming check (HIGH 3): after the listing, each outstanding intent's claim no listed lease covered
            # is fenced once, with the same request (the account answers it alike: its closure, or its receipt).
            intents = self._intent_fences
            self._intent_asked &= set(intents)  # an intent settled since is forgotten here too
            for key, (binding, evidence, host_client) in intents.items():
                if (key in self._intent_asked or key in self._earlier_fences or key in self._earlier_admitted
                        or binding.run_handle in self._fenced or host_client not in HOST_CLIENTS
                        or binding.claim_request_id != key):
                    continue
                self._intent_asked.add(key)
                self._earlier_fences[key] = {
                    'binding': binding, 'evidence': evidence, 'host_client': host_client, 'future': None,
                    'closure': None, 'settling': None, 'refusals': 0, 'outages': 0, 'at': 0.0}
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
                self._earlier_admitted = {**self._earlier_admitted, key: reply['receipt']}  # (its process intent's)
                continue
            entry['closure'] = canonical_json(reply['closure'])  # Kept until its lease is expired.

    @staticmethod
    def _earlier_delay(attempts):
        return _backoff(FENCE_RETRY_SECONDS, FENCE_RETRY_MAX_SECONDS, attempts - 1)

    def _earlier_later(self, now, code='listing_failed'):
        """The earlier leases could not be listed: asked again later, less and less often."""
        self._warm_failures['listing'] = code
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
        finished rows, at start and then every EXPIRY_SECONDS (0.3.4 R2). A full lease pass or one
        that backed rows off queues the next pass on the next driver step (RS-B section 12).
        In 0.8.29 nothing ran this,
        so a project's results were never retired and its store filled for good."""
        if self._expiry is not None:
            if self._expiry.state == 'queued':
                return
            if self._expiry.state == 'done' and getattr(self._expiry, 'lease_pass_pending', False):
                self._next_expiry = self._clock()
            self._expiry = None  # Done or failed: a failure stays charged and is tried again later.
        if self._clock() < self._next_expiry:
            return
        self._next_expiry = self._clock() + EXPIRY_SECONDS
        try:
            self._expiry = self._runtime.persistence.expire_results()
        except HistoryError:
            pass  # The writer is closing.

    def _unheld_reply(self, run_handle, host_client):
        """LRS-6: the answer for a run handle this service does not hold (run.describe, run.abandon).

        Until the service both listed the earlier leases and collected its first sweep it cannot tell an
        earlier run from an unknown handle, so it answers `run_settling` first, whatever fence or lost
        entry it already has: a final answer never changes afterwards."""
        if not self._earlier_listed or not self._swept:
            return {'state': 'failed', 'reason_code': 'run_settling'}
        lost, fenced = self._lost.get(run_handle), self._fenced.get(run_handle)
        if fenced is not None:
            # An earlier service's run this one fenced: it never launched and holds nothing.
            return ({'state': 'refused', 'reason_code': 'history_not_authorized'} if fenced != host_client
                    else {'state': 'failed', 'reason_code': 'run_closed'})
        if lost is None and any(entry['binding'].run_handle == run_handle for entry in self._earlier_fences.values()):
            # This service may still be closing it, an earlier service's run (C3d).
            return {'state': 'failed', 'reason_code': 'run_settling'}
        if lost is not None:
            # A run of an earlier service of this project; it is never started again here.
            if lost[0] != host_client:
                return {'state': 'refused', 'reason_code': 'history_not_authorized'}
            return {'state': 'failed', 'reason_code': 'run_lost' if lost[1] == 'released' else 'run_lost_held'}
        retired = None if self._memory is None else next(
            (owner for key, owner in self._retired.items() if key[0] == run_handle), None)
        if retired is not None:
            return ({'state': 'failed', 'reason_code': 'run_closed'} if retired[0] == host_client
                    else {'state': 'refused', 'reason_code': 'history_not_authorized'})
        return {'state': 'refused', 'reason_code': 'history_orchestration_unavailable'}

    def warm_up(self):
        """LRS-7: (state, elapsed ms, failed phase, last failure code) of this service's start-up checks.

        `ready` once the earlier leases are listed and the first sweep is collected; `failing` while a
        check last failed; else `warming`. Fixed codes only, never paths or raw error text."""
        elapsed = max(0, int((self._clock() - self._warm_started) * 1000))
        if (self._earlier_listed and self._swept and
                (self._memory is None or self._memory_start.state == 'done')):
            return ('ready', elapsed, None, None)
        for phase in ('listing', 'sweep'):
            if phase in self._warm_failures:
                return ('failing', elapsed, phase, self._warm_failures[phase])
        return ('warming', elapsed, None, None)

    # Runs of earlier service instances of this project.
    def _sweep_lost(self):
        """Owner step: collect the last sweep, and start the next one on its own helper thread."""
        if self._sweep is not None:
            if not self._sweep.done():
                return
            try:
                self._lost = self._sweep.result()
                self._swept = True  # Only a collected result ends the warm-up.
                self._warm_failures.pop('sweep', None)
            except Exception:
                self._warm_failures['sweep'] = 'sweep_failed'
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
        notes = getattr(getattr(getattr(self, '_runtime', None), 'persistence', None), 'recovery_notes', None)
        if notes is not None:
            notes.update_own(run.key[0] for run in self._runs.values()
                             if not run.own_settled and run.launch_token is not None
                             and run.receipt is not None and run.state == 'failed')
        for run in self._runs.values():
            if (run.own_settled or run.launch_token is None or run.receipt is None or run.state != 'failed'
                    or run.future is not None):
                continue
            if run.own_settle is not None:
                if not run.own_settle.done():
                    continue
                future, run.own_settle = run.own_settle, None
                try:
                    run.own_reply, run.own_settle_at = future.result(), None
                    if notes is not None:
                        notes.own_gone(run.key[0], account_unreachable=False)
                except Exception as error:
                    if notes is not None:
                        notes.own_gone(run.key[0], account_unreachable=(
                            isinstance(error, AccountError) and error.reason_code == 'account_unavailable'))
                    run.own_settle_failures += 1
                    run.own_settle_at = now + container_launch.stop_retry_delay(run.own_settle_failures)
                    continue
            if run.own_reply is not None:
                if run.own_settle_at is not None and now < run.own_settle_at:
                    continue
                # B4s r1 (MED 2): the preparation goes back, then the journal is `settled` only once the run's
                # workspace is confirmed gone; otherwise both are tried again later (the account's answer is kept).
                self._give_back_own(run)
                if not self._remove_workspace(run.own_workspace):
                    run.own_settle_failures += 1
                    run.own_settle_at = now + container_launch.stop_retry_delay(run.own_settle_failures)
                    continue
                try:
                    self._journal.settled(run.launch_token, run.own_reply['state'])
                except JournalError:
                    pass  # The account settled it; a later service asks again and gets the same answer.
                run.own_reply = None
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
            run.own_workspace = entry['workspace']
            if notes is not None:
                notes.own_gone(run.key[0])
            run.own_settle = self._sweeper.submit(self._settle_entry, entry)

    def _settled_own(self, run):
        """The account no longer holds the run: its preparation goes back, as a run never admitted's does."""
        run.own_settled, run.own_settle_at = True, None
        self._give_back_own(run)

    def _give_back_own(self, run):
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
        # B4s confirming check (pruning): only the `settled` entries this pass inspected (and recovered, or needing
        # nothing) may be pruned; an entry it never saw, or one whose recovery failed, stays.
        inspected = set()
        entries = journal.entries()
        pending, process_pending, stops, unreachable = set(), set(), set(), set()
        complete = True
        call = getattr(self, '_call', None)
        if call is not None:
            call.recovery_unreachable = unreachable
        try:
            claimed_requests = {entry['claim_request_id'] for entry in entries if entry.get('version') == 1}
            for entry in entries:
                # B4s confirming check: one entry's failure never aborts the sweep for the later ones.
                try:
                    if entry['state'] is None:
                        complete = False
                        continue
                    handle, host_client = entry['run_handle'], entry['host_client']
                    if entry.get('version') == 3:
                        # B4s r1 HIGH 3: a process intent; only a stopped instance's is settled (`_settle_intent`).
                        if entry['state'] == 'settled':
                            inspected.add(launch_journal_key(entry))
                            if entry['outcome'] != 'fenced':
                                lost[handle] = (host_client, 'released')
                        elif entry['instance'] != journal.instance and stopped.setdefault(
                                entry['instance'], journal.instance_stopped(entry['instance'])) is True:
                            try:
                                outcome = self._settle_intent(entry, entry['claim_request_id'] in claimed_requests)
                            except AccountError as error:
                                pending.add(handle)
                                if error.reason_code == 'account_unavailable':
                                    unreachable.add(handle)
                                continue
                            if outcome in {'settled', 'fenced'}:
                                pending.discard(handle)
                                if outcome == 'settled':
                                    lost[handle] = (host_client, 'released')
                            elif outcome != 'removed':
                                pending.add(handle)
                        continue
                    if entry['state'] == 'settled':
                        lost[handle] = (host_client, 'released')
                        if self._recover_settled(entry):
                            inspected.add(launch_journal_key(entry))
                        else:
                            # s2fix round 2 (L14.1): the project store has not recorded this older entry's settlement
                            # yet (its report counts the run pending), so run-status does not say `released` early.
                            lost[handle] = (host_client, 'held')
                            pending.add(handle)
                        continue
                    instance = entry['instance']
                    adopted = entry.get('version') == 2 and recovery is not None and recovery.owns(entry['token'])
                    if adopted:
                        pass
                    elif instance == journal.instance:
                        if not own:
                            # This service still owns its failed stop; it is recovery work,
                            # even though ordinary live runs of this instance are excluded.
                            if entry.get('version') == 2 and entry['state'] == 'stop_failed':
                                pending.add(handle)
                                process_pending.add(handle)
                                stops.add(handle)
                            continue
                    elif stopped.setdefault(instance, journal.instance_stopped(instance)) is not True:
                        continue
                    lost[handle] = (host_client, 'held')
                    pending.add(handle)
                    if entry.get('version') == 2:
                        if entry['state'] != 'gone':
                            process_pending.add(handle)
                        if entry['state'] == 'stop_failed':
                            stops.add(handle)
                        if adopted or (instance != journal.instance and not own):
                            if recovery is not None and entry['bridge_run_id'] not in self._unverified_known:
                                recovering.append(entry)  # UH-3: a run learnt unverified is not recovered again
                            continue
                        if instance != journal.instance or not self._own_container_gone(entry):
                            continue
                    elif entry.get('version') == 1 and entry['state'] == 'watched':
                        # Decision 221: the same two-sample proof, recorded once here; the settlement reads it back as `gone`.
                        try:
                            journal.process_gone(entry['bridge_run_id'])
                        except JournalError:
                            process_pending.add(handle)
                            continue
                    elif tree_state(entry) != 'gone':
                        process_pending.add(handle)
                        continue
                    if entry['bridge_run_id'] in self._unverified_known:
                        continue  # UH-3: the update left this hold unverified; it stays unchanged, never retried here
                    try:
                        reply = self._settle_entry(entry)
                    except AccountError as error:
                        if error.reason_code == 'account_unavailable':
                            unreachable.add(handle)
                        continue
                    except HistoryError as error:
                        if error.reason_code == 'history_conflict':
                            self._takeover_unverified(entry)
                        continue
                    except Exception:
                        continue  # A local failure is not evidence that the account is unreachable.
                    if not self._remove_workspace(entry['workspace']):
                        continue  # B4s r1 (MED 2): not confirmed gone; the next sweep replays the identical settlement
                    try:
                        journal.settled(launch_journal_key(entry), reply['state'])
                    except JournalError:
                        continue
                    lost[handle] = (host_client, 'released')
                    pending.discard(handle)
                except Exception:
                    complete = False
                    continue  # Asked again on the next sweep.
            if recovery is not None:
                try:
                    recovered = recovery.recover(recovering, orphans=not own)
                except Exception:
                    complete = False
                    recovered = {}  # Asked again on the next sweep.
                for entry in recovering:
                    if entry['token'] in recovered:
                        lost[entry['run_handle']] = (entry['host_client'], 'released')
                        pending.discard(entry['run_handle'])
                        process_pending.discard(entry['run_handle'])
                        stops.discard(entry['run_handle'])
            try:
                pruned = frozenset(journal.prune(only=frozenset(inspected), keep=frozenset(self._settled_retry)))
            except Exception:
                pruned = frozenset()
            checked = self._settled_checked - pruned
            if len(checked) > MAX_SETTLED_CHECKED:
                checked = checked & inspected  # bounded: a key forgotten here is checked once more, harmlessly
            self._settled_checked = frozenset(checked)
            for instance in journal.instances():
                if instance != journal.instance:
                    try:
                        journal.forget_instance(instance)
                    except Exception:
                        pass  # Asked again on the next sweep.
            notes = getattr(getattr(getattr(self, '_runtime', None), 'persistence', None), 'recovery_notes', None)
            if notes is not None and complete:
                counts = dict.fromkeys(RECOVERY_REASONS, 0)
                counts.update(needs_settlement=len(pending), needs_process_evidence=len(process_pending),
                              stop_failed=len(stops), account_unreachable=len(unreachable & pending))
                notes.publish(counts, runs={'needs_settlement': pending, 'needs_process_evidence': process_pending,
                                            'stop_failed': stops, 'account_unreachable': unreachable & pending})
            return lost
        finally:
            if call is not None:
                call.recovery_unreachable = None

    _unverified_known = ()

    def _takeover_unverified(self, entry):
        """UH-3 (decisions 366/367): after a `history_conflict`, does the store's durable record (written by the
        takeover's adoption) say this run's hold was left unverified (A1: unchanged, never released)? A yes is kept in
        a small memory, capped at MAX_UNVERIFIED_KNOWN, oldest first out; the store stays the authority, so after a
        restart or an eviction one more refused attempt (local, no account request) learns it again. Any failure to
        ask is 'no': the settlement is tried again as before."""
        try:
            found = self._settlement_step('takeover_unverified', entry['run_handle'], entry['attempt'],
                                          entry['bridge_run_id']) is True
        except Exception:
            return False
        if found and entry['bridge_run_id'] not in self._unverified_known:
            self._unverified_known = (*self._unverified_known, entry['bridge_run_id'])[-MAX_UNVERIFIED_KNOWN:]
        return found

    def _settle_recovered(self, entry):
        """Container recovery's settlement (C4): the same `_settle_entry`; a conflict for a run the store records
        unverified is learnt here too, so the next sweep no longer hands that run to recovery (UH-3)."""
        try:
            return self._settle_entry(entry)
        except HistoryError as error:
            if error.reason_code == 'history_conflict' and entry.get('bridge_run_id') is not None:
                self._takeover_unverified(entry)
            raise

    def _settle_intent(self, entry, claimed):
        """Sweep thread, B4s r1 HIGH 3 (decision 146): a stopped instance's process intent with no later entry.

        Its claim request is not fenced here: the earlier service's armed lease is fenced by `_fence_earlier` (as
        before). When the account answers there that it admitted the claim after all, the run is settled as lost
        through the one sequence (`_settle_entry`) with the intent's own identity and that receipt's bridge run id:
        nothing of it spawned, since a child is created only from a `claimed` entry. When `_fence_earlier` closed
        the claim unadmitted (nothing was counted), the intent is `settled` as `fenced`. An intent whose `claimed`
        entry exists (`claimed`: a stop between that write and the intent's removal), or one whose receipt names a
        bridge run id with an entry, is only removed: the process rule settles that run. The workspace goes before
        the intent is written `settled`. 'settled', 'fenced', 'removed' or None (nothing known yet, or a failure:
        the next sweep asks again, identically)."""
        journal, token = self._journal, entry['token']
        try:
            receipt = self._earlier_admitted.get(entry['claim_request_id'])
            if claimed or (receipt is not None and journal.entry(receipt['bridge_run_id']) is not None):
                journal.remove(token)
                return 'removed'
            if receipt is not None:
                # B5a A5/L6b: the fence's authenticated admission is durable before settlement. Nothing spawned
                # from an intent; this claimed entry lets the writer mark its lease admitted_unbound.
                journal.claimed(receipt['bridge_run_id'], run_handle=entry['run_handle'], attempt=entry['attempt'],
                                host_client=entry['host_client'], lease=entry['lease'],
                                identity_digest=entry['identity_digest'], claim_request_id=entry['claim_request_id'],
                                storage_evidence=bytes.fromhex(entry['storage_evidence']), workspace=entry['workspace'],
                                instance=entry['instance'])
                outcome = self._settle_entry({**entry, 'bridge_run_id': receipt['bridge_run_id']})['state']
                answer = 'settled'
            elif self._fenced.get(entry['run_handle']) is not None:
                outcome = answer = 'fenced'
            else:
                # B4s confirming check (HIGH 3): an earlier service may have fenced this claim already (its lease is
                # no longer `claim_pending`, so it is not listed again) and stopped before this intent's completion:
                # the intent itself asks for the identical fence, so its answer is known again.
                if entry['claim_request_id'] not in self._intent_fences:
                    self._intent_fences = {**self._intent_fences, entry['claim_request_id']: (
                        ResultLeaseBinding(**entry['lease']), bytes.fromhex(entry['storage_evidence']),
                        entry['host_client'])}
                return None
            if not self._remove_workspace(entry['workspace']):
                if receipt is not None:
                    journal.remove(receipt['bridge_run_id'])  # the intent stays the single record; replay is identical
                return None
            if receipt is not None:
                journal.settled(receipt['bridge_run_id'], outcome)
            journal.settled(token, outcome)
        except Exception:
            return None
        self._intent_fences = {k: v for k, v in self._intent_fences.items() if k != entry['claim_request_id']}
        return answer

    def _recover_settled(self, entry):
        """Sweep thread, B4s r1 (HIGH 3): a journal entry written `settled` before project settlement existed (0.8.54
        and earlier: the account settled the run, its reservation hold stayed `releasing`) is recovered from the
        project store before it can be pruned: an outstanding hold of its attempt is settled through the one
        sequence (`_settle_entry`; the account answers `known`). Checked once per entry by each service; a failed
        recovery is tried again later, with backoff, and keeps the entry from the prune. Whether the entry is
        recovered (or needs nothing): only such an entry, inspected in this pass, may be pruned (confirming check)."""
        key = launch_journal_key(entry)
        if key in self._settled_checked or entry.get('lease') is None or entry.get('bridge_run_id') is None:
            return True
        failures, at = self._settled_retry.get(key, (0, 0.0))
        if self._clock() < at:
            return False
        try:
            lease = ResultLeaseBinding(**entry['lease'])
            try:
                opened = self._settlement_step('open_account_settlement', lease.run_handle, lease.attempt,
                    'lost-' + hashlib.sha256(entry['bridge_run_id'].encode()).hexdigest()[:32],
                    SettleLostInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                    entry['bridge_run_id'], parse_bounded_json(bytes.fromhex(
                                        entry['storage_evidence']))['lease']['lease_digest']).payload(),
                    runtime_digest=COLLECTOR_RUNTIME_DIGEST)['state']
            except HistoryError as error:
                if error.reason_code != 'history_result_not_reserved':
                    raise
                opened = None  # nothing of this attempt is outstanding in the store
            if opened == 'releasing':
                self._settle_entry(entry)
        except Exception:
            self._settled_retry = {**self._settled_retry, key: (failures + 1, self._clock() + _backoff(
                FENCE_RETRY_SECONDS, FENCE_RETRY_MAX_SECONDS, failures))}
            return False
        self._settled_retry = {k: v for k, v in self._settled_retry.items() if k != key}
        self._settled_checked = self._settled_checked | {key}
        return True

    def _container_recovery(self):
        """C4: this service's container recovery, or None while containers are switched off."""
        if self._containers is None:
            return None
        if self._recovery is None:
            self._recovery = ContainerRecovery(
                self._containers, self._journal, fence=lambda entry: self._fence_entry(entry),
                settle=lambda entry: self._settle_recovered(entry),
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

    def _arbitrate_publication(self, lease, entry):
        """Helper thread, D4 (shared rule S-3): the project writer decides this attempt before the account is asked to
        settle it. A verifiable body is committed first (`recovered`); with none, the obligation goes `releasing` so
        nothing commits afterwards (`unrecoverable`). An unanswered request raises: the settlement is asked again on
        the next sweep. At this service's own exit its writer has stopped: the lane arbitrates on a store of its own."""
        if self._runtime.persistence is None:
            raise HistoryError('history_service_unavailable')
        request_id = 'lost-' + hashlib.sha256(entry['bridge_run_id'].encode()).hexdigest()[:32]
        if self._exiting:
            return self._runtime.persistence.arbitrate_after_stop(lease.run_handle, lease.attempt, request_id)
        request = self._runtime.persistence.arbitrate_publication(lease.run_handle, lease.attempt, request_id)
        end = time.monotonic() + SETTLE_TIMEOUT_SECONDS
        while request.state == 'queued':
            if time.monotonic() >= end:
                raise HistoryError('history_service_unavailable')
            time.sleep(0.01)
        if request.state != 'done':
            raise HistoryError(request.reason or 'history_service_unavailable')
        return request.result

    def _settlement_step(self, operation, *arguments, **options):
        """Helper thread, B4s: one settlement step on the project writer (the outbox, or the closure's record); at
        this service's own exit its writer has stopped, and the step runs on a store opened for it. An unanswered
        step raises: the whole settlement is asked again, identically, on the next sweep."""
        persistence = self._runtime.persistence
        if persistence is None:
            raise HistoryError('history_service_unavailable')
        if self._exiting:
            return persistence.account_settlement_after_stop(operation, *arguments, **options)
        request = persistence.account_settlement(operation, *arguments, **options)
        end = time.monotonic() + SETTLE_TIMEOUT_SECONDS
        while request.state == 'queued':
            if time.monotonic() >= end:
                raise HistoryError('history_service_unavailable')
            time.sleep(0.01)
        if request.state != 'done':
            raise HistoryError(request.reason or 'history_service_unavailable')
        return request.result

    def _settle_entry(self, entry):
        """Helper thread: the account's settlement of this run, sent with the run's own custody.

        B4s (RS-B-B4-2..4): D4's arbitration first; then the outbox (the run's register row `releasing` with the
        exact closure request); then the account settles the run (`settle_lost_run`, its reply unchanged) and gives
        its closure (`lost_run_closure`), and the project writer records the closure. Only then does it return, so
        each caller removes the workspace and writes the journal `settled` after the settlement is committed. Any
        failure raises before the journal is written: the next sweep sends the identical requests again."""
        lease = ResultLeaseBinding(**entry['lease'])
        if lease.project_id != self._scope.project_id or entry['host_client'] not in HOST_CLIENTS:
            raise HistoryError('history_conflict')
        if entry.get('version') == 1 and entry['state'] == 'watched':
            self._journal.process_gone(entry['bridge_run_id'])
        self._arbitrate_publication(lease, entry)
        evidence = bytes.fromhex(entry['storage_evidence'])
        settle = SettleLostInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                 entry['bridge_run_id'], parse_bounded_json(evidence)['lease']['lease_digest'])
        request_id = 'lost-' + hashlib.sha256(entry['bridge_run_id'].encode()).hexdigest()[:32]
        try:
            opened = self._settlement_step('open_account_settlement', lease.run_handle, lease.attempt, request_id,
                                           settle.payload(), runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        except HistoryError as error:
            if error.reason_code != 'history_result_not_reserved':
                raise
            opened = {'state': None}  # Nothing of this attempt is left to settle in the store: the account alone.
        outbox = opened['state']
        custody = canonical_json({'purpose': 'run', 'lease': entry['lease'], 'storage_evidence': entry['storage_evidence'],
                                  'terminal_evidence': None, 'cleanup_evidence': None})
        call = self._call
        call.role, call.host_client, call.context = 'project', entry['host_client'], custody
        try:
            client = AccountClient(self._account, self._scope.project_id, COLLECTOR_RUNTIME_DIGEST,
                                   timeout=SETTLE_TIMEOUT_SECONDS)
            try:
                reply = client.settle_lost_run(settle, request_id=request_id)
            except Exception:
                if outbox != 'settled':
                    raise
                # B4s r1 (HIGH 1): the store holds this attempt's verified settlement (the account only closes a run
                # it settled or released), so its local receipt finishes the journal even when the account cannot
                # answer any more (its open purged, the run settled before its tombstone kept the replay evidence).
                return {'state': 'released' if opened['release_kind'] == 'released_unacknowledged' else 'settled',
                        'known': True}
            closure = None if outbox != 'releasing' else client.lost_run_closure(settle, request_id=request_id)
        except AccountError as error:
            failed = getattr(call, 'recovery_unreachable', None)
            if error.reason_code == 'account_unavailable' and failed is not None:
                failed.add(entry['run_handle'])
            raise
        finally:
            call.role = call.host_client = call.context = None
        if closure is not None:
            self._settlement_step('record_account_settlement', lease, canonical_json(closure))
        return reply

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
        except AccountError as error:
            failed = getattr(call, 'recovery_unreachable', None)
            if error.reason_code == 'account_unavailable' and failed is not None:
                failed.add(entry['run_handle'])
            raise
        finally:
            call.role = call.host_client = call.context = None

    @staticmethod
    def _remove_workspace(workspace):
        """The settled run's private workspace, only if it is one of the host scratch folder's. B4s r1 (MED 2):
        whether it is confirmed gone afterwards (True too for no workspace, or a path that is not one of this
        host's workspaces, which is never removed); a removal that failed or could not be checked is False, and so
        is an entry left at the path because it is not this host's own folder (a symlink, another owner's)."""
        if workspace is None:
            return True
        from .bridge import SCRATCH_CONTAINER, scratch_base
        from .host_paths import remove_private_directory
        try:
            # Resolved on both sides: no component of either path may be a symlink.
            container = Path(os.path.realpath(scratch_base())) / SCRATCH_CONTAINER
            path = Path(workspace)
            if path.parent != container or not path.name.startswith('prepare-'):
                return True
            # B4s confirming check (MED 2): whatever stands at the recorded path and is not this host's own folder
            # (a symlink, a symlinked container, another owner's entry, a file) is left there and is NOT gone.
            if os.path.realpath(container) != str(container):
                return not os.path.lexists(path)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                return True
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or os.path.realpath(path) != str(path)):
                return False
            remove_private_directory(path)
            return not os.path.lexists(path)
        except Exception:
            return False

    def settle_on_exit(self, *, settle=True):
        """After every thread of this host stopped: settle its own runs, then let later hosts settle the rest.

        An in-flight sweep and account call are waited for first, so nothing of
        this host writes its journal or reaches the account after its lock is gone.
        Without `settle` (O10 D13: its result writer faulted) it settles nothing and
        only frees its journal lock, so the next host settles its runs.
        """
        self._sweeper.shutdown(wait=True)
        self._executor.shutdown(wait=True)
        if self._removal_pool is not None:
            self._removal_pool.shutdown(wait=True)
        if not settle:
            if self._containers is not None:
                self._containers.close()
            self._journal.release()
            return
        install_enrollment(self._preamble, owner=self._owner_name)
        self._exiting = True  # D4: the writer has stopped; each settlement arbitrates on a store opened for it
        settled = False
        try:
            lost = self._settle_lost(True)
            # Decision 179: verified only when every run it knows of is released, not just when the pass ran.
            settled = all(status == 'released' for _client, status in lost.values())
        except Exception:
            pass
        finally:
            install_enrollment(None, owner=self._owner_name)
            if self._containers is not None:
                self._containers.close()
        if self._memory is not None:
            try:
                # No runtime thread can launch or publish. Drop the actual retained memory before the receipt.
                pending = self._pending_workspaces()  # PREPCLEAN-4: recorded for the next service before it is cleared
                self._runtime.persistence._jobs.clear()
                self._runtime.persistence._by_worker.clear()
                self._runtime.pool._jobs.clear()
                self._preparation._jobs.clear()
                self._preparation._reserved_bytes = self._preparation._reserved_export_bytes = 0
                for slot in self._preparation._slots:
                    slot.current = None
                self._runs.clear()
                self._giving_back.clear()
                self._memory.takes.clear()
                self._memory.preparations.clear()
                self._runtime.persistence.retire_memory_after_stop(self._journal.instance, pending)
                self._journal.keep_lock = False
            except Exception:
                settled = False  # Keep stopping evidence for the next start.
        self._journal.release()
        return settled  # RS-E E2a-8: whether final settlement released every run (a clean stop may then be published)

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

    def pending_cleanup(self):
        """Folder removals still owed here: discarded preparations kept until observed cleanup and retirement,
        and removals an earlier service recorded (PREPCLEAN-4).

        Keep custody through unknown, queued retry and cleaned-but-not-retired
        states. A live host must not turn an idle timeout into abandoned folders.
        This keeps the idle and exhaustion replacement waiting; it never keeps a host an update or uninstall
        has withdrawn, since the removal is recorded for the next service.
        """
        return len(self._giving_back) + len(self._removals)

    def stuck_slots(self):
        """Stranded runs and abandoned monitor slots; cleanup keeps its live retry duty."""
        return sum(1 for run in self._runs.values() if self._stranded(run)) + len(self._stuck_monitor)

    def replace_when_idle(self):
        """Replace stranded work only after pending folder cleanup has retired."""
        return not self.pending_cleanup() and not self.busy() and self.stuck_slots() > 0

    def exhausted(self):
        """No room for another run; preserve the live cleanup custodian before replacement."""
        return not self.pending_cleanup() and not self.busy() and (
            len(self._runs) >= MAX_RUNS or len(self._dispatch._jobs) >= DISPATCH_SLOTS
            or self.stuck_slots() >= PREPARATION_SLOTS)

    def close(self):
        if self._closed:
            return
        self._closed = True
        install_enrollment(None, owner=self._owner_name)
        for connection, *_ in self._intake + self._register_waiting:
            connection.close()
        self._intake, self._register_waiting = [], []
        for run in self._runs.values():
            for connection, _ in run.waiters:
                connection.close()
            run.waiters = []
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._sweeper.shutdown(wait=False, cancel_futures=True)
        if self._removal_pool is not None:
            self._removal_pool.shutdown(wait=False, cancel_futures=True)
