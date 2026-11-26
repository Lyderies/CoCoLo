"""HM-4: report-only integrity, size and growth checks.

Live ledger access, including the independent read-only backup handle, belongs to
LedgerOwner. sqlite3_backup_step retains its position between passes and releases
the source read lock after each step. A full integrity VM runs only on the private
copy and pauses in its progress handler between health passes. No live connection,
cursor or transaction crosses the owner handoff.
"""
from __future__ import annotations

from contextlib import closing
from collections import OrderedDict
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time

from .health_monitor import Check, Finding

GIB = 1024 ** 3
DAY_MS = 86_400_000
OWNER_SECONDS = .5  # total deadline shared by enumeration, quick check and copy
QUICK_SECONDS = .15
COPY_PAGES = 8
SQL_PROGRESS_OPS = 256
BUSY_COPY_SECONDS = .15
SETTINGS_BYTES = 4096
STATE_BYTES = 1024
MAX_SETTING_INTEGER = 2 ** 63 - 1
MAX_TRACKED_STORES = 32
DEFAULT_LIMITS = {'store_size_bytes': 8 * GIB, 'store_growth_bytes_per_day': GIB}
_SAVED_REASONS = {
    'store_corrupt': 'The store integrity check found damaged data; please review its backup.',
    'store_unreadable': 'The store could not be read; the health check will try again.',
    'quick_check_pending': 'The store check needs another pass; its time allowance was used.',
    'store_size': 'At its last check the store reached its size warning. New runs are still allowed.',
    'store_growth': 'At its last check the store exceeded its growth warning. New runs are still allowed.',
}


def open_reader(path):
    """Never creates a missing store, follows a final symlink, or writes the store."""
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise OSError('store unavailable')
    return sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=0)


def _finding(project, reason, text):
    return Finding('ledger', 'project' if project else 'machine', 'project_store' if project else 'ledger',
                   'reported', reason, text, project)


def _amount(value):
    return f'{value // GIB} GiB' if value % GIB == 0 else f'{value} bytes'


_SQLITE_API = None


def _sqlite_api():
    # Resolve/load the library before entering the runtime owner request. On
    # Linux find_library may invoke the system linker cache helper.
    global _SQLITE_API
    if _SQLITE_API is None:
        library = ctypes.util.find_library('sqlite3')
        if library is None:
            raise OSError('SQLite backup unavailable')
        library_api = ctypes.CDLL(library)
        pointer = ctypes.c_void_p
        signatures = {
            'sqlite3_open_v2': ([ctypes.c_char_p, ctypes.POINTER(pointer), ctypes.c_int, ctypes.c_char_p], ctypes.c_int),
            'sqlite3_close': ([pointer], ctypes.c_int),
            'sqlite3_backup_init': ([pointer, ctypes.c_char_p, pointer, ctypes.c_char_p], pointer),
            'sqlite3_backup_step': ([pointer, ctypes.c_int], ctypes.c_int),
            'sqlite3_backup_finish': ([pointer], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(library_api, name)
            function.argtypes, function.restype = args, result
        _SQLITE_API = library_api
    return _SQLITE_API


class Snapshot:
    """Incremental SQLite online backup; every method is called on the owner.

    Python's Connection.backup loops to completion, even with pages=N. Use the
    public C backup API with independent handles instead of Python object pointers
    so a pass really returns after one bounded page slice. No extension is needed.
    """
    def __init__(self, source, path):
        self.path = Path(path)
        self._thread = threading.current_thread()
        self.source = ctypes.c_void_p()
        self.target = ctypes.c_void_p()
        self.backup = None
        self.blocked = False
        self.lib = _sqlite_api()
        try:
            # READONLY | NOFOLLOW; no busy timeout and no source transaction
            # persists between backup_step calls.
            self._check(self.lib.sqlite3_open_v2(os.fsencode(source), ctypes.byref(self.source), 1 | 0x01000000, None))
            self._check(self.lib.sqlite3_open_v2(os.fsencode(path), ctypes.byref(self.target), 2 | 4, None))
            self.backup = self.lib.sqlite3_backup_init(self.target, b'main', self.source, b'main')
            if not self.backup:
                raise sqlite3.DatabaseError('snapshot unavailable')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code):
        if code in (11, 26):
            raise sqlite3.DatabaseError('store corrupt')
        if code:
            raise OSError('snapshot unavailable')

    def _guard(self):
        if threading.current_thread() is not self._thread:
            raise RuntimeError('snapshot wrong owner')

    def step(self, deadline):
        self._guard()
        if time.monotonic() >= deadline:
            return False
        code = self.lib.sqlite3_backup_step(self.backup, COPY_PAGES)
        self.blocked = code in (5, 6)
        if code == 101:  # SQLITE_DONE
            self.close()
            return True
        if code in (0, 5, 6):  # OK / BUSY / LOCKED: retry next pass, never sleep
            return False
        self._check(code)
        return False

    def close(self):
        self._guard()
        if self.backup:
            self.lib.sqlite3_backup_finish(self.backup)
            self.backup = None
        for handle in (self.target, self.source):
            if handle.value:
                self.lib.sqlite3_close(handle)
                handle.value = None


class CopyScan:
    """One resumable full integrity VM on a private copy, never a live ledger.

    A condition grants execution only until the current pass deadline. Between
    passes the progress callback sleeps, retaining VM position, rather than
    interrupting and restarting a large integrity_check forever.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.done = threading.Event()
        self.condition = threading.Condition()
        self.deadline = 0.
        self.stopped = False
        self.result = None
        self.thread = threading.Thread(target=self._run, name='health-ledger-copy', daemon=True)
        self.thread.start()

    def advance(self, deadline):
        with self.condition:
            self.deadline = deadline
            self.condition.notify_all()

    def _progress(self):
        with self.condition:
            while not self.stopped and time.monotonic() >= self.deadline:
                self.condition.wait()
            return int(self.stopped)

    def _run(self):
        try:
            if self._progress():
                return
            # This is a private completed copy, so no live owner is involved.
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
                db.set_progress_handler(self._progress, SQL_PROGRESS_OPS)
                rows = db.execute('PRAGMA integrity_check(1)').fetchone()
                self.result = 'ok' if rows == ('ok',) else 'corrupt'
        except sqlite3.DatabaseError as error:
            self.result = 'corrupt' if getattr(error, 'sqlite_errorcode', 0) in (11, 26) else 'unavailable'
        finally:
            self.done.set()

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        self.thread.join(.1)


class LedgerCheck(Check):
    name = 'ledger'

    def __init__(self, state, *, settings_path=None):
        self.state = state
        self.settings_path = Path(settings_path) if settings_path is not None else state.root / 'health-limits.json'
        self.limits = dict(DEFAULT_LIMITS)
        self._settings_invalid = False
        self._baseline = {}
        self._last_full = {}
        self._first_seen = {}
        self._observed = {}
        self._project_after = ''
        self._next_ledger = True
        self._job = None
        self._scan = None
        self._temporary = None
        self._job_project = None
        self._job_path = None
        self._loaded = set()
        self._loaded_completion = {}
        self._full_result = {}
        self._seen = set()
        self._state_invalid = False
        self._order = OrderedDict()
        self._now_ms = 0

    def _retain(self, project):
        if project in self._order:
            self._order.move_to_end(project)
            return
        while len(self._order) >= MAX_TRACKED_STORES:
            # Keep the ledger and the one active full-check job; every other
            # observation can be reloaded from bounded per-store metadata.
            deleted = next(key for key in self._order if key is not None and key != self._job_project)
            del self._order[deleted]
            self._loaded.discard(deleted)
            self._seen.discard(deleted)
            for cache in (self._observed, self._baseline, self._last_full, self._first_seen,
                          self._loaded_completion, self._full_result):
                cache.pop(deleted, None)
        self._order[project] = None

    def _state_path(self, project):
        from .health_monitor import PROJECT_ID
        if project is not None and PROJECT_ID.fullmatch(project) is None:
            raise ValueError('invalid project identity')
        if project is not None:
            return self.state.root / 'projects' / project / 'health-ledger.json'
        return self.state.root / 'health-ledger' / 'ledger.json'

    def _load(self, project):
        self._retain(project)
        if project in self._loaded:
            return
        self._loaded.add(project)
        try:
            fd = os.open(self._state_path(project), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError()
                raw = stream.read(STATE_BYTES + 1)
            if len(raw) > STATE_BYTES:
                raise ValueError()
            row = json.loads(raw)
            if set(row) != {'first_seen', 'last_full', 'baseline', 'full_result', 'observed'}:
                raise ValueError()
            first, last, baseline = row['first_seen'], row['last_full'], row['baseline']
            if (type(first) is not int or first < 0 or
                    (last is not None and (type(last) is not int or last < 0)) or
                    type(baseline) is not list or len(baseline) != 2 or
                    any(type(value) is not int or value < 0 for value in baseline) or
                    row['full_result'] not in (None, 'ok', 'corrupt', 'unavailable')):
                raise ValueError()
            reasons = row['observed']
            if (type(reasons) is not list or any(type(reason) is not str or reason not in _SAVED_REASONS for reason in reasons)
                    or len(reasons) != len(set(reasons))):
                raise ValueError()
            self._observed[project] = [_finding(project, reason, _SAVED_REASONS[reason]) for reason in reasons]
            self._first_seen[project] = first
            self._baseline[project] = tuple(baseline)
            self._full_result[project] = row['full_result']
            if last is not None:
                self._last_full[project] = last
                self._loaded_completion[project] = last
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self._state_invalid = True

    def _save(self, project):
        path = self._state_path(project)
        temporary = None
        try:
            if project is not None:
                path.parent.parent.mkdir(mode=0o700, exist_ok=True)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            row = {'first_seen': self._first_seen.get(project, self._baseline[project][0]),
                   'last_full': self._last_full.get(project), 'baseline': self._baseline[project],
                   'full_result': self._full_result.get(project),
                   'observed': sorted({item.reason_code for item in self._observed.get(project, [])
                                       if item.reason_code in _SAVED_REASONS})}
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(row, separators=(',', ':')).encode('ascii'))
            os.replace(temporary, path)
        except OSError:
            self._state_invalid = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def reload_settings(self):
        limits = dict(DEFAULT_LIMITS)
        invalid = False
        try:
            fd = os.open(self.settings_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError()
                raw = stream.read(SETTINGS_BYTES + 1)
            if len(raw) > SETTINGS_BYTES:
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            data = json.loads(raw, object_pairs_hook=unique)
            if type(data) is not dict or set(data) - set(limits):
                raise ValueError()
            for key, value in data.items():
                if type(value) is not int or not 0 < value <= MAX_SETTING_INTEGER:
                    raise ValueError()
            limits.update(data)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            invalid = True
        self.limits = limits
        self._settings_invalid = invalid

    def status_summary(self):
        return ('Ledger warnings: size at ' + _amount(self.limits['store_size_bytes'])
                + '; growth over ' + _amount(self.limits['store_growth_bytes_per_day']) + '/day.')

    def status_findings(self, project):
        """Load at most two fixed-size observation files, never a live store.

        The monitor caps one pass's returned findings. Reading the viewer's saved
        observation here keeps an older project's warning visible even after its
        in-memory cache entry was evicted by many other projects.
        """
        reading = LedgerCheck(self.state, settings_path=self.settings_path)
        reading.limits = self.limits
        for key in (None,) if project is None else (None, project):
            reading._load(key)
        return reading._findings(self._now_ms)

    def measure(self, project, size, now):
        self._load(project)
        found = []
        ceiling = self.limits['store_size_bytes']
        rate = self.limits['store_growth_bytes_per_day']
        label = 'The project store' if project else 'The ledger'
        if size >= ceiling:
            found.append(_finding(project, 'store_size',
                                  f'{label} has reached the size warning of {_amount(ceiling)}. New runs are still allowed.'))
        before = self._baseline.get(project)
        if before is not None:
            timestamp, previous = before
            elapsed = now - timestamp
            if elapsed > 0 and (size - previous) * DAY_MS > rate * elapsed:
                found.append(_finding(project, 'store_growth',
                                      f'{label} is growing faster than {_amount(rate)} per day. New runs are still allowed.'))
            # A full-day comparison window, rather than a single noisy sweep.
            if elapsed >= DAY_MS or size < previous:
                self._baseline[project] = (max(now, timestamp), size)
        else:
            self._baseline[project] = (now, size)
        self._save(project)
        return found

    def completion_findings(self, project, completed, now):
        if now - completed > DAY_MS:
            return [_finding(project, 'full_check_overdue',
                             'The full store integrity check has not completed within 24 hours; it is overdue.')]
        return []

    def check_copy(self, path, project, context):
        return self._quick(path, project, context.deadline, full=True)

    def _quick(self, path, project, deadline, *, full=False):
        try:
            with closing(open_reader(path)) as db:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), SQL_PROGRESS_OPS)
                row = db.execute('PRAGMA integrity_check(1)' if full else 'PRAGMA quick_check(1)').fetchone()
                if row != ('ok',):
                    return [_finding(project, 'store_corrupt', 'The store integrity check found damaged data; please review its backup.')]
        except sqlite3.DatabaseError as error:
            code = getattr(error, 'sqlite_errorcode', 0) & 255
            if code == 9:  # budget spent: incomplete, never a clean result
                return [_finding(project, 'quick_check_pending', 'The store check needs another pass; its time allowance was used.')]
            reason = 'store_corrupt' if code in (11, 26) else 'store_unreadable'
            return [_finding(project, reason, 'The store integrity could not be verified; please review its backup.')]
        except OSError:
            return [_finding(project, 'store_unreadable', 'The store could not be read; the health check will try again.')]
        return []

    def _clear_job(self):
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._scan is not None:
            self._scan.close()
            # Do not unlink a copy while its VM still runs.
            if self._scan.thread.is_alive():
                return
            self._scan = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._job_path = None
        self._job_project = None

    def visit(self, path, project, context):
        """Owner-only live store slice; total deadline was set before queuing."""
        now = context.now_ms
        self._load(project)
        self._first_seen.setdefault(project, now)
        if project is not None:
            self._seen.add(project)
        found = self._quick(path, project, min(context.deadline, time.monotonic() + QUICK_SECONDS))
        try:
            # Include retained WAL bytes: growth must not hide in an uncheckpointed WAL.
            size = Path(path).stat().st_size
            wal = Path(str(path) + '-wal')
            if wal.exists():
                size += wal.stat().st_size
            found.extend(self.measure(project, size, now))
            last = self._last_full.get(project, self._first_seen[project])
            found.extend(self.completion_findings(project, last, now))
            # Begin before the daily deadline; a continuously busy queue still
            # gets a shorter copy quantum on every pass.
            due = project not in self._last_full or now - last >= DAY_MS // 2
            if self._job_path is None and due and time.monotonic() < context.deadline:
                scratch = self.state.root / 'scratch'
                scratch.mkdir(mode=0o700, exist_ok=True)
                self._temporary = tempfile.TemporaryDirectory(prefix='health-ledger-', dir=scratch)
                self._job_path, self._job_project = Path(path), project
                self._job = Snapshot(path, Path(self._temporary.name) / 'copy.sqlite3')
            if self._job is not None and self._job_path == Path(path):
                self._copy_slice(context.deadline)
        except sqlite3.DatabaseError:
            found.append(_finding(project, 'store_corrupt', 'The store copy found damaged data; please review its backup.'))
            self._clear_job()
        except OSError:
            found.append(_finding(project, 'store_unreadable', 'The store copy could not be made; the health check will try again.'))
            self._clear_job()
        self._observed[project] = found
        if project in self._baseline:
            self._save(project)
        return found

    def _copy_slice(self, deadline):
        owner = vars(self.state).get('_ledger_owner')
        queue = getattr(owner, 'queue', None)
        if queue is not None and queue.snapshot()['retained'] > 1:
            deadline = min(deadline, time.monotonic() + BUSY_COPY_SECONDS)
        while self._job is not None and time.monotonic() < deadline:
            if self._job.step(deadline):
                self._job = None
                self._scan = CopyScan(Path(self._temporary.name) / 'copy.sqlite3')
                break
            if self._job.blocked:
                break

    def _owner(self, operation, deadline):
        from . import ledger_waits
        from .sql_read_budget import SQLReadError
        owner = vars(self.state).get('_ledger_owner')
        if owner is None:
            raise SQLReadError('sql_read_unavailable')
        with ledger_waits.request_until(int(deadline * 1_000_000_000)):
            return owner.call(operation, caller='health_ledger', contain_errors=True)

    def _select(self, context):
        if self._next_ledger:
            self._next_ledger = False
            return self.state.database, None
        with closing(open_reader(self.state.database)) as db:
            db.set_progress_handler(lambda: int(context.expired()), SQL_PROGRESS_OPS)
            row = db.execute('SELECT project_id FROM projects WHERE project_id>? ORDER BY project_id LIMIT 1',
                             (self._project_after,)).fetchone()
        if row is None:
            self._project_after = ''
            self._next_ledger = True
            caches = (self._observed, self._baseline, self._last_full, self._first_seen,
                      self._loaded_completion, self._full_result)
            for cache in caches:
                for deleted in set(cache) - self._seen - {None}:
                    del cache[deleted]
                    self._loaded.discard(deleted)
                    self._order.pop(deleted, None)
                    self._state_path(deleted).unlink(missing_ok=True)
            self._seen.clear()
            return self.state.database, None
        self._project_after = row[0]
        if row[0] in self._loaded:
            self._seen.add(row[0])
        return self.state.root / 'projects' / row[0] / 'history.sqlite3', row[0]

    def run(self, context):
        from dataclasses import replace
        from .sql_read_budget import SQLReadError
        if context.expired():
            return []
        self._now_ms = context.now_ms
        self.reload_settings()
        try:
            _sqlite_api()
        except OSError:
            return [_finding(None, 'store_unreadable', 'The SQLite copy check is unavailable; the health check will try again.')]
        # Shared absolute budget: queue wait, enumeration, quick check and backup
        # never each receive their own 750 ms allowance.
        bounded = replace(context, deadline=min(context.deadline, time.monotonic() + OWNER_SECONDS))
        try:
            def work():
                if bounded.expired():
                    return
                path, project = self._select(bounded)
                if Path(path).exists() and not bounded.expired():
                    self.visit(path, project, bounded)
                # Continue the one active copy even while selection rotates fairly.
                if self._job is not None and self._job_path != path and not bounded.expired():
                    self._copy_slice(bounded.deadline)
            self._owner(work, bounded.deadline)
        except (OSError, sqlite3.Error, SQLReadError):
            self._observed[None] = [_finding(None, 'store_unreadable', 'The ledger health check could not read the store; it will try again.')]
        if self._scan is not None:
            self._scan.advance(context.deadline)
            self._scan.done.wait(min(.02, max(0, context.deadline - time.monotonic())))
            if self._scan.done.is_set():
                project = self._job_project
                if self._scan.result in ('ok', 'corrupt'):
                    self._last_full[project] = context.now_ms
                    items = [f for f in self._observed.get(project, []) if f.reason_code != 'full_check_overdue']
                    self._observed[project] = items
                self._full_result[project] = self._scan.result
                if project in self._baseline:
                    self._save(project)
                try:
                    self._owner(self._clear_job, min(context.deadline, time.monotonic() + .05))
                except SQLReadError:
                    pass  # Retry closing owner handles on the following pass.
        return self._findings(context.now_ms)

    def _findings(self, now_ms):
        found = [item for items in self._observed.values() for item in items
                 if item.reason_code != 'full_check_overdue']
        for project, first in self._first_seen.items():
            found.extend(self.completion_findings(project, self._last_full.get(project, first), now_ms))
        for project, result in self._full_result.items():
            if result == 'corrupt':
                found.append(_finding(project, 'store_corrupt', 'The full store integrity check found damaged data; please review its backup.'))
            elif result == 'unavailable':
                found.append(_finding(project, 'full_check_unavailable', 'The full store check could not finish; it will try again.'))
        if self._settings_invalid:
            found.append(_finding(None, 'health_settings_invalid', 'The ledger warning settings could not be read; the default limits are in force.'))
        if self._state_invalid:
            found.append(_finding(None, 'health_state_unavailable', 'The saved ledger health reading could not be read or saved; the check will try again.'))
        return found

    def close(self):
        """Called at collector shutdown; owner handles must close on their thread."""
        if self._job is not None:
            self._owner(self._clear_job, time.monotonic() + OWNER_SECONDS)
        else:
            self._clear_job()


def register(monitor, state, **options):
    """Primary wiring (deferred with S8): health_ledger.register(monitor, state)."""
    check = LedgerCheck(state, **options)
    monitor.register(check)
    return check
