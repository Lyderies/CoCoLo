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
MAX_ACTIVE_ATTEMPTS = 4
# A host is started on demand, so one with no run and no client for this long
# stops; the next command starts another. Its journals and scope are durable.
IDLE_STOP_NS = 900 * SECOND
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
    return ResultCapacity(64, MAX_ACTIVE_ATTEMPTS * MAX_RESULT_BODY_BYTES, 1_800_000, 7 * 86_400_000)


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


def serve_project(state_root, project_root, *, stopped=None, idle_stop_ns=IDLE_STOP_NS, bundle_root=None,
                  liveness=LIVENESS):
    """Run the host until a signal, idleness or withdrawal; return a closed report."""
    from .project_history import ProjectHistory

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
        return ProjectHistory(directory, project_id, result_key=key, orchestration_limits=ORCHESTRATIONS,
                              result_capacity=_result_capacity())

    try:
        # The installed package this host runs from is the bundle a child is prepared from.
        host = ProjectServiceHost(state, root, supervisor_scope(state, project_id), open_history,
                                  runtime_limits(liveness), host_limits(),
                                  bundle_root=Path(__file__).resolve().parents[3] if bundle_root is None else Path(bundle_root))
    except HistoryError as error:
        return {'ready': False, 'reason_code': error.reason_code}
    stopping, checked = [], [0.0]
    if stopped is None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.append(True))
        stopped = lambda: bool(stopping)  # noqa: E731

    def finished():
        if stopped():
            return True
        if time.monotonic() - checked[0] >= 1:
            checked[0] = time.monotonic()
            # Stop when idle, or when the state under this host has been removed.
            return host.withdrawn() or host.exhausted() or host.idle_ns() >= idle_stop_ns
        return False

    try:
        host.serve(finished)
    finally:
        host.close()
        end = time.monotonic() + 10
        while not host.threads_stopped() and time.monotonic() < end:
            time.sleep(0.01)
    return {'ready': True, 'stopped': True}
