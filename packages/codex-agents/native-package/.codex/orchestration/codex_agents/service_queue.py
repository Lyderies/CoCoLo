"""Bounded in-memory metadata scheduling, without I/O or caller authority.

Use separate instances for reserved control/terminal/restriction lanes and routine
work. The trusted service chooses the lane after validation and authorization;
neither a project label nor acceptance by this queue is an identity or receipt.
This queue does not execute callbacks, wait for workers, retain durable work, or
make a stalled persistence owner safe for local timer/control callbacks.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass

MAX_PROJECTS = 256
MAX_PENDING_ITEMS = 4096
MAX_ITEM_BYTES = 65_536
MAX_PAYLOAD_BYTES = 64 * 1_048_576
REASON_CODES = frozenset({
    "invalid_queue_limits", "invalid_queue_item", "queue_item_too_large",
    "project_queue_full", "queue_project_limit",
})
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")


class ServiceQueueError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in REASON_CODES:
            reason_code = "invalid_queue_item"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class QueueLimits:
    max_projects: int
    max_per_project: int
    max_item_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.max_projects) is not int or not 1 <= self.max_projects <= MAX_PROJECTS
            or type(self.max_per_project) is not int or not 1 <= self.max_per_project <= MAX_PENDING_ITEMS
            or type(self.max_item_bytes) is not int or not 1 <= self.max_item_bytes <= MAX_ITEM_BYTES
            or self.max_projects * self.max_per_project > MAX_PENDING_ITEMS
            or self.max_projects * self.max_per_project * self.max_item_bytes > MAX_PAYLOAD_BYTES
        ):
            raise ServiceQueueError("invalid_queue_limits")


@dataclass(frozen=True)
class PendingMetadata:
    project_id: str
    payload: bytes


@dataclass(frozen=True)
class QueueSnapshot:
    projects: int
    pending: int
    payload_bytes: int


class ProjectFairQueue:
    """FIFO per project and one item per active project per round.

    A project has its own fixed quota; another admitted project's backlog cannot
    consume it. The configured active-project ceiling and the product of quotas
    bound every retained item, project entry and payload byte. Limits are explicit
    service configuration, not measured installed defaults or a latency guarantee.
    """

    def __init__(self, limits: QueueLimits) -> None:
        if type(limits) is not QueueLimits:
            raise ServiceQueueError("invalid_queue_limits")
        limits.__post_init__()
        self._limits = limits
        self._projects: dict[str, deque[bytes]] = {}
        self._rotation: deque[str] = deque()
        self._pending = 0
        self._payload_bytes = 0
        self._lock = threading.Lock()

    def offer(self, project_id: str, payload: bytes) -> None:
        if type(project_id) is not str or _PROJECT.fullmatch(project_id) is None:
            raise ServiceQueueError("invalid_queue_item")
        if type(payload) is not bytes or not payload:
            raise ServiceQueueError("invalid_queue_item")
        if len(payload) > self._limits.max_item_bytes:
            raise ServiceQueueError("queue_item_too_large")
        with self._lock:
            items = self._projects.get(project_id)
            if items is None:
                if len(self._projects) >= self._limits.max_projects:
                    raise ServiceQueueError("queue_project_limit")
                items = deque()
                self._projects[project_id] = items
                self._rotation.append(project_id)
            if len(items) >= self._limits.max_per_project:
                raise ServiceQueueError("project_queue_full")
            items.append(payload)
            self._pending += 1
            self._payload_bytes += len(payload)

    def take_nowait(self) -> PendingMetadata | None:
        with self._lock:
            if not self._rotation:
                return None
            project_id = self._rotation.popleft()
            items = self._projects[project_id]
            payload = items.popleft()
            if items:
                self._rotation.append(project_id)
            else:
                del self._projects[project_id]
            self._pending -= 1
            self._payload_bytes -= len(payload)
            return PendingMetadata(project_id, payload)

    def snapshot(self) -> QueueSnapshot:
        """Return bounded counters only, without queued bodies or project labels."""

        with self._lock:
            return QueueSnapshot(len(self._projects), self._pending, self._payload_bytes)
