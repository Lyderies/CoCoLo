"""How the last collector stopped (0.3.3, C10): its cause, and for an error its type and code locations.

The collector writes this owner-only record to the state root as it stops; the next collector
reports it in its health reply, so `learning-status` can say why the services restarted. No
message, value, path or identifier is ever recorded: only a fixed cause, an exception type name,
and `file.py:line function` locations.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

CAUSES = frozenset({'signal', 'requested', 'state_removed', 'thread_ended', 'error'})
MAX_LOCATIONS = 8
MAX_RECORD_BYTES = 4096
_TYPE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
_WHERE = re.compile(r'(?:[A-Za-z0-9_.-]{1,64}\.py|<other>):[0-9]{1,6} (?:[A-Za-z_<>][A-Za-z0-9_<>]{0,63})')
_FIELDS = {'schema_version', 'stopped_at_ms', 'started_at_ms', 'cause', 'error', 'where'}
# Only COCOLO's own files and Python's are named; anything else could carry an identifier.
_OWN_ROOT = Path(__file__).resolve().parent.parent
_STDLIB = Path(os.__file__).resolve().parent


def exit_path(state_root) -> Path:
    return Path(state_root) / 'collector-exit.json'


def _runtime_files():
    from .runtime_files import RUNTIME_FILES
    return frozenset(RUNTIME_FILES)


def _trusted_file(filename):
    """COCOLO's own files, or the standard library's, never an installed third-party package."""
    try:
        path = Path(filename).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    if path.suffix != '.py':
        return False
    if path.is_relative_to(_OWN_ROOT):
        # Only COCOLO's exact runtime files, not anything else placed under its folder.
        return path.relative_to(_OWN_ROOT).as_posix() in _runtime_files()
    return path.is_relative_to(_STDLIB) and not {'site-packages', 'dist-packages'} & set(path.parts)


def _error_name(error):
    """The exception's type name only when the type really is a built-in, or really is defined by
    a COCOLO or standard-library module: a class's own claim of where it comes from is not trusted."""
    kind = type(error)
    name = kind.__name__
    if not _TYPE.fullmatch(name):
        return 'Exception'
    module_name = getattr(kind, '__module__', None)
    if module_name == 'builtins':
        return name if getattr(builtins, name, None) is kind else 'Exception'
    module = sys.modules.get(module_name) if type(module_name) is str else None
    source = getattr(module, '__file__', None)
    if module is None or getattr(module, kind.__qualname__, None) is not kind or not source or not _trusted_file(source):
        return 'Exception'
    return name


def _locations(error):
    """`file.py:line function` from frame metadata only: no source file is read."""
    if error is None:
        return []
    frames = []
    traceback_ = error.__traceback__
    while traceback_ is not None:
        frames.append((traceback_.tb_frame.f_code, traceback_.tb_lineno))
        traceback_ = traceback_.tb_next
    found = []
    for code, line in frames[-MAX_LOCATIONS:]:
        if _trusted_file(code.co_filename):
            name = code.co_name if _TYPE.fullmatch(code.co_name) or code.co_name == '<module>' else '<other>'
            item = f'{Path(code.co_filename).resolve().name}:{line} {name}'  # The file that was checked.
        else:
            item = f'<other>:0 <other>'
        found.append(item if _WHERE.fullmatch(item) else '<other>:0 <other>')
    return found


def write_exit(state_root, *, started_at_ms, cause, error) -> None:
    """Best effort: a record that cannot be written is skipped, and stopping carries on."""
    with contextlib.suppress(Exception):
        record = {'schema_version': 1, 'stopped_at_ms': time.time_ns() // 1_000_000,
                  'started_at_ms': started_at_ms if type(started_at_ms) is int else None,
                  'cause': cause if cause in CAUSES else 'error',
                  'error': _error_name(error) if cause == 'error' else None,
                  'where': _locations(error) if cause == 'error' else []}
        if record['cause'] == 'error' and record['error'] is None:
            record['error'] = 'Exception'
        data = json.dumps(record, sort_keys=True).encode()
        target = exit_path(state_root)
        temporary = target.with_name(f'.{target.name}.{os.getpid()}.{secrets.token_hex(8)}')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                os.fchmod(descriptor, 0o600)  # Exactly owner-only, whatever the umask.
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()  # Gone already once replaced; removed when anything failed.


def checked(record):
    """The record if it has exactly the fixed shape, else None. Never raises."""
    try:
        if (type(record) is not dict or set(record) != _FIELDS
                or type(record['schema_version']) is not int or record['schema_version'] != 1
                or type(record['cause']) is not str or record['cause'] not in CAUSES
                or type(record['stopped_at_ms']) is not int
                or (record['started_at_ms'] is not None and type(record['started_at_ms']) is not int)
                or type(record['where']) is not list or len(record['where']) > MAX_LOCATIONS
                or any(type(item) is not str or not _WHERE.fullmatch(item) for item in record['where'])):
            return None
        if record['cause'] == 'error':
            # An error always names its type.
            if type(record['error']) is not str or not _TYPE.fullmatch(record['error']):
                return None
        elif record['error'] is not None or record['where']:
            return None
        return record
    except Exception:
        return None


def read_exit(state_root):
    """The last collector's exit record, or None when missing, malformed or not trusted.

    Only a regular file owned by this user and readable by no one else is read, without
    blocking: a FIFO, device or symlink reads as none.
    """
    try:
        descriptor = os.open(exit_path(state_root),
                             os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_RECORD_BYTES):
                return None
            raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_RECORD_BYTES:
            return None
        return checked(json.loads(raw))
    except Exception:
        return None
