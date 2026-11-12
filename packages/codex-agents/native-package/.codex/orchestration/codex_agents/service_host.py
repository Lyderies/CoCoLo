"""Host process frontends for local services.

Owns the listeners, the enrollment step and the owner loop that a service
runtime itself never creates. A connection reaches a service only after its
first line verifies against this host start's capability; the service then sees
an opaque token, and the role, host class and request digest stay here.

Authority follows 0.2.x: only a process that can write the owner-only state
root can enroll, because each request needs a fresh proof file there. On top of
that a frontend binds role, host class, generation, freshness and, on project
endpoints, the exact request. It does not identify a process. Hints stay hints.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import threading
import time

from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .history_contracts import HistoryError, valid_integer
from .learning_contracts import canonical_json, keyed_digest
from .orchestration_api import ORCHESTRATION_OPERATIONS, OrchestrationScope, orchestration_request_digest
from .orchestration_service import OrchestrationAuthorization
from .project_runtime import ProjectRuntime, ProjectRuntimeLimits
from .service_enrollment import (
    MAX_NONCES, MAX_PREAMBLE_BYTES, MAX_WINDOW_MS, Enrolled, EnrollmentError, EnrollmentVerifier,
    account_endpoint_path, issue_capability, prepare_proof_directory, proof_directory, prune_proofs, revoke_capability,
)

MAX_SOCKET_PATH_BYTES = 100  # Below the smallest supported sun_path, including its terminator.
_KINDS = ('control', 'project')


@dataclass(frozen=True)
class ServiceHostLimits:
    """Trusted startup budgets for work done before a connection is enrolled."""
    pending_connections: int
    enrollment_timeout_ns: int
    contexts: int
    context_lifetime_ns: int
    enrollment_window_ms: int
    max_nonces: int

    def __post_init__(self):
        if (not valid_integer(self.pending_connections, minimum=1, maximum=256)
                or not valid_integer(self.enrollment_timeout_ns, minimum=1_000_000, maximum=60_000_000_000)
                or not valid_integer(self.contexts, minimum=1, maximum=4_096)
                or not valid_integer(self.context_lifetime_ns, minimum=1_000_000, maximum=3_600_000_000_000)
                or not valid_integer(self.enrollment_window_ms, minimum=1, maximum=MAX_WINDOW_MS)
                or not valid_integer(self.max_nonces, minimum=1, maximum=MAX_NONCES)):
            raise HistoryError('history_invalid_input')


@dataclass
class _Pending:
    connection: socket.socket
    kind: str
    deadline_ns: int


@dataclass(frozen=True)
class EnrolledContext:
    """Server-side facts for one enrolled connection; never sent or decoded."""
    enrolled: Enrolled
    kind: str
    deadline_ns: int
    connection: socket.socket


def _peer_is_owner(connection):
    """Supporting check only; the owner-only socket directory is the boundary."""
    if not hasattr(os, 'getuid'):
        return True
    try:
        if sys.platform.startswith('linux'):
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        elif sys.platform == 'darwin':
            # struct xucred from LOCAL_PEERCRED: version, then the effective uid.
            _, uid = struct.unpack_from('II', connection.getsockopt(0, 1, 76))
        else:
            return True
    except (OSError, struct.error):
        return False
    return uid == os.getuid()


def runtime_directory(state, project_root):
    """Short private socket directory; the keyed digest keeps project paths out of it."""
    return state.root / 'rt' / keyed_digest(state.digest_key(), str(project_root))[:16]


def write_owner_file(path, body):
    """Replace one owner-only state file atomically; the parent is already checked."""
    staged = path.with_name(path.name + '.' + str(os.getpid()) + '.new')
    try:
        staged.unlink()  # Only a dead process with this PID could have left it.
    except FileNotFoundError:
        pass
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, body) != len(body):
            raise OSError()
        os.fsync(descriptor)
        os.replace(staged, path)
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


class EnrolledFrontend:
    """Listeners and the enrollment step shared by every service host.

    ``attach(kind, connection, token)`` hands a verified connection to its
    service and raises to refuse it. Tokens are plain objects; ``context(token)``
    is the only way back to what the preamble established.
    """

    def __init__(self, verifier, limits, attach, proofs, *, clock_ns=time.monotonic_ns):
        if (type(verifier) is not EnrollmentVerifier or type(limits) is not ServiceHostLimits
                or not callable(attach) or not callable(clock_ns) or not isinstance(proofs, Path)):
            raise HistoryError('history_invalid_input')
        limits.__post_init__()
        self._verifier, self._limits, self._attach, self._clock_ns = verifier, limits, attach, clock_ns
        self._proofs, self._next_prune_ns = proofs, 0
        self.last_enrolled_ns = clock_ns()
        self._lock = threading.Lock()
        self._contexts, self._pending, self._listeners = {}, [], []
        self._closed = False

    def listen(self, path, kind):
        if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
            raise HistoryError('history_invalid_input')
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()  # Left by an earlier start; the caller holds the owner lock now.
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            listener.bind(str(path))
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(previous)
        self._listeners.append((listener, kind, path))
        path.chmod(0o600)
        listener.listen(min(64, self._limits.pending_connections))
        listener.setblocking(False)

    def context(self, token):
        with self._lock:
            entry = self._contexts.get(token)
            if self._closed or entry is None or self._clock_ns() >= entry.deadline_ns:
                return None
            return entry

    def _accept(self):
        # Each listener has its own unenrolled budget, and the first to be
        # served rotates, so a flood on one endpoint cannot starve another.
        if self._listeners:
            self._listeners.append(self._listeners.pop(0))
        for listener, kind, _ in self._listeners:
            waiting = sum(1 for pending in self._pending if pending.kind == kind)
            while waiting < self._limits.pending_connections:
                try:
                    connection, _ = listener.accept()
                except OSError:  # Includes nothing waiting.
                    break
                connection.setblocking(False)
                if not _peer_is_owner(connection):
                    connection.close()
                    continue
                self._pending.append(_Pending(connection, kind, self._clock_ns() + self._limits.enrollment_timeout_ns))
                waiting += 1

    def _enroll(self, pending):
        """Return True once this connection has been attached or must be closed."""
        connection = pending.connection
        if self._clock_ns() >= pending.deadline_ns:
            return True  # Absolute: a line that completes late is still late.
        try:
            seen = connection.recv(MAX_PREAMBLE_BYTES, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
        newline = seen.find(b'\n')
        if newline < 0:
            # Bytes without a line end never extend the absolute deadline.
            return not seen or len(seen) >= MAX_PREAMBLE_BYTES
        try:
            # Consume exactly the preamble; the request frame stays queued for the service.
            line = connection.recv(newline + 1)
            if line != seen[:newline + 1]:
                return True
            enrolled = self._verifier.verify(line[:-1])
        except (EnrollmentError, OSError):
            return True
        token = object()
        now = self._clock_ns()
        with self._lock:
            self._release_locked(now)
            if self._closed or len(self._contexts) >= self._limits.contexts:
                return True
            self._contexts[token] = EnrolledContext(enrolled, pending.kind, now + self._limits.context_lifetime_ns,
                                                    connection)
        try:
            self._attach(pending.kind, connection, token)
        except Exception:
            with self._lock:
                self._contexts.pop(token, None)
            return True
        pending.connection = None  # Custody moved to the service.
        self.last_enrolled_ns = now
        return True

    def _release_locked(self, now):
        # A service closes the socket when its one exchange ends; that frees
        # the context at once instead of holding a slot until its deadline.
        for token in [key for key, entry in self._contexts.items()
                      if now >= entry.deadline_ns or entry.connection.fileno() < 0]:
            del self._contexts[token]

    def step(self):
        now = self._clock_ns()
        with self._lock:
            self._release_locked(now)
        if now >= self._next_prune_ns:
            self._next_prune_ns = now + 10_000_000_000
            prune_proofs(self._proofs, older_than_seconds=4 * self._limits.enrollment_window_ms / 1000)
        self._accept()
        remaining = []
        for pending in self._pending:
            if self._enroll(pending):
                if pending.connection is not None:
                    pending.connection.close()
            else:
                remaining.append(pending)
        self._pending = remaining

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._contexts.clear()
        for pending in self._pending:
            if pending.connection is not None:
                pending.connection.close()
        self._pending = []
        for listener, _, path in self._listeners:
            listener.close()
            try:
                path.unlink()
            except OSError:
                pass
        self._listeners = []


class ProjectServiceHost:
    def __init__(self, state, project_root, scope, open_history, runtime_limits, limits, *, clock_ns=time.monotonic_ns):
        if (type(state) is not CollectorPaths or type(scope) is not OrchestrationScope
                or type(runtime_limits) is not ProjectRuntimeLimits or type(limits) is not ServiceHostLimits
                or scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST or not callable(open_history) or not callable(clock_ns)):
            raise HistoryError('history_invalid_input')
        limits.__post_init__()
        self._state, self._clock_ns = state, clock_ns
        self._scope = OrchestrationScope(**scope.as_data())
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._closed = self._published = self._issued = False
        self._runtime = self._frontend = self._lock_fd = None
        try:
            self._project_root = _canonical_project_root(Path(project_root))
            directory = runtime_directory(state, self._project_root)
            self._endpoints = {kind: directory / (kind[0] + '.sock') for kind in _KINDS}
            self._hint = state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(self._project_root)) + '.json')
            if any(len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES for path in self._endpoints.values()):
                raise HistoryError('history_invalid_input')
            # The runtime takes the project's exclusive writer lock first, so a
            # second host refuses here, before it can touch another host's files.
            self._runtime = ProjectRuntime(self._scope, open_history, runtime_limits,
                orchestration_authorizer=self._authorize_orchestration, ingress_authorizer=self._authorize_ingress,
                clock_ns=clock_ns)
            for path in (directory.parent, directory, self._hint.parent):
                path.mkdir(mode=0o700, exist_ok=True)
                _assert_no_symlink_components(path)
                _check_owner_mode(path, directory=True)
            # Sockets, hint and capability are keyed by the project root, the
            # writer lock by its history. This lock makes the root exclusive
            # too, whatever history a second host was handed.
            descriptor = os.open(directory / 'host.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.fchmod(descriptor, 0o600)
            self._lock_fd = descriptor
            # A client's liveness probe holds a shared lock for an instant; a
            # live host holds this one for good. Retry briefly to tell them apart.
            for attempt in range(20):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt == 19:
                        raise HistoryError('history_writer_busy') from None
                    time.sleep(0.01)
            proofs = proof_directory(self._endpoints['project'])
            prepare_proof_directory(proofs)
            key = issue_capability(state, self._project_root, 'host')
            self._issued = True
            self._frontend = EnrolledFrontend(
                EnrollmentVerifier(key, 'host', self._scope.supervisor_generation, proofs,
                                   window_ms=limits.enrollment_window_ms, max_nonces=limits.max_nonces),
                limits, self._attach, proofs, clock_ns=clock_ns)
            for kind in _KINDS:
                self._frontend.listen(self._endpoints[kind], kind)
            self._publish()
        except BaseException as error:
            try:
                self.close()
            except BaseException:
                pass  # Preserve the original startup failure.
            if isinstance(error, (StateError, EnrollmentError, OSError, ValueError)):
                raise HistoryError('history_service_unavailable') from None
            raise

    def _owner(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise HistoryError('history_wrong_owner')

    def _attach(self, kind, connection, token):
        if kind == 'control':
            self._runtime.attach_control(connection, token)
        else:
            self._runtime.attach_project(connection, token)

    def _publish(self):
        value = {'schema_version': 1,
            'project_root_digest': keyed_digest(self._state.digest_key(), str(self._project_root)),
            'scope': self._scope.as_data(), 'dispatch_endpoint': str(self._endpoints['control']),
            'result_endpoint': str(self._endpoints['project'])}
        account = account_endpoint_path(self._state)
        try:
            # Name the account endpoint only while one exists; it is a hint either way.
            if stat.S_ISSOCK(account.lstat().st_mode):
                value.update(schema_version=2, account_endpoint=str(account))
        except OSError:
            pass
        write_owner_file(self._hint, canonical_json(value))
        self._published = True

    def _authorize_ingress(self, raw, token, phase):
        # Runs on the ingress thread for the request and again before each send.
        entry = self._frontend.context(token) if self._frontend is not None else None
        return (entry is not None and entry.kind == 'project' and phase in {'request', 'publication'}
                and type(raw) is bytes and hashlib.sha256(raw).hexdigest() == entry.enrolled.request_digest)

    def _authorize_orchestration(self, request, token):
        entry = self._frontend.context(token) if self._frontend is not None else None
        if entry is None or entry.kind != 'project' or entry.enrolled.role != 'host':
            return None
        return OrchestrationAuthorization(self._scope, orchestration_request_digest(request),
                                          entry.enrolled.host_client, ORCHESTRATION_OPERATIONS)

    def serve_once(self):
        self._owner()
        if self._closed:
            raise HistoryError('history_service_unavailable')
        self._frontend.step()
        self._runtime.run_once()

    def serve(self, stopped, *, idle_seconds=0.002):
        while not stopped():
            self.serve_once()
            time.sleep(idle_seconds)

    def endpoints(self):
        return {kind: str(path) for kind, path in self._endpoints.items()}

    def withdrawn(self):
        """True once this host's lock file or hint has been removed from under it."""
        self._owner()
        try:
            return os.fstat(self._lock_fd).st_nlink == 0 or not self._hint.exists()
        except (OSError, TypeError):
            return True

    def idle_ns(self):
        """Time since the last enrolled connection, or 0 while any run is held."""
        self._owner()
        runtime = self._runtime
        if len(runtime.pool._jobs) or len(runtime.persistence._jobs):
            return 0
        return max(0, self._clock_ns() - self._frontend.last_enrolled_ns)

    def close(self):
        """Withdraw the hint and capability first, then stop intake and the runtime."""
        self._owner()
        if self._closed:
            return
        self._closed = True
        if self._published:
            try:
                self._hint.unlink()
            except OSError:
                pass
        if self._issued:
            revoke_capability(self._state, self._project_root, 'host')
        if self._frontend is not None:
            self._frontend.close()
        if self._runtime is not None:
            self._runtime.request_close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # Last: nothing of this host's is left to disturb.
            self._lock_fd = None

    def threads_stopped(self):
        self._owner()
        return self._closed and (self._runtime is None or self._runtime.threads_stopped())
