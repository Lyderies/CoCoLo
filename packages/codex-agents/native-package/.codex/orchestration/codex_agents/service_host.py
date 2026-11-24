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
import base64
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

from .client_runtime import runtime_directory
from .collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .history_contracts import AttemptBinding, HistoryError, valid_integer
from .hosts import OWNER_CLIENT
from .learning_contracts import canonical_json, keyed_digest
from .orchestration_api import ORCHESTRATION_OPERATIONS, OrchestrationScope, orchestration_request_digest
from .orchestration_service import OrchestrationAuthorization
from .control_api import ControlScope, encode_control_request
from .service_transport import refusal_frame
from .control_service import ControlAuthorization
from .service_protocol import service_request_digest
from .history_api import history_request_digest
from .history_service import HistoryAuthorization
from .host_result_contracts import OperationalCheckContext, OperationalQAContext, decode_host_result_reference
from .result_read_api import result_read_request_digest
from .result_read_service import ResultReadAuthorization
from .learning_contracts import parse_bounded_json
from .open_scope_contracts import OpenScopeContext
from .service_enrollment import MAX_CONTEXT_BYTES
from .project_runtime import ProjectRuntime, ProjectRuntimeLimits
from .service_enrollment import (
    MAX_NONCES, MAX_PREAMBLE_BYTES, MAX_WINDOW_MS, Enrolled, EnrollmentError, EnrollmentVerifier,
    account_endpoint_path, issue_capability, prepare_proof_directory, proof_directory, prune_proofs, revoke_capability,
)

MAX_SOCKET_PATH_BYTES = 100  # Below the smallest supported sun_path, including its terminator.
_KINDS = ('control', 'project', 'driver')
# What the owner's terminal label may ask of a project's service.
OWNER_HISTORY_OPERATIONS = frozenset({'history.learning_cycle', 'history.learning_report',
                                      'history.learning_feedback', 'history.learning_review_case',
                                      'history.orchestration_report', 'history.capacity_report', 'history.holds_report'})
DRIVER_ENDPOINT = 'run.sock'  # Beside the control socket; client_runtime uses the same name.
# D1: refusals of the stored record context a caller is told as they are; any other failure is "not authorized".
_PLAIN_CONTEXT_REFUSALS = frozenset({'history_worker_context_unavailable', 'history_result_expired',
                                     'history_result_not_ready'})
# O0d R1: what the record command's lookup of a published attempt also names plainly: no such dispatch, and
# original inputs that differ from the stored ones (a conflict of input, not a refusal of the caller).
_PLAIN_REFERENCE_REFUSALS = _PLAIN_CONTEXT_REFUSALS | {'history_not_found', 'history_conflict'}


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
    refusal: str | None = None  # R7b: the fixed reason sent before the close, if any


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


def _refuse(pending):
    """Best effort, never blocking: the refusal line, if any, before the close (R7b). Only a peer of
    this account reached this point (checked at accept); a write that does not fit is simply dropped."""
    if pending.refusal is None:
        return
    try:
        pending.connection.send(refusal_frame(pending.refusal))
    except OSError:
        pass


def _exclusive_hint_lock(directory):
    """The project's hint lock (`project_host_client.HINT_LOCK`), held exclusively: its descriptor,
    which the caller closes to release it. Its holders keep it briefly, so a few seconds' retries."""
    descriptor = os.open(directory / 'hint.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        for attempt in range(500):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if attempt == 499:
                    raise HistoryError('history_writer_busy') from None
                time.sleep(0.01)
    except BaseException:
        os.close(descriptor)
        raise


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
        # O10 UP-1: no umask change here; the umask is process-wide, so another thread's mkdir(0o700)
        # during this bind came out 0o600 and its owner-only check refused it. Nobody else can reach
        # the socket before the chmod below: its directory must be the owner's alone.
        _assert_no_symlink_components(path.parent)
        _check_owner_mode(path.parent, directory=True)
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()  # Left by an earlier start; the caller holds the owner lock now.
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
        except BaseException:
            listener.close()
            raise
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
            pending.refusal = 'service_refused_late'
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
            if seen and len(seen) >= MAX_PREAMBLE_BYTES:
                pending.refusal = 'service_refused_enrollment'
            return not seen or len(seen) >= MAX_PREAMBLE_BYTES
        try:
            # Consume exactly the preamble; the request frame stays queued for the service.
            line = connection.recv(newline + 1)
            if line != seen[:newline + 1]:
                return True
            enrolled = self._verifier.verify(line[:-1])
        except EnrollmentError:
            pending.refusal = 'service_refused_enrollment'  # never why: the reason is fixed
            return True
        except OSError:
            return True
        token = object()
        now = self._clock_ns()
        with self._lock:
            self._release_locked(now)
            if self._closed or len(self._contexts) >= self._limits.contexts:
                pending.refusal = 'service_refused_unavailable' if self._closed else 'service_refused_busy'
                return True
            self._contexts[token] = EnrolledContext(enrolled, pending.kind, now + self._limits.context_lifetime_ns,
                                                    connection)
        try:
            self._attach(pending.kind, connection, token)
        except Exception as error:
            with self._lock:
                self._contexts.pop(token, None)
            pending.refusal = ('service_refused_busy' if getattr(error, 'reason_code', None) == 'account_busy'
                               else 'service_refused_unavailable')
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
                    _refuse(pending)
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
                pending.refusal = 'service_refused_unavailable'
                _refuse(pending)
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
    def __init__(self, state, project_root, scope, open_history, runtime_limits, limits, *, clock_ns=time.monotonic_ns,
                 bundle_root=None):
        if (type(state) is not CollectorPaths or type(scope) is not OrchestrationScope
                or type(runtime_limits) is not ProjectRuntimeLimits or type(limits) is not ServiceHostLimits
                or scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST or not callable(open_history) or not callable(clock_ns)):
            raise HistoryError('history_invalid_input')
        limits.__post_init__()
        self._state, self._clock_ns = state, clock_ns
        self._scope = OrchestrationScope(**scope.as_data())
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._closed = self._published = self._issued = False
        self._runtime = self._frontend = self._lock_fd = self._driver = None
        try:
            self._project_root = _canonical_project_root(Path(project_root))
            directory = runtime_directory(state, self._project_root)
            self._endpoints = {kind: directory / {'control': 'c.sock', 'project': 'p.sock', 'driver': DRIVER_ENDPOINT}[kind]
                               for kind in _KINDS}
            self._hint = state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(self._project_root)) + '.json')
            if any(len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES for path in self._endpoints.values()):
                raise HistoryError('history_invalid_input')
            # The runtime takes the project's exclusive writer lock first, so a
            # second host refuses here, before it can touch another host's files.
            self._runtime = ProjectRuntime(self._scope, open_history, runtime_limits,
                orchestration_authorizer=self._authorize_orchestration, ingress_authorizer=self._authorize_ingress,
                history_authorizer=self._authorize_history, control_authorizer=self._authorize_control,
                result_authorizer=self._authorize_result,
                clock_ns=clock_ns)
            for path in (directory.parent, directory, self._hint.parent):
                path.mkdir(mode=0o700, exist_ok=True)
                _assert_no_symlink_components(path)
                _check_owner_mode(path, directory=True)
            # Sockets, hint and capability are keyed by the project root, the
            # writer lock by its history. This lock makes the root exclusive
            # too, whatever history a second host was handed.
            # CI2: from before it takes the host lock until a dead host's hint is withdrawn, this host
            # holds the hint lock, which a client reading the hint with its host's liveness shares:
            # no client sees the dead host's hint with this host's lock and takes them for a service.
            hint_lock = _exclusive_hint_lock(directory)
            try:
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
                # A dead host's hint would make this one look served before it listens.
                try:
                    self._hint.unlink()
                except FileNotFoundError:
                    pass
            finally:
                os.close(hint_lock)
            proofs = proof_directory(self._endpoints['project'])
            prepare_proof_directory(proofs)
            key = issue_capability(state, self._project_root, 'host')
            self._issued = True
            self._frontend = EnrolledFrontend(
                EnrollmentVerifier(key, 'host', self._scope.supervisor_generation, proofs,
                                   window_ms=limits.enrollment_window_ms, max_nonces=limits.max_nonces),
                limits, self._attach, proofs, clock_ns=clock_ns)
            if bundle_root is not None:
                from .project_driver import RunDriver
                self._driver = RunDriver(state, self._project_root, bundle_root, self._scope, self._runtime,
                                         history_endpoint=self._endpoints['project'])
                self._driver._enrolled = self._enrolled_control
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
        if kind != 'project' and self._frontend.context(token).enrolled.host_client == OWNER_CLIENT:
            # The owner's terminal has learning commands only; it controls and drives no run.
            raise HistoryError('history_not_authorized')
        if kind == 'control':
            self._runtime.attach_control(connection, token)
        elif kind == 'project':
            self._runtime.attach_project(connection, token)
        elif self._driver is not None:
            self._driver.handle(connection, self._frontend.context(token).enrolled)
        else:
            raise HistoryError('history_service_unavailable')

    def _enrolled_control(self, token):
        entry = self._frontend.context(token) if self._frontend is not None else None
        if entry is None or entry.kind != 'control' or entry.enrolled.role != 'host':
            return None
        return entry.enrolled

    def _authorize_control(self, request, token):
        """Status and cancellation of a run, only for the host class that owns it."""
        enrolled = self._enrolled_control(token)
        if (enrolled is None or self._driver is None or request.operation not in {'project.status', 'project.cancel'}
                or hashlib.sha256(encode_control_request(request)).hexdigest() != enrolled.request_digest
                or not self._driver.owns(request.target, enrolled.host_client)):
            return None
        return ControlAuthorization(ControlScope(**self._scope.as_data()), request.target, service_request_digest(request),
                                    frozenset({'project.status', 'project.cancel'}))

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
        # Publication and a client's withdrawal of another version's hint take this
        # lock in turn, so a hint published here is never the one being withdrawn.
        descriptor = _exclusive_hint_lock(self._endpoints['control'].parent)
        try:
            write_owner_file(self._hint, canonical_json(value))
        finally:
            os.close(descriptor)
        self._published = True

    def _authorize_ingress(self, raw, token, phase):
        # Runs on the ingress thread for the request and again before each send.
        entry = self._frontend.context(token) if self._frontend is not None else None
        return (entry is not None and entry.kind == 'project' and phase in {'request', 'publication'}
                and type(raw) is bytes and hashlib.sha256(raw).hexdigest() == entry.enrolled.request_digest)

    def _authorize_orchestration(self, request, token):
        entry = self._frontend.context(token) if self._frontend is not None else None
        if (entry is None or entry.kind != 'project' or entry.enrolled.role != 'host'
                or entry.enrolled.host_client == OWNER_CLIENT):
            return None
        return OrchestrationAuthorization(self._scope, orchestration_request_digest(request),
                                          entry.enrolled.host_client, ORCHESTRATION_OPERATIONS)

    def _stored(self, read, *, plain=False):
        """D1 (RS-D-2): a run's record context, read from the project store on the writer thread (where every
        authorizer of this host's project services runs), never from the driver's memory. None when it is not this
        host class's or does not verify; with `plain`, a refusal worth naming is raised as it is."""
        if self._driver is None or self._runtime is None:
            return None
        try:
            return read(self._runtime.persistence)
        except HistoryError as error:
            if plain and error.reason_code in (_PLAIN_CONTEXT_REFUSALS if plain is True else plain):
                raise
            return None
        except Exception:
            return None

    def _authorize_result(self, request, token):
        """A run's own result, read by the host class that dispatched it (its stored context names that class)."""
        entry = self._frontend.context(token) if self._frontend is not None else None
        if (entry is None or entry.kind != 'project' or entry.enrolled.role != 'host'
                or entry.enrolled.host_client == OWNER_CLIENT
                or self._stored(lambda lane: lane.stored_result_context(request.binding,
                                                                        entry.enrolled.host_client)) is None):
            return None
        return ResultReadAuthorization(request.binding, request.result_digest, result_read_request_digest(request))

    def _authorize_history(self, request, token):
        """Open scope from the originals sent with the request; a run's records and result admission."""
        entry = self._frontend.context(token) if self._frontend is not None else None
        if entry is None or entry.kind != 'project' or entry.enrolled.role != 'host':
            return None
        if entry.enrolled.host_client == OWNER_CLIENT and request.operation not in OWNER_HISTORY_OPERATIONS:
            return None
        if request.operation == 'history.read_record':
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({'history.read_record'}))
        if request.operation == 'history.admit_host_result':
            try:
                binding = decode_host_result_reference(request.payload).binding
            except Exception:
                return None
            original = self._stored(lambda lane: lane.stored_result_context(binding, entry.enrolled.host_client),
                                    plain=True)
            if original is None:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({'history.admit_host_result', 'history.read_record'}),
                                        host_result_context=original)
        if request.operation in {'history.learning_cycle', 'history.learning_report', 'history.learning_review_case',
                                 'history.orchestration_report', 'history.capacity_report', 'history.holds_report'}:
            # The project's own learning: any enrolled primary of this project may run and
            # read it. Reviewing a case is the owner's command, by the same convention as
            # every earlier version (the installed deny rules keep it from model sessions).
            # O0c M1, M3: the capacity report also says which run slots this host can no longer free.
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({request.operation}),
                                        run_slots=self.stuck_run_slots()
                                        if request.operation == 'history.capacity_report' else None)
        if request.operation == 'history.learning_feedback':
            # The authority is the owner's signature over this record, checked by the
            # account owner that holds the owner key; this process never reads that key.
            try:
                valid = CollectorClient(self._state.socket, timeout=10).verify_owner_record(
                    self._project_root, request.payload['record'],
                    base64.b64decode(request.payload['signature_b64'], validate=True))
            except Exception:
                return None
            if valid is not True:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({request.operation}))
        if request.operation == 'history.result_reference':
            # D1: a published attempt of this host class, named by its run handle, attempt and open inputs.
            payload = request.payload
            original = self._stored(lambda lane: lane.stored_attempt_context(
                payload['run_handle'], payload['attempt'], entry.enrolled.host_client, payload['inputs_digest']),
                plain=_PLAIN_REFERENCE_REFUSALS)
            if original is None:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({request.operation}), host_result_context=original)
        if request.operation in {'history.worker_input', 'history.routing_evidence'}:
            # About a run this host class recorded: its originals are the ones the store keeps for it.
            worker = self._stored(lambda lane: lane.stored_worker_context(
                request.payload.get('host_outcome_digest'), entry.enrolled.host_client))
            if worker is None:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({request.operation}), host_result_context=worker)
        if request.operation in {'history.qa_scope', 'history.issue_qa_checks', 'history.commit_qa', 'history.qa_details'}:
            # QA of a worker and its verifier, both recorded by this host class: the
            # originals are the ones the store keeps for them, never request JSON.
            try:
                bindings = [AttemptBinding(**request.payload[name + '_binding']) for name in ('worker', 'verifier')]
            except Exception:
                return None
            pair = [self._stored(lambda lane, binding=binding: lane.stored_result_context(
                binding, entry.enrolled.host_client, recorded=True)) for binding in bindings]
            if None in pair:
                return None
            qa = OperationalQAContext(*pair)
            check = None
            if request.operation == 'history.issue_qa_checks':
                try:
                    supplied = parse_bounded_json(entry.enrolled.context or b'', max_bytes=MAX_CONTEXT_BYTES)
                    if supplied.keys() != {'purpose', 'report'} or supplied['purpose'] != 'checks':
                        return None
                    check = OperationalCheckContext(qa, supplied['report'])
                    check.snapshot()
                except Exception:
                    return None
            return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                        frozenset({request.operation}), operational_qa_context=qa,
                                        operational_check_context=check)
        if request.operation != 'history.open_scope' or entry.enrolled.context is None:
            return None
        try:
            original = parse_bounded_json(entry.enrolled.context, max_bytes=MAX_CONTEXT_BYTES)
            if (original.keys() != {'purpose', 'orchestration_handle', 'assignment', 'task', 'route', 'parent_grant'}
                    or original['purpose'] != 'open'
                    or original['orchestration_handle'] != request.payload.get('orchestration_handle')):
                return None
            # A verifier binds the worker this host recorded for that outcome; none, no open.
            digest = original['task'].get('verification_for_outcome') if type(original['task']) is dict else None
            worker = None
            if digest is not None:
                worker = self._stored(lambda lane: lane.stored_worker_context(
                    digest, entry.enrolled.host_client, orchestration_handle=original['orchestration_handle']))
                if worker is None:
                    return None
            prospective = OpenScopeContext(self._scope, original['orchestration_handle'], original['assignment'],
                                           original['task'], original['route'], original['parent_grant'], worker)
            prospective.snapshot()
        except Exception:
            return None
        return HistoryAuthorization(request.project_id, request.runtime_digest, history_request_digest(request), 'host',
                                    frozenset({'history.open_scope'}), open_scope_context=prospective)

    def serve_once(self):
        self._owner()
        if self._closed:
            raise HistoryError('history_service_unavailable')
        self._frontend.step()
        self._runtime.run_once()
        if self._driver is not None:
            self._driver.step()

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

    def exhausted(self):
        self._owner()
        return self._driver is not None and self._driver.exhausted()

    def replacing(self):
        """O0c M3: this host holds slots it will never free and no run is under way; it stops once idle."""
        self._owner()
        return self._driver is not None and self._driver.replace_when_idle()

    def client_idle_ns(self):
        """Time since the last enrolled connection, whatever this host holds."""
        return max(0, self._clock_ns() - self._frontend.last_enrolled_ns)

    def stuck_run_slots(self):
        """O0c M1, M3: (slots held that nothing here will free, the run slots there are), for status."""
        from .project_driver import PREPARATION_SLOTS
        return (0 if self._driver is None else self._driver.stuck_slots(), PREPARATION_SLOTS)

    def holding(self):
        """Whether any run or unfinished work for one is held here."""
        self._owner()
        runtime = self._runtime
        return bool(len(runtime.pool._jobs) or len(runtime.persistence._jobs)
                    or (self._driver is not None and self._driver.busy()))

    def faulted(self):
        """O10 D13: whether this host's result writer faulted; it then stops and settles nothing itself."""
        self._owner()
        persistence = self._runtime.persistence
        return persistence is not None and persistence.faulted()

    def settle_lost_runs_later(self):
        """O10 D13: once closed with every thread stopped, free the journal lock and settle nothing here:
        the next host settles this one's runs as those of a stopped instance."""
        if self._driver is not None and self.threads_stopped():
            self._driver.settle_on_exit(settle=False)

    def idle_ns(self):
        """Time since the last enrolled connection, or 0 while any run is held."""
        if self.holding():
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
        if self._driver is not None:
            self._driver.close()
        if self._runtime is not None:
            self._runtime.request_close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # Last: nothing of this host's is left to disturb.
            self._lock_fd = None

    def threads_stopped(self):
        self._owner()
        return self._closed and (self._runtime is None or self._runtime.threads_stopped())

    def settle_lost_runs(self):
        """Once closed with every thread stopped: settle this host's own lost runs, then free its journal lock.

        A host whose threads did not stop keeps the lock until its process
        ends, so no later host settles a run one of them may still launch.
        """
        if self._driver is not None and self.threads_stopped():
            self._driver.settle_on_exit()
