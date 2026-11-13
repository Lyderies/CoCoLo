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
run where it is: nothing is retried, relaunched or released on its behalf, and
an uncertain account reply leaves the capacity charged.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
from threading import Lock, local
import time

from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountClient, AccountError, AccountRequest, account_request_digest
from .account_claim_contracts import LaunchClaimInput
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
from .learning_contracts import canonical_json, content_digest, parse_bounded_json
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
MAX_REQUEST_BYTES = 4_096
WAIT_SECONDS = 5.0
ACCOUNT_TIMEOUT_SECONDS = 60
ACCOUNT_RETRY_SECONDS = 600  # How long an unreachable account is waited for, per call.
ACCOUNT_RETRY_INTERVAL = 0.5
RATE_LIMIT_TIMEOUT_SECONDS = 10  # One event; the child's stream waits for it.
MAX_PENDING_RATE_LIMITS = 64  # The account keeps at most this many per run.
MAX_RUN_RATE_LIMIT_EVENTS = 1_000
_RETRYING = object()
REASON_CODES = frozenset({
    'run_invalid_request', 'run_not_found', 'run_conflict', 'run_full', 'run_failed', 'run_unavailable', 'run_abandoned',
})
_REGISTER_FIELDS = {'purpose', 'orchestration_handle', 'run_id', 'host_client', 'assignment', 'task', 'route',
                    'parent_grant'}
_PREPARE_FIELDS = {'purpose', 'expected_export_commit'}
_ACTIVE = frozenset({'preparing', 'observing', 'leasing', 'arming', 'recording_open', 'claiming', 'binding',
                     'registering', 'provenance', 'consuming'})
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
    released_at: float | None = None
    recorder: object = None


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
    def __init__(self, state, project_root, bundle_root, scope, runtime, *, clock=time.monotonic):
        self._state, self._root, self._bundle = state, Path(project_root), Path(bundle_root)
        self._scope, self._runtime, self._clock = scope, runtime, clock
        self._runs, self._closed, self._intake = {}, False, []
        self._call = _Call()
        self._account = account_endpoint_path(state)
        self._preparation = runtime.bind_preparation(
            PreparationLimits(1, MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES),
            authorizer=lambda context: any(run.preparation is context for run in self._runs.values()))
        self._dispatch = runtime.bind_dispatch(max_requests=DISPATCH_SLOTS, steps_per_cycle=1,
                                               authorizer=self._authorize_dispatch)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='project-driver-account')
        self._owner_name = 'project-driver-%d-%d' % (os.getpid(), id(self))
        install_enrollment(self._preamble, owner=self._owner_name)
        self._enrolled = None  # Set by the host: token -> its enrolled facts, for dispatch requests.

    # Enrollment for this host's own account calls, one helper thread at a time.
    def _preamble(self, socket_path, message):
        call = self._call
        if Path(socket_path) != self._account or call.role is None:
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
            if not held:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_orchestration_unavailable'})
            elif held[0].host_client != enrolled.host_client:
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_not_authorized'})
            elif request['operation'] == 'run.describe':
                self._reply(connection, self._view(held[0]))
            elif held[0].lease is not None or held[0].state not in {'registered', 'failed'} or held[0].future is not None:
                # A leased or claimed run holds custody and maybe capacity; that needs reconciliation.
                self._reply(connection, {'state': 'refused', 'reason_code': 'history_conflict'})
            else:
                run = held[0]
                if run.prepared is not None:
                    try:
                        self._preparation.discard(run.prepared)
                    except Exception:
                        pass
                del self._runs[run.key]
                self._reply(connection, {'state': 'failed', 'reason_code': 'run_abandoned'})
            return
        try:
            if request['operation'] == 'run.register':
                self._register(key, request['inputs_digest'], context, enrolled.host_client)
            elif request['operation'] == 'run.prepare':
                self._prepare(key, request['inputs_digest'], context, enrolled.host_client)
            else:
                self._recorded(key, request['inputs_digest'], enrolled.host_client)
        except HistoryError as error:
            self._reply(connection, {'state': 'refused', 'reason_code': error.reason_code})
            return
        run = self._runs[key]
        if request['operation'] != 'run.prepare' or run.state in _AFTER or run.state == 'failed':
            self._reply(connection, self._view(run))
        else:
            run.waiters.append((connection, self._clock() + WAIT_SECONDS))

    def _recorded(self, key, inputs_digest, host_client):
        """The opening primary reports its result admitted; custody may then retire.

        Only after its own successful admission does the CLI say so. A false
        report can only shorten custody of that host class's own released run.
        """
        run = self._runs.get(key)
        if run is None:
            raise HistoryError('history_orchestration_unavailable')
        if run.host_client != host_client:
            raise HistoryError('history_not_authorized')
        if run.inputs_digest != inputs_digest:
            raise HistoryError('history_conflict')
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
        if len(self._runs) >= MAX_RUNS:
            raise HistoryError('history_service_unavailable')
        prospective = OpenScopeContext(self._scope, value['orchestration_handle'], value['assignment'], value['task'],
                                       value['route'], value['parent_grant'])
        prospective.snapshot()
        self._runs[key] = _Run(key, self._scope, host_client, prospective, value['run_id'], inputs_digest)

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
        commit = value['expected_export_commit']
        run.preparation = PreparationContext(preparation_payload(run.prospective, self._root, self._bundle,
            {'PATH': os.defpath}, expected_export_commit=commit))
        try:
            run.prepared = self._preparation.submit(run.preparation)
        except PreparationError:
            self._fail(run, 'run_unavailable')
            return
        run.state = 'preparing'

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

    def _fail(self, run, reason):
        run.state, run.reason = 'failed', reason

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
            self._bundle / '.codex' / 'orchestration' / 'config' / 'learning-clients.json')['bridge-v1']
        original = run.prospective
        return HostResultContext(run.binding, original.assignment, original.task, original.route, capability)

    def owns_binding(self, binding, host_client):
        """Whether this exact claimed run is one this driver holds for that host class."""
        run = self._runs.get((binding.run_handle, binding.attempt))
        return run is not None and run.binding == binding and run.host_client == host_client

    def owns(self, target, host_client):
        """Whether this run exists here for that host class; status and cancel stay with it."""
        run = self._runs.get((target.run_handle, target.attempt))
        return (run is not None and run.host_client == host_client
                and run.prospective.orchestration_handle == target.orchestration_handle)

    # Owner-thread state machine.
    def step(self):
        if self._closed:
            return
        self._read_intake()
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
            self._fail(run, 'run_unavailable')
        return None

    def _advance(self, run):
        lane, prep = self._runtime.persistence, self._preparation
        if run.state == 'preparing':
            state = prep.snapshot(run.prepared)['state']
            if state in {'failed', 'unknown', 'cleaned'}:
                self._fail(run, 'run_unavailable')
            elif state == 'ready':
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
                self._fail(run, 'run_unavailable')
                return
            run.identity_digest = content_digest(identity[0])
            run.claim_request_id = 'claim-' + self._id(run)
            claim = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.claim_launch', run.claim_request_id,
                run.scope.project_id, COLLECTOR_RUNTIME_DIGEST, LaunchClaimInput(run.key[0], run.identity_digest).payload())
            run.lease = ResultLeaseBinding(**run.scope.as_data(), orchestration_handle=run.prospective.orchestration_handle,
                run_handle=run.key[0], run_id=run.run_id, attempt=run.key[1],
                claim_request_id=run.claim_request_id, claim_request_digest=account_request_digest(claim))
            run.persistence = lane.prepare_lease(run.lease, run.prepared_inputs.worker)
            run.state = 'leasing'
        elif run.state == 'leasing':
            state = lane.snapshot(run.persistence)['state']
            if state != 'preparing':
                lane.arm(run.persistence)
                run.state = 'arming'
        elif run.state == 'arming':
            if lane.snapshot(run.persistence)['state'] == 'arming':
                return
            run.evidence = lane.storage_evidence(run.persistence)
            self._account_call(run, 'project', 'open_provenance',
                OpenProvenanceInput(run.key[0], run.prospective.assignment['assignment_id']),
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
            self._account_call(run, 'account', 'claim_launch', run.key[0], run.identity_digest,
                               request_id=run.claim_request_id, custody=self._custody(run, storage=run.evidence))
            run.state = 'claiming'
        elif run.state == 'claiming':
            receipt = self._result(run)
            if receipt is None:
                return
            if receipt.get('state') != 'claimed':
                self._fail(run, receipt.get('reason_code') or 'run_failed')
                return
            run.receipt = receipt
            run.binding = AttemptBinding(**{n: getattr(run.lease, n) for n in LEASE_SCOPE_FIELDS},
                                         claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
            lane.bind_claim(run.persistence, run.binding, hard_deadline_at_ms=receipt['deadline_at_ms'])
            run.state = 'binding'
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
                run.state = 'released'

    @staticmethod
    def _id(run):
        return hashlib.sha256(canonical_json([run.key[0], run.key[1], run.inputs_digest])).hexdigest()[:32]

    def busy(self):
        return any(run.state not in {'registered', 'failed', 'released'} for run in self._runs.values())

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
