"""Client-side start of a project's service host.

Imports no persistence owner: the host runs as its own process from
``project_host.py``, and this module only decides whether one must be started.
"""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

from .client_runtime import read_runtime_hint
from .collector_paths import CollectorPaths, StateError, _canonical_project_root
from .dispatch_api import DispatchError
from .host_paths import PYTHON_ISOLATION_FLAGS

START_TIMEOUT_SECONDS = 8.0
HOST_ENTRY = 'project_host.py'
HOST_LOCK = 'host.lock'


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


def ensure_project_host(state_root, orchestration_root, project_root, *, timeout=START_TIMEOUT_SECONDS):
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
        return _start(state, entry, root, serving, timeout)
    except OSError:
        return False


def _start(state, entry, root, serving, timeout):
    process = subprocess.Popen(
        [sys.executable, *PYTHON_ISOLATION_FLAGS, str(entry), '--state-root', str(state.root), '--project-root', str(root)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd='/', close_fds=True,
        # No inherited environment, as for the collector.
        env={'PATH': os.defpath, 'xcrun_nocache': '1'}, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if serving():
            return True
        if process.poll() is not None:
            return serving()  # It refused or lost the race to another starter.
        time.sleep(0.02)
    return False
