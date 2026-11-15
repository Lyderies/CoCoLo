"""Fixed SQL-free probe preparation/execution and original process custody.

The coordinator retains admission and native authorization. Input values and
prepared metadata establish no caller authority. Only fixed data crosses into
workers; no account owner, callback, ledger or SQLite connection is passed.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import queue
import threading
import time

from . import bridge
from .admission_metadata import _headroom_state
from .history_contracts import _DIGEST, _PROJECT, matches, valid_integer
from .learning_contracts import canonical_json, parse_bounded_json_value, validate_learning_record
from .owned_execution import ExecutionTicket, OwnedExecutionKey, OwnedExecutionRegistry
from .provider_restrictions import BILLING_RANK, merged_restriction
from .providers import claude_rate_limit_records, client_environment, resolve_client

MAX_PROBE_INPUT_BYTES = 128*1024
MAX_PROBE_PACKET_BYTES = 16*1024
PROBE_SLOT_BYTES = MAX_PROBE_INPUT_BYTES+MAX_PROBE_PACKET_BYTES+bridge.MAX_STREAM_BYTES


class ProbeWorkerError(RuntimeError):
    def __init__(self, reason='probe_worker_unavailable'):
        self.reason_code = reason if reason in {'probe_worker_unavailable', 'probe_worker_full',
            'probe_worker_invalid', 'probe_worker_not_ready', 'probe_worker_busy'} else 'probe_worker_unavailable'
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class ProbeWorkerLimits:
    workers: int
    retained_bytes: int

    def __post_init__(self):
        if (not valid_integer(self.workers, minimum=1, maximum=16)
                or not valid_integer(self.retained_bytes, minimum=self.workers*PROBE_SLOT_BYTES,
                                     maximum=512*1_048_576)):
            raise ProbeWorkerError('probe_worker_invalid')


@dataclass(frozen=True, repr=False)
class PreparedBillingProbe:
    workspace: Path
    arguments: tuple
    environment: tuple
    client_identity: tuple
    identity: bytes
    key: OwnedExecutionKey
    run_handle: str
    deadline_ns: int
    inputs_digest: str


@dataclass(frozen=True, repr=False)
class BillingProbePacket:
    body: bytes
    inputs_digest: str
    workspace_removed: bool
    reason_code: str | None


class ProbeWorkerHandle:
    pass


@dataclass(repr=False)
class _Job:
    payload: bytes
    slot: object
    state: str = 'preparing'
    ticket: ExecutionTicket | None = None
    supervisor: object = None
    prepared: PreparedBillingProbe | None = None
    snapshot: tuple | None = None
    packet: BillingProbePacket | None = None
    packet_snapshot: tuple | None = None
    workspace: Path | None = None
    cancelled: bool = False


class _Slot:
    def __init__(self):
        self.incoming = queue.Queue(maxsize=1)
        self.ready, self.stop = threading.Event(), threading.Event()
        self.thread, self.current, self.fault = None, None, False


def probe_payload(project, request_key, root, identity, *, deadline_ns, created_at_ms, language='C.UTF-8'):
    """Encode trusted captured values before controller intake; grant no authority."""
    raw = canonical_json({'project_id': project, 'request_key': request_key, 'root': str(root),
        'identity': identity, 'deadline_ns': deadline_ns, 'created_at_ms': created_at_ms, 'language': language})
    if not 0 < len(raw) <= MAX_PROBE_INPUT_BYTES:
        raise ProbeWorkerError('probe_worker_invalid')
    return raw


def _remove(workspace):
    if workspace is None:
        return True
    try:
        bridge.remove_private_directory(workspace)
        workspace.lstat()
    except FileNotFoundError:
        return True
    except BaseException:
        pass
    return False


def _prepared_snapshot(prepared):
    return (prepared.workspace, prepared.arguments, prepared.environment, prepared.client_identity,
        prepared.identity, (prepared.key.project_id, prepared.key.handle_digest, prepared.key.run_id,
        prepared.key.bridge_run_id), prepared.run_handle, prepared.deadline_ns, prepared.inputs_digest)


def _prepare(payload):
    workspace, started = None, False
    try:
        value = parse_bounded_json_value(payload, max_bytes=MAX_PROBE_INPUT_BYTES)
        if (type(value) is not dict or value.keys() != {'project_id', 'request_key', 'root', 'identity',
                'deadline_ns', 'created_at_ms', 'language'} or not matches(value['project_id'], _PROJECT)
                or not matches(value['request_key'], _DIGEST) or not valid_integer(value['deadline_ns'], minimum=1)
                or not valid_integer(value['created_at_ms']) or type(value['language']) is not str
                or not 0 < len(value['language']) <= 128 or type(value['root']) is not str
                or not 0 < len(value['root']) <= 4096 or not Path(value['root']).is_absolute()
                or '..' in Path(value['root']).parts or '\0' in value['root']):
            raise ProbeWorkerError('probe_worker_invalid')
        identity = validate_learning_record(value['identity'])
        if identity['record_type'] != 'provider_identity' or identity['provider'] != 'claude':
            raise ProbeWorkerError('probe_worker_invalid')
        root, environment = Path(value['root']), {'LANG': value['language']}
        from .claude_host import check_backend
        if bridge.api_keys_present(os.environ) or bridge.managed_configuration_present():
            raise ProbeWorkerError()
        check_backend(os.environ, root)
        client = resolve_client('claude', untrusted=[root])
        if client != (identity['client_path'], identity['client_sha256']):
            raise ProbeWorkerError()
        started = True
        workspace = bridge.private_workspace('probe-prepared-', claude=True, project_root=root)
        settings = bridge._private_file(workspace/'control/settings.json',
            canonical_json(bridge.claude_settings(bridge.PROBE_MODEL)))
        arguments = (client[0], '-p', '--model', bridge.PROBE_MODEL, '--effort', bridge.PROBE_EFFORT,
            '--tools', '', '--permission-mode', 'dontAsk', '--setting-sources', 'project', '--settings', str(settings),
            '--strict-mcp-config', '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
            '--max-turns', '1', '--', bridge.PROBE_PROMPT)
        if resolve_client('claude', untrusted=[root]) != client or time.monotonic_ns() >= value['deadline_ns']:
            raise ProbeWorkerError()
        # Probe-local identities are not host-run bindings or learning receipts.
        def digest(label):
            return hashlib.sha256(label.encode()+value['request_key'].encode()).hexdigest()
        key = OwnedExecutionKey(value['project_id'], digest('probe-handle-v1'),
            'run:'+digest('probe-run-v1'), 'brg_'+digest('probe-bridge-v1')[:32])
        prepared = PreparedBillingProbe(workspace, arguments,
            tuple(sorted(client_environment(environment, workspace/'tmp').items())), client,
            canonical_json(identity), key, 'hrn_'+digest('probe-native-v1')[:48], value['deadline_ns'],
            hashlib.sha256(payload).hexdigest())
        return prepared, workspace, False
    except BaseException:
        if workspace is not None:
            clean = _remove(workspace)
            return None, None if clean else workspace, not clean
        return None, None, started


def _normalized(infos, failure, identity, observed):
    heads, bills, invalid = [], [], 0
    for info in infos:
        try:
            head, bill = claude_rate_limit_records(info,
                account_fingerprint=identity['account_fingerprint'], observed_at_ms=observed)
            heads.append(head)
            bills.append(bill)
        except (ValueError, TypeError):
            invalid += 1
    records = []
    if heads:
        head = merged_restriction(heads, observed_at_ms=observed)
        bill = max(reversed(bills), key=lambda r: BILLING_RANK[r['paid_usage_usable']])
        records = [head, bill]
        if failure is not None or invalid:
            records = [*([head] if head['status'] != 'allowed'
                or _headroom_state('claude', head, observed) not in {'ample', None} else []),
                *([bill] if bill['paid_usage_usable'] is not False else [])]
    reason = 'probe_incomplete' if failure is not None else ('probe_invalid_events' if invalid
        else 'probe_no_events' if not heads else None)
    body = canonical_json({'records': records, 'observed_at_ms': observed, 'reason_code': reason})
    if len(body) > MAX_PROBE_PACKET_BYTES:
        raise ProbeWorkerError()
    return body, reason


def _execute(job):
    original = job.snapshot
    prepared = PreparedBillingProbe(*original[:5], OwnedExecutionKey(*original[5]), *original[6:])
    body, reason = b'', 'probe_incomplete'
    try:
        from .claude_host import check_backend
        value = parse_bounded_json_value(job.payload, max_bytes=MAX_PROBE_INPUT_BYTES)
        if bridge.api_keys_present(os.environ) or bridge.managed_configuration_present():
            raise ProbeWorkerError()
        check_backend(os.environ, Path(value['root']))
        if resolve_client('claude', untrusted=[Path(value['root'])]) != prepared.client_identity:
            raise ProbeWorkerError()
        infos, failure = bridge._run_claude_billing_probe(list(prepared.arguments),
            environment=dict(prepared.environment), cwd=prepared.workspace/'export',
            deadline_seconds=bridge.PROBE_DEADLINE_SECONDS, execution=job.ticket)
        observed = time.time_ns()//1_000_000
        if observed < value['created_at_ms']:
            failure = 'probe_clock_changed'
            observed = value['created_at_ms']  # Incomplete; retain restrictions without shortening their horizon.
        body, reason = _normalized(infos, failure,
            parse_bounded_json_value(prepared.identity, max_bytes=MAX_PROBE_INPUT_BYTES), observed)
    except BaseException:
        body = canonical_json({'records': [], 'observed_at_ms': None, 'reason_code': 'probe_incomplete'})
    removed = _remove(prepared.workspace)
    return BillingProbePacket(body, prepared.inputs_digest, removed, reason)


def _worker(slot, registry):
    if slot.thread is not threading.current_thread():
        return
    slot.ready.set()
    job = None
    try:
        while True:
            item = slot.incoming.get()
            if item is None:
                return
            operation, handle, job = item
            if operation == 'prepare':
                with registry._lock:
                    if job.cancelled:
                        job.state = 'cleaned'
                        if slot.stop.is_set():
                            return
                        job = handle = item = None
                        continue
                prepared, workspace, unknown = _prepare(job.payload)
                with registry._lock:
                    job.prepared, job.workspace = prepared, workspace
                    discard = job.cancelled or slot.stop.is_set()
                    job.state = 'discarding' if discard else 'prepared' if prepared is not None else 'unknown' if unknown else 'failed'
                    if prepared is not None:
                        job.snapshot = _prepared_snapshot(prepared)
                if not discard:
                    job = handle = item = prepared = None
                    continue
                operation = 'discard'
            if operation == 'discard':
                removed = _remove(job.workspace)
                with registry._lock:
                    job.workspace = None if removed else job.workspace
                    job.prepared = None
                    job.state = 'cleaned' if removed and job.ticket is None else 'unknown'
                if slot.stop.is_set():
                    return
            elif operation == 'execute':
                with registry._lock:
                    job.state = 'active'
                packet = _execute(job)
                with registry._lock:
                    job.packet, job.state = packet, 'finishing'
                    job.packet_snapshot = (packet.body, packet.inputs_digest, packet.workspace_removed, packet.reason_code)
                    entry = registry._entry(job.ticket)
                    registry._complete_probe_worker(entry.worker_pool, handle)
                    job.state = 'returned'
                if slot.stop.is_set():
                    return
            job = handle = item = None
    except BaseException:
        with registry._lock:
            slot.fault = True
            if job is not None:
                job.state = 'unknown'


class BillingProbeWorkers:
    def __init__(self, registry, limits):
        if type(registry) is not OwnedExecutionRegistry or type(limits) is not ProbeWorkerLimits:
            raise ProbeWorkerError('probe_worker_invalid')
        limits.__post_init__()
        self._registry, self._limits = registry, ProbeWorkerLimits(limits.workers, limits.retained_bytes)
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._jobs, self._slots, self._closed, self._charged = {}, [_Slot() for _ in range(limits.workers)], False, 0
        registry._register_probe_worker_pool(self)
        try:
            for slot in self._slots:
                slot.thread = threading.Thread(target=_worker, args=(slot, registry), daemon=True)
                slot.thread.start()
                if not slot.ready.wait(5):
                    raise ProbeWorkerError()
        except BaseException:
            self.close()
            raise

    def _owner(self):
        if (self._pid != os.getpid() or self._thread is not threading.current_thread()
                or self._registry._pid != self._pid):
            raise ProbeWorkerError()

    def _job(self, handle):
        self._owner()
        if type(handle) is not ProbeWorkerHandle or handle not in self._jobs:
            raise ProbeWorkerError('probe_worker_invalid')
        return self._jobs[handle]

    def _reserved(self, handle):
        job = self._job(handle)
        return job.state == 'reserved' and job.slot.current is handle and job.slot.thread.is_alive()

    def prepare(self, payload):
        self._owner()
        if self._closed or type(payload) is not bytes or not 0 < len(payload) <= MAX_PROBE_INPUT_BYTES:
            raise ProbeWorkerError('probe_worker_invalid')
        with self._registry._lock:
            slot = next((s for s in self._slots if s.current is None and not s.fault and s.thread.is_alive()), None)
            if slot is None or self._charged+PROBE_SLOT_BYTES > self._limits.retained_bytes:
                raise ProbeWorkerError('probe_worker_full')
            handle = ProbeWorkerHandle()
            job = _Job(payload, slot)
            self._jobs[handle], slot.current = job, handle
            self._charged += PROBE_SLOT_BYTES
            slot.incoming.put_nowait(('prepare', handle, job))
            return handle

    def prepared(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.state != 'prepared' or job.prepared is None:
                raise ProbeWorkerError('probe_worker_not_ready')
            if _prepared_snapshot(job.prepared) != job.snapshot:
                raise ProbeWorkerError()
            return job.prepared

    def bind(self, handle, ticket):
        with self._registry._lock:
            job, prepared = self._job(handle), self.prepared(handle)
            if (self._closed or type(ticket) is not ExecutionTicket or ticket._registry is not self._registry):
                raise ProbeWorkerError()
            entry = self._registry._entry(ticket)
            key = (entry.key.project_id, entry.key.handle_digest, entry.key.run_id, entry.key.bridge_run_id)
            if key != job.snapshot[5] or entry.run_handle != prepared.run_handle or entry.deadline_ns > prepared.deadline_ns:
                raise ProbeWorkerError()
            job.ticket, job.state = ticket, 'reserved'
            try:
                self._registry._handoff_worker(ticket, self, handle)
            except BaseException:
                job.state = 'unknown'
                raise

    def submit(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.state in {'queued', 'active', 'returned'}:
                return
            if self._closed or job.state != 'reserved' or job.supervisor is None:
                raise ProbeWorkerError()
            job.state = 'queued'
            job.slot.incoming.put_nowait(('execute', handle, job))

    def snapshot(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            return {'state': job.state, 'returned': job.state == 'returned',
                'reason_code': None if job.packet is None else job.packet.reason_code}

    def supervisor(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.supervisor is None:
                raise ProbeWorkerError('probe_worker_not_ready')
            return job.supervisor

    def packet(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.state != 'returned' or type(job.packet) is not BillingProbePacket:
                raise ProbeWorkerError('probe_worker_not_ready')
            if (job.packet.body, job.packet.inputs_digest, job.packet.workspace_removed,
                    job.packet.reason_code) != job.packet_snapshot:
                raise ProbeWorkerError()
            return job.packet

    def discard(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.ticket is not None:
                raise ProbeWorkerError('probe_worker_busy')
            job.cancelled = True
            if job.state in {'preparing', 'discarding', 'cleaned', 'failed', 'unknown'}:
                return
            job.state = 'discarding'
            job.slot.incoming.put_nowait(('discard', handle, job))

    def retire(self, handle):
        with self._registry._lock:
            job = self._job(handle)
            if job.ticket is None:
                eligible = job.state in {'failed', 'cleaned'} and job.workspace is None
            else:
                entry = self._registry._issued.get(job.ticket)
                eligible = (job.state == 'returned' and entry is not None and entry.state == 'released'
                            and self.packet(handle).workspace_removed)
            if not eligible:
                raise ProbeWorkerError('probe_worker_busy')
            self._jobs.pop(handle)
            job.slot.current = None
            self._charged -= PROBE_SLOT_BYTES

    def close(self):
        self._owner()
        with self._registry._lock:
            if self._closed:
                return
            self._closed = True
            for slot in self._slots:
                slot.stop.set()
                job = self._jobs.get(slot.current)
                if job is None:
                    slot.incoming.put_nowait(None)
                elif job.ticket is None:
                    self.discard(slot.current)
                    if job.state in {'failed', 'cleaned', 'unknown'}:
                        slot.incoming.put_nowait(None)
                else:
                    if job.supervisor is not None:
                        job.supervisor.request_stop()
                    if job.state == 'reserved':
                        job.state = 'discarding'
                        slot.incoming.put_nowait(('discard', slot.current, job))
                    elif job.state in {'returned', 'unknown'}:
                        slot.incoming.put_nowait(None)
