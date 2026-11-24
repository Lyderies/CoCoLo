"""Client-side start of a project's service host.

Imports no persistence owner: the host runs as its own process from
``project_host.py``, and this module only decides whether one must be started.
"""
import fcntl
import os
from pathlib import Path
import sys
import time

from .client_runtime import _read_runtime_endpoints, runtime_directory, runtime_hint_path
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, StateError, _canonical_project_root
from .dispatch_api import DispatchError
from .host_paths import PYTHON_ISOLATION_FLAGS

START_TIMEOUT_SECONDS = 8.0
WITHDRAW_TIMEOUT_SECONDS = 5.0
# L15.1: how long a project's deletion waits for its idle service to stop (it checks its hint once a second).
STOP_TIMEOUT_SECONDS = 10.0
# R4: how long a start that lost to another start's host (see _spawn_and_wait) waits for that host.
START_COLLISION_GRACE_SECONDS = 3.0
START_COLLISION_REASONS = frozenset({'history_writer_busy', 'history_service_unavailable'})
HOST_ENTRY = 'project_host.py'
HOST_LOCK = 'host.lock'
# Taken in turn by a host publishing its hint, a host starting (from before it takes the host lock
# until it has withdrawn a dead host's hint), a client withdrawing one, and, shared, by a client
# reading a hint with its host's liveness (CI2), so none of them sees the other half-way.
HINT_LOCK = 'hint.lock'
# How long a client waits for that lock before it counts the project as not served now.
HINT_READ_WAIT_SECONDS = 1.0


def _host_alive(directory):
    """Whether a live host holds its lock; the kernel drops it when a host dies.

    None means there is no lock file: the endpoints were not published by a
    product host, so only the hint can be believed. A lock this probe wins is
    released at once, and a host starting in that instant retries.
    """
    descriptor = None
    try:
        try:
            descriptor = os.open(directory / HOST_LOCK, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hinted_host(state, root, *, any_runtime=False, hint_locked=False):
    """The published hint's (scope, endpoints) and its host's liveness (`_host_alive`), or None.

    CI2: a host takes the project's host lock before it withdraws a dead host's hint, so the
    dead hint and the new host's lock are on disk together for a moment, and a client that read
    one and then probed the other took them for a live service: it then used the dead host's
    sockets and failed. A starting host holds HINT_LOCK through that moment, and the hint is read
    and its host probed here under the same lock, shared: either before the new host took its
    lock (the dead hint, nothing alive) or after the dead hint is gone. `hint_locked`: the caller
    holds HINT_LOCK itself. A lock still busy after HINT_READ_WAIT_SECONDS is None, not served now.
    """
    try:
        scope, endpoints = _read_runtime_endpoints(state, root, any_runtime=any_runtime)
    except (DispatchError, StateError, OSError, ValueError):
        return None
    if hint_locked:
        return scope, endpoints, _host_alive(endpoints[0].parent)
    descriptor = None
    try:
        try:
            descriptor = os.open(endpoints[0].parent / HINT_LOCK, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            # No product host published here: only the hint can be believed (as `_host_alive`).
            return scope, endpoints, _host_alive(endpoints[0].parent)
        deadline = time.monotonic() + HINT_READ_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)
        try:  # read again under the lock: the hint read first may be the dead host's, withdrawn since
            scope, endpoints = _read_runtime_endpoints(state, root, any_runtime=any_runtime)
        except (DispatchError, StateError, OSError, ValueError):
            return None
        return scope, endpoints, _host_alive(endpoints[0].parent)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def project_host_serving(state_root, project_root):
    """Whether a live host serves this project now; any failure is a plain False."""
    try:
        hinted = _hinted_host(CollectorPaths(state_root), _canonical_project_root(Path(project_root)))
    except (StateError, OSError, ValueError):
        return False
    return hinted is not None and hinted[2] is not False


def other_version_host(state_root, project_root, *, hint_locked=False):
    """The lock directory of a live host that another runtime version started, else None.

    Such a host cannot serve this version's commands, and while it lives no
    host of this version can start for the project.
    """
    try:
        hinted = _hinted_host(CollectorPaths(state_root), _canonical_project_root(Path(project_root)),
                              any_runtime=True, hint_locked=hint_locked)
    except (StateError, OSError, ValueError):
        return None
    if hinted is None:
        return None
    scope, endpoints, alive = hinted
    return endpoints[0].parent if scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST and alive is True else None


def withdraw_project_host(state_root, project_root, directory, *, timeout=WITHDRAW_TIMEOUT_SECONDS):
    """Ask another version's idle host to stop by withdrawing its published hint; whether none is left.

    The caller has confirmed that no run is active: a host stops whatever it
    holds when its hint is withdrawn. Its journals are durable. The hint is
    read again under the lock a host publishes under, so a host of this version
    that took the project's place since the caller looked is left alone, and
    one that takes it now publishes only after the old hint is gone.
    """
    deadline = time.monotonic() + timeout
    descriptor = None
    try:
        descriptor = os.open(directory / HINT_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        current = other_version_host(state_root, project_root, hint_locked=True)  # this holds HINT_LOCK
        if current is None:
            return True
        if current != directory:
            return False
        runtime_hint_path(CollectorPaths(state_root), Path(project_root)).unlink(missing_ok=True)
    except (DispatchError, StateError, OSError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    while _host_alive(directory) is True:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop_project_host(state_root, project_root, *, project_id, timeout=None):
    """T7 S2 (L15.1): stop the service of the project `project_id` at this root, whatever version started it, by the
    withdrawal path above (its hint removed under HINT_LOCK; the host stops once it holds no run). The caller has
    removed the project's records, so no run is held. A hint naming another project id is a newer registration at
    the same root: its service is not touched (`not_running` for the deleted project). Else: `not_running` when
    no live host serves it, `stopped` once its host lock is released within `timeout`, `still_running` when it
    is not (a wedged host), and `unknown` when the hint cannot be read now (its lock stayed busy): never a
    `not_running` from a reading that did not happen."""
    timeout = STOP_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        state = CollectorPaths(state_root)
        root = _canonical_project_root(Path(project_root))
        hinted = _hinted_host(state, root, any_runtime=True)
        if hinted is None:
            try:
                _read_runtime_endpoints(state, root, any_runtime=True)
            except (DispatchError, StateError, OSError, ValueError):
                return 'not_running'  # no hint to read: nothing is serving this root
            return 'unknown'  # a hint exists but could not be read under its lock now
    except (StateError, OSError, ValueError):
        return 'unknown'
    scope = hinted[0]
    if scope.project_id != project_id or hinted[2] is False:
        return 'not_running'
    if hinted[2] is not True:
        return 'unknown'
    directory = hinted[1][0].parent
    deadline = time.monotonic() + timeout
    descriptor = None
    try:
        descriptor = os.open(directory / HINT_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return 'unknown'
                time.sleep(0.01)
        current = _hinted_host(state, root, any_runtime=True, hint_locked=True)
        if current is None or current[0].project_id != project_id or current[1][0].parent != directory:
            return 'not_running'  # the hint is gone, or another registration's service took the root since
        runtime_hint_path(state, root).unlink(missing_ok=True)
    except (DispatchError, StateError, OSError):
        return 'unknown'
    finally:
        if descriptor is not None:
            os.close(descriptor)
    while _host_alive(directory) is True:
        if time.monotonic() >= deadline:
            return 'still_running'
        time.sleep(0.05)
    return 'stopped'


# O10 HS-4: the fixed reasons a project service reports when it ends at once (project_service.serve_project).
SERVICE_START_REASONS = frozenset({
    'project_unregistered', 'registry_unavailable', 'runtime_unauthorized', 'history_service_unavailable',
    'history_incompatible', 'history_writer_busy', 'history_unavailable', 'history_unsafe_path',
    'history_not_authorized', 'history_invalid_input',
})


def ensure_project_host(state_root, orchestration_root, project_root, *, timeout=START_TIMEOUT_SECONDS, report=None):
    """Start this project's detached host unless a live one already serves.

    A published hint counts only while its host still holds the host lock, so
    a dead host's leftovers are never mistaken for a service and no probe
    connection is made. Returns whether a host is serving; every failure is a
    plain False. Nothing here opens a database or the project's history.
    """
    try:
        state = CollectorPaths(state_root)
        root = _canonical_project_root(Path(project_root))
        entry = (Path(orchestration_root) / HOST_ENTRY).resolve(strict=True)
    except (StateError, OSError, ValueError):
        return False

    def serving():
        try:
            hinted = _hinted_host(state, root)
        except (StateError, OSError):
            return False
        return hinted is not None and hinted[2] is not False

    if serving():
        return True
    try:
        return _start(state, entry, root, serving, timeout, report=report)
    except OSError:
        return False


def _start(state, entry, root, serving, timeout, *, report=None):
    # A detached service outlives this client, so no Popen object is kept to be
    # collected while it runs. Python opens no other descriptor as inheritable.
    # O10 HS-4: its one-line report goes to a private owner-only file, read only if it ends at once.
    import secrets
    report_path = state.root / f'.project-start-{secrets.token_hex(8)}.json'
    actions = [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDWR, 0),
               (os.POSIX_SPAWN_OPEN, 1, str(report_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
               (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_RDWR, 0)]
    try:
        return _spawn_and_wait(state, entry, root, serving, timeout, actions, report_path, report)
    finally:
        try:
            report_path.unlink()
        except OSError:
            pass


def _spawn_and_wait(state, entry, root, serving, timeout, actions, report_path, report):
    pid = os.posix_spawn(sys.executable,
        [sys.executable, *PYTHON_ISOLATION_FLAGS, str(entry), '--state-root', str(state.root), '--project-root', str(root)],
        # No inherited environment, as for the collector.
        {'PATH': os.defpath, 'xcrun_nocache': '1'}, file_actions=actions, setsid=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if serving():
            return True
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            if serving():
                return True  # It lost the race to another starter.
            # CI2: it found the project taken by another host that holds the host lock and has not
            # served yet (another start's, still starting): wait for that one, within this start's time.
            # R4: another start's host may not hold the host lock yet (it takes the history writer lock
            # first), and the start that lost to it ends with one of these two reasons. For a short
            # grace it waits for that host as well; a host that does hold the lock is waited for to
            # the deadline, as above.
            contended = _ended_report(report_path).get('reason_code') in START_COLLISION_REASONS
            grace = time.monotonic() + START_COLLISION_GRACE_SECONDS
            while time.monotonic() < deadline:
                if serving():
                    return True
                if _host_alive(runtime_directory(state, root)) is not True and not (contended and time.monotonic() < grace):
                    break
                time.sleep(0.02)
            if report is not None:
                report.update(_ended_report(report_path))
            return False
        time.sleep(0.02)
    return False


def _ended_report(path):
    """The fixed reason of a service that ended at once, from its private report file; {} otherwise."""
    import json
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        try:
            value = json.loads(os.read(descriptor, 4096) or b'{}')
        finally:
            os.close(descriptor)
    except Exception:  # O10 HS-19: whatever the file holds, never an error
        return {}
    reason = value.get('reason_code') if type(value) is dict else None
    return {'reason_code': reason} if type(reason) is str and reason in SERVICE_START_REASONS else {}
