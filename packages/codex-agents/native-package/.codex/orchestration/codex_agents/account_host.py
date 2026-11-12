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
from .billing_probe_worker import PROBE_SLOT_BYTES, BillingProbeWorkers, ProbeWorkerLimits
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, _assert_no_symlink_components, _check_owner_mode
from .history_contracts import HistoryError
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
# What an enrolled primary may ask the account owner. Owner-only operations and
# launch claims are never offered here. A host open must carry its originals.
HOST_OPERATIONS = frozenset({
    'account.health', 'account.project', 'account.owner_key', 'account.capacity_status',
    'account.observe_provider', 'account.record_observation_unavailable', 'account.billing_probe',
    'account.open_host_run',
})
GENERATION = 1


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
            limits = SQLReadLimits(100_000, 1_000, 2 * 1_048_576)
            self._claims = AccountClaimJobs(self._views, SignatureIntakeLimits(8, 2, 512, 262_144, 4),
                SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000), limits,
                open_limits=OpenOwnerLimits(4, 256, MAX_ACCOUNT_OPEN_CONTEXT_BYTES))
            self._observations = AccountObservationJobs(AccountObservationOwner(self._views, limits),
                                                        ObservationJobLimits(16, 2, 2, 2))
            self._workers = BillingProbeWorkers(OwnedExecutionRegistry(), ProbeWorkerLimits(1, PROBE_SLOT_BYTES))
            self._billing = AccountBillingJobs(self._claims, self._observations, self._workers,
                                               BillingJobLimits(16, 2, 4))
            self._claims.bootstrap()
            self._observations.bootstrap()
            self._service = AccountService(COLLECTOR_RUNTIME_DIGEST, self._views, authorizer=self._authorize,
                                           claim_jobs=self._claims, observation_jobs=self._observations)
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
            self._frontend = EnrolledFrontend(EnrollmentVerifier(key, 'account', GENERATION, proofs),
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
        if (entry is None or entry.kind != 'account' or entry.enrolled.role != 'account'
                or request.operation not in HOST_OPERATIONS
                or account_request_digest(request) != entry.enrolled.request_digest):
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
        opening = None
        if request.operation == 'account.open_host_run':
            opening = self._open_context(entry.enrolled.context)
            if opening is None:
                return None
        return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                    'host', HOST_OPERATIONS, caller, observation_context=context, open_context=opening)

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
