"""Client-side start of a project's service host.

Imports no persistence owner: the host runs as its own process from
``project_host.py``, and this module only decides whether one must be started.
"""
import fcntl
import os
from pathlib import Path
import sys
import time

from .client_runtime import _read_runtime_endpoints, read_runtime_hint, runtime_hint_path
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, StateError, _canonical_project_root
from .dispatch_api import DispatchError
from .host_paths import PYTHON_ISOLATION_FLAGS

START_TIMEOUT_SECONDS = 8.0
WITHDRAW_TIMEOUT_SECONDS = 5.0
HOST_ENTRY = 'project_host.py'
HOST_LOCK = 'host.lock'
HINT_LOCK = 'hint.lock'  # Taken in turn by a host publishing its hint and a client withdrawing one.


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


def project_host_serving(state_root, project_root):
    """Whether a live host serves this project now; any failure is a plain False."""
    try:
        state = CollectorPaths(state_root)
        _, control, _ = read_runtime_hint(state, _canonical_project_root(Path(project_root)))
    except (DispatchError, StateError, OSError, ValueError):
        return False
    return _host_alive(control.parent) is not False


def other_version_host(state_root, project_root):
    """The lock directory of a live host that another runtime version started, else None.

    Such a host cannot serve this version's commands, and while it lives no
    host of this version can start for the project.
    """
    try:
        state = CollectorPaths(state_root)
        scope, endpoints = _read_runtime_endpoints(
            state, _canonical_project_root(Path(project_root)), any_runtime=True)
    except (DispatchError, StateError, OSError, ValueError):
        return None
    directory = endpoints[0].parent
    return directory if scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST and _host_alive(directory) is True else None


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
        current = other_version_host(state_root, project_root)
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
            _, control, _ = read_runtime_hint(state, root)
        except (DispatchError, StateError, OSError):
            return False
        return _host_alive(control.parent) is not False

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
    except (OSError, ValueError):
        return {}
    reason = value.get('reason_code') if type(value) is dict else None
    return {'reason_code': reason} if type(reason) is str and reason in SERVICE_START_REASONS else {}
