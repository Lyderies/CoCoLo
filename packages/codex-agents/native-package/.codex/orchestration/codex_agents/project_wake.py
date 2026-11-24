"""RS-E E2a-7/8: a project service's wake publications to the collector (`account.publish_wake`).

The collector must start a stopped project whose holds come due, without opening its store. So the service tells it:

- at open, `pending` (which also allocates this instance's epoch), then `exact` with the store's next due;
- before a hold write whose deadline is earlier than the last acknowledged publication's (or before any hold write
  while this instance has no acknowledged publication), `pending`, waiting for the acknowledgement (at most
  WAKE_TIMEOUT_SECONDS; with the collector unreachable the write proceeds and `unpublished` stays set). Review round
  1: while this instance has no acknowledged publication the collector may still hold an earlier `clean_stop`, so
  an unacknowledged first write first leaves the WAKE_MARKER file the collector's reconcile reads (and is refused,
  `WakeRefused`, when even that cannot be written); the first acknowledged publication removes it;
- after the store transaction ends, `exact` with the next due, from a background sender that never sends an `exact`
  computed before a later write (every hold write invalidates a queued one; its own transaction queues the next);
- at a normal stop, only after verified final settlement and a successful final read of the store's next due,
  `clean_stop` with it, best effort within 2 s. A writer-fault stop sends nothing, so the collector keeps its state
  unknown.

The next due is the earliest `due_at_ms` of a held or releasing hold, or the withdrawal retry (`next_check_ms`) of a
releasing open binding (`project_holds_schema.next_hold_due`).

A process killed between a `pending` acknowledgement and the commit, or between the commit and `exact`, leaves
`pending` in the ledger, which the collector's reconcile treats as unknown (due now).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
import socket
import threading
import time
from pathlib import Path

from .account_api import AccountError, AccountRequest, decode_account_response, encode_account_request
from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .hosts import CODEX_HOST_CLIENT
from .project_holds_schema import next_hold_due
from .wake_contracts import WAKE_INSTANCE_FIELD, WAKE_MARKER
from .service_enrollment import (
    EnrollmentError, account_endpoint_path, enrollment_preamble, proof_directory, read_wake_capability, withdraw_proof,
)

WAKE_TIMEOUT_SECONDS = 2.0
CLEAN_STOP_SECONDS = 2.0
RETRY_SECONDS = 1.0
MAX_REPLY_BYTES = 16_384


class WakeUnavailable(Exception):
    """The collector did not acknowledge (no detail: the caller only keeps its unpublished flag)."""


class WakeRefused(Exception):
    """A first write with no acknowledged publication and no marker the collector can see: the write must not go."""


def send_wake(state, project_root, project_id, payload, timeout):
    """One `account.publish_wake` exchange under the project's wake key; the checked reply or an exception."""
    deadline = time.monotonic() + timeout
    request = AccountRequest(1, 'account.publish_wake', 'wake-' + secrets.token_hex(12), project_id,
                             COLLECTOR_RUNTIME_DIGEST, payload)
    frame = encode_account_request(request)
    endpoint = account_endpoint_path(state)
    proofs = proof_directory(endpoint)
    line = enrollment_preamble(read_wake_capability(state, Path(project_root)), 'project', CODEX_HOST_CLIENT, 1,
                               frame, proofs, subject=str(project_root))
    received, delivered = bytearray(), False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(0.01, deadline - time.monotonic()))
            client.connect(str(endpoint))
            client.sendall(line + b'\n' + frame + b'\n')
            delivered = True
            while b'\n' not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WakeUnavailable()
                client.settimeout(remaining)
                chunk = client.recv(MAX_REPLY_BYTES + 1 - len(received))
                if not chunk or len(received) + len(chunk) > MAX_REPLY_BYTES:
                    raise WakeUnavailable()
                received.extend(chunk)
    except OSError:
        raise WakeUnavailable() from None
    finally:
        if not delivered:
            withdraw_proof(proofs, line)
    return decode_account_response(bytes(received).split(b'\n', 1)[0], request)


class WakePublisher:
    """Thread-safe: hold writes call it on the store's writer thread; its sender thread sends `exact`."""

    def __init__(self, state, project_root, project_id, *, send=None, timeout=WAKE_TIMEOUT_SECONDS,
                 start_sender=True):
        self._state, self._root, self._project = state, project_root, project_id
        self._send = send or (lambda payload, seconds: send_wake(state, project_root, project_id, payload, seconds))
        self._timeout = timeout
        self._lock = threading.Lock()  # one publication at a time: seq order is send order
        self._wakeup = threading.Condition(threading.Lock())
        self._epoch, self._seq = None, 0
        self.acknowledged = (None, None)  # (state, next due) of the last acknowledged publication
        self._version = 0  # counts pendings; an exact computed under an older version is never sent
        self._queued = None  # (next due, version) for the sender
        self._next_due = None
        self.unpublished = False
        self.dirty = False
        self._instance = secrets.token_hex(16)  # this service instance: a lost epoch reply is retried idempotently
        self.marker = None  # decision 179: this instance's own marker, a file named by it in the WAKE_MARKER folder
        if state is not None and type(project_id) is str and project_id.startswith('prj_'):
            self.marker = Path(state.root) / 'projects' / project_id / WAKE_MARKER / self._instance
        self._marker_lock = None  # held (flock) while this instance lives: no other instance removes its marker
        self._marked = True  # an earlier instance may have left one: the first acknowledgement removes stale ones
        self._opened = self._closed = False
        self._thread = None
        if start_sender:
            self._thread = threading.Thread(target=self._run, name='project-wake', daemon=True)
            self._thread.start()

    def _publish(self, state, due, seconds):
        """Under self._lock. Whether the collector acknowledged."""
        self._seq += 1
        payload = {'epoch': self._epoch, 'seq': self._seq, 'state': state, 'next_due_at_ms': due}
        if self._epoch is None:
            payload[WAKE_INSTANCE_FIELD] = self._instance
        try:
            reply = self._send(payload, seconds)
        except (AccountError, EnrollmentError, WakeUnavailable, OSError, ValueError):
            self.unpublished = True
            return False
        if self._epoch is None:
            self._epoch = reply['epoch']
        self.acknowledged = (state, due)
        self.unpublished = False
        if self._marked and self.marker is not None:
            self._clear_markers()
            self._marked = False
        return True

    def _clear_markers(self):
        """After an acknowledgement: this instance's own marker, and any whose instance is gone (its lock free);
        never a live instance's (decision 179: an overlapping publisher's acknowledgement says nothing of its
        writes)."""
        if self._marker_lock is not None:
            with contextlib.suppress(OSError):
                os.unlink(self.marker)
            os.close(self._marker_lock)
            self._marker_lock = None
        try:
            names = os.listdir(self.marker.parent)  # a dot name is one being created (locked once it counts)
        except OSError:
            return
        for name in names:
            with contextlib.suppress(OSError):
                descriptor = os.open(self.marker.parent / name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # its instance is gone
                    os.unlink(self.marker.parent / name)
                finally:
                    os.close(descriptor)

    def _leave_marker(self):
        """Under self._lock: this instance's marker the collector's reconcile reads, durably and locked for this
        instance's life (created under a dot name, locked, then renamed into place), or WakeRefused."""
        if self.marker is None:
            raise WakeRefused()
        if self._marker_lock is not None:
            self._marked = True
            return
        staging = self.marker.parent / ('.' + self.marker.name)
        descriptor = None
        try:
            for folder in (self.marker.parent.parent.parent, self.marker.parent.parent, self.marker.parent):
                folder.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(descriptor)
            os.rename(staging, self.marker)
            directory = os.open(self.marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise WakeRefused() from None
        self._marker_lock = descriptor
        self._marked = True

    def open(self, connection):
        """At the store's open, on its thread: `pending` (allocating the epoch), then `exact` queued."""
        if self._opened:
            return
        self._opened = True
        with self._lock:
            with self._wakeup:
                self._version += 1
            self._publish('pending', None, self._timeout)
        try:
            self.after_settle(next_hold_due(connection))
        except Exception:
            self.dirty = True

    def touch(self):
        """Before a hold write that can only move the next due later: an exact queued before it is dropped too
        (decision 179), as before_due does; this write's own transaction queues the next."""
        self.dirty = True
        with self._wakeup:
            self._version += 1

    def before_due(self, due_at_ms):
        """Before a hold write with this deadline: a pending, acknowledged, when the collector's view may be later.
        Raises WakeRefused only for a first write that can neither be published nor marked."""
        self.dirty = True
        with self._lock:
            with self._wakeup:
                self._version += 1  # an exact queued before this write no longer describes the store
            state, published = self.acknowledged
            needed = state is None or (state != 'pending' and due_at_ms is not None
                                       and (published is None or due_at_ms < published))
            if not needed:
                return
            if not self._publish('pending', due_at_ms, self._timeout) and state is None:
                self._leave_marker()

    def after_settle(self, next_due):
        """After the transaction ended: queue `exact` with the store's next due."""
        with self._wakeup:
            self.dirty = False
            self._next_due = next_due
            self._queued = (next_due, self._version)
            self._wakeup.notify()

    def _send_queued(self):
        """One attempt for the queued exact; whether nothing is left to send."""
        with self._wakeup:
            queued = self._queued
        if queued is None:
            return True
        with self._lock:
            if queued[1] != self._version:
                sent = True  # a later pending overtook it: its own transaction queues the next exact
            elif self.acknowledged == ('exact', queued[0]) and not self.unpublished:
                sent = True
            else:
                sent = self._publish('exact', queued[0], self._timeout)
        if sent:
            with self._wakeup:
                if self._queued == queued:
                    self._queued = None
        return sent

    def drain(self):
        """Tests and a sender-less publisher: one attempt at what is queued."""
        return self._send_queued()

    def _run(self):
        while True:
            with self._wakeup:
                while self._queued is None and not self._closed:
                    self._wakeup.wait()
                if self._closed:
                    return
            if not self._send_queued():
                with self._wakeup:
                    self._wakeup.wait(RETRY_SECONDS)

    def finish(self, *, faulted, deadline=CLEAN_STOP_SECONDS, final_due=None):
        """The service's stop: `clean_stop` within `deadline`, with the next due `final_due()` reads from the store
        once settlement is verified (the caller passes it only then). Nothing after a writer fault, without a final
        read or when that read fails: the collector then keeps the last `pending`/`exact`, which reconcile counts
        unknown."""
        self.close()
        if faulted or final_due is None:
            return False
        end = time.monotonic() + deadline
        try:
            due = final_due()
        except Exception:
            return False
        if due is not None and (type(due) is not int or due < 0):
            return False
        if not self._lock.acquire(timeout=max(0.0, end - time.monotonic())):
            return False
        try:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            return self._publish('clean_stop', due, remaining)
        finally:
            self._lock.release()

    def close(self):
        with self._wakeup:
            self._closed = True
            self._wakeup.notify_all()
        # The marker stays (the collector's reconcile reads it); only its lock goes with this instance, so a later
        # instance's acknowledgement may clear it.
        if self._lock.acquire(blocking=False):  # never waits: a publication in flight keeps it until exit
            try:
                if self._marker_lock is not None:
                    os.close(self._marker_lock)
                    self._marker_lock = None
            finally:
                self._lock.release()
