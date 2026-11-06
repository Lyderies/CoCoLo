"""Bounded control I/O over connections supplied by a trusted native frontend.

No listener, enrollment, persistence, cleanup executor or caller authority lives
here. Only already verified, bounded in-memory authorization belongs on this
owner thread. The loop polls local timers around small nonblocking I/O batches.
"""

from __future__ import annotations

import os
import selectors
import socket
import threading
from collections import deque
from dataclasses import dataclass, field

from .control_api import MAX_CONTROL_FRAME_BYTES, ControlError
from .control_service import ControlService
from .liveness_clock import MAX_CLOCK_NS, LivenessClockError
from .project_monitor import ProjectMonitor, ProjectMonitorError


@dataclass(frozen=True)
class ControlLoopLimits:
    max_connections: int
    steps_per_cycle: int
    connection_timeout_ns: int
    max_wait_ns: int

    def __post_init__(self) -> None:
        if (type(self.max_connections) is not int or not 1 <= self.max_connections <= 256
                or type(self.steps_per_cycle) is not int or not 1 <= self.steps_per_cycle <= self.max_connections
                or type(self.connection_timeout_ns) is not int or not 1 <= self.connection_timeout_ns <= 60_000_000_000
                or type(self.max_wait_ns) is not int or not 1 <= self.max_wait_ns <= 1_000_000_000):
            raise ControlError("control_invalid_input")


@dataclass
class _Peer:
    connection: socket.socket
    context: object
    deadline_ns: int
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytes | None = None
    sent: int = 0


class ControlLoop:
    """One monitor owner; one request per exclusively transferred connection.

    attach is an internal trusted-owner operation, not a native authorization
    check. The frontend must not pass slow checks or unbounded contexts here.
    At most max_connections * 32 KiB of request/reply bodies is retained.
    Separate reserved native-event/terminal intake and cleanup remain required.
    """

    def __init__(self, monitor: ProjectMonitor, service: ControlService, limits: ControlLoopLimits) -> None:
        if (type(monitor) is not ProjectMonitor or type(service) is not ControlService
                or type(limits) is not ControlLoopLimits or service._monitor is not monitor):
            raise ControlError("control_invalid_input")
        limits.__post_init__()
        try:
            monitor._owner()
        except ProjectMonitorError:
            raise ControlError("control_wrong_owner") from None
        if service._closed or service._pid != os.getpid() or service._thread is not threading.current_thread():
            raise ControlError("control_wrong_owner")
        self._monitor, self._service = monitor, service
        self._limits = ControlLoopLimits(limits.max_connections, limits.steps_per_cycle,
                                         limits.connection_timeout_ns, limits.max_wait_ns)
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self._selector = selectors.DefaultSelector()
        self._peers: dict[socket.socket, _Peer] = {}
        self._rotation: deque[socket.socket] = deque()
        self._closed = False
        self._running = False

    def _owner(self, *, closing=False) -> None:
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise ControlError("control_wrong_owner")
        if self._closed and not closing:
            raise ControlError("control_unavailable")
        if self._running:
            raise ControlError("control_unavailable")

    def _drop(self, connection: socket.socket) -> None:
        self._peers.pop(connection)
        self._rotation.remove(connection)
        try:
            self._selector.unregister(connection)
        except (OSError, ValueError, KeyError):
            pass
        connection.close()

    def _now(self) -> int:
        try:
            return self._monitor._sample_clock()
        except LivenessClockError:
            # The shared failure latch marks undecided attempts unavailable.
            self._monitor.tick()
            self._shutdown()
            raise ControlError("control_unavailable") from None
        except BaseException:
            self._monitor.tick()
            self._shutdown()
            raise

    def attach(self, connection: socket.socket, context: object) -> None:
        self._owner()
        self._running = True
        try:
            self._attach(connection, context)
        finally:
            self._running = False

    def _attach(self, connection: socket.socket, context: object) -> None:
        if (type(connection) is not socket.socket or connection.family != socket.AF_UNIX
                or connection.type != socket.SOCK_STREAM or connection in self._peers
                or connection.fileno() < 0 or connection.fileno() in self._selector.get_map()):
            raise ControlError("control_invalid_input")
        if len(self._peers) >= self._limits.max_connections:
            raise ControlError("control_unavailable")
        now = self._now()
        if now > MAX_CLOCK_NS - self._limits.connection_timeout_ns:
            raise ControlError("control_unavailable")
        peer = _Peer(connection, context, now + self._limits.connection_timeout_ns)
        previous_timeout = connection.gettimeout()
        try:
            connection.getpeername()
            connection.setblocking(False)
            self._selector.register(connection, selectors.EVENT_READ)
            self._peers[connection] = peer
            self._rotation.append(connection)
        except BaseException as error:
            # Failed transfer leaves the socket with its caller, including its
            # exact original timeout. Undo partial registration before returning.
            self._peers.pop(connection, None)
            try:
                self._rotation.remove(connection)
            except ValueError:
                pass
            try:
                if self._selector.get_key(connection).fileobj is connection:
                    self._selector.unregister(connection)
            except (OSError, ValueError, KeyError):
                pass
            try:
                connection.settimeout(previous_timeout)
            except (OSError, ValueError):
                pass  # External descriptor closure violates exclusive custody.
            if isinstance(error, (OSError, ValueError, KeyError)):
                raise ControlError("control_invalid_input") from None
            raise

    def _expire(self, now: int) -> None:
        for connection, peer in tuple(self._peers.items()):
            if now >= peer.deadline_ns:
                self._drop(connection)

    def _step(self, connection: socket.socket, events: int) -> None:
        peer = self._peers[connection]
        if self._now() >= peer.deadline_ns:
            self._drop(connection)
            return
        try:
            if peer.outgoing is not None:
                if events & selectors.EVENT_WRITE:
                    sent = connection.send(peer.outgoing[peer.sent:])
                    peer.sent += sent
                    if not sent or peer.sent == len(peer.outgoing):
                        self._drop(connection)
                return
            if not events & selectors.EVENT_READ:
                return
            part = connection.recv(MAX_CONTROL_FRAME_BYTES - len(peer.incoming))
            if not part:
                self._drop(connection)
                return
            peer.incoming.extend(part)
            if b"\n" not in peer.incoming:
                if len(peer.incoming) == MAX_CONTROL_FRAME_BYTES:
                    self._drop(connection)
                return
            raw, trailing = bytes(peer.incoming).split(b"\n", 1)
            if trailing:
                self._drop(connection)
                return
            reply = self._service.handle(raw, peer.context)
            if self._now() >= peer.deadline_ns:
                # A cancel may already be latched: an absent reply never proves
                # that the operation had no effect, and never triggers a resend.
                self._drop(connection)
                return
            if type(reply) is not bytes or len(reply) + 1 > MAX_CONTROL_FRAME_BYTES:
                self._drop(connection)
                raise ControlError("control_invalid_response")
            peer.incoming.clear()
            peer.outgoing = reply + b"\n"
            self._selector.modify(connection, selectors.EVENT_WRITE)
        except BlockingIOError:
            pass
        except OSError:
            self._drop(connection)

    def run_once(self) -> int:
        """Run a bounded fair batch, returning only current connection count.

        The trusted outer owner must continue calling this method and operate
        its independent cleanup executor/watchdog. No caller callback runs here
        except ControlService's already-verified in-memory authorizer.
        """
        self._owner()
        self._running = True
        try:
            try:
                self._monitor.tick()
                now = self._now()
                self._expire(now)
                remaining = min((peer.deadline_ns - now for peer in self._peers.values()),
                                default=self._limits.max_wait_ns)
                events = {key.fileobj: mask for key, mask in
                          self._selector.select(min(self._limits.max_wait_ns, remaining) / 1_000_000_000)}
                self._monitor.tick()
                self._expire(self._now())
                for _ in range(min(self._limits.steps_per_cycle, len(self._rotation))):
                    connection = self._rotation.popleft()
                    self._rotation.append(connection)
                    if connection in events:
                        self._step(connection, events[connection])
            finally:
                self._monitor.tick()
        except BaseException:
            self._shutdown()
            raise
        finally:
            self._running = False
        return len(self._peers)

    def _shutdown(self) -> None:
        if not self._closed:
            for connection in tuple(self._peers):
                self._drop(connection)
            self._selector.close()
            self._closed = True

    def close(self) -> None:
        self._owner(closing=True)
        self._running = True
        try:
            self._shutdown()
        finally:
            self._running = False
