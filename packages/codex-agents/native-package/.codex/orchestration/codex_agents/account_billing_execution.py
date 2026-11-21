"""Owner-local original probe execution and durable completion.

Only the fixed registered worker and its actual retained cleanup can settle an
admitted row. This component authenticates no native caller or containment: the
production adapter must prove those gates before calling launch. No public API
is enabled by constructing it, and no callback/SQL owner crosses to a worker.
"""
from dataclasses import dataclass
import hashlib
import os
import threading

from .account_billing_admission import AccountBillingAdmission, ProbeAdmissionError, keyed
from .billing_probe_worker import (
    BillingProbeWorkers, MAX_PROBE_INPUT_BYTES, MAX_PROBE_PACKET_BYTES, probe_payload,
)
from .billing_probe_contracts import PROBE_COMPLETION_REASONS, valid_client_version
from .learning_contracts import canonical_json, content_digest, parse_bounded_json_value, validate_learning_record
from .owned_execution import OwnedExecutionKey, OwnedExecutionRegistry


@dataclass(repr=False)
class _Execution:
    reservation: object
    identity: bytes
    deadline_ns: int
    payload: bytes
    worker: object
    permit: object = None
    inputs: object = None
    ticket: object = None
    state: str = 'preparing'
    cleanup: object = None


class AccountBillingExecutions:
    def __init__(self, owner, workers, registry):
        if (type(owner) is not AccountBillingAdmission or type(workers) is not BillingProbeWorkers
                or type(registry) is not OwnedExecutionRegistry or workers._registry is not registry):
            raise ProbeAdmissionError('probe_invalid_request')
        owner._guard()
        workers._owner()
        if getattr(owner, '_execution_owner', None) is not None:
            raise ProbeAdmissionError()
        self._owner, self._workers, self._registry = owner, workers, registry
        self._jobs, self._reservations = {}, {}
        owner._execution_owner = self

    def _guard(self):
        self._owner._guard()
        self._workers._owner()
        if self._owner._execution_owner is not self or self._workers._registry is not self._registry:
            raise ProbeAdmissionError('probe_not_authorized')

    def _job(self, handle):
        self._guard()
        if type(handle) is not object or handle not in self._jobs:
            raise ProbeAdmissionError('probe_not_authorized')
        return self._jobs[handle]

    def prepare(self, reservation, identity):
        """Finish private preparation before consuming model admission/capacity."""
        self._guard()
        row = self._owner._original(reservation)
        identity = validate_learning_record(identity)
        raw_identity = canonical_json(identity)
        if (row['state'] != 'reserved' or identity['record_type'] != 'provider_identity'
                or identity['provider'] != 'claude'
                or not reservation.created <= identity['observed_at_ms'] <= self._owner._now()):
            raise ProbeAdmissionError('probe_conflict')
        actual = self._owner._one('SELECT content,content_digest,source FROM records WHERE project_id=? AND record_id=?',
                                 (reservation.project, identity['record_id']))
        if (actual is None or bytes(actual['content']) != raw_identity or actual['source'] != 'provider'
                or actual['content_digest'] != content_digest(identity)):
            raise ProbeAdmissionError('probe_conflict')
        if reservation in self._reservations:
            handle = self._reservations[reservation]
            if self._jobs[handle].identity != raw_identity:
                raise ProbeAdmissionError('probe_conflict')
            return handle
        payload = probe_payload(reservation.project, reservation.key, reservation.snapshot[1], identity,
            deadline_ns=reservation.deadline_ns, created_at_ms=reservation.created,
            language=os.environ.get('LANG') or 'C.UTF-8')
        worker = self._workers.prepare(payload)
        handle = object()
        self._jobs[handle] = _Execution(reservation, raw_identity, reservation.deadline_ns, payload, worker)
        self._reservations[reservation] = handle
        return handle

    def _prepared_identity(self, reservation):
        """Read the associated owner's actual unoffered preparation, never a caller claim."""
        self._guard()
        job = self._job(self._reservations.get(reservation))
        prepared = self._workers.prepared(job.worker)
        if (job.reservation is not reservation or job.state != 'preparing' or job.ticket is not None
                or prepared.inputs_digest != hashlib.sha256(job.payload).hexdigest()
                or prepared.identity != job.identity
                or prepared.deadline_ns != job.deadline_ns or job.deadline_ns != reservation.deadline_ns):
            raise ProbeAdmissionError('probe_conflict')
        return job.identity

    def worker_of(self, handle):
        """The worker a preparation still owns, or None once it was retired."""
        job = self._jobs.get(handle) if type(handle) is object else None
        return None if job is None else job.worker

    def ticketed(self, reservation):
        """Whether this reservation's preparation was ever offered an execution ticket."""
        self._guard()
        handle = self._reservations.get(reservation)
        return handle is None or self._job(handle).ticket is not None

    def child_ended(self, reservation):
        """O0c M10: whether no model this reservation's preparation launched can still be running: its
        worker is not executing and its ticket was confirmed cleaned or never started a child. With no
        preparation left (never prepared, or retired after confirmed cleanup), nothing runs."""
        self._guard()
        handle = self._reservations.get(reservation)
        if handle is None:
            return True
        job = self._jobs[handle]
        if not self.ticketed(reservation):
            return self._workers.children_ended(job.worker)  # never offered: nothing can have started
        # Ticketed (the same answer settle_unlaunched relies on, read inside its transaction): its child
        # is proven only through the ticket this owner holds.
        return job.ticket is not None and self._workers.children_ended(job.worker, job.ticket)

    def returned(self, handle):
        """O0c review M5: whether this preparation's worker returned a packet (its probe ran)."""
        job = self._jobs.get(handle) if type(handle) is object else None
        return job is not None and job.state == 'offered' and self._workers.snapshot(job.worker)['returned']

    def snapshot(self, handle):
        job = self._job(handle)
        actual = self._workers.snapshot(job.worker)
        return {'state': job.state, 'worker_state': actual['state'], 'reason_code': actual['reason_code']}

    def discard(self, handle):
        """Cancel only actual unoffered preparation; grant no capacity release."""
        job = self._job(handle)
        if job.ticket is not None:
            raise ProbeAdmissionError('probe_busy')
        self._workers.discard(job.worker)
        job.state = 'discarding'

    def retire_discarded(self, handle):
        job = self._job(handle)
        if job.ticket is not None or job.state != 'discarding':
            raise ProbeAdmissionError('probe_busy')
        self._workers.retire(job.worker)
        self._jobs.pop(handle)
        self._reservations.pop(job.reservation)

    def launch(self, handle, permit):
        job = self._job(handle)
        if job.state != 'preparing' or job.ticket is not None:
            raise ProbeAdmissionError('probe_conflict')  # Never offer a second execution.
        prepared = self._workers.prepared(job.worker)
        inputs, _ = self._owner._execution(permit, launching=True)
        if (inputs.reservation is not job.reservation or prepared.inputs_digest != hashlib.sha256(job.payload).hexdigest()
                or prepared.identity != inputs.identity or prepared.identity != job.identity
                or prepared.deadline_ns != inputs.deadline_ns or prepared.deadline_ns != job.deadline_ns):
            raise ProbeAdmissionError('probe_conflict')
        job.permit, job.inputs = permit, inputs
        # Copy metadata; the public preparation object must not own registry keys.
        key = OwnedExecutionKey(prepared.key.project_id, prepared.key.handle_digest,
                               prepared.key.run_id, prepared.key.bridge_run_id)
        job.ticket = self._registry.issue(key, deadline_ns=inputs.deadline_ns, run_handle=prepared.run_handle)
        job.state = 'unknown'  # Any ambiguous handoff/offer stays charged and unreplayable.
        self._workers.bind(job.worker, job.ticket)
        self._workers.submit(job.worker)
        job.state = 'offered'
        return self._workers.supervisor(job.worker)

    def _completion(self, job, kept_workspace=False):
        inputs, row = self._owner._execution(job.permit)
        if inputs is not job.inputs or job.state != 'offered' or job.ticket is None:
            raise ProbeAdmissionError('probe_conflict')
        packet = self._workers.packet(job.worker)
        if ((not packet.workspace_removed and not kept_workspace)
                or packet.inputs_digest != hashlib.sha256(job.payload).hexdigest()):
            raise ProbeAdmissionError()
        if job.cleanup is None:
            job.cleanup = self._registry.retain_cleanup(job.ticket, self._workers, job.worker,
                                                       consumer=threading.current_thread())
        proof = self._registry.cleanup_snapshot(job.cleanup)
        prepared_key = self._workers._jobs[job.worker].snapshot[5]
        if (tuple(getattr(proof['key'], n) for n in ('project_id', 'handle_digest', 'run_id', 'bridge_run_id'))
                != prepared_key or proof['deadline_ns'] != inputs.deadline_ns
                or prepared_key[0] != inputs.reservation.project):
            raise ProbeAdmissionError('probe_conflict')
        value = parse_bounded_json_value(packet.body, max_bytes=MAX_PROBE_PACKET_BYTES)
        if (type(value) is not dict or value.keys() != {'records', 'observed_at_ms', 'reason_code', 'client_version'}
                or not valid_client_version(value['client_version'])
                or type(value['records']) is not list or len(value['records']) > 2
                or value['reason_code'] not in {None, *PROBE_COMPLETION_REASONS}
                or value['reason_code'] != packet.reason_code):
            raise ProbeAdmissionError()
        observed, now = value['observed_at_ms'], self._owner._now()
        if observed is None:
            if value['records'] or value['reason_code'] != 'probe_incomplete':
                raise ProbeAdmissionError()
        elif type(observed) is not int or not inputs.reservation.created <= observed <= now:
            raise ProbeAdmissionError()
        records = [validate_learning_record(r) for r in value['records']]
        kinds = [r['record_type'] for r in records]
        if (len(kinds) != len(set(kinds)) or any(k not in {'provider_headroom', 'provider_billing'} for k in kinds)
                or any(r['provider'] != 'claude' or r['account_fingerprint'] != job.permit.account_fingerprint
                    or r['observed_at_ms'] != observed for r in records)
                or (value['reason_code'] is None and set(kinds) != {'provider_headroom', 'provider_billing'})):
            raise ProbeAdmissionError()
        identity = validate_learning_record(parse_bounded_json_value(inputs.identity, max_bytes=MAX_PROBE_INPUT_BYTES))
        raw = self._owner._one('SELECT content,content_digest,source FROM records WHERE project_id=? AND record_id=?',
                               (inputs.reservation.project, identity['record_id']))
        if (raw is None or bytes(raw['content']) != inputs.identity or raw['source'] != 'provider'
                or raw['content_digest'] != content_digest(identity)):
            raise ProbeAdmissionError('probe_conflict')
        digest = keyed(self._owner._key, b'billing-probe-completion-v1\0', {
            'project_id': row['project_id'], 'request_key': row['request_key'],
            'admission_digest': job.permit.admission_digest, 'inputs_digest': packet.inputs_digest,
            'packet_digest': hashlib.sha256(packet.body).hexdigest(), 'execution_key': list(prepared_key),
            'deadline_ns': inputs.deadline_ns})
        return row, packet, identity, records, digest

    def complete(self, handle):
        """Commit original records and capacity settlement atomically, then retire."""
        return self._commit(handle, kept_workspace=False)

    def complete_ended(self, handle):
        """O0c review M5: commit a lost probe's records once its child is proven gone.

        The probe ran and its worker returned a valid packet, but its workspace could not be removed, so
        the normal completion refused it. The workspace is a folder, not a child: the records and the
        row's settlement are committed exactly as `complete` commits them (the same validation and
        cleanup proof), the ticket is released, and the worker stays held with its workspace until the
        account restarts. Its caller already had its answer, so only the project context is checked, not
        the caller's publication callback. A packet that does not validate raises ProbeAdmissionError."""
        return self._commit(handle, kept_workspace=True)

    def _commit(self, handle, *, kept_workspace):
        job = self._job(handle)
        owner, ledger = self._owner, self._owner._ledger
        with ledger._account_transaction():
            row, packet, identity, records, digest = self._completion(job, kept_workspace)
            references = [{'project_id': row['project_id'], 'record_id': identity['record_id'],
                           'content_digest': content_digest(identity)}]
            for record in records:
                ledger._append_checked_locked(row['project_id'], record, producer='provider',
                                               timestamp=record['observed_at_ms'])
                owner._execution(job.permit)
                references.append({'project_id': row['project_id'], 'record_id': record['record_id'],
                                   'content_digest': content_digest(record)})
            if self._completion(job, kept_workspace) != (row, packet, identity, records, digest):
                raise ProbeAdmissionError('probe_conflict')
            packet_version = parse_bounded_json_value(packet.body, max_bytes=MAX_PROBE_PACKET_BYTES)['client_version']
            value = canonical_json({'probe': 'completed', 'reason_code': packet.reason_code,
                                    'records': references, 'completion_digest': digest,
                                    'client_version': packet_version})
            if len(value) > 4096:
                raise ProbeAdmissionError()
            final = owner._update(row, state='committed', references_json=value.decode())
            reservation = job.inputs.reservation
            # Review M5: a lost probe's records are the account's own readings and reach no caller, so
            # its caller's publication callback is not asked; its project context still is.
            authorize = (lambda: True) if kept_workspace else reservation.authorize
            owner._authorize(reservation.context, reservation.snapshot, authorize)
            # Revalidate the actual cleanup after all persistence/callback work.
            self._registry.cleanup_snapshot(job.cleanup)
            if self._workers.packet(job.worker) is not packet or owner._row(row['project_id'], row['request_key']) != final:
                raise ProbeAdmissionError('probe_conflict')
        self._registry.acknowledge_release(job.ticket)
        if kept_workspace:
            job.state = 'committed'  # its worker stays held, with its workspace, until the restart
        else:
            self._workers.retire(job.worker)
            self._jobs.pop(handle)
            self._reservations.pop(reservation)
        owner._execution_inputs.pop(job.permit)
        owner._permits.pop(reservation)
        owner._pending.pop((reservation.project, reservation.key))
        owner._reserved_inputs.pop(reservation)
        reply = owner._reply(final, known=False)
        owner._authorize(reservation.context, reservation.snapshot, authorize)
        if owner._row(row['project_id'], row['request_key']) != final:
            raise ProbeAdmissionError('probe_conflict')
        return reply
