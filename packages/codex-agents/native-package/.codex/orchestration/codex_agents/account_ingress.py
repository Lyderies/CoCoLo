"""Bounded socket I/O separated from the original account database owner.

Trusted startup supplies exclusively transferred, connected Unix sockets and
bounded opaque contexts. This component binds no listener and proves no native
identity. The health authorizer must use only cheap, verified in-memory state.
Normal-client activation and fully bounded admission work remain separate gates.
"""
from __future__ import annotations

import os
import selectors
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import lane_health
from .sql_read_budget import SQLReadError
from .account_api import MAX_ACCOUNT_FRAME_BYTES, AccountError, decode_account_request
from .account_service import AccountService


IDLE_WAIT_SECONDS = 0.05  # how long the frontend thread sleeps when no connection is attached


@dataclass(frozen=True)
class AccountIngressLimits:
    ordinary_connections: int
    health_connections: int
    steps_per_cycle: int
    owner_steps_per_cycle: int
    connection_timeout_ns: int
    poll_interval_ns: int

    def __post_init__(self):
        counts = (self.ordinary_connections, self.health_connections,
                  self.steps_per_cycle, self.owner_steps_per_cycle)
        if (any(type(value) is not int or value < 1 for value in counts)
                or self.ordinary_connections + self.health_connections > 256
                or max(self.steps_per_cycle, self.owner_steps_per_cycle) > sum(counts[:2])
                or type(self.connection_timeout_ns) is not int or not 1 <= self.connection_timeout_ns <= 60_000_000_000
                or type(self.poll_interval_ns) is not int or not 1 <= self.poll_interval_ns <= 100_000_000):
            raise AccountError("account_invalid_request")


@dataclass(repr=False)
class _Peer:
    connection: socket.socket
    context: object
    health: bool
    deadline: int
    # Only the front thread touches these I/O fields.
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytes | None = None
    sent: int = 0
    registered: bool = False
    io_closed: bool = False
    # Shared metadata/body transitions use the short ingress lock.
    handed: bool = False
    processing: bool = False
    disconnected: bool = False
    request: bytes | None = None
    reply: bytes | None = None


class AccountIngress:
    """One prestarted I/O thread and one original database-owner pump.

    Construction binds exclusive delivery and shutdown custody of the configured
    claim coordinator, which must be bootstrapped and empty. Closing this ingress
    never closes its service or shared database. Replacement on the same bound
    coordinator is deliberately refused, including after drain.
    """

    def __init__(self, service: AccountService, limits: AccountIngressLimits):
        if type(service) is not AccountService or type(limits) is not AccountIngressLimits:
            raise AccountError("account_invalid_request")
        limits.__post_init__()
        service._views._guard()
        jobs = service._claim_jobs
        if (service._pid != os.getpid() or service._closed or jobs is None
                or not jobs._started or jobs._closing or jobs._faulted
                or jobs._ingress_owner is not None or jobs._jobs):
            raise AccountError("account_unavailable")
        self._service, self._jobs = service, jobs
        self._billing = service._billing_jobs
        if self._billing is not None and (self._billing._ingress_owner is not None or self._billing._jobs
                or self._billing._claims is not jobs or self._billing._closing):
            raise AccountError('account_unavailable')
        self._observations = service._observation_jobs
        if self._observations is not None:
            observation = self._observations
            if (not observation._started or observation._closing or observation._faulted
                    or observation._ingress_owner is not None or observation._jobs):
                raise AccountError('account_unavailable')
            observation._owner._guard()
        self._limits = AccountIngressLimits(**vars(limits))
        self._pid, self._owner_thread = os.getpid(), threading.current_thread()
        self._lock = threading.Lock()
        self._wake = threading.Event()  # set when a peer is attached or a close is requested (see IDLE_WAIT_SECONDS)
        self._peers: dict[object, _Peer] = {}
        # Owner-only ticket/subscriber state. Detached tickets retain custody
        # until the actual coordinator returns a ready result or fully drains.
        self._groups: dict[object, set[object]] = {}
        self._subscriptions: dict[object, object] = {}
        self._rotation: deque[object] = deque()
        self._started = self._closing = self._closed = self._faulted = False
        # S1-H1a: published notes (reporting only): the front thread's and the owner's.
        self.health = lane_health.Lane('ingress')
        self._front_note, self._note = self.health.slot('front'), self.health.slot('owner')
        self._kept_descriptors = set()  # the front's own record, written only by the front
        self._front_exiting = self._front_stopped = self._pumping = False
        self._thread = threading.Thread(target=self._front, name="account-ingress", daemon=True)
        jobs._ingress_owner = self
        if self._billing is not None:
            self._billing._ingress_owner = self
        if self._observations is not None:
            self._observations._ingress_owner = self

    def _owner(self):
        if self._pid != os.getpid() or self._owner_thread is not threading.current_thread():
            raise AccountError("account_wrong_owner")
        self._service._views._guard()
        if self._pumping:
            raise AccountError("account_unavailable")

    def bootstrap(self):
        self._owner()
        if self._started or self._closing or self._faulted:
            raise AccountError("account_unavailable")
        try:
            self._started = True
            self._thread.start()
        except BaseException as error:
            with self._lock:
                self._faulted = self._front_stopped = True
            lane_health.safely(lambda: self._note.publish(faulted=True))  # S1-H1a
            lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'ingress_start_failed', error))
            raise

    def attach(self, connection: socket.socket, context: object, *, health=False):
        """Trusted same-process frontend transfer; rejection retains caller custody.

        The reserved health lane is startup classification, never a JSON field.
        This operation does no SQL, authentication, transfer I/O or mode change.
        The frontend must transfer the descriptor exclusively after success.
        """
        if self._pid != os.getpid():
            raise AccountError("account_wrong_owner")
        if (type(connection) is not socket.socket or connection.family != socket.AF_UNIX
                or connection.type != socket.SOCK_STREAM or connection.fileno() < 0 or type(health) is not bool):
            raise AccountError("account_invalid_request")
        # Bounded local metadata queries before taking custody; no wait.
        try:
            connection.getpeername()
        except OSError:
            raise AccountError("account_invalid_request") from None
        peer = _Peer(connection, context, health, time.monotonic_ns() + self._limits.connection_timeout_ns)
        with self._lock:
            if (not self._started or self._closing or self._front_exiting or self._front_stopped
                    or (self._faulted and not health)):
                raise AccountError("account_unavailable")
            if any(p.connection.fileno() == connection.fileno() for p in self._peers.values()):
                raise AccountError("account_invalid_request")
            count = sum(p.health == health for p in self._peers.values())
            if count >= (self._limits.health_connections if health else self._limits.ordinary_connections):
                raise AccountError("account_busy")  # R7b: told to the client as service_refused_busy
            self._peers[object()] = peer
        self._wake.set()

    @staticmethod
    def _wire(raw):
        if type(raw) is not bytes or not raw or b"\n" in raw or len(raw) + 1 > MAX_ACCOUNT_FRAME_BYTES:
            raise AccountError("account_incompatible")
        return raw

    def _drop(self, selector, token, peer):
        # Front thread only. Close outside the shared lock, then publish the
        # disconnect. Handed requests retain their quota until owner ack.
        if peer.io_closed:
            return
        if peer.registered:
            try:
                selector.unregister(peer.connection)
            except (OSError, ValueError, KeyError):
                pass
            peer.registered = False
        peer.connection.close()
        peer.io_closed = True
        peer.incoming.clear()
        peer.outgoing = None
        with self._lock:
            peer.disconnected = True
            if not peer.handed:
                self._peers.pop(token, None)

    def _front_step(self, selector, token, peer, mask):
        if time.monotonic_ns() >= peer.deadline:
            self._drop(selector, token, peer)
            return
        try:
            if peer.outgoing is not None:
                if mask & selectors.EVENT_WRITE:
                    count = peer.connection.send(peer.outgoing[peer.sent:])
                    peer.sent += count
                    if not count or peer.sent == len(peer.outgoing):
                        self._drop(selector, token, peer)
                return
            if not mask & selectors.EVENT_READ:
                return
            # After handoff only disconnect/extra-frame detection is allowed.
            part = peer.connection.recv(1 if peer.handed else MAX_ACCOUNT_FRAME_BYTES - len(peer.incoming))
            if not part or peer.handed:
                self._drop(selector, token, peer)
                return
            peer.incoming.extend(part)
            if b"\n" not in peer.incoming:
                if len(peer.incoming) == MAX_ACCOUNT_FRAME_BYTES:
                    self._drop(selector, token, peer)
                return
            raw, trailing = bytes(peer.incoming).split(b"\n", 1)
            if trailing:
                self._drop(selector, token, peer)
                return
            upgrade = self._service.runtime_upgrade_reply(raw)
            if upgrade is not None:
                peer.outgoing = self._wire(upgrade) + b"\n"
                peer.incoming.clear()
                selector.modify(peer.connection, selectors.EVENT_WRITE, token)
                return
            try:
                request = decode_account_request(raw)
            except AccountError:
                self._drop(selector, token, peer)
                return
            if request.operation == "account.health":
                # No owner/SQL path. The adapter must be bounded in-memory.
                peer.outgoing = self._wire(self._service.handle(raw, peer.context)) + b"\n"
                peer.incoming.clear()
                selector.modify(peer.connection, selectors.EVENT_WRITE, token)
            elif peer.health:
                self._drop(selector, token, peer)
            else:
                with self._lock:
                    if not self._faulted and not self._closing:
                        peer.request, peer.handed = raw, True
                peer.incoming.clear()
                if not peer.handed:
                    self._drop(selector, token, peer)
        except BlockingIOError:
            pass
        except OSError:
            self._drop(selector, token, peer)

    def _front(self):
        selector = None
        rotation = deque()
        try:
            selector = selectors.DefaultSelector()
            while True:
                with self._lock:
                    closing, faulted = self._closing, self._faulted
                    peers = tuple(self._peers.items())
                if closing:
                    break
                if not peers:
                    # S13d-1: with nothing attached there is nothing to poll. Wait for an attach or a close
                    # (both set the event); the bound only covers a state change nobody signals.
                    self._wake.wait(IDLE_WAIT_SECONDS)
                    self._wake.clear()
                    continue
                for token, peer in peers:
                    if peer.io_closed:
                        continue
                    if time.monotonic_ns() >= peer.deadline or (faulted and not peer.health):
                        self._drop(selector, token, peer)
                        continue
                    if not peer.registered:
                        peer.connection.setblocking(False)
                        selector.register(peer.connection, selectors.EVENT_READ, token)
                        peer.registered = True
                        rotation.append(token)
                    with self._lock:
                        reply, peer.reply = peer.reply, None
                    if reply is not None:
                        peer.outgoing = self._wire(reply) + b"\n"
                        selector.modify(peer.connection, selectors.EVENT_WRITE, token)
                # Rebuild from bounded live slots; no stale rotation accumulates.
                active = {token: peer for token, peer in peers if not peer.io_closed}
                rotation = deque(token for token in rotation if token in active)
                events = {key.data: mask for key, mask in selector.select(self._limits.poll_interval_ns / 1e9)}
                for _ in range(min(self._limits.steps_per_cycle, len(rotation))):
                    token = rotation.popleft()
                    rotation.append(token)
                    if token in events:
                        self._front_step(selector, token, active[token], events[token])
        except BaseException as error:
            lane_health.safely(lambda: self._front_note.publish(faulted=True))  # S1-H1a, by the front, before its lock
            lane_health.safely(lambda error=error: self.health.note_failure(self._front_note, 'ingress_front_failed', error))
            with self._lock:
                self._faulted = True
        finally:
            with self._lock:
                # Fence every lane before the final snapshot. Owner faults may
                # preserve front health; an exiting front can own no new peer.
                self._front_exiting = True
                peers = tuple(self._peers.items())
            for token, peer in peers:
                try:
                    self._drop(selector, token, peer)
                except Exception as error:
                    # Unknown descriptor state fails closed, never a cleanup
                    # acknowledgement. The slot remains for explicit shutdown.
                    lane_health.safely(lambda: self._kept_descriptors.add(id(peer)))  # S1-H1a, by the front
                    lane_health.safely(lambda: self._front_note.publish(faulted=True, kept=frozenset(self._kept_descriptors)))
                    lane_health.safely(lambda error=error: self.health.note_failure(self._front_note, 'ingress_cleanup_failed', error))
                    with self._lock:
                        self._faulted = True
            if selector is not None:
                selector.close()
            with self._lock:
                self._front_stopped = True

    def _deliver(self, token, peer, reply):
        self._wire(reply)
        with self._lock:
            if peer.disconnected:
                self._peers.pop(token, None)
            else:
                peer.reply = reply

    def _claim_has_subscribers(self, ticket):
        """Owner-only decision immediately before a returned job may commit.

        A disconnect published after this decision can lose to completion; an
        earlier published disconnect/expired deadline cannot. No metadata lock
        covers the subsequent SQL commit, and no socket operation runs here.
        """
        self._service._views._guard()
        group = self._groups[ticket]
        now = time.monotonic_ns()
        with self._lock:
            return any(not self._peers[token].disconnected and now < self._peers[token].deadline for token in group)

    def _publish_health(self):
        """S1-H1a C5: this owner's note: requests being answered (busy) and waiting (queued)."""
        try:
            with self._lock:
                queued = sum(p.request is not None and not p.processing for p in self._peers.values())
            lane_health.safely(lambda: self._note.publish(busy=len(self._groups), queued=queued))
        except Exception:
            pass

    def pump_owner(self):
        """Bounded owner batches, with no socket wait, worker start or join.

        Existing claim capture/final legacy JSON/filesystem work is still owner
        work; this method does not claim a complete latency bound for that work.
        """
        self._owner()
        with self._lock:
            if not self._started or self._closing or self._faulted or self._front_stopped:
                raise AccountError("account_unavailable")
        self._pumping = True
        try:
            steps = self._limits.owner_steps_per_cycle
            with self._lock:
                # Control notices precede normal batches. This is bounded by
                # the fixed connection ceiling, including unacknowledged slots.
                gone = [(t, p) for t, p in self._peers.items() if p.disconnected]
            for token, peer in gone:
                ticket = self._subscriptions.pop(token, None)
                if ticket is not None:
                    group = self._groups[ticket]
                    group.remove(token)
                    if not group:
                        if self._billing is not None and ticket in self._billing._jobs:
                            self._billing.cancel(ticket, _ingress=self)
                        elif self._observations is not None and ticket in self._observations._jobs:
                            self._observations.cancel(ticket, _ingress=self)
                        else:
                            self._jobs.cancel(ticket, _ingress=self)
                with self._lock:
                    self._peers.pop(token, None)
            with self._lock:
                batch = [(t, p, p.request) for t, p in self._peers.items()
                         if p.request is not None and not p.processing and not p.disconnected][:steps]
                for _, peer, _ in batch:
                    peer.processing, peer.request = True, None
                waiting = sum(p.request is not None and not p.processing for p in self._peers.values())
            # S1-H1a C5: busy and queued published before the owner hands the batch on (where it may block).
            lane_health.safely(lambda: self._note.publish(busy=len(self._groups) + len(batch), queued=waiting))
            for token, peer, raw in batch:
                reply = self._service.begin(raw, peer.context, _ingress=self)
                if type(reply) is bytes:
                    self._deliver(token, peer, reply)
                elif type(reply) is object:
                    if reply not in self._groups:
                        limits = self._jobs._intake._limits
                        maximum = limits.max_projects * limits.max_jobs_per_project
                        if self._billing is not None:
                            maximum += self._billing._limits.max_projects*self._billing._limits.jobs_per_project
                        if self._observations is not None:
                            probe_limits = self._observations._limits
                            maximum += probe_limits.max_projects * probe_limits.max_jobs_per_project
                        if len(self._groups) >= maximum:
                            raise AccountError("account_unavailable")
                        self._groups[reply] = set()
                        self._rotation.append(reply)
                    self._groups[reply].add(token)
                    self._subscriptions[token] = reply
                else:
                    raise AccountError("account_incompatible")
            try:
                self._service.drive_claims()
            except SQLReadError as error:
                if error.reason_code != "sql_read_busy":
                    raise
                return  # Existing tickets retain custody until a later owner turn.
            for _ in range(min(steps, len(self._rotation))):
                ticket = self._rotation.popleft()
                self._rotation.append(ticket)
                try:
                    reply = self._service.take_claim_reply(ticket, _ingress=self)
                except SQLReadError as error:
                    if error.reason_code != "sql_read_busy":
                        raise
                    continue  # Queue admission failed before consuming the completion.
                if reply is None:
                    continue
                group = self._groups[ticket]
                self._wire(reply)
                for token in group:
                    with self._lock:
                        peer = self._peers[token]
                    self._deliver(token, peer, reply)
                    del self._subscriptions[token]
                del self._groups[ticket]
                self._rotation.remove(ticket)
        except BaseException as error:
            lane_health.safely(lambda: self._note.publish(faulted=True))  # S1-H1a
            lane_health.safely(lambda error=error: self.health.note_failure(self._note, 'ingress_owner_failed', error))
            with self._lock:
                self._faulted = True
            raise
        finally:
            self._pumping = False
            self._publish_health()  # S1-H1a C5

    def engaged(self):
        """S13d-1: whether any connection or in-flight request is held; read-only, any thread."""
        with self._lock:
            return bool(self._peers or self._groups)

    def request_close(self):
        self._owner()
        self._pumping = True
        try:
            with self._lock:
                self._closing = True
                if not self._started:
                    self._front_stopped = True
            self._wake.set()
            self._jobs.request_close()
            if self._observations is not None:
                self._observations.request_close()
        finally:
            self._pumping = False

    def drain(self):
        """No join or deadline inference: true requires actual front/pool stop."""
        self._owner()
        self._pumping = True
        try:
            return self._drain()
        finally:
            self._pumping = False

    def _drain(self):
        with self._lock:
            if not self._closing:
                raise AccountError("account_unavailable")
            stopped = self._front_stopped and all(p.io_closed for p in self._peers.values())
        claims_drained = self._jobs.drain()
        observations_drained = self._observations is None or self._observations.drain()
        if not claims_drained or not observations_drained or not stopped or self._thread.is_alive():
            return False
        self._groups.clear()
        self._subscriptions.clear()
        self._rotation.clear()
        with self._lock:
            retained, self._peers = self._peers, {}
            self._closed = True
        del retained  # Release trusted contexts outside the metadata lock.
        return True

    def snapshot(self):
        self._owner()
        with self._lock:
            return {"ordinary": sum(not p.health for p in self._peers.values()),
                    "health": sum(p.health for p in self._peers.values()),
                    "disconnected": sum(p.disconnected for p in self._peers.values()),
                    "queued": sum(p.request is not None and not p.processing for p in self._peers.values()),
                    "groups": len(self._groups), "subscribers": len(self._subscriptions),
                    "faulted": self._faulted, "closing": self._closing,
                    "front_stopped": self._front_stopped, "closed": self._closed}
