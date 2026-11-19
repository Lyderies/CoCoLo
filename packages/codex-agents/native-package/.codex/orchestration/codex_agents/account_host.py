"""Account services hosted in the collector process, behind enrollment.

The collector's main thread created the ledger, so it is the only thread that
may own these account views. It runs this host's loop while the collector
thread keeps serving hooks; both serialize on the ledger lock as before.

A client enrolls with the state root's account key and a proof of write, and
names the project root it acts for. The owner accepts a request only for the
project registered at that root. Nothing here grants owner-only operations.
"""
import os
from pathlib import Path
import threading
import time

from .account_api import account_request_digest
from .account_billing_jobs import AccountBillingJobs, BillingJobLimits
from .account_claim_jobs import AccountClaimJobs
from .account_ingress import AccountIngress, AccountIngressLimits
from .account_observation import AccountObservationOwner, ObservationContext
from .account_open_contracts import MAX_ACCOUNT_OPEN_CONTEXT_BYTES, AccountOpenContext, OpenOwnerLimits
from .account_observation_jobs import AccountObservationJobs, ObservationJobLimits
from .account_service import AccountAuthorization, AccountOwnerViews, AccountService
from .result_contracts import ResultLeaseBinding
from .billing_probe_worker import PROBE_SLOT_BYTES, BillingProbeWorkers, ProbeWorkerLimits
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, _assert_no_symlink_components, _check_owner_mode
from .history_contracts import HistoryError
from .hosts import OWNER_CLIENT
from .provider_observation_contracts import ObservationError, decode_observation_input
from .learning_contracts import keyed_digest, parse_bounded_json
from .owned_execution import OwnedExecutionRegistry
from .service_enrollment import (
    EnrollmentVerifier, account_endpoint_path, issue_capability, prepare_proof_directory, proof_directory, revoke_capability,
)
from .service_host import EnrolledFrontend, ServiceHostLimits
from .signature_job_intake import SignatureIntakeLimits
from .signature_worker_pool import SignatureWorkerLimits
from .sql_read_budget import SQLReadLimits

SECOND = 1_000_000_000
# What an enrolled primary may ask the account owner. Owner-only operations are
# never offered. A host open must carry its originals; a claim, the run's lease.
HOST_OPERATIONS = frozenset({
    'account.health', 'account.project', 'account.owner_key', 'account.capacity_status',
    'account.observe_provider', 'account.record_observation_unavailable', 'account.billing_probe',
    'account.open_host_run', 'account.claim_launch',
})
# A project host's custody calls for one admitted run, each bound to its lease.
PROJECT_OPERATIONS = frozenset({
    'account.health', 'account.observe_provider', 'account.record_observation_unavailable',
    'account.open_provenance', 'account.register_execution', 'account.run_provenance',
    'account.submit_terminal', 'account.release_capacity', 'account.close_denied_attempt',
    'account.fence_claim', 'account.settle_lost_run', 'account.record_rate_limit_event',
})
# The owner's terminal (setup, status, attestation): reads that spend nothing, and
# never an open, a claim or a probe. Its provider observation is the standalone kind
# (who is signed in): one for a run's launch evidence is refused.
OWNER_OPERATIONS = frozenset({
    'account.health', 'account.project', 'account.owner_key', 'account.capacity_status',
    'account.observe_provider',
})
GENERATION = 1


# The account's own reads and its open and claim jobs. One open's or claim's admission history:
# 4,096 records and 2 MiB, about ten times a busy day's provider readings (0.3.3, C1); with the
# records COCOLO writes (over 500 bytes each) the 2 MiB is reached first. The workers' protocol
# budget fits two such jobs.
ACCOUNT_READ_LIMITS = SQLReadLimits(100_000, 1_000, 2 * 1_048_576)
# Up to eight projects, each preparing three claims or opens at once, so a project's three dispatches
# started together are all claimed (0.3.3, C8).
ACCOUNT_INTAKE_LIMITS = SignatureIntakeLimits(8, 3, 4096, 2 * 1_048_576, 4)
ACCOUNT_WORKER_LIMITS = SignatureWorkerLimits(2, 32 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000)


def account_endpoint(state):
    return account_endpoint_path(state)


def account_directory(state):
    return account_endpoint(state).parent


class AccountServiceHost:
    def __init__(self, ledger, state, *, clock_ns=time.monotonic_ns):
        if type(state) is not CollectorPaths or not callable(clock_ns):
            raise HistoryError('history_invalid_input')
        self._state, self._clock_ns = state, clock_ns
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._closed = self._issued = False
        self._contexts = {}
        self._claims = self._observations = self._workers = self._billing = None
        self._service = self._ingress = self._frontend = None
        try:
            self._views = AccountOwnerViews(ledger)
            limits = ACCOUNT_READ_LIMITS
            self._claims = AccountClaimJobs(self._views, ACCOUNT_INTAKE_LIMITS, ACCOUNT_WORKER_LIMITS, limits,
                open_limits=OpenOwnerLimits(4, 256, MAX_ACCOUNT_OPEN_CONTEXT_BYTES))
            self._observations = AccountObservationJobs(AccountObservationOwner(self._views, limits),
                                                        ObservationJobLimits(8, 3, 3, 3))
            self._workers = BillingProbeWorkers(OwnedExecutionRegistry(), ProbeWorkerLimits(1, PROBE_SLOT_BYTES))
            self._billing = AccountBillingJobs(self._claims, self._observations, self._workers,
                                               BillingJobLimits(16, 2, 4))
            self._claims.bootstrap()
            self._observations.bootstrap()
            self._service = AccountService(COLLECTOR_RUNTIME_DIGEST, self._views, authorizer=self._authorize,
                                           claim_jobs=self._claims, observation_jobs=self._observations,
                                           run_provenance_work_limits=limits, open_provenance_work_limits=limits)
            self._ingress = AccountIngress(self._service, AccountIngressLimits(32, 2, 8, 4, 60 * SECOND, 1_000_000))
            self._ingress.bootstrap()
            directory = account_directory(state)
            for path in (directory.parent, directory):
                path.mkdir(mode=0o700, exist_ok=True)
                _assert_no_symlink_components(path)
                _check_owner_mode(path, directory=True)
            proofs = proof_directory(account_endpoint(state))
            prepare_proof_directory(proofs)
            key = issue_capability(state, None, 'account')
            self._issued = True
            self._frontend = EnrolledFrontend(EnrollmentVerifier(key, frozenset({'account', 'project'}), GENERATION, proofs),
                ServiceHostLimits(32, 2 * SECOND, 64, 65 * SECOND, 30_000, 4_096),
                lambda kind, connection, token: self._ingress.attach(connection, token), proofs, clock_ns=clock_ns)
            self._frontend.listen(account_endpoint(state), 'account')
        except BaseException as error:
            try:
                self.close()
            except BaseException:
                pass
            if isinstance(error, Exception):
                # Any component refusing to start leaves the collector serving hooks.
                raise HistoryError('history_service_unavailable') from None
            raise

    def _owner(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError('history_wrong_owner')

    def _authorize(self, request, token):
        """Owner thread: the enrolled root must be the registered home of the project."""
        entry = self._frontend.context(token) if self._frontend is not None else None
        role = None if entry is None else entry.enrolled.role
        allowed = {'account': HOST_OPERATIONS, 'project': PROJECT_OPERATIONS}.get(role)
        if role == 'account' and entry.enrolled.host_client == OWNER_CLIENT:
            allowed = OWNER_OPERATIONS
        if (entry is None or entry.kind != 'account' or allowed is None or request.operation not in allowed
                or account_request_digest(request) != entry.enrolled.request_digest):
            return None
        if allowed is OWNER_OPERATIONS and request.operation == 'account.observe_provider':
            # The owner's terminal sees who is signed in; it never refreshes a run's launch evidence.
            try:
                observation = decode_observation_input(request.payload)
            except ObservationError:
                return None
            if observation.run_handle is not None or observation.phase is not None:
                return None
        root = entry.enrolled.subject
        try:
            if self._views._ledger.project_for_root(Path(root)) != request.project_id:
                return None
        except Exception:
            return None
        # One context object per connection: the owner compares its identity
        # across the reauthorizations of a single request.
        context = self._contexts.get(token)
        if context is None or context.root != root:
            context = self._contexts[token] = ObservationContext(root)
        caller = keyed_digest(self._state.digest_key(), 'account-caller\0' + entry.enrolled.host_client
                              + '\0' + request.project_id)
        opening, custody = None, {}
        if request.operation == 'account.open_host_run':
            opening = self._open_context(entry.enrolled.context)
            if opening is None:
                return None
        elif role == 'project' or request.operation == 'account.claim_launch':
            custody = self._run_custody(entry.enrolled.context, request.project_id)
            if custody is None and request.operation not in {'account.health', 'account.observe_provider',
                                                             'account.record_observation_unavailable'}:
                return None
        return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                    'host' if role == 'account' else 'project', allowed, caller,
                                    observation_context=context, open_context=opening, **(custody or {}))

    @staticmethod
    def _run_custody(raw, project_id):
        """The run's lease and the project writer's sealed evidence, as its host sent them."""
        if raw is None:
            return None
        try:
            value = parse_bounded_json(raw, max_bytes=65_536)
            if (value.keys() != {'purpose', 'lease', 'storage_evidence', 'terminal_evidence', 'cleanup_evidence'}
                    or value['purpose'] != 'run' or type(value['lease']) is not dict
                    or value['lease'].get('project_id') != project_id):
                return None
            evidence = {name: None if value[name] is None else bytes.fromhex(value[name])
                        for name in ('storage_evidence', 'terminal_evidence', 'cleanup_evidence')}
            return {'storage_binding': ResultLeaseBinding(**value['lease']), **evidence}
        except Exception:
            return None

    @staticmethod
    def _open_context(raw):
        """Originals and the project's open-scope proof, as the enrolled client sent them."""
        if raw is None:
            return None
        try:
            value = parse_bounded_json(raw, max_bytes=MAX_ACCOUNT_OPEN_CONTEXT_BYTES + 64)
            if value.keys() != {'purpose', 'prospective', 'project_scope'} or value['purpose'] != 'open':
                return None
            opening = AccountOpenContext.from_snapshot(value)
            opening.snapshot()  # The proof must bind exactly these originals.
            return opening
        except Exception:
            return None

    def health_notes(self):
        """S1-H1a: each account lane's published notes, for the collector's health reply. Reads only
        immutable values: no lock, owner thread or lane method."""
        lanes = {'signature': self._claims and self._claims._pool.health, 'claims': self._claims and self._claims.health,
                 'observations': self._observations and self._observations.health,
                 'ingress': self._ingress and self._ingress.health, 'billing': self._billing and self._billing.health,
                 'probe_worker': self._workers and self._workers.health}
        return {name: lane.view() for name, lane in lanes.items() if lane is not None}

    def serve_once(self):
        self._owner()
        if self._closed:
            raise HistoryError('history_service_unavailable')
        self._frontend.step()
        self._ingress.pump_owner()
        # Contexts belong to connections the frontend still holds.
        for token in [key for key in self._contexts if self._frontend.context(key) is None]:
            del self._contexts[token]

    def close(self):
        """Withdraw the key first, then stop intake and every owned worker."""
        self._owner()
        if self._closed:
            return
        self._closed = True
        if self._issued:
            revoke_capability(self._state, None, 'account')
        if self._frontend is not None:
            self._frontend.close()
        if self._ingress is not None:
            self._ingress.request_close()
        for component in (self._billing, self._claims, self._observations):
            if component is not None:
                component.request_close()
        if self._workers is not None:
            self._workers.close()

    def drained(self):
        """Poll after close until every owned worker has stopped; then close the service."""
        self._owner()
        if self._ingress is not None and not self._ingress.drain():
            return False
        if self._claims is not None:
            for notice in self._claims._pool.poll():
                self._claims._pool.discard(notice.handle)
        if self._observations is not None:
            self._observations.drain()
        threads = []
        if self._claims is not None:
            threads += [slot.thread for slot in self._claims._pool._slots]
        if self._observations is not None:
            threads += [slot.thread for slot in self._observations._slots]
        if self._workers is not None:
            threads += [slot.thread for slot in self._workers._slots]
        if any(thread is not None and thread.is_alive() for thread in threads):
            return False
        if self._service is not None:
            self._service.close()
            self._service = None
        return True
