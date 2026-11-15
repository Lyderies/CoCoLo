"""Bounded connected-socket frontend for the original project writer.

Trusted native startup must transfer exclusive descriptor custody and supply a
cheap, current whole-operation authorizer. Context tokens/callbacks are not native
proof. This component binds no listener, authenticates no OS caller and grants no
launch/release authority. Real enrollment and publication containment remain gates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
import selectors
import socket
import threading
import time

from .history_contracts import HistoryError
from .result_persistence import ResultPersistenceLane
from .service_transport import MAX_FRAME_BYTES

MAX_CONNECTIONS = 64
FRONTEND_SLOT_BYTES = 2 * MAX_FRAME_BYTES + 1024


@dataclass(frozen=True)
class ProjectIngressLimits:
    connections: int
    steps_per_cycle: int
    connection_timeout_ns: int
    poll_interval_ns: int

    def __post_init__(self):
        if (type(self.connections) is not int or not 1 <= self.connections <= MAX_CONNECTIONS
                or type(self.steps_per_cycle) is not int or not 1 <= self.steps_per_cycle <= self.connections
                or type(self.connection_timeout_ns) is not int
                or not 1 <= self.connection_timeout_ns <= 60_000_000_000
                or type(self.poll_interval_ns) is not int
                or not 1 <= self.poll_interval_ns <= min(self.connection_timeout_ns, 100_000_000)):
            raise HistoryError("history_invalid_input")

    @property
    def retained_bytes(self):
        return self.connections * FRONTEND_SLOT_BYTES


@dataclass(repr=False)
class _Peer:
    connection: socket.socket
    context: object
    deadline_ns: int
    incoming: bytearray = field(default_factory=bytearray)
    raw: bytes | None = None
    request: object = None
    outgoing: bytes | None = None
    sent: int = 0
    registered: bool = False
    read_ended: bool = False
    drop_requested: bool = False


class ProjectIngress:
    """One fixed I/O thread; APIs use the existing project persistence thread.

    The startup authorizer receives immutable (request_bytes, native_context,
    phase), with phase 'request' or 'publication'. It must validate current native
    custody and the entire operation, without SQL/I/O/waits, and maintain the real
    publication guard. A True result alone is not proof that such a guard exists.
    Default denial opens no store. Control/cancellation use their separate loop.
    One ingress retains custody for the lane's lifetime, including after drain;
    replacement requires a separately drained/restarted project runtime.
    """

    def __init__(self, lane, limits: ProjectIngressLimits, *, authorizer=None):
        if (type(lane) is not ResultPersistenceLane or type(limits) is not ProjectIngressLimits
                or (authorizer is not None and not callable(authorizer))):
            raise HistoryError("history_invalid_input")
        lane._check_owner()
        if lane._closed or lane._fault or lane._requests is None or lane._api_ingress_owner is not None:
            raise HistoryError("history_service_unavailable")
        self._lane = lane
        self._limits = ProjectIngressLimits(limits.connections, limits.steps_per_cycle,
                                           limits.connection_timeout_ns, limits.poll_interval_ns)
        self._request_wait_ns = lane._requests._limits.max_wait_ns
        self._authorizer = authorizer
        self._pid, self._owner_thread = os.getpid(), threading.current_thread()
        self._lock = threading.Lock()
        self._peers: dict[object, _Peer] = {}
        self._rotation = deque()
        self._started = self._closing = self._stopped = self._faulted = False
        self._thread = threading.Thread(target=self._front, name="project-api-ingress", daemon=True)
        lane._api_ingress_owner = self

    def _owner(self):
        if self._pid != os.getpid() or self._owner_thread is not threading.current_thread():
            raise HistoryError("history_wrong_owner")
        self._lane._check_owner()

    def bootstrap(self):
        self._owner()
        with self._lock:
            if (self._started or self._closing or self._stopped or self._faulted
                    or self._lane._closed or self._lane._fault):
                raise HistoryError("history_service_unavailable")
            self._started = True
        try:
            self._thread.start()
        except BaseException:
            with self._lock:
                self._closing = self._stopped = self._faulted = True
            raise

    def attach(self, connection: socket.socket, context: object = None):
        """Transfer an already enrolled connected socket; refusal retains custody."""
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        if (type(connection) is not socket.socket or connection.family != socket.AF_UNIX
                or connection.type != socket.SOCK_STREAM
                or (context is not None and type(context) is not object)):
            raise HistoryError("history_invalid_input")
        descriptor = connection.fileno()
        if descriptor < 0:
            raise HistoryError("history_invalid_input")
        try:
            connection.getpeername()
        except OSError:
            raise HistoryError("history_invalid_input") from None
        peer = _Peer(connection, context, time.monotonic_ns() + self._limits.connection_timeout_ns)
        token = object()
        with self._lock:
            if (not self._started or self._closing or self._stopped or self._faulted
                    or self._lane._closed or self._lane._fault
                    or len(self._peers) >= self._limits.connections):
                raise HistoryError("history_service_unavailable")
            if any(value.connection.fileno() == descriptor for value in self._peers.values()):
                raise HistoryError("history_invalid_input")
            self._peers[token] = peer
        return token

    def detach(self, token):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        with self._lock:
            peer = self._peers.get(token)
            if peer is None:
                raise HistoryError("history_not_authorized")
            peer.drop_requested = True

    def _active(self, token, peer):
        if self._pid != os.getpid():
            return False
        with self._lock:
            return (not self._closing and not self._faulted and not self._stopped
                    and self._peers.get(token) is peer and not peer.drop_requested
                    and not self._lane._closed and not self._lane._fault
                    and self._lane._api_ingress_owner is self)

    def _authorized(self, token, peer, phase):
        if not self._active(token, peer) or self._authorizer is None:
            return False
        try:
            permitted = self._authorizer(peer.raw, peer.context, phase)
        except Exception:
            return False
        return permitted is True and self._active(token, peer)

    def _drop(self, selector, token, peer):
        if peer.registered:
            try:
                selector.unregister(peer.connection)
            except (OSError, ValueError, KeyError):
                pass
            peer.registered = False
        peer.connection.close()
        if peer.request is not None:
            try:
                self._lane.abandon_request(peer.request)
            except HistoryError:
                pass
        peer.request = peer.context = peer.raw = peer.outgoing = None
        peer.incoming.clear()
        with self._lock:
            self._peers.pop(token, None)
        try:
            self._rotation.remove(token)
        except ValueError:
            pass

    def _step(self, selector, token, peer, mask):
        if self._pid != os.getpid():
            return
        if not self._active(token, peer) or time.monotonic_ns() >= peer.deadline_ns:
            self._drop(selector, token, peer)
            return
        try:
            if mask & selectors.EVENT_READ:
                if peer.raw is not None:
                    # One request per connection, including while waiting/writing.
                    if peer.connection.recv(1):
                        self._drop(selector, token, peer)
                        return
                    # Send-side EOF does not prove the receiver has gone. Keep
                    # the original request/charge and allow its one reply.
                    peer.read_ended = True
                    selector.unregister(peer.connection)
                    peer.registered = False
                    if peer.outgoing is not None:
                        selector.register(peer.connection, selectors.EVENT_WRITE, token)
                        peer.registered = True
                else:
                    data = peer.connection.recv(min(8192, MAX_FRAME_BYTES - len(peer.incoming)))
                    if not data:
                        self._drop(selector, token, peer)
                        return
                    peer.incoming.extend(data)
                    newline = peer.incoming.find(b"\n")
                    if newline >= 0:
                        if newline == 0 or newline != len(peer.incoming) - 1:
                            self._drop(selector, token, peer)
                            return
                        peer.raw = bytes(peer.incoming[:-1])
                        peer.incoming.clear()
                        if not self._authorized(token, peer, "request"):
                            self._drop(selector, token, peer)
                            return
                        deadline = min(peer.deadline_ns, time.monotonic_ns() + self._request_wait_ns)
                        peer.request = self._lane.queue_request(peer.raw, peer.context, deadline_ns=deadline)
                    elif len(peer.incoming) >= MAX_FRAME_BYTES:
                        self._drop(selector, token, peer)
                        return
            if peer.request is not None:
                response = self._lane.take_request(peer.request)
                if response is not None:
                    peer.request = None
                    if not self._authorized(token, peer, "publication"):
                        self._drop(selector, token, peer)
                        return
                    peer.outgoing = response + b"\n"
                    events = selectors.EVENT_WRITE | (0 if peer.read_ended else selectors.EVENT_READ)
                    if peer.registered:
                        selector.modify(peer.connection, events, token)
                    else:
                        selector.register(peer.connection, events, token)
                        peer.registered = True
            if peer.outgoing is not None and mask & selectors.EVENT_WRITE:
                if not self._authorized(token, peer, "publication"):
                    self._drop(selector, token, peer)
                    return
                count = peer.connection.send(peer.outgoing[peer.sent:])
                if not count:
                    self._drop(selector, token, peer)
                    return
                peer.sent += count
                if peer.sent == len(peer.outgoing):
                    self._drop(selector, token, peer)
        except (BlockingIOError, InterruptedError):
            return
        except (OSError, ValueError, KeyError, HistoryError):
            self._drop(selector, token, peer)

    def _front(self):
        if self._pid != os.getpid():
            return
        selector = None
        try:
            selector = selectors.DefaultSelector()
            while True:
                with self._lock:
                    if self._closing:
                        return
                    peers = list(self._peers.items())
                for token, peer in peers:
                    if not self._active(token, peer) or time.monotonic_ns() >= peer.deadline_ns:
                        self._drop(selector, token, peer)
                    elif not peer.registered and not peer.read_ended:
                        peer.connection.setblocking(False)
                        selector.register(peer.connection, selectors.EVENT_READ, token)
                        peer.registered = True
                        self._rotation.append(token)
                events = {key.data: mask for key, mask in selector.select(self._limits.poll_interval_ns / 1e9)}
                remaining = min(self._limits.steps_per_cycle, len(self._rotation))
                while remaining:
                    remaining -= 1
                    token = self._rotation.popleft()
                    self._rotation.append(token)
                    with self._lock:
                        peer = self._peers.get(token)
                    if peer is not None:
                        self._step(selector, token, peer, events.get(token, 0))
        except BaseException:
            with self._lock:
                self._faulted = True
        finally:
            with self._lock:
                peers = list(self._peers.items())
            for token, peer in peers:
                if selector is not None:
                    self._drop(selector, token, peer)
                else:
                    peer.connection.close()
            if selector is not None:
                selector.close()
            with self._lock:
                self._peers.clear()
                self._stopped = self._closing = True

    def close(self):
        """Request frontend shutdown only; never wait or close the shared writer."""
        self._owner()
        with self._lock:
            self._closing = True
            if not self._started:
                self._stopped = True

    def snapshot(self):
        if self._pid != os.getpid():
            raise HistoryError("history_wrong_owner")
        with self._lock:
            return {"connections": len(self._peers), "maximum": self._limits.connections,
                    "retained_bytes_limit": self._limits.retained_bytes, "started": self._started,
                    "closing": self._closing, "stopped": self._stopped, "faulted": self._faulted}
