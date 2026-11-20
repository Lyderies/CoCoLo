"""S1-H1a: health notes the account lanes publish for learning-status (DESIGN-0.4.0-S1-H1a.md).

Each lane has slots, one per writing thread (its owner, each worker, the ingress front). A slot
is written only by its own thread, by replacing one immutable value; a reader takes no lock and
calls no lane method. Nothing reads these notes to decide anything: they are reporting only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import itertools
import re
import threading
import time

_TOKENS = itertools.count(1)
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
CAUSE = re.compile(r"[a-z][a-z_]{2,63}")


def safely(record) -> None:
    """Run one piece of health instrumentation, its arguments included; never raise (C1)."""
    try:
        record()
    except BaseException:  # noqa: BLE001 - instrumentation must never change a lane's outcome
        return


def new_token() -> int:
    """A job's identity for counting it once, whichever slots publish it (atomic under the GIL)."""
    return next(_TOKENS)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class _Failure:
    cause: str
    type: str
    at_ms: int
    sequence: int


@dataclass(frozen=True)
class _Note:
    faulted: bool = False
    dead: bool = False
    kept: frozenset = frozenset()
    busy: int = 0
    busy_tokens: frozenset = frozenset()  # busy by token: never counted while kept anywhere
    queued: int = 0
    failure: _Failure | None = None


@dataclass(frozen=True)
class _Episode:
    count: int = 0
    onset_ms: int | None = None


class Slot:
    """One writer's note; only its own thread publishes it."""

    def __init__(self) -> None:
        self.note = _Note()

    def publish(self, **changes) -> None:
        """Replace this slot's note; fields not given keep their values (the failure always does).
        Never raises: a note that cannot be built is dropped and the lane goes on."""
        try:
            changes.pop('failure', None)
            self.note = replace(self.note, **changes)
        except BaseException:  # noqa: BLE001 - instrumentation must never change a lane's outcome
            return


class Lane:
    def __init__(self, name: str) -> None:
        self.name = name
        self._slots: dict[str, Slot] = {}
        self._episode = _Episode()
        # Held only for an episode or slot-table update, never across work, never by a reader.
        self._health_lock = threading.Lock()

    def slot(self, name: str) -> Slot:
        with self._health_lock:
            return self._slots.setdefault(name, Slot())

    def note_failure(self, slot: Slot, cause: str, error: BaseException | None) -> None:
        """Record this slot's latest failure: a fixed cause, the exception's type name only (or
        `invariant` without one, `other` for an odd name), its time and its own sequence. Never
        raises: anything that fails here drops this one note."""
        try:
            if type(cause) is not str or not CAUSE.fullmatch(cause):
                return
            if error is None:
                kind = 'invariant'
            else:
                name = type(error).__name__
                kind = name if _TYPE_NAME.fullmatch(name) else 'other'
            failure = _Failure(cause, kind, _wall_ms(), new_token())
            slot.note = replace(slot.note, failure=failure)
        except BaseException:  # noqa: BLE001 - instrumentation must never change a lane's outcome
            return

    def enter_held(self) -> None:
        try:
            with self._health_lock:
                episode = self._episode
                onset = episode.onset_ms if episode.count else _wall_ms()
                self._episode = _Episode(episode.count + 1, onset)
        except BaseException:  # noqa: BLE001
            return

    def leave_held(self) -> None:
        try:
            with self._health_lock:
                count = max(0, self._episode.count - 1)
                self._episode = _Episode(count, self._episode.onset_ms if count else None)
        except BaseException:  # noqa: BLE001
            return

    def view(self) -> dict:
        """The lane as a content-free dict: counts, fixed codes and times only. Takes no lock."""
        notes = [slot.note for slot in list(self._slots.values())]
        failures = [note.failure for note in notes if note.failure is not None]
        newest = max(failures, key=lambda failure: failure.sequence) if failures else None
        kept = frozenset().union(*(note.kept for note in notes)) if notes else frozenset()
        busy_tokens = frozenset().union(*(note.busy_tokens for note in notes)) if notes else frozenset()
        return {
            'faulted': any(note.faulted or note.dead for note in notes),
            'kept': len(kept),
            'busy': sum(note.busy for note in notes) + len(busy_tokens - kept),
            'queued': sum(note.queued for note in notes),
            'last_failure': None if newest is None else {
                'cause': newest.cause, 'type': newest.type, 'at_ms': newest.at_ms},
            'held_since_ms': self._episode.onset_ms,
        }
