"""Trusted startup of one project's service host.

``project_host.py`` runs ``serve_project`` as a detached process, started by
the checked CLI entry the same way it starts the collector. Budgets here are installed policy for that
process, never values from a request. The registry lookup goes through the
collector; this process opens only its own project's history.
"""
from pathlib import Path
import signal
import time

from .codex_worker import CodexWorkerLimits
from .collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from .collector_contracts import CollectorError
from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .control_loop import ControlLoopLimits
from .history_contracts import HistoryError
from .host_commands import HostCommandError, bound_project_root
from .learning_contracts import keyed_digest
from .liveness_clock import LivenessLimits
from .orchestration_api import OrchestrationLimits, OrchestrationScope
from .project_ingress import ProjectIngressLimits
from .project_request_queue import ProjectRequestLimits
from .project_runtime import ProjectRuntimeLimits
from .result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from .result_persistence import MAX_PERSISTENCE_ENTRY_BYTES
from .service_host import ProjectServiceHost, ServiceHostLimits

SECOND = 1_000_000_000
# Orchestrations and bridged runs of one project are addressed through this fixed
# namespace. Ownership between primaries is the recorded host class, as in 0.2.x.
PRIMARY_NAMESPACE = 'primary'
# Provisional thresholds from the liveness design: first event within three
# minutes, quiet after two, stale after five, silence cutoff after ten. They are
# not yet calibrated against real client cadence.
LIVENESS = LivenessLimits(180 * SECOND, 120 * SECOND, 300 * SECOND, 600 * SECOND)
# One project runs at most three of the pool's six children: with four or more, a cancellation was not
# always carried out (0.3.3, C8; to fix in 0.4.0).
MAX_ACTIVE_ATTEMPTS = 3
MAX_ROUTE_TIMEOUT_SECONDS = 21_600  # config/routing-policy.json; a test keeps them equal
RESULT_RESERVATION_MS = (MAX_ROUTE_TIMEOUT_SECONDS + 1_800) * 1000
# A host is started on demand, so one with no run and no client for this long
# stops; the next command starts another. Its journals and scope are durable.
IDLE_STOP_NS = 900 * SECOND
# Recorded runs' originals for verification and QA are kept in this process only,
# so a host that holds any waits much longer before it stops: a primary may take
# its time between recording a worker and verifying it.
RETAINING_IDLE_STOP_NS = 12 * 3600 * SECOND
ORCHESTRATIONS = OrchestrationLimits(256, 4_096)


def runtime_limits(liveness=LIVENESS):
    requests = ProjectRequestLimits(16, 10 * SECOND)
    return ProjectRuntimeLimits(MAX_ACTIVE_ATTEMPTS, MAX_ACTIVE_ATTEMPTS,
        MAX_ACTIVE_ATTEMPTS * MAX_PERSISTENCE_ENTRY_BYTES + requests.retained_bytes, liveness,
        ControlLoopLimits(16, 8, 10 * SECOND, 1_000_000), CodexWorkerLimits(MAX_ACTIVE_ATTEMPTS, 64 * 1_048_576),
        requests, ProjectIngressLimits(16, 8, 10 * SECOND, 1_000_000))


def host_limits():
    return ServiceHostLimits(16, 2 * SECOND, 64, 60 * SECOND, 30_000, 4_096)


def supervisor_scope(state, project_id):
    """Durable identity for this project's journals; a restart keeps it.

    What a restart replaces is the capability. The generation changes only
    through an explicit succession, which does not exist yet.
    """
    instance = 'sup-' + keyed_digest(state.digest_key(), 'project-supervisor\0' + project_id)[:32]
    return OrchestrationScope(project_id, PRIMARY_NAMESPACE, COLLECTOR_RUNTIME_DIGEST, instance, 1)


def history_directory(state, project_id):
    return state.root / 'projects' / project_id


def _owned_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    _assert_no_symlink_components(path)
    _check_owner_mode(path, directory=True)


def _result_capacity():
    # A lease must outlast the run it holds space for (the claim requires the run's hard deadline
    # to fall inside it): the longest route time limit, then the usual 30-minute margin.
    return ResultCapacity(64, MAX_ACTIVE_ATTEMPTS * MAX_RESULT_BODY_BYTES, RESULT_RESERVATION_MS, 7 * 86_400_000)


def open_offline_history(state, project_id):
    """The project's store for an offline owner tool (a drained migration).

    The store's exclusive lock refuses this with `history_writer_busy` while
    the project's own service runs; nothing here stops or waits for it.
    """
    from .project_history import ProjectHistory

    directory = history_directory(state, project_id)
    _owned_directory(directory.parent)
    _owned_directory(directory)
    return ProjectHistory(directory, project_id, result_key=state.digest_key(), orchestration_limits=ORCHESTRATIONS,
                          result_capacity=_result_capacity(), allow_schema_upgrade=True)


def service_bundle_root(orchestration_root, project_root):
    """The bundle children are prepared from: always the project being served.

    A child's role is the project's agent file, checked against the digest its
    route recorded. A service may run from the project's own install, from the
    owner's protected runtime copy (which holds only runtime code and serves only
    the project it is bound to), or from a clone, as setup starts it (O10 SB):
    in every case its children's files are the project's.
    """
    try:
        bound = bound_project_root(Path(orchestration_root))
    except HostCommandError:
        raise HistoryError('history_not_authorized') from None
    if bound is None:
        # O10 SB: the project being served, whatever this service runs from. A service setup starts runs
        # from the clone, whose tree holds no install (preparation_failed_at_role_prompt); from the
        # project's own install this is the same folder as before.
        return Path(project_root).resolve()
    if bound != Path(project_root).resolve():
        raise HistoryError('history_not_authorized')
    return bound


def service_history(directory, project_id, **options):
    """O10 HS-1: the project's own service opens its store and upgrades an earlier format, under the
    store's exclusive writer lock (what makes the offline path safe; the service holds it too), whether
    or not the project's learning has moved. The store keeps a copy of itself first (HS-5)."""
    from .project_history import ProjectHistory

    return ProjectHistory(directory, project_id, allow_schema_upgrade=True, **options)


def serve_project(state_root, project_root, *, stopped=None, idle_stop_ns=IDLE_STOP_NS, bundle_root=None,
                  liveness=LIVENESS, retaining_idle_stop_ns=RETAINING_IDLE_STOP_NS):
    """Run the host until a signal, idleness, withdrawal or a writer fault; return a closed report."""

    state = CollectorPaths(state_root)
    root = _canonical_project_root(Path(project_root))
    try:
        project_id = CollectorClient(state.socket, timeout=2).project(root)
    except CollectorError as error:
        return {'ready': False, 'reason_code': 'project_unregistered' if error.code == 'project_unregistered'
                else 'registry_unavailable'}
    directory = history_directory(state, project_id)
    try:
        _owned_directory(directory.parent)
        _owned_directory(directory)
        key = state.digest_key()
    except (StateError, OSError):
        return {'ready': False, 'reason_code': 'history_service_unavailable'}

    def open_history():
        return service_history(directory, project_id, result_key=key, orchestration_limits=ORCHESTRATIONS,
                               result_capacity=_result_capacity())

    try:
        # Children are prepared from the project's installed package (see service_bundle_root).
        host = ProjectServiceHost(state, root, supervisor_scope(state, project_id), open_history,
                                  runtime_limits(liveness), host_limits(),
                                  bundle_root=service_bundle_root(Path(__file__).resolve().parent.parent, root)
                                  if bundle_root is None else Path(bundle_root))
    except HistoryError as error:
        return {'ready': False, 'reason_code': error.reason_code}
    stopping, checked, faulted = [], [0.0], []
    if stopped is None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.append(True))
        stopped = lambda: bool(stopping)  # noqa: E731

    def finished():
        if stopped():
            return True
        if host.faulted():
            # O10 D13: its result writer faulted, so every later store call is refused for good.
            # It stops, holding or not, by the normal stop path below, after one more step: the lane
            # marked its jobs `unknown` with the fault, so that step ends the runs waiting on it
            # (run_unavailable, O10 D12) and their status shows the fault before the service goes.
            if faulted:
                return True
            faulted.append(True)
            return False
        if time.monotonic() - checked[0] >= 1:
            checked[0] = time.monotonic()
            # Stop when idle, or when the state under this host has been removed.
            limit = max(idle_stop_ns, retaining_idle_stop_ns) if host.retaining() else idle_stop_ns
            if host.withdrawn():
                # Asked to stop (its hint was withdrawn): never from under a run it
                # holds. It finishes that run first; no new client can find it meanwhile.
                return not host.holding()
            return host.exhausted() or host.idle_ns() >= limit
        return False

    try:
        host.serve(finished)
    finally:
        host.close()
        end = time.monotonic() + 10
        while not host.threads_stopped() and time.monotonic() < end:
            time.sleep(0.01)
        if faulted:
            # Nothing is settled or released by a service whose writer faulted: what its store
            # committed is not known here. The next host settles its runs and reconciles the store.
            host.settle_lost_runs_later()
        else:
            # A run it still held (a running child it stopped, a finished one not yet
            # released) is settled now on kernel evidence, or by the next host.
            host.settle_lost_runs()
    if faulted:
        return {'ready': True, 'stopped': True, 'reason_code': 'writer_fault'}
    return {'ready': True, 'stopped': True}
