"""Trusted startup of one project's service host.

``project_host.py`` runs ``serve_project`` as a detached process, started by
the checked CLI entry the same way it starts the collector. Budgets here are installed policy for that
process, never values from a request. The registry lookup goes through the
collector; this process opens only its own project's history.
"""
from pathlib import Path
import os
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
# D1: a recorded run's originals are in the project store, so a host keeps nothing a later verifier or QA needs and
# stops on this one rule (the 12-hour rule for a host holding recorded workers is gone).
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


def _empty_directory(descriptor, depth=0, keep=()):
    """Remove everything under an open directory, never following a symlink (it goes itself, not its target).
    Names in `keep` are left in the top directory."""
    import stat
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        for name in os.listdir(descriptor):
            if name in keep:
                continue
            if stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
                if depth >= 4:
                    return False
                child = os.open(name, flags, dir_fd=descriptor)
                try:
                    if not _empty_directory(child, depth + 1):
                        return False
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
    except OSError:
        return False
    return True


def _safe_store_directory(state, project_id):
    """The project's store directory opened component by component without following a link, each one a directory
    of this user (s2fix round 3). The descriptor, or None when a component is a link, foreign or not a directory."""
    import stat
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        _assert_no_symlink_components(Path(state.root))
        descriptor = os.open(state.root, flags)
    except OSError:
        return None
    except StateError:
        return None
    try:
        for name in ('projects', project_id):
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise OSError('foreign or not a directory')
        return descriptor
    except OSError:
        os.close(descriptor)
        return None


def remove_project_history(state, project_id):
    """s2fix round 2 (L15.1): a deleted project's own history store (`history.sqlite3` with its journals and older
    copies) removed, once no service holds its writer lock. True when nothing of it is left; False when a service
    still holds it (the caller says so), a path component is a link or not this user's, or a file could not be
    removed. `history.lock` itself is kept (round 3): removing the file we hold would let another writer take a
    replacement lock; an empty lock file is all that remains."""
    import fcntl
    import re
    if re.fullmatch(r'prj_[0-9a-f]{32}', project_id) is None:
        return False
    if not os.path.lexists(Path(state.root) / 'projects' / project_id):
        return True
    descriptor = _safe_store_directory(state, project_id)
    if descriptor is None:
        return False
    try:
        try:
            lock = os.open('history.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=descriptor)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return _empty_directory(descriptor, keep=('history.lock',))
        finally:
            os.close(lock)
    finally:
        os.close(descriptor)


def _pending_directory(state):
    return Path(state.root) / 'pending-history-cleanup'


def mark_pending_cleanup(state, project_id, project_root):
    """A durable marker that this deleted project's history store is not yet removed (s2fix round 3, decision 185.4):
    the deletion clears the root binding, so a retry cannot find the project by it; the marker keeps the way."""
    import json
    directory = _pending_directory(state)
    _owned_directory(directory)
    path = directory / project_id
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(json.dumps({'project_root': str(project_root)}))
        handle.flush()
        os.fsync(handle.fileno())


def pending_cleanups(state):
    """The (project id, project root) pairs whose history store removal is still pending."""
    import json
    directory = _pending_directory(state)
    found = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        try:
            with open(directory / name) as handle:
                found.append((name, json.load(handle)['project_root']))
        except (OSError, ValueError, KeyError):
            continue
    return found


def clear_pending_cleanup(state, project_id):
    try:
        os.unlink(_pending_directory(state) / project_id)
    except OSError:
        pass


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


def _final_next_due(directory):
    """RS-E E2a-8: the store's next due, read once more after settlement on a read-only connection of its own."""
    import sqlite3
    from .project_holds_schema import next_hold_due
    connection = sqlite3.connect((Path(directory) / 'history.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        return next_hold_due(connection)
    finally:
        connection.close()


def serve_project(state_root, project_root, *, stopped=None, idle_stop_ns=IDLE_STOP_NS, bundle_root=None,
                  liveness=LIVENESS):
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

    # RS-E E2a-7: this instance's wake publications; every hold write of this project tells it first.
    from . import project_holds_schema
    from .project_wake import WakePublisher
    wake = WakePublisher(state, root, project_id)
    project_holds_schema.install_wake_guard(project_id, wake)

    def open_history():
        history = service_history(directory, project_id, result_key=key, orchestration_limits=ORCHESTRATIONS,
                                  result_capacity=_result_capacity())
        wake.open(history._connection)  # pending (this instance's epoch), then exact; once
        return history

    try:
        # Children are prepared from the project's installed package (see service_bundle_root).
        host = ProjectServiceHost(state, root, supervisor_scope(state, project_id), open_history,
                                  runtime_limits(liveness), host_limits(),
                                  bundle_root=service_bundle_root(Path(__file__).resolve().parent.parent, root)
                                  if bundle_root is None else Path(bundle_root))
    except HistoryError as error:
        wake.close()
        project_holds_schema.install_wake_guard(project_id, None)
        return {'ready': False, 'reason_code': error.reason_code}
    stopping, checked, faulted, settled = [], [0.0], [], False
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
                from .health_holds import stop_faulted_service
                stop_faulted_service(host, project_id)
                return True
            faulted.append(True)
            return False
        if time.monotonic() - checked[0] >= 1:
            checked[0] = time.monotonic()
            # Stop when idle, or when the state under this host has been removed.
            if host.withdrawn():
                # Asked to stop (its hint was withdrawn): never from under a run it
                # holds. It finishes that run first; no new client can find it meanwhile.
                # A folder removal still owed never keeps it: that is recorded for the next service.
                return not host.work_held()
            # O0c M3: a host holding slots it will never free is replaced once idle (no run under way
            # and no client for the idle period), whatever those holds; at once if none is left.
            return (host.exhausted() or host.idle_ns() >= idle_stop_ns
                    or (host.replacing() and host.client_idle_ns() >= idle_stop_ns))
        return False

    try:
        host.serve(finished)
    finally:
        host.close()
        end = time.monotonic() + 10
        while not host.threads_stopped() and time.monotonic() < end:
            time.sleep(0.01)
        if host.faulted() and not faulted:
            # Decision 179: a stop signal ends the serve loop before its own fault check; the writer fault is
            # checked once more here, so it is never settled or published as a normal stop.
            faulted.append(True)
        if faulted:
            # Nothing is settled or released by a service whose writer faulted: what its store
            # committed is not known here. The next host settles its runs and reconciles the store.
            host.settle_lost_runs_later()
        else:
            # A run it still held (a running child it stopped, a finished one not yet
            # released) is settled now on kernel evidence, or by the next host.
            settled = host.settle_lost_runs()
        # RS-E E2a-8: only after verified final settlement (every thread stopped, every run released) and a
        # successful final read of the store's next due, clean_stop within 2 s, best effort; otherwise, and after
        # a writer fault, nothing, so the collector keeps this project's wake state unknown.
        try:
            wake.finish(faulted=bool(faulted),
                        final_due=(lambda: _final_next_due(directory)) if not faulted and settled else None)
        finally:
            project_holds_schema.install_wake_guard(project_id, None)
    if faulted:
        return {'ready': True, 'stopped': True, 'reason_code': 'writer_fault'}
    return {'ready': True, 'stopped': True}
