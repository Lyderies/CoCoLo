"""Bounded original export preparation outside control and persistence threads.

Trusted native startup supplies original input bytes and current authorization.
This component provides no enrollment, listener, account admission or launch.
Contexts, callbacks, paths and hashes do not establish native caller authority.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import stat
import threading

from .preparation_reasons import PREPARATION_STAGE_REASONS
from . import bridge
from .codex_worker import MAX_INPUT_BYTES, CodexWorkerError, CodexWorkerPool, _decode_input
from .contracts import validate_assignment
from .history_api import HistoryClient
from .history_contracts import valid_integer
from .host_paths import remove_private_directory
from .learning_contracts import canonical_json, parse_bounded_json
from .open_scope_contracts import OpenScopeContext, open_inputs_digest
from .orchestration_api import OrchestrationScope
from .providers import resolve_client
from .result_contracts import MAX_RESULT_BODY_BYTES, checked_result_body
from .result_read_api import ResultReadClient

MAX_PREPARATION_BYTES = 3*MAX_INPUT_BYTES + MAX_RESULT_BODY_BYTES
# Exported source and the canonical verifier input, excluding provider-owned scratch.
MAX_PREPARATION_EXPORT_BYTES = bridge.MAX_EXPORT_BYTES + MAX_RESULT_BODY_BYTES


# The runtime this code runs from (the folder holding `.codex`): the protected copy,
# a project install or a checkout. Data that no route digest binds, such as the
# result schema, is read from here, as the CLI reads it from ORCHESTRATION_ROOT;
# only digest-bound agent files come from the project's bundle.
RUNTIME_ROOT = Path(__file__).resolve().parents[3]


class PreparationError(RuntimeError):
    def __init__(self, reason='preparation_unavailable'):
        self.reason_code = reason if reason in {'preparation_unavailable', 'preparation_full', 'preparation_busy',
            'preparation_not_ready', *PREPARATION_STAGE_REASONS} else 'preparation_unavailable'
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class PreparationLimits:
    workers: int
    retained_bytes: int
    export_bytes: int

    def __post_init__(self):
        if (not valid_integer(self.workers, minimum=1, maximum=16)
                or not valid_integer(self.retained_bytes, minimum=MAX_PREPARATION_BYTES, maximum=512*1_048_576)
                or not valid_integer(self.export_bytes, minimum=MAX_PREPARATION_EXPORT_BYTES, maximum=512*1_048_576)):
            raise PreparationError('preparation_invalid_input')


@dataclass(frozen=True, repr=False)
class PreparationContext:
    """Original immutable bytes retained by the native adapter, never wire authority."""
    payload: bytes
    _pid: int = field(default_factory=os.getpid, init=False)


@dataclass(frozen=True, repr=False)
class PreparedCodexInputs:
    worker: object
    workspace: Path
    export_info: tuple
    inputs_digest: str
    client_identity: tuple


class PreparationHandle:
    pass


@dataclass(repr=False)
class _Artifact:
    payload: bytes
    workspace: Path
    export_info: tuple
    inputs_digest: str
    client_identity: tuple


@dataclass(repr=False)
class _Job:
    context: PreparationContext
    payload: bytes
    slot: object
    state: str = 'queued'
    cancelled: bool = False
    artifact: _Artifact | None = None
    workspace: Path | None = None
    prepared: PreparedCodexInputs | None = None
    prepared_snapshot: tuple | None = None
    reason: str | None = None


class _Slot:
    def __init__(self):
        self.incoming = queue.Queue(maxsize=1)
        self.ready = threading.Event()
        self.current = None
        self.thread = None


def preparation_payload(prospective, project_root, bundle_root, environ, *, expected_export_commit=None, history_endpoint=None,
                        worker_result=None):
    """Frontend encoding, before controller intake; no I/O or authority is granted.

    A verifier's worker result is either read here through `history_endpoint` or,
    in the project host, supplied as `worker_result` by the owner that already
    read and checked it against the worker's terminal.
    """
    if type(prospective) is not OpenScopeContext or (worker_result is not None and history_endpoint is not None):
        raise PreparationError('preparation_invalid_input')
    raw = canonical_json({'prospective': prospective.snapshot()[1], 'project_root': str(project_root),
        'bundle_root': str(bundle_root), 'environ': environ, 'expected_export_commit': expected_export_commit,
        'history_endpoint': None if history_endpoint is None else str(history_endpoint), 'worker_result': worker_result})
    if not 0 < len(raw) <= MAX_INPUT_BYTES:
        raise PreparationError('preparation_invalid_input')
    return raw


def _remove(workspace):
    try:
        remove_private_directory(workspace)
    except BaseException:
        pass
    # The remover may raise ENOENT for a child. Only observing this directory's
    # absence proves cleanup, whatever the remover returned or raised.
    try:
        workspace.lstat()
    except FileNotFoundError:
        return True
    except BaseException:
        pass
    return False


def remove_recorded_workspace(recorded):
    """Remove a folder an earlier service recorded as not yet removed (PREPCLEAN-4); True once it is observed absent.

    The record is data from the store, never trusted as a path: only this host's own preparation folder (a real
    directory of this user, named `prepare-*`, directly in the scratch container) is removed. A record that names
    anything else is not ours and has nothing to remove (True). Something else standing where our folder was (a link,
    a file, another owner's entry) is left alone and is not proof of removal (False)."""
    try:
        path = Path(recorded)
        container = Path(os.path.realpath(bridge.scratch_base())) / bridge.SCRATCH_CONTAINER
        if (not path.is_absolute() or '..' in path.parts or not path.name.startswith('prepare-')
                or path.parent != container):
            return True
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or os.path.realpath(container) != str(container):
            return False
    except Exception:
        return False
    return _remove(path)


def _worker_input(prospective, endpoint, supplied=None):
    """Read the original verifier input through fixed project APIs, never SQLite."""
    worker = prospective.worker
    digest = prospective.task.get('verification_for_outcome')
    if worker is None:
        if digest is not None or prospective.task['family'] == 'verification' or supplied is not None:
            raise PreparationError('preparation_invalid_input')
        return None
    if (digest is None or prospective.task['family'] != 'verification'
            or not prospective.scope.matches_attempt(worker.binding)
            or worker.binding.orchestration_handle != prospective.orchestration_handle):
        raise PreparationError('preparation_invalid_input')
    if supplied is not None:
        if type(supplied) is not dict:
            raise PreparationError('preparation_invalid_input')
        return supplied
    if type(endpoint) is not str or not Path(endpoint).is_absolute():
        raise PreparationError('preparation_invalid_input')
    history = HistoryClient(Path(endpoint), prospective.scope.project_id, prospective.scope.runtime_digest, timeout=10)
    reference = history.verification_worker_reference(worker.binding, digest)
    entry = history.read_record(reference['terminal_record_id'])
    terminal = entry['record']
    if (entry['metadata']['source'] != 'host' or terminal['run_id'] != worker.binding.run_id
            or terminal['bridge_run_id'] != worker.binding.claim_id
            or terminal['result_digest'] != reference['result_digest']):
        raise PreparationError('preparation_unavailable')
    body = ResultReadClient(Path(endpoint), timeout=10).read_result(worker.binding, reference['result_digest'])
    checked_result_body(body, worker.assignment, terminal)
    return parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)


def _build(payload, scope):
    workspace, started, stage = None, False, 'decode'
    try:
        value = parse_bounded_json(payload, max_bytes=MAX_INPUT_BYTES)
        if (type(value) is not dict or value.keys() != {'prospective', 'project_root', 'bundle_root',
                'environ', 'expected_export_commit', 'history_endpoint', 'worker_result'}):
            raise PreparationError('preparation_invalid_input')
        stage = 'scope'
        prospective = OpenScopeContext.from_snapshot(value['prospective'])
        if prospective.scope != scope:
            raise PreparationError('preparation_not_authorized')
        stage = 'assignment'
        assignment = validate_assignment(prospective.assignment, task=prospective.task,
            route=prospective.route, parent_grant=prospective.parent_grant)
        route, environment = prospective.route, value['environ']
        stage = 'route'
        provider = route.get('provider')
        claude = provider == 'claude'
        if (provider not in {'codex', 'claude'} or route.get('dispatch') != 'bridge'
                or route.get('sandbox_mode') != 'read-only' or assignment['permissions']['network'] is not False
                or assignment['permissions']['mcps'] or any(not p.endswith(':read') for p in assignment['permissions']['filesystem'])
                or type(environment) is not dict or len(environment) > 256
                or any(type(k) is not str or type(v) is not str or len(k) > 256 or len(v) > 16_384 for k,v in environment.items())
                or bridge.api_keys_present(environment)):
            raise PreparationError('preparation_invalid_input')
        if bridge.managed_configuration_present() if claude else bridge.codex_managed_configuration_present():
            raise PreparationError('preparation_managed_configuration')
        stage = 'roots'
        for name in ('project_root', 'bundle_root'):
            if (type(value[name]) is not str or not 0 < len(value[name]) <= 4096
                    or not Path(value[name]).is_absolute() or '..' in Path(value[name]).parts):
                raise PreparationError('preparation_invalid_input')
        root, bundle = Path(value['project_root']), Path(value['bundle_root'])
        stage = 'role_prompt'
        role_prompt = bridge._claude_role_prompt(bundle, route) if claude else bridge._codex_role_prompt(bundle, route)
        # O10 VF-1: both providers' children are held to the same result schema.
        from .learning_engine import _read_managed_bytes
        schema = _read_managed_bytes(RUNTIME_ROOT, '.codex/orchestration/schemas/result.schema.json',
                                     max_bytes=MAX_RESULT_BODY_BYTES)
        bridge.codex_schemas(schema)
        stage = 'worker_input'
        worker_result = _worker_input(prospective, value['history_endpoint'], value['worker_result'])
        stage = 'read_paths'
        paths = bridge.assignment_read_paths(assignment, worker_outcome_digest=prospective.task.get('verification_for_outcome'))
        stage = 'client'
        identity = resolve_client(provider, untrusted=[root])
        if identity[0] is None:
            raise PreparationError('preparation_client_missing')
        started = True
        stage = 'workspace'
        workspace = bridge.private_workspace('prepare-', claude=claude, project_root=root)
        try:
            metadata = bridge.export_committed_snapshot(root, paths, workspace/'export',
                expected_commit=value['expected_export_commit'])
        except Exception:
            raise PreparationError('preparation_export_failed') from None
        stage = 'worker_result'
        worker_path = None if worker_result is None else bridge.place_worker_result(workspace/'export', worker_result)
        if claude:
            # The same metered-backend refusal an open applies, against the export.
            from .claude_host import check_backend
            try:
                check_backend(environment, workspace/'export')
            except Exception:
                raise PreparationError('preparation_backend_refused') from None
        elif not set(bridge.CODEX_REQUIRED_EXEC_OPTIONS) <= bridge.codex_exec_options(
                identity[0], environ=environment, tmpdir=workspace/'tmp'):
            raise PreparationError('preparation_client_unsupported')
        stage = 'client_recheck'
        if resolve_client(provider, untrusted=[root]) != identity:
            raise PreparationError('preparation_client_changed')
        stage = 'body'
        body = canonical_json({'task': prospective.task, 'route': route, 'assignment': assignment,
            'parent_grant': prospective.parent_grant, 'schema': None if schema is None else schema.decode('utf-8'),
            'export': str(workspace/'export'), 'control': str(workspace/'control'), 'executable': identity[0],
            'environ': environment, 'role_prompt': role_prompt, 'worker_result_path': worker_path})
        if len(body) > MAX_INPUT_BYTES:
            raise PreparationError('preparation_invalid_input')
        stage = 'decode_check'
        _decode_input(body)  # The original execution decoder must accept it before account readiness.
        artifact = _Artifact(body, workspace, (metadata['file_count'], metadata['commit'], metadata['manifest_digest']),
            open_inputs_digest(assignment, prospective.task, route, prospective.parent_grant), identity)
        return artifact, None, workspace, False
    except BaseException as error:
        # No exception text, traceback or rejected/private input is retained: only the
        # fixed reason of the stage that refused, if it was one.
        reason = (getattr(error, 'reason_code', None) if type(error) is PreparationError
                  else 'preparation_failed_at_' + stage)
        reason = reason if reason in PREPARATION_STAGE_REASONS else 'preparation_unavailable'
        if workspace is not None:
            clean = _remove(workspace)
            return None, reason, None if clean else workspace, not clean
        return None, reason, None, started


class CodexPreparationLane:
    def __init__(self, scope, pool, limits, *, authorizer=None):
        if (type(scope) is not OrchestrationScope or type(pool) is not CodexWorkerPool
                or type(limits) is not PreparationLimits or (authorizer is not None and not callable(authorizer))):
            raise PreparationError('preparation_invalid_input')
        pool._owner()
        limits.__post_init__()
        self._limits = PreparationLimits(limits.workers, limits.retained_bytes, limits.export_bytes)
        self._reserved_bytes = self._reserved_export_bytes = 0
        self._scope, self._pool, self._authorizer = OrchestrationScope(**scope.as_data()), pool, authorizer
        self._pid, self._owner_thread = os.getpid(), threading.current_thread()
        self._lock, self._jobs = threading.Lock(), {}
        self._slots = [_Slot() for _ in range(limits.workers)]
        self._closing = self._busy = False
        try:
            for slot in self._slots:
                slot.thread = threading.Thread(target=self._worker, args=(slot,), daemon=True)
                slot.thread.start()
                if not slot.ready.wait(5):
                    raise PreparationError()
        except BaseException:
            self.request_close()
            raise

    def _owner(self):
        if self._pid != os.getpid() or self._owner_thread is not threading.current_thread():
            raise PreparationError('preparation_not_authorized')
        self._pool._owner()

    def _job(self, handle):
        self._owner()
        if type(handle) is not PreparationHandle or handle not in self._jobs:
            raise PreparationError('preparation_not_authorized')
        return self._jobs[handle]

    def _authorize(self, job):
        self._owner()
        if (self._closing or self._authorizer is None or job.context._pid != self._pid
                or job.context.payload is not job.payload or job.cancelled):
            raise PreparationError('preparation_not_authorized')
        accepted = self._authorizer(job.context)
        if (accepted is not True or self._closing or job.context.payload is not job.payload or job.cancelled):
            raise PreparationError('preparation_not_authorized')

    def submit(self, context):
        self._owner()
        if (self._busy or type(context) is not PreparationContext or type(context.payload) is not bytes
                or not 0 < len(context.payload) <= MAX_INPUT_BYTES):
            raise PreparationError('preparation_invalid_input')
        self._busy = True
        try:
            job = _Job(context, context.payload, None)
            self._authorize(job)
            with self._lock:
                slot = next((s for s in self._slots if s.current is None and s.thread.is_alive()), None)
                if (slot is None or self._reserved_bytes+MAX_PREPARATION_BYTES > self._limits.retained_bytes
                        or self._reserved_export_bytes+MAX_PREPARATION_EXPORT_BYTES > self._limits.export_bytes):
                    raise PreparationError('preparation_full')
                handle, job.slot = PreparationHandle(), slot
                self._jobs[handle], slot.current = job, handle
                self._reserved_bytes += MAX_PREPARATION_BYTES
                self._reserved_export_bytes += MAX_PREPARATION_EXPORT_BYTES
                slot.incoming.put_nowait(handle)
            return handle
        finally:
            self._busy = False

    def drive(self):
        self._owner()
        if self._busy:
            raise PreparationError()
        self._busy = True
        try:
            for handle, job in tuple(self._jobs.items()):
                with self._lock:
                    exported = job.state == 'exported'
                if not exported:
                    continue
                try:
                    self._authorize(job)
                    worker = self._pool.prepare(job.artifact.payload, client_sha256=job.artifact.client_identity[1])
                    job.prepared = PreparedCodexInputs(worker, job.workspace, job.artifact.export_info,
                        job.artifact.inputs_digest, job.artifact.client_identity)
                    job.prepared_snapshot = (worker, job.workspace, job.artifact.export_info,
                        job.artifact.inputs_digest, job.artifact.client_identity)
                    self._authorize(job)
                    with self._lock:
                        job.artifact, job.state = None, 'ready'
                except Exception as error:
                    # Which part of the handoff refused: authorization, a busy pool, or other.
                    job.reason = (error.reason_code if type(error) is PreparationError
                                  and error.reason_code in PREPARATION_STAGE_REASONS
                                  else 'preparation_worker_busy' if type(error) is CodexWorkerError
                                  and error.reason_code == 'worker_busy' else 'preparation_worker_failed')
                    self.discard(handle)
        finally:
            self._busy = False

    def prepared(self, handle):
        job = self._job(handle)
        if self._busy:
            raise PreparationError()
        self._busy = True
        try:
            self._authorize(job)
            with self._lock:
                if job.state != 'ready':
                    raise PreparationError('preparation_not_ready')
                value = job.prepared
                if (value.worker, value.workspace, value.export_info, value.inputs_digest,
                        value.client_identity) != job.prepared_snapshot:
                    raise PreparationError('preparation_not_authorized')
                return value
        finally:
            self._busy = False

    def snapshot(self, handle):
        job = self._job(handle)
        with self._lock:
            return {'state': job.state, 'reason_code': job.reason}

    def capacity(self):
        """Reserved serialized buffers and export contents, not RSS or filesystem quota."""
        self._owner()
        with self._lock:
            return {'retained_bytes_reserved': self._reserved_bytes,
                'export_bytes_reserved': self._reserved_export_bytes,
                'retained_bytes_limit': self._limits.retained_bytes,
                'export_bytes_limit': self._limits.export_bytes, 'jobs': len(self._jobs)}

    def discard(self, handle):
        job = self._job(handle)
        if job.prepared is not None:
            worker = job.prepared_snapshot[0]
            current = self._pool._jobs.get(worker)
            if current is not None:
                if current.state != 'prepared' or current.ticket is not None:
                    raise PreparationError('preparation_busy')
                self._pool.withdraw_prepared(worker)
        with self._lock:
            job.cancelled = True
            if job.state in {'queued', 'exporting', 'cleanup', 'cleaned', 'failed', 'unknown'}:
                return
            if not job.slot.thread.is_alive():
                job.state, job.reason = 'unknown', 'preparation_unavailable'
                raise PreparationError()
            job.state = 'cleanup'
            try:
                job.slot.incoming.put_nowait(handle)
            except queue.Full:
                job.state, job.reason = 'unknown', 'preparation_unavailable'
                raise PreparationError() from None

    def retry_cleanup(self, handle):
        """Ask the slot's worker again to remove a discarded preparation whose cleanup did not finish.

        Only for a job already given back ('unknown' after `discard`) whose
        worker no longer holds it in the pool and whose slot thread still runs.
        """
        job = self._job(handle)
        with self._lock:
            if job.prepared is not None and (job.prepared_snapshot is None
                                             or self._pool._jobs.get(job.prepared_snapshot[0]) is not None):
                raise PreparationError('preparation_busy')
            # Without a known workspace there is nothing to name for removal, so it stays unknown.
            if (job.state != 'unknown' or not job.cancelled or self._closing or job.workspace is None
                    or job.slot.thread is None or not job.slot.thread.is_alive() or job.slot.current != handle):
                raise PreparationError('preparation_busy')
            job.state = 'cleanup'
            try:
                job.slot.incoming.put_nowait(handle)
            except queue.Full:
                job.state = 'unknown'
                raise PreparationError() from None

    def workspace_of(self, handle):
        """The folder of a given-back preparation whose removal is not observed, or None (PREPCLEAN-4)."""
        job = self._job(handle)
        with self._lock:
            return job.workspace if job.state == 'unknown' and job.cancelled else None

    def unlocated(self, handle):
        """Whether a given-back preparation's removal is unconfirmed and no folder is known for it (PREPCLEAN-3)."""
        job = self._job(handle)
        with self._lock:
            return job.state == 'unknown' and job.cancelled and job.workspace is None

    def retire(self, handle):
        job = self._job(handle)
        with self._lock:
            if job.state not in {'cleaned', 'failed'} or job.workspace is not None:
                raise PreparationError('preparation_busy')
            self._jobs.pop(handle)
            job.slot.current = None
            self._reserved_bytes -= MAX_PREPARATION_BYTES
            self._reserved_export_bytes -= MAX_PREPARATION_EXPORT_BYTES

    def request_close(self, *, abandon_bound=False):
        self._owner()
        if type(abandon_bound) is not bool or (abandon_bound and not self._pool._closed):
            raise PreparationError('preparation_invalid_input')
        self._closing = True
        for handle in tuple(self._jobs):
            try:
                self.discard(handle)
            except PreparationError:
                pass  # Bound executions retain original workspace/capacity custody.
        with self._lock:
            for slot in self._slots:
                job = self._jobs.get(slot.current)
                if job is not None and job.state == 'ready':
                    if not abandon_bound:
                        continue  # Keep cleanup available after the original execution retires.
                    job.state, job.reason = 'unknown', 'preparation_unavailable'
                if job is None or job.state not in {'queued', 'exporting', 'cleanup'}:
                    try:
                        slot.incoming.put_nowait(None)
                    except queue.Full:
                        pass

    def threads_stopped(self):
        self._owner()
        return self._closing and all(s.thread is None or not s.thread.is_alive() for s in self._slots)

    def _worker(self, slot):
        slot.ready.set()
        try:
            while True:
                handle = slot.incoming.get()
                if handle is None:
                    return
                with self._lock:
                    job = self._jobs[handle]
                    state = job.state
                    if state == 'queued' and job.cancelled:
                        job.state = 'cleaned'
                        if self._closing:
                            return
                        job = handle = None
                        continue
                    if state == 'queued':
                        job.state = 'exporting'
                if state == 'queued':
                    artifact, reason, workspace, unknown = _build(job.payload, self._scope)
                    with self._lock:
                        job.artifact, job.reason, job.workspace = artifact, reason, workspace
                        cleanup = artifact is not None and (job.cancelled or self._closing)
                        job.state = 'cleanup' if cleanup else ('exported' if artifact is not None else ('unknown' if unknown else 'failed'))
                        stop = self._closing
                    if not cleanup:
                        if stop:
                            return
                        artifact = workspace = job = handle = None
                        continue
                elif state != 'cleanup':
                    raise PreparationError()
                clean = _remove(job.workspace)
                with self._lock:
                    job.artifact = None
                    if clean:
                        job.workspace, job.state = None, 'cleaned'
                    else:
                        job.state, job.reason = 'unknown', 'preparation_unavailable'
                    if self._closing:
                        return
                artifact = workspace = job = handle = None
        except BaseException:
            with self._lock:
                job = self._jobs.get(slot.current)
                if job is not None:
                    job.state, job.reason = 'unknown', 'preparation_unavailable'
