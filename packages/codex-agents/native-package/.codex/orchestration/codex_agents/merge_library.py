"""Unimplemented ML-1..ML-30 interface (final merge plan §§10, 11.1–11.2).

History is private SQLite, including event contents; staging is
project/.cocolo/staging/<child_id>/. The caller supplies a trusted seal verifier
(the GA-19 adapter), project checks and, optionally, a fake/future builder.
No caller may derive authority from the editable project .cocolo directory.

Hook contract: EVERY filesystem and SQLite call, including reads, has before
and after IOCall notifications. `phase` is intake/history/staging/forward/
compensation/reverse/validation/cleanup/retirement. Semantic checkpoints use
operation='checkpoint', detail['name']. Normalized mutation operation names
are exchange, rename_noreplace, mkdir, rmdir, unlink; SQLite uses sqlite_write,
sqlite_commit (detail['commit'] = A/B/undo/retire). detail['scope'] identifies
tree/history/staging; commits identify full_flush and parent flush obligations.
The after notification carries the real result. Raising injects an error;
returning IOOverride at before replaces that call's result. A short-write
injector must itself write the prefix before overriding the returned count.
Hooks are diagnostics, never an authorization source. This is a test seam,
not an implementation of any filesystem or recovery behavior.

Named checkpoints: after_intake, staging_complete, final_validation,
cleanup_inspect. sqlite_read verification uses detail['part'] = events,
old_contents, new_contents, batch_digest. Write calls expose fd/data for real
short-write injection. Project checks have project_checks before/after calls.
History rows expose recorded directory identities and staging names to the
independent oracle. Empty-directory cleanup is an explicit post-terminal job.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LANDING_BYTES = 16 * 1024 * 1024
MAX_PATHS = 2000


class MergeError(Exception):
    """ML-14: interface/configuration errors; operational refusals use Report."""


@dataclass(frozen=True)
class Content:
    """ML-3, ML-5: actual bytes and permission bits, plus the sealed git blob."""
    data: bytes
    mode_bits: int = 0o644
    git_blob: str = ""


@dataclass(frozen=True)
class Event:
    """ML-5, ML-13: canonical version-1 file/folder event, kind kept on reverse."""
    path: str
    base: Content | None
    new: Content | None
    kind: str = "file"
    v: int = 1


@dataclass(frozen=True)
class SealedBatch:
    """ML-1: immutable input; manifest/event order and GA-19 receipt are bound."""
    child_id: str
    base_commit: str
    events: tuple[Event, ...]
    manifest: bytes
    candidate_digest: str
    git_link: str
    batch_digest: str
    approval: str
    candidate_root: Path | None = None


@dataclass(frozen=True)
class Policy:
    """ML-1, ML-27..ML-30: trusted caller adapters, never loaded from project."""
    verify_seal: Callable[[SealedBatch], bool]
    checks: Callable[[Path], bool]
    builder: Callable[[SealedBatch, str], SealedBatch | None] | None = None
    write_children: bool = False


@dataclass(frozen=True)
class IOCall:
    """ML-5..ML-22: syscall boundary exposed to the test kill/fault oracle."""
    operation: str
    when: str
    phase: str
    path: str | None = None
    other: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    result: object = None


@dataclass(frozen=True)
class IOOverride:
    """ML-4, ML-12, ML-15: test substitution for one underlying call result."""
    value: object


Hook = Callable[[IOCall], IOOverride | None]


@dataclass(frozen=True)
class PathReport:
    """ML-14: observed base/new/owner/absent/unknown/moved, including mode."""
    path: str
    classification: str
    retained: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    """ML-10, ML-14, ML-17: truthful durable state and every retained version."""
    state: str
    reason: str = ""
    batch_no: int | None = None
    paths: tuple[PathReport, ...] = ()
    merged: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    leftovers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    recorded: bool = False


@dataclass(frozen=True)
class HistoryRow:
    """ML-5, ML-11, ML-18: private durable row, with contents until retired."""
    batch_no: int
    batch: SealedBatch
    state: str
    kind: str = "forward"
    undoes: int | None = None
    contents_kept: bool = True
    cleanup_complete: bool = False
    created_folders: Mapping[str, tuple[int, int] | None] = field(default_factory=dict)
    emptied_folders: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    staged_paths: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueItem:
    """ML-17, ML-28: durable project FIFO, with owner work taking priority."""
    ticket: int
    child_id: str
    state: str
    base_commit: str


def land(project: Path, private_root: Path, batch: SealedBatch, *, policy: Policy,
         hook: Hook | None = None) -> Report:
    """ML-1..ML-30: land one whole sealed change, using the project's FIFO."""
    raise NotImplementedError


def rollback(project: Path, private_root: Path, batch_no: int, *,
             hook: Hook | None = None) -> Report:
    """ML-8..ML-15, ML-20..ML-22: reverse using positive history evidence."""
    raise NotImplementedError


def undo(project: Path, private_root: Path, batch_no: int, *,
         hook: Hook | None = None) -> Report:
    """ML-13, ML-19, ML-20: ordered durable undo and combined terminal commit."""
    raise NotImplementedError


def recover(project: Path, private_root: Path, *, hook: Hook | None = None) -> tuple[Report, ...]:
    """ML-7, ML-11..ML-16, ML-21, ML-24: history-only direction, idempotent."""
    raise NotImplementedError


def history(project: Path, private_root: Path) -> tuple[HistoryRow, ...]:
    """ML-5, ML-11, ML-18, ML-30: read this project's private change history."""
    raise NotImplementedError


def stage(project: Path, private_root: Path, batch: SealedBatch, *,
          policy: Policy, hook: Hook | None = None) -> Path:
    """ML-4, ML-7, ML-15, ML-24: complete bytes in ignored child staging."""
    raise NotImplementedError


def enqueue(project: Path, private_root: Path, batch: SealedBatch, *,
            policy: Policy, hook: Hook | None = None) -> QueueItem:
    """ML-17, ML-28..ML-30: seal admission into the durable per-project FIFO."""
    raise NotImplementedError


def queue(project: Path, private_root: Path) -> tuple[QueueItem, ...]:
    """ML-17, ML-28: FIFO status, including waiting/cancelled/unresolved work."""
    raise NotImplementedError


def status(project: Path, private_root: Path) -> tuple[Report, ...]:
    """ML-14, ML-24: read-only reports, including one leftover child staging folder."""
    raise NotImplementedError


def cancel(project: Path, private_root: Path, ticket: int, *, hook: Hook | None = None) -> Report:
    """ML-17: stop at any phase, keep a durable queue barrier until resolved."""
    raise NotImplementedError


def retry(project: Path, private_root: Path, batch_no: int, *,
          policy: Policy, hook: Hook | None = None) -> Report:
    """ML-12, ML-17: retry an unresolved operation before releasing the queue."""
    raise NotImplementedError


def accept(project: Path, private_root: Path, batch_no: int) -> Report:
    """ML-17, ML-18: owner handoff permits retirement and queue continuation."""
    raise NotImplementedError


def cleanup(project: Path, private_root: Path, *, hook: Hook | None = None) -> tuple[Report, ...]:
    """ML-7, ML-9, ML-18, ML-23, ML-24: terminal cleanup and safe retirement."""
    raise NotImplementedError


def three_way_merge(base: bytes, owner: bytes, child: bytes, *,
                    hook: Hook | None = None) -> Content | None:
    """ML-25, ML-28: plain-file merge-file, None for overlapping edits."""
    raise NotImplementedError


def _seed_history(project: Path, private_root: Path, rows: tuple[HistoryRow, ...], *,
                  created_folders: Mapping[str, tuple[int, int] | None] | None = None,
                  emptied_folders: Mapping[str, tuple[int, int]] | None = None,
                  staged_paths: Mapping[str, str] | None = None) -> None:
    """ML-13, ML-20, ML-21, ML-23: INTERNAL fixture for otherwise-unadmittable modes/states."""
    raise NotImplementedError
