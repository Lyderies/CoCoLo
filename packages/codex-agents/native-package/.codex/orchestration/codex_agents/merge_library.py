"""ML-1..ML-30 event-staged merge library (final merge plan §§10, 11.1–11.2).

History is private SQLite, including event contents; staging is
project/.cocolo/staging/<child_id>/. The caller supplies a trusted seal verifier
(the GA-19 adapter), project checks and, optionally, a fake/future builder.
No caller may derive authority from the editable project .cocolo directory.

Hook contract: EVERY filesystem and SQLite call, including reads, has before
and after IOCall notifications. `phase` is intake/history/staging/forward/
compensation/reverse/recovery/validation/cleanup/retirement. Semantic checkpoints use
operation='checkpoint', detail['name']. Normalized mutation operation names
are exchange, rename_noreplace, mkdir, rmdir, unlink; SQLite uses sqlite_write,
sqlite_commit (detail['commit'] = A/B/undo/retire). detail['scope'] identifies
tree/history/staging. All paths are absolute, anchored paths. detail['mutating']
is required for every call and is true for any write (including write-open,
metadata changes and subprocess writes). Commit labels queue/retire describe
admission/retirement transactions, outside the exactly-two landing commits.
Flush after notifications mean successful completion (errors have no after).
On macOS fullfsync is required; elsewhere fsync completes the full flush.
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
merge_file details contain the actual subprocess argv and cwd; its three input
paths are plain files under child staging. No target-adjacent merge temporaries.
Emptied identities are in A; created identities follow mkdir, in B or a durable
non-commit record, never a third history commit. history() reads durable data.
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
    """States: refused/reverted/needs_owner/waiting_owner/landed/cancelled.

    recorded means this outcome is durable; needs_owner(io) may be unrecorded
    when the terminal write failed. Undo success is landed on an undo row.
    """
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
    kind: str = "child"  # child or owner; owner entries precede stale children


# The database contains the sealed batch as well as its effective pre/post images.
# Effective images differ only for a clean three-way merge. Never deserialize code
# from history: all records are versioned JSON, with explicit bytes encoding.
import configparser
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import replace


class _Refusal(Exception):
    pass


_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_LIBC = ctypes.CDLL(None, use_errno=True)
_NOTES = ('An old open handle can change a displaced file until the cleanup inspection.',)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _cv(c):
    return None if c is None else [c.data.hex(), c.mode_bits, c.git_blob]


def _dc(c):
    return None if c is None else Content(bytes.fromhex(c[0]), c[1], c[2])


def _ev(e):
    return [e.path, _cv(e.base), _cv(e.new), e.kind, e.v]


def _de(e):
    return Event(e[0], _dc(e[1]), _dc(e[2]), e[3], e[4])


def _pack(b):
    return [b.child_id, b.base_commit, [_ev(e) for e in b.events], b.manifest.hex(),
            b.candidate_digest, b.git_link, b.batch_digest, b.approval,
            str(b.candidate_root) if b.candidate_root else None]


def _unpack(b):
    return SealedBatch(b[0], b[1], tuple(_de(e) for e in b[2]), bytes.fromhex(b[3]),
                       *b[4:8], Path(b[8]) if b[8] else None)


def _equal(a, b):
    return a is b or (isinstance(a, Content) and isinstance(b, Content)
                     and a.data == b.data and a.mode_bits == b.mode_bits)


def _atomic(src, dst, exchange, a=-2, b=-2):
    if sys.platform == 'darwin':
        result = _LIBC.renameatx_np(a, os.fsencode(src), b, os.fsencode(dst), 2 if exchange else 4)
    elif sys.platform.startswith('linux') and hasattr(_LIBC, 'renameat2'):
        result = _LIBC.renameat2(a if a != -2 else -100, os.fsencode(src), b if b != -2 else -100, os.fsencode(dst), 2 if exchange else 1)
    else:
        raise OSError(errno.ENOTSUP, 'one-step rename unavailable')
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(src))


class _Store:
    def __init__(self, project, private, hook=None):
        self.project = Path(os.path.abspath(project))
        self.private = Path(os.path.abspath(private))
        self.hook = hook
        self.phase = 'intake'
        self.dirty = set()
        self.db = None
        self.unknown = set()
        self.moved = set()
        self.device = self.info(self.project).st_dev
        # ML-30: authority must never be taken from a child-editable location.
        if self.private == self.project or self.project in self.private.parents:
            raise _Refusal('private_location')
        self.check_dir(self.project)
        self.check_dir(self.private)
        self.path = self.private / 'history.sqlite'

    def io(self, op, fn, path=None, other=None, *, mutating=False, scope=None, **detail):
        path = str(path) if path is not None else None
        other = str(other) if other is not None else None
        detail = dict(detail, mutating=mutating, reporting=getattr(self, 'reporting', False))
        if scope:
            detail['scope'] = scope
        override = self.hook(IOCall(op, 'before', self.phase, path, other, detail)) if self.hook else None
        result = override.value if isinstance(override, IOOverride) else fn()
        if mutating:
            if op in ('write', 'chmod', 'sqlite_write', 'ftruncate'):
                self.dirty.add(path)
            if op in ('mkdir', 'rmdir', 'unlink', 'exchange', 'rename_noreplace'):
                self.dirty.add(str(Path(path).parent))
                if other:
                    self.dirty.add(str(Path(other).parent))
                if op == 'mkdir':
                    self.dirty.add(path)
            if op == 'open':
                self.dirty.update((path, str(Path(path).parent)))
        if self.hook:
            try:
                self.hook(IOCall(op, 'after', self.phase, path, other, detail, result))
            except BaseException:
                # The caller never receives a resource when its acknowledgement
                # fails. Close it here; any created pathname remains recorded.
                if op == 'open':
                    self.io('close', lambda: os.close(result), path, fd=result)
                elif op == 'sqlite_open':
                    self.io('sqlite_close', result.close, path)
                raise
        return result

    def checkpoint(self, name):
        self.io('checkpoint', lambda: None, name=name)

    def info(self, path):
        def inspect():
            try:
                return os.lstat(path)
            except FileNotFoundError:
                return None
        result = self.io('lstat', inspect, path)
        if result is None:
            raise FileNotFoundError(errno.ENOENT, 'absent', str(path))
        return result

    def exists(self, path):
        try:
            self.info(path)
            return True
        except FileNotFoundError:
            return False

    def is_dir(self, path):
        try:
            return stat.S_ISDIR(self.info(path).st_mode)
        except FileNotFoundError:
            return False

    def children(self, path):
        return tuple(Path(path) / name for name in self.io('listdir', lambda: os.listdir(path), path))

    def walk(self, root, *, control=True):
        for path in self.children(root):
            if not control and path.name in ('.git', '.cocolo'):
                continue
            info = self.info(path)
            if info.st_dev != self.device:
                raise _Refusal('cross_device')
            yield path, info
            if stat.S_ISDIR(info.st_mode):
                yield from self.walk(path)

    def check_dir(self, path):
        # Rewalk every ancestor; no-follow opens on mutations additionally anchor
        # their parents, so a late symlink is never followed by a write.
        path = Path(path)
        roots = [r for r in (getattr(self, 'project', None), getattr(self, 'private', None))
                 if r and (path == r or r in path.parents)]
        if roots:
            root = max(roots, key=lambda r: len(r.parts))
            current = root.parent
            parts = path.relative_to(current).parts
        else:
            current = Path(path.anchor)
            parts = path.parts[1:]
        for part in parts:
            current /= part
            try:
                info = self.info(current)
            except FileNotFoundError:
                raise _Refusal('moved')
            if not stat.S_ISDIR(info.st_mode):
                raise _Refusal('unsafe_path')
            if current == self.project or self.project in current.parents or current == self.private:
                if info.st_dev != self.device:
                    raise _Refusal('cross_device')
        return info

    def read(self, path):
        try:
            info = self.info(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _Refusal('unsafe_path')
        if info.st_size > MAX_FILE_BYTES:
            raise _Refusal('file exceeds 4 MiB')
        fd = self.io('open', lambda: os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), path, flags=os.O_RDONLY)
        try:
            actual = self.io('fstat', lambda: os.fstat(fd), path, fd=fd)
            if (actual.st_ino, actual.st_dev, actual.st_nlink) != (info.st_ino, info.st_dev, 1):
                raise _Refusal('unsafe_path')
            data = self.io('read', lambda: os.read(fd, MAX_FILE_BYTES + 1), path, fd=fd, size=MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise _Refusal('file exceeds 4 MiB')
            return Content(data, stat.S_IMODE(actual.st_mode))
        finally:
            self.io('close', lambda: os.close(fd), path, fd=fd)

    def flush(self, path):
        # F_FULLFSYNC is required for macOS; fsync is the full-flush primitive
        # elsewhere. SQLite also uses synchronous=FULL and fullfsync=ON.
        fd = self.io('open', lambda: os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), path, flags=os.O_RDONLY)
        try:
            self.io('fullfsync' if sys.platform == 'darwin' else 'fsync',
                    lambda: fcntl.fcntl(fd, 51) if sys.platform == 'darwin' else os.fsync(fd), path, fd=fd)
        finally:
            self.io('close', lambda: os.close(fd), path, fd=fd)
        self.dirty.discard(str(path))

    def flush_all(self):
        for path in sorted(self.dirty):
            try:
                self.flush(path)
            except FileNotFoundError:
                # Removed entries were flushed as complete files before rename;
                # it is their surviving parents which require a fresh flush.
                self.dirty.discard(path)

    def mkdir(self, path, scope='staging'):
        try:
            self.check_dir(path)
            return False
        except _Refusal as error:
            if str(error) != 'moved':
                raise
        self.check_dir(path.parent)
        self.io('mkdir', lambda: os.mkdir(path, 0o755), path, mutating=True, scope=scope)
        return True

    def write(self, path, c):
        self.check_dir(path.parent)
        # Writes prepare private complete files. Only atomic rename publishes
        # them into staging or the owner tree.
        fd = self.io('open', lambda: os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, c.mode_bits),
                     path, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=c.mode_bits, mutating=True, scope='history')
        try:
            offset = 0
            while offset < len(c.data):
                data = c.data[offset:]
                n = self.io('write', lambda: os.write(fd, data), path, mutating=True, scope='history', fd=fd, data=data)
                if n <= 0:
                    raise OSError(errno.EIO, 'no write progress')
                offset += n
            self.io('chmod', lambda: os.fchmod(fd, c.mode_bits), path, mutating=True, scope='history', fd=fd, mode=c.mode_bits)
            self.flush(path)
        finally:
            self.io('close', lambda: os.close(fd), path, fd=fd)

    def rename(self, src, dst, exchange=False, scope='tree'):
        self.check_dir(src.parent)
        self.check_dir(dst.parent)
        # Anchor both parents with descriptors. /dev/fd paths bind the kernel
        # operation to those open directories, including a concurrently moved one.
        a = self.io('open', lambda: os.open(src.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), src.parent, flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            b = self.io('open', lambda: os.open(dst.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), dst.parent, flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.io('exchange' if exchange else 'rename_noreplace', lambda: _atomic(src.name, dst.name, exchange, a, b),
                        src, dst, mutating=True, scope=scope)
            finally:
                self.close_fd(b, dst.parent)
        finally:
            self.close_fd(a, src.parent)

    def close_fd(self, fd, path):
        attempted = False
        def close():
            nonlocal attempted
            attempted = True
            os.close(fd)
        try:
            self.io('close', close, path, fd=fd)
        except BaseException:
            # A hook may fail before the close. Release the real resource even
            # while another parent's close is unwinding. Once os.close starts,
            # an error may still mean the descriptor was released and reused.
            if not attempted:
                try:
                    os.close(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
            raise

    def connect(self):
        if self.db is None:
            self.check_dir(self.private)
            self.db = self.io('sqlite_open', lambda: sqlite3.connect(self.path, timeout=10), self.path)
            self.dirty.add(str(self.private))
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA fullfsync=ON')
            self.db.execute('CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, value BLOB NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, value BLOB NOT NULL)')
            self.db.commit()
        return self.db

    def rows(self):
        db = self.connect()
        rows = self.io('sqlite_read', lambda: db.execute('SELECT value FROM rows ORDER BY id').fetchall(), self.path)
        result = []
        for (data,) in rows:
            r = json.loads(data)
            result.append(r)
        # Directory identity records are append-only and individually full synced;
        # they do not introduce a third history transaction.
        record = self.private / (self.path.stem + '.directories')
        try:
            data = self.io('read', lambda: record.read_bytes() if record.exists() else b'', record)
        except FileNotFoundError:
            data = b''
        by_id = {r['id']: r for r in result}
        for line in data.splitlines():
            try:
                number, name, identity = json.loads(line)
            except (ValueError, TypeError):
                continue  # incomplete final append carries no authority
            if number in by_id:
                by_id[number]['created'][name] = identity
        return result

    def qrows(self):
        return [json.loads(r[0]) for r in self.io('sqlite_read', lambda: self.connect().execute('SELECT value FROM queue ORDER BY id').fetchall(), self.path)]

    def save(self, rows=(), qs=(), label='A', verify=False):
        db = self.connect()
        # Freeze expected values before any write/hook can affect the input row.
        expected_row = json.loads(_json(rows[0])) if verify else None
        def parts(row):
            return {'events': row['effective'], 'old_contents': [e[1] for e in row['effective']],
                    'new_contents': [e[2] for e in row['effective']], 'batch_digest': row['batch'][6]}
        expected_parts = {key: _json(value) for key, value in parts(expected_row).items()} if verify else {}
        def read_part(part):
            try:
                stored = [json.loads(value) for (value,) in db.execute('SELECT value FROM rows ORDER BY id').fetchall()]
                row = next(row for row in stored if row['id'] == expected_row['id'])
                return _json(parts(row)[part])
            except (ValueError, KeyError, IndexError, TypeError, StopIteration) as error:
                raise _Refusal('corrupt_history') from error
        try:
            for table, values in (('rows', rows), ('queue', qs)):
                for value in values:
                    self.io('sqlite_write', lambda v=value, t=table: db.execute('INSERT OR REPLACE INTO ' + t + ' VALUES (?,?)', (v['id'], _json(v))),
                            self.path, mutating=True, scope='history')
            if verify:
                for part, expected in expected_parts.items():
                    observed = self.io('sqlite_read', lambda part=part: read_part(part), self.path, part=part)
                    if observed != expected:
                        raise _Refusal('corrupt_history')
            self.flush_all()
            self.io('sqlite_commit', db.commit, self.path, mutating=True, scope='history', commit=label)
        except BaseException:
            db.rollback()
            raise

    def identity(self, r, name, identity):
        path = self.private / (self.path.stem + '.directories')
        fd = self.io('open', lambda: os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600),
                     path, flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode=0o600, mutating=True, scope='history')
        try:
            data = _json([r['id'], name, identity]) + b'\n'
            while data:
                n = self.io('write', lambda: os.write(fd, data), path, mutating=True, scope='history', fd=fd, data=data)
                if n <= 0:
                    raise OSError(errno.EIO, 'short identity append')
                data = data[n:]
            self.flush(path)
            self.flush(path.parent)
        finally:
            self.io('close', lambda: os.close(fd), path, fd=fd)
        r['created'][name] = identity

    def close(self):
        if self.db:
            self.db.close()
            self.db = None


def _row(r):
    return HistoryRow(r['id'], _unpack(r['batch']), r['state'], r.get('kind', 'forward'),
                      r.get('undoes'), r.get('kept', True), r.get('clean', False),
                      {k: tuple(v) if v else None for k, v in r['created'].items()},
                      {k: tuple(v) for k, v in r['emptied'].items()}, {} if r.get('hide_staged') else r['staged'])


def _newrow(s, batch, effective=None, kind='forward', undoes=None):
    rows = s.rows()
    staging = s.project / '.cocolo/staging' / batch.child_id
    return dict(id=max([r['id'] for r in rows], default=0) + 1, batch=_pack(batch), state='applying',
                effective=[_ev(e) for e in (effective or batch.events)], kind=kind, undoes=undoes,
                created={}, emptied={}, parents={}, staged={e.path: str(staging / f'e{i:05d}')
                for i, e in enumerate(batch.events) if e.kind == 'file'}, kept=True, clean=False)


def _staging(s, batch):
    for path in (s.project / '.cocolo', s.project / '.cocolo/staging', s.project / '.cocolo/staging' / batch.child_id):
        s.mkdir(path)
    return path


def _publish(s, path, c):
    existing = s.read(path)
    if existing is not None:
        if not _equal(existing, c):
            raise _Refusal('staging_collision')
        return
    # Private filenames are deterministic so an interrupted write is always named.
    temp = s.private / (s.path.stem + '-prepare-' + hashlib.sha256(os.fsencode(path.relative_to(s.project))).hexdigest()[:20])
    if s.read(temp) is not None:
        # An incomplete private prepare is not an owner pathname. Do not trust it.
        if not _equal(s.read(temp), c):
            s.io('unlink', lambda: os.unlink(temp), temp, mutating=True, scope='history')
    if s.read(temp) is None:
        s.write(temp, c)
    s.rename(temp, path, scope='staging')


def _stage(s, batch, effective):
    staging = _staging(s, batch)
    _publish(s, staging / '.gitignore', Content(b'*\n'))
    for i, e in enumerate(effective):
        if e.kind == 'file' and e.new:
            _publish(s, staging / f'e{i:05d}', e.new)
    return staging


def _git(s, *args):
    def read():
        result = subprocess.run(['git', '-C', str(s.project), *args], capture_output=True, timeout=15,
                                env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1'))
        if result.returncode:
            raise _Refusal('io')
        return result.stdout
    return s.io('git_read', read, s.project / '.git', argv=args)


def _head(s):
    head = s.read(s.project / '.git/HEAD').data.decode().strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if '..' in Path(ref).parts or Path(ref).is_absolute():
            raise _Refusal('unsafe_ref')
        value = s.read(s.project / '.git' / ref)
        if value:
            return value.data.decode().strip()
        packed = s.read(s.project / '.git/packed-refs')
        if packed:
            for line in packed.data.decode().splitlines():
                if line.endswith(' ' + ref):
                    return line.split()[0]
        raise _Refusal('missing_head')
    return head


def _validate(s, b, policy):
    if not b.child_id or b.child_id in ('.', '..') or '/' in b.child_id or '\\' in b.child_id:
        raise _Refusal('unsafe_child')
    if len(b.events) > MAX_PATHS:
        raise _Refusal('MAX_PATHS 2000')
    if not policy.verify_seal(b):
        raise _Refusal('seal')
    totals = [0, 0]
    names = set()
    aliases = {}
    for path, _ in s.walk(s.project, control=False):
        rel = path.relative_to(s.project).as_posix()
        aliases[unicodedata.normalize('NFC', rel).casefold()] = rel
    for e in b.events:
        p = Path(e.path)
        alias = unicodedata.normalize('NFC', e.path).casefold()
        if (p.is_absolute() or '..' in p.parts or not p.parts or p.as_posix() != e.path
                or any(n.casefold() in ('.git', '.cocolo') for n in p.parts)
                or e.path in names or e.kind not in ('file', 'folder') or e.v != 1
                or (alias in aliases and aliases[alias] != e.path)):
            raise _Refusal('invalid_path_or_alias')
        names.add(e.path)
        aliases[alias] = e.path
        for j, c in enumerate((e.base, e.new)):
            if c:
                if len(c.data) > MAX_FILE_BYTES:
                    raise _Refusal('file exceeds 4 MiB')
                totals[j] += len(c.data)
        if e.kind == 'file' and e.new and (e.new.mode_bits not in (0o644, 0o755) or (e.base and e.base.mode_bits != e.new.mode_bits)):
            raise _Refusal('candidate_mode')
        # Candidate and target parents are checked before any potentially blocking
        # open, independently of the seal verifier.
        for root, candidate in ((s.project, False), (b.candidate_root, True)):
            if root is None or (candidate and e.new is None):
                continue
            cur = Path(root)
            s.check_dir(cur)
            for part in p.parts[:-1]:
                cur /= part
                try:
                    s.check_dir(cur)
                except _Refusal as error:
                    if not candidate and str(error) == 'moved':
                        break
                    raise
                if not candidate and s.exists(cur / '.git'):
                    raise _Refusal('nested_repository')
            else:
                if e.kind == 'file':
                    actual = s.read(Path(root) / p)
                    if candidate and not _equal(actual, e.new):
                        raise _Refusal('candidate_changed')
                    if not candidate:
                        if e.base is None and actual is not None:
                            raise _Refusal('add_existing')
                        if e.base and (actual is None or actual.mode_bits != e.base.mode_bits):
                            raise _Refusal('base_mode_or_absence')
    if max(totals, default=0) > MAX_LANDING_BYTES:
        raise _Refusal('landing exceeds 16 MiB')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply'):
        if s.exists(s.project / '.git' / name):
            raise _Refusal('git_operation')
    config = configparser.ConfigParser(strict=False)
    config.read_string(s.read(s.project / '.git/config').data.decode())
    if config.get('core', 'sparsecheckout', fallback='false').lower() == 'true' or config.get('core', 'autocrlf', fallback='false').lower() not in ('false', ''):
        raise _Refusal('git_conversion_or_sparse')
    listing = _git(s, 'ls-files', '--stage', '-v').splitlines()
    if any(b'160000 ' in line[:10] or line.startswith(b'S ') for line in listing):
        raise _Refusal('submodule_or_sparse')
    paths = [e.path for e in b.events if e.kind == 'file']
    if paths:
        attrs = _git(s, 'check-attr', '-z', 'filter', 'text', 'eol', 'working-tree-encoding', 'ident', '--', *paths).split(b'\0')
        if any(attrs[i] not in (b'unspecified', b'unset', b'') for i in range(2, len(attrs), 3)):
            raise _Refusal('converted')
    for e in b.events:
        if e.path.endswith('.gitattributes') and e.new and any(x in e.new.data for x in (b'filter', b'text', b'eol', b'encoding', b'ident')):
            raise _Refusal('converted_candidate')
    space = s.io('statvfs', lambda: os.statvfs(s.private), s.private)
    if space.f_bavail * space.f_frsize < sum(totals) * 2:
        raise _Refusal('insufficient_space')


def _probe(s):
    # Probe under private storage, never in the owner tree; names are fixed and
    # discoverable even if the process dies before publishing a history row.
    a, b, c = [s.private / (s.path.stem + '-probe-' + x) for x in ('a', 'b', 'c')]
    for p in (a, b):
        if s.read(p) is None:
            s.write(p, Content(b'merge capability probe\n'))
    try:
        s.rename(a, b, True, scope='history')
        s.rename(a, c, scope='history')
        for p in (b, c):
            s.io('unlink', lambda: os.unlink(p), p, mutating=True, scope='history')
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS, errno.EXDEV):
            raise _Refusal('unsupported_filesystem')
        raise


def _anchor(s, r, e):
    parent = (s.project / e.path).parent
    s.check_dir(parent)
    rel = parent.relative_to(s.project).as_posix()
    info = s.info(parent)
    expected = r['created'].get(rel) or r.get('parents', {}).get(rel)
    if expected and list(expected) != [info.st_dev, info.st_ino]:
        s.moved.add(e.path)
        raise _Refusal('moved')


def _snapshot(s, r):
    deleted = {e.path for e in map(_de, r['effective']) if e.kind == 'file' and e.new is None}
    added = {e.path for e in map(_de, r['effective']) if e.new is not None or e.kind == 'folder'}
    candidates = {}
    for e in map(_de, r['effective']):
        parent = (s.project / e.path).parent
        while parent != s.project.parent:
            try:
                info = s.info(parent)
            except FileNotFoundError:
                break
            r['parents'][parent.relative_to(s.project).as_posix()] = [info.st_dev, info.st_ino]
            if parent == s.project:
                break
            parent = parent.parent
        if e.kind == 'file' and e.new is None:
            parent = (s.project / e.path).parent
            while parent != s.project and s.exists(parent):
                info = s.info(parent)
                name = parent.relative_to(s.project).as_posix()
                candidates[name] = [info.st_dev, info.st_ino]
                parent = parent.parent
    # Only ancestors whose surviving children are themselves eligible removals
    # become empty. An owner's pre-existing empty subfolder is not ours to prune.
    eligible = {}
    for name in sorted(candidates, key=lambda name: (-name.count('/'), name)):
        children = {p.relative_to(s.project).as_posix() for p in s.children(s.project / name)}
        if children.issubset(deleted | set(eligible)) and not any(p.startswith(name + '/') for p in added):
            eligible[name] = candidates[name]
    r['emptied'].update(eligible)


def _report(s, r, state=None, reason='', merged=(), conflicts=(), recorded=True, pending=False):
    oldphase = s.phase
    was_reporting = getattr(s, 'reporting', False)
    s.reporting = True
    s.phase = 'validation'
    try:
        s.moved.update(r.get('moved', ()))
        b = _unpack(r['batch'])
        staging = s.project / '.cocolo/staging' / b.child_id
        leftovers = []
        try:
            s.check_dir(staging)
            leftovers = [str(p) for p, info in s.walk(staging) if stat.S_ISREG(info.st_mode) and p.name != '.gitignore']
        except (_Refusal, OSError):
            pass
        rows = []
        for e in b.events:
            retained = tuple(p for p in leftovers if p == r['staged'].get(e.path))
            try:
                s.check_dir((s.project / e.path).parent)
                observed = s.read(s.project / e.path) if e.kind == 'file' else None
                classification = ('absent' if observed is None else 'base' if _equal(observed, e.base)
                                  else 'new' if _equal(observed, e.new) else 'owner')
            except _Refusal as error:
                classification = 'absent' if str(error) == 'moved' else 'owner'
            except OSError:
                classification = 'unknown'
            if e.path in s.moved:
                classification = 'moved'
            rows.append(PathReport(e.path, classification, retained))
        listed = {path.path for path in rows}
        for name in r.get('unrecorded_folders', ()):
            if name not in listed:
                rows.append(PathReport(name, 'owner'))
        notes = _NOTES + (('Moved parent locations are unverified.',) if s.moved else ())
        if pending:
            notes += ('Unresolved record pending; backups remain pinned.',)
        return Report(state or r['state'], reason or r.get('reason', ''), r['id'], tuple(rows), tuple(merged), tuple(conflicts),
                      tuple(leftovers), notes, recorded)

    finally:
        s.phase = oldphase
        s.reporting = was_reporting


def _apply(s, r):
    for e in map(_de, r['effective']):
        p = s.project / e.path
        _anchor(s, r, e)
        if e.kind == 'folder':
            if s.exists(p):
                raise _Refusal('owner')
            s.mkdir(p, 'tree')
            info = s.info(p)
            s.identity(r, e.path, [info.st_dev, info.st_ino])
            continue
        n = Path(r['staged'][e.path])
        observed = s.read(p)
        if not _equal(observed, e.base):
            raise _Refusal('owner')
        if _equal(e.base, e.new):
            continue
        if e.base is None:
            s.rename(n, p)
        elif e.new is None:
            s.rename(p, n)
        else:
            s.rename(n, p, True)
        captured = s.read(n) if e.base else None
        current = s.read(p)
        if (e.base and not _equal(captured, e.base)) or not _equal(current, e.new):
            if e.base and captured is not None and not _equal(captured, e.base):
                phase = s.phase
                s.phase = 'compensation'
                try:
                    if e.new and _equal(current, e.new):
                        s.rename(n, p, True)
                    elif current is None:
                        s.rename(n, p)
                    # One compensation only. Inspect again; any newer version
                    # at either name is left for the report, never overwritten.
                    s.read(p)
                    s.read(n)
                finally:
                    s.phase = phase
            raise _Refusal('owner')


def _reverse(s, r):
    owner = False
    for e in reversed(tuple(map(_de, r['effective']))):
        p = s.project / e.path
        try:
            _anchor(s, r, e)
        except _Refusal as error:
            parent_rel = p.parent.relative_to(s.project).as_posix()
            if str(error) == 'moved' and (parent_rel not in r.get('parents', {}) or parent_rel in r['created']):
                continue
            if str(error) == 'moved':
                s.moved.add(e.path)
            owner = True
            continue
        if e.kind == 'folder':
            identity = r['created'].get(e.path)
            try:
                info = s.info(p)
            except FileNotFoundError:
                continue
            if not identity:
                # A mkdir may have completed without its identity record. Its
                # pathname alone is never authority to remove that directory.
                if e.path not in r.setdefault('unrecorded_folders', []):
                    r['unrecorded_folders'].append(e.path)
                owner = True
            elif list(identity) == [info.st_dev, info.st_ino]:
                try:
                    s.flush(p)
                    s.io('rmdir', lambda: os.rmdir(p), p, mutating=True, scope='tree')
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
            continue
        n = Path(r['staged'][e.path])
        current = s.read(p)
        saved = s.read(n)
        if _equal(current, e.base):
            continue
        if e.new is not None and not _equal(current, e.new):
            owner = True
            continue
        if e.new is None and current is not None:
            owner = True
            continue
        if e.base is None:
            if current is not None:
                if saved is not None:
                    owner = True
                    continue
                s.rename(p, n)
            continue
        if e.new is None and saved is None:
            # Absence is not evidence that our delete ran.
            owner = True
            continue
        if saved is not None and not _equal(saved, e.base):
            owner = True
            continue
        if saved is None:
            _publish(s, n, e.base)
        if current is None:
            s.rename(n, p)
        else:
            s.rename(n, p, True)
        # A raced owner save is captured, inspected and compensated once.
        captured = s.read(n)
        if current is not None and not _equal(captured, e.new):
            owner = True
            if captured is not None:
                phase = s.phase
                s.phase = 'compensation'
                try:
                    if _equal(s.read(p), e.base):
                        s.rename(n, p, True)
                finally:
                    s.phase = phase
    return owner


def _terminal(s, r, state, label):
    r['state'] = state
    r['moved'] = sorted(s.moved)
    rows = [r]
    if r.get('kind') == 'undo' and state == 'landed':
        original = next(x for x in s.rows() if x['id'] == r['undoes'])
        original['state'] = 'undone'
        rows.append(original)
    try:
        s.save(rows, label=label)
        return True
    except OSError:
        # The after notification may have been lost after a successful commit.
        s.close()
        durable = next((x for x in s.rows() if x['id'] == r['id']), None)
        return bool(durable and durable['state'] == state)


def _putback(s, r, reason='', merged=()):
    try:
        owner = _reverse(s, r)
        if r.get('unrecorded_folders') and not reason:
            reason = 'io'
        state = 'needs_owner' if owner else 'reverted'
        if reason == 'io' and owner:
            state = 'needs_owner'
        r['reason'] = (('moved' if s.moved else reason if reason in ('unsafe_path', 'cross_device')
                        else 'owner') if owner and reason != 'io' else reason)
        durable = _terminal(s, r, state, 'reverse')
        if not durable:
            return _report(s, r, 'needs_owner', 'io', merged, recorded=False, pending=True)
        if state == 'reverted':
            try:
                _clean_row(s, r)
            except (OSError, _Refusal):
                return _report(s, r, reason='io', merged=merged)
        return _report(s, r, state, ('moved' if s.moved else reason if reason in ('unsafe_path', 'cross_device') else 'owner') if owner and reason != 'io' else reason, merged)
    except (OSError, _Refusal):
        # Keep the applying record on a persistent failure: recovery retains
        # its direction, and admission remains blocked.
        return _report(s, r, 'needs_owner', 'io', merged, recorded=False, pending=True)


def _clean_row(s, r):
    if r['state'] in ('applying', 'needs_owner'):
        return
    s.phase = 'cleanup'
    s.checkpoint('cleanup_inspect')
    b = _unpack(r['batch'])
    staging = s.project / '.cocolo/staging' / b.child_id
    try:
        s.check_dir(staging)
    except _Refusal as error:
        if str(error) == 'moved':
            _clean_empty(s, r)
            return
        raise
    allowed = [c for e in map(_de, r['effective']) for c in (e.base, e.new) if c]
    allowed += [Content(b'*\n')]
    # Every named staging file is inspected. A foreign prefix is never ours.
    for p in sorted(s.children(staging)):
        try:
            c = s.read(p)
        except _Refusal:
            continue
        if c is not None and any(_equal(c, a) for a in allowed):
            info = s.info(p)
            s.io('unlink', lambda: os.unlink(p), p, mutating=True, scope='staging',
                 terminal_durable=True, content_authorized=True, nlink=info.st_nlink)
    s.flush(staging)
    try:
        s.io('rmdir', lambda: os.rmdir(staging), staging, mutating=True, scope='staging')
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
    _clean_empty(s, r)
    s.flush_all()


def _clean_empty(s, r):
    if r['state'] == 'landed' and r['kind'] == 'undo':
        for name, identity in sorted(r.get('remove_folders', {}).items(), key=lambda x: -len(x[0])):
            p = s.project / name
            try:
                s.check_dir(p)
                info = s.info(p)
                if list(identity or ()) == [info.st_dev, info.st_ino]:
                    s.flush(p)
                    s.io('rmdir', lambda: os.rmdir(p), p, mutating=True, scope='tree')
            except _Refusal:
                continue
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
    if r['state'] == 'landed' and r['kind'] == 'forward':
        for name, identity in sorted(r['emptied'].items(), key=lambda item: -len(item[0])):
            p = s.project / name
            try:
                s.check_dir(p)
                info = s.info(p)
                if [info.st_dev, info.st_ino] == list(identity):
                    s.flush(p)
                    s.io('rmdir', lambda: os.rmdir(p), p, mutating=True, scope='tree')
            except _Refusal:
                continue
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
    s.flush_all()


def history(project, private_root, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        return tuple(_row(r) for r in s.rows())
    finally:
        s.close()


def queue(project, private_root, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        return tuple(QueueItem(q['id'], q['child'], q['state'], q['base'], q.get('kind', 'child'))
                     for q in sorted(s.qrows(), key=lambda q: (q.get('kind') != 'owner', q['id'])))
    finally:
        s.close()


def _enqueue(s, b):
    qs = s.qrows()
    existing = next((q for q in qs if q['child'] == b.child_id and (q.get('batch') == _pack(b) or q['state'] not in ('landed', 'resolved', 'cancelled'))), None)
    if existing:
        return existing
    q = dict(id=max([q['id'] for q in qs], default=0)+1, child=b.child_id, state='queued', base=b.base_commit, kind='child', batch=_pack(b))
    s.save(qs=[q], label='queue')
    return q


def enqueue(project, private_root, batch, *, policy, hook=None):
    with _project_lock(project):
        s = _Store(project, private_root, hook)
        try:
            _validate(s, batch, policy)
            previously_queued = any(x['child'] == batch.child_id for x in s.qrows())
            q = _enqueue(s, batch)
            return QueueItem(q['id'], q['child'], q['state'], q['base'])
        finally:
            s.close()


def _project_lock(project):
    with _LOCK_GUARD:
        return _LOCKS.setdefault(str(Path(project).absolute()), threading.RLock())


def stage(project, private_root, batch, *, policy, hook=None):
    s = _Store(project, private_root, hook)
    try:
        _validate(s, batch, policy)
        s.phase = 'staging'
        r = _newrow(s, batch)
        # Admission records can outlive staging before commit A. They carry its
        # complete permitted contents for restart cleanup, without tree direction.
        r['state'] = 'staged'
        s.save([r], label='queue')
        result = _stage(s, batch, batch.events)
        s.flush_all()
        return result
    finally:
        s.close()


def _merge(s, base, owner, child, staging, tag):
    basepath = staging / (tag + '-base')
    ownerpath = basepath if owner == base else staging / (tag + '-owner')
    childpath = staging / (tag + '-child')
    for p, data in dict([(basepath, base), (ownerpath, owner), (childpath, child)]).items():
        _publish(s, p, Content(data))
    argv = ['git', 'merge-file', '-p', str(ownerpath), str(basepath), str(childpath)]
    result = s.io('merge_file', lambda: subprocess.run(argv, cwd=staging, capture_output=True, timeout=15),
                  staging, argv=argv, cwd=str(staging))
    return Content(result.stdout) if result.returncode == 0 else None


def three_way_merge(base, owner, child, *, hook=None):
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        for name, data in (('base', base), ('owner', owner), ('child', child)):
            (root / name).write_bytes(data)
        argv = ['git', 'merge-file', '-p', str(root / 'owner'), str(root / 'base'), str(root / 'child')]
        detail = dict(argv=argv, cwd=str(root), mutating=False)
        if hook:
            hook(IOCall('merge_file', 'before', 'staging', str(root), detail=detail))
        result = subprocess.run(argv, cwd=root, capture_output=True, timeout=15)
        if hook:
            hook(IOCall('merge_file', 'after', 'staging', str(root), detail=detail, result=result))
        return Content(result.stdout) if result.returncode == 0 else None


def land(project, private_root, batch, *, policy, hook=None):
    with _project_lock(project):
        s = None
        r = None
        merged = []
        try:
            s = _Store(project, private_root, hook)
            _validate(s, batch, policy)
            rows = s.rows()
            if any(x['state'] in ('applying', 'needs_owner') for x in rows) or any(x['state'] in ('cancelled', 'waiting_owner') and x['child'] != batch.child_id for x in s.qrows()):
                r = _newrow(s, batch)
                return _report(s, r, 'waiting_owner', 'unresolved', recorded=False)
            # Reuse a stage-only row, which is not a landing intent.
            staged = next((x for x in rows if x['state'] == 'staged' and x['batch'] == _pack(batch)), None)
            previously_queued = any(x['child'] == batch.child_id for x in s.qrows())
            q = _enqueue(s, batch)
            if q['state'] == 'cancelled':
                return _report(s, _newrow(s, batch), 'cancelled', 'cancelled', recorded=False)
            if any(x['id'] < q['id'] and x['state'] == 'queued' for x in s.qrows()):
                raise _Refusal('fifo_order')
            current_head = _head(s)
            if current_head != batch.base_commit:
                if not previously_queued:
                    raise _Refusal('head_changed')
                # A stale already-admitted batch waits; a direct stale submission
                # is refused (the same immutable base is never silently rewritten).
                q['state'] = 'waiting_owner'
                ownerq = dict(id=max(x['id'] for x in s.qrows())+1, child='owner-'+current_head, state='owner', base=current_head, kind='owner')
                s.save(qs=[q, ownerq], label='queue')
                if policy.write_children and policy.builder:
                    rebuilt = policy.builder(batch, current_head)
                    if rebuilt:
                        q['state'] = 'resolved'
                        ownerq['state'] = 'resolved'
                        s.save(qs=[q, ownerq], label='queue')
                        return land(project, private_root, rebuilt, policy=policy, hook=hook)
                return _report(s, _newrow(s, batch), 'waiting_owner', 'owner', recorded=False)
            r = staged or _newrow(s, batch)
            _snapshot(s, r)
            s.checkpoint('after_intake')
            # Revalidate all anchors after the intake checkpoint, before preparing
            # any paths in staging or deciding on owner contents.
            s.check_dir(s.private)
            for e in batch.events:
                if (s.project / e.path).parent.relative_to(s.project).as_posix() in r['parents']:
                    _anchor(s, r, e)
            _probe(s)
            s.phase = 'staging'
            staging = _staging(s, batch)
            effective = []
            conflicts = []
            for i, e in enumerate(batch.events):
                if e.kind == 'file' and e.base and e.new:
                    owner = s.read(s.project / e.path)
                    result = _merge(s, e.base.data, owner.data, e.new.data, staging, f'm{i:05d}')
                    if result is None:
                        conflicts.append(e.path)
                    else:
                        result = replace(result, mode_bits=e.new.mode_bits)
                        if not _equal(owner, e.base) and not _equal(result, e.new):
                            merged.append(e.path)
                        e = replace(e, base=owner, new=result)
                effective.append(e)
            r['effective'] = [_ev(e) for e in effective]
            if conflicts:
                for e in effective:
                    if e.path in conflicts and e.new:
                        _publish(s, Path(r['staged'][e.path]), e.new)
                r['state'] = 'needs_owner'
                s.save([r], label='queue')
                q['state'] = 'waiting_owner'
                s.save(qs=[q], label='queue')
                return _report(s, r, 'waiting_owner', 'owner', merged, conflicts, recorded=False)
            _stage(s, batch, effective)
            s.checkpoint('staging_complete')
            s.check_dir(staging)
            s.check_dir(s.private)
            s.phase = 'history'
            r['state'] = 'applying'
            s.save([r], label='A', verify=True)
            s.phase = 'forward'
            if next(x for x in s.qrows() if x['id'] == q['id'])['state'] == 'cancelled':
                return _putback(s, r, 'cancelled', merged)
            _apply(s, r)
            s.phase = 'validation'
            s.checkpoint('final_validation')
            if next(x for x in s.qrows() if x['id'] == q['id'])['state'] == 'cancelled':
                try:
                    durable = next(x for x in s.rows() if x['id'] == r['id'])
                except (OSError, sqlite3.Error, StopIteration):
                    return Report('unknown', 'io', r['id'], recorded=False)
                if durable['state'] != 'applying':
                    return _report(s, durable, merged=merged)
                s.phase = 'reverse'
                return _putback(s, durable, 'cancelled', merged)
            bad = []
            for e in effective:
                _anchor(s, r, e)
                if e.kind == 'file' and not _equal(s.read(s.project / e.path), e.new):
                    bad.append(e.path)
            if bad:
                s.phase = 'reverse'
                result = _putback(s, r, 'owner', merged)
                return replace(result, conflicts=tuple(bad))
            if _head(s) != current_head:
                s.phase = 'reverse'
                return _putback(s, r, 'head_changed', merged)
            if not s.io('project_checks', lambda: policy.checks(s.project), s.project):
                s.phase = 'reverse'
                return _putback(s, r, 'checks', merged)
            if next(x for x in s.qrows() if x['id'] == q['id'])['state'] == 'cancelled':
                s.phase = 'reverse'
                return _putback(s, r, 'cancelled', merged)
            if not _terminal(s, r, 'landed', 'B'):
                return _report(s, r, 'needs_owner', 'io', merged, recorded=False, pending=True)
            _clean_row(s, r)
            q['state'] = 'landed'
            s.phase = 'cleanup'
            s.save(qs=[q], label='queue')
            return _report(s, r, merged=merged)
        except _Refusal as error:
            reason = str(error)
            if s is None:
                return Report('refused', reason)
            durable = next((x for x in s.rows() if r and x['id'] == r['id'] and x['state'] == 'applying'), None)
            if durable:
                s.phase = 'reverse'
                return _putback(s, durable, reason, merged)
            r = r or _newrow(s, batch)
            if reason == 'moved':
                for e in batch.events:
                    if not s.exists(s.project / e.path) and e.base:
                        s.moved.add(e.path)
            return _report(s, r, 'refused', reason, merged, recorded=False)
        except OSError:
            if s is None:
                return Report('refused', 'io')
            s.close()
            try:
                durable = next((x for x in s.rows() if r and x['id'] == r['id']), None)
            except (OSError, sqlite3.Error):
                return Report('unknown', 'io', r['id'] if r else None, recorded=False)
            if durable and durable['state'] == 'landed':
                # A terminal record fixes direction. Cleanup remains retryable;
                # its failure cannot turn a completed landing into a put-back.
                return _report(s, durable, reason='io', merged=merged)
            r = durable or r or _newrow(s, batch)
            if not durable:
                r['state'] = 'applying'
                try:
                    s.phase = 'history'
                    s.save([r], label='A')
                except OSError:
                    return _report(s, r, 'needs_owner', 'io', merged, recorded=False, pending=True)
            s.phase = 'reverse'
            return _putback(s, r, 'io', merged)
        finally:
            if s:
                s.close()


def rollback(project, private_root, batch_no, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        try:
            r = next(x for x in s.rows() if x['id'] == batch_no)
        except (OSError, sqlite3.Error, StopIteration):
            return Report('unknown', 'io', batch_no, recorded=False)
        if r['state'] not in ('applying', 'needs_owner'):
            return _report(s, r, 'refused', 'terminal', recorded=False)
        s.phase = 'reverse'
        return _putback(s, r)
    finally:
        s.close()


def recover(project, private_root, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        s.phase = 'recovery'
        try:
            rows = s.rows()
            queued = s.qrows()
        except (OSError, sqlite3.Error):
            return (Report('unknown', 'io', recorded=False),)
        for q in queued:
            if q.get('batch') and not any(r['batch'][0] == q['child'] for r in rows):
                b = _unpack(q['batch'])
                if s.is_dir(s.project / '.cocolo/staging' / b.child_id):
                    r = _newrow(s, b)
                    r['state'] = 'staged'
                    rows.append(r)
        reports = []
        for r in reversed(rows):
            # Forward and undo share the child's staging directory. An older
            # terminal row cannot discard a newer unresolved undo's evidence.
            if any(x['id'] > r['id'] and x['batch'][0] == r['batch'][0]
                   and x['state'] in ('applying', 'needs_owner') for x in rows):
                continue
            if r['state'] in ('undone', 'resolved'):
                continue
            if r['state'] == 'applying':
                s.phase = 'recovery'
                reports.append(_putback(s, r))
            elif r['state'] == 'staged':
                r['state'] = 'reverted'
                _terminal(s, r, 'reverted', 'reverse')
                _clean_row(s, r)
                reports.append(_report(s, r))
            else:
                try:
                    _clean_row(s, r)
                    if r['state'] != 'landed':
                        reports.append(_report(s, r))
                except OSError:
                    reports.append(_report(s, r, reason='io'))
        return tuple(reports)
    finally:
        s.close()


def undo(project, private_root, batch_no, *, hook=None):
    s = _Store(project, private_root, hook)
    r = None
    try:
        try:
            rows = s.rows()
        except (OSError, sqlite3.Error):
            return Report('unknown', 'io', recorded=False)
        original = next((x for x in rows if x['id'] == batch_no), None)
        if not original:
            return Report('refused', 'unknown_batch')
        if original['state'] != 'landed' or original['kind'] != 'forward' or not original['kept']:
            return _report(s, original, 'refused', 'undo_state', recorded=False)
        events = tuple(map(_de, original['effective']))
        paths = {e.path for e in events}
        def overlap(a, b):
            return a == b or a.startswith(b + '/') or b.startswith(a + '/')
        if any(x['id'] > batch_no and x['kind'] == 'forward' and x['state'] in ('landed', 'applying', 'needs_owner')
               and any(overlap(a, e[0]) for a in paths for e in x['effective']) for x in rows):
            return _report(s, original, 'refused', 'undo_order', recorded=False)
        # Undo uses the original event orientation in its public batch, while
        # the effective events hold the exact reversed operation.
        batch = _unpack(original['batch'])
        reverse_events = tuple(replace(e, base=e.new, new=e.base) for e in reversed(events) if e.kind == 'file')
        folders = set()
        for e in reverse_events:
            if e.new:
                parent = (s.project / e.path).parent
                while parent != s.project and not s.exists(parent):
                    folders.add(parent.relative_to(s.project).as_posix())
                    parent = parent.parent
        reverse_events = tuple(Event(name, None, None, kind='folder') for name in sorted(folders)) + reverse_events
        r = _newrow(s, batch, reverse_events, 'undo', batch_no)
        r['staged'] = {e.path: str(s.project / '.cocolo/staging' / batch.child_id / ('u'+str(i))) for i, e in enumerate(reverse_events) if e.kind == 'file'}
        r['remove_folders'] = original['created']
        for e in events:
            p = s.project / e.path
            if e.kind == 'file':
                if not _equal(s.read(p), e.new):
                    return _report(s, original, 'refused', 'owner', recorded=False)
            elif not s.is_dir(p):
                return _report(s, original, 'refused', 'missing_folder', recorded=False)
        _snapshot(s, r)
        s.phase = 'staging'
        staging = _staging(s, batch)
        _publish(s, staging / '.gitignore', Content(b'*\n'))
        for e in reverse_events:
            if e.new:
                _publish(s, Path(r['staged'][e.path]), e.new)
        s.phase = 'validation'
        s.save([r], label='A')
        s.phase = 'forward'
        _apply(s, r)
        s.phase = 'validation'
        if not _terminal(s, r, 'landed', 'undo'):
            return _report(s, r, 'needs_owner', 'io', recorded=False, pending=True)
        _clean_row(s, r)
        return _report(s, r)
    except (OSError, _Refusal):
        if r:
            s.close()
            durable = next((x for x in s.rows() if x['id'] == r['id']), None)
            if durable and durable['state'] == 'landed':
                return _report(s, durable, reason='io')
            if not durable:
                s.save([r], label='A')
            s.phase = 'reverse'
            return _putback(s, r, 'io')
        return _report(s, original, 'needs_owner', 'io', recorded=False)
    finally:
        s.close()


def status(project, private_root, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        rows = s.rows()
        for q in s.qrows():
            if q.get('batch') and not any(r['batch'][0] == q['child'] for r in rows):
                b = _unpack(q['batch'])
                if s.is_dir(s.project / '.cocolo/staging' / b.child_id):
                    rows.append(_newrow(s, b))
        results = []
        for r in rows:
            report = _report(s, r, recorded=r['state'] != 'applying')
            staging = s.project / '.cocolo/staging' / _unpack(r['batch']).child_id
            if s.is_dir(staging):
                report = replace(report, leftovers=tuple(dict.fromkeys(report.leftovers + (str(staging),))))
            results.append(report)
        return tuple(results)
    finally:
        s.close()


def cancel(project, private_root, ticket, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        q = next(q for q in s.qrows() if q['id'] == ticket)
        rows = [r for r in s.rows() if r['batch'] == q.get('batch')]
        latest = max(rows, key=lambda r: r['id']) if rows else None
        if latest and latest['state'] in ('landed', 'reverted', 'resolved', 'undone'):
            _clean_row(s, latest)
            return _report(s, latest)
        q['state'] = 'cancelled'
        s.save(qs=[q], label='queue')
        if latest and latest['state'] == 'applying':
            s.phase = 'reverse'
            return _putback(s, latest, 'cancelled')
        return Report('cancelled', 'cancelled', ticket, recorded=False)
    finally:
        s.close()


def retry(project, private_root, batch_no, *, policy, hook=None):
    s = _Store(project, private_root, hook)
    try:
        r = next(x for x in s.rows() if x['id'] == batch_no)
        if r['state'] not in ('applying', 'needs_owner'):
            return _report(s, r, 'refused', 'terminal', recorded=False)
        s.phase = 'reverse'
        return _putback(s, r)
    finally:
        s.close()


def accept(project, private_root, batch_no):
    s = _Store(project, private_root)
    try:
        rows = s.rows()
        r = next((x for x in rows if x['id'] == batch_no), None)
        qs = s.qrows()
        for q in qs:
            if q['state'] in ('cancelled', 'waiting_owner'):
                q['state'] = 'resolved'
        if r and r['state'] in ('applying', 'needs_owner'):
            r['state'] = 'resolved'
        s.save([r] if r else [], qs, label='retire')
        return _report(s, r) if r else Report('cancelled')
    finally:
        s.close()


def cleanup(project, private_root, *, hook=None):
    s = _Store(project, private_root, hook)
    try:
        reports = []
        try:
            rows = s.rows()
        except (OSError, sqlite3.Error):
            return (Report('unknown', 'io', recorded=False),)
        for r in rows:
            if any(x['state'] in ('applying', 'needs_owner') and x['batch'][0] == r['batch'][0]
                   for x in rows):
                reports.append(_report(s, r, recorded=r['state'] != 'applying'))
                continue
            try:
                _clean_row(s, r)
                r['clean'] = True
                s.phase = 'retirement'
                if sum(x['id'] > r['id'] and x['state'] == 'landed' and x['kind'] == 'forward' for x in rows) >= 10 or r['state'] == 'undone':
                    r['kept'] = False
                s.save([r], label='retire')
                reports.append(_report(s, r))
            except OSError:
                # E1/E2/F1: cleanup and retirement cannot change durable direction.
                # The local row may already have clean/kept edits from a failed commit.
                s.close()
                try:
                    durable = next(x for x in s.rows() if x['id'] == r['id'])
                except (OSError, sqlite3.Error, StopIteration):
                    reports.append(Report('unknown', 'io', r['id'], recorded=False))
                    continue
                pending = _report(s, durable, reason='io', recorded=durable['state'] != 'applying')
                reports.append(replace(pending, notes=pending.notes + ('Cleanup or retirement pending.',)))
        return tuple(reports)
    finally:
        s.close()


def _seed_history(project, private_root, rows, *, created_folders=None, emptied_folders=None, staged_paths=None):
    s = _Store(project, private_root)
    try:
        values = []
        for row in rows:
            r = _newrow(s, row.batch)
            r.update(id=row.batch_no, state=row.state, kind=row.kind, undoes=row.undoes,
                     kept=row.contents_kept, clean=row.cleanup_complete,
                     created=dict(created_folders if created_folders is not None else row.created_folders),
                     emptied=dict(emptied_folders if emptied_folders is not None else row.emptied_folders),
                     staged=dict(staged_paths if staged_paths is not None else row.staged_paths))
            if not r['staged']:
                r['hide_staged'] = True
                staging = s.project / '.cocolo/staging' / row.batch.child_id
                r['staged'] = {e.path: str(staging / f'e{i:05d}') for i, e in enumerate(row.batch.events) if e.kind == 'file'}
            values.append(r)
        s.save(values, label='queue')
    finally:
        s.close()
