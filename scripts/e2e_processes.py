"""Recorded ownership for disposable e2e Worlds; never search the machine by process name.

Copied beside boot.py so detached hosts still register when launched from an installed copy
with a deliberately empty environment. Importing this module performs no I/O.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

MANIFEST = '.e2e-owner.json'
PROCESSES = '.e2e-processes'
_CLIENT_PATHS = {}
_LIBPROC = []
SPAWN_ENV = 'COCOLO_E2E_SPAWN'
UNINSPECTABLE = 'uninspectable'  # identity() value: the process exists but this user cannot read its start time


def identity(pid):
    """Kernel birth identity (microseconds on macOS, boot ticks on Linux), excluding zombies."""
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform == 'darwin':
        if not _LIBPROC:  # one dlopen per process, not per call
            _LIBPROC.append(ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True))
        lib = _LIBPROC[0]
        info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo, PROC_PIDTBSDINFO
        ctypes.set_errno(0)
        size = lib.proc_pidinfo(pid, 3, 0, info, len(info))
        if size != len(info):
            error = ctypes.get_errno()
            if error in (0, errno.ESRCH, errno.ENOENT):
                return None
            if error in (errno.EPERM, errno.EACCES):
                # A privileged image (/bin/ps) cannot be inspected. That is "exists, uninspectable", not "gone":
                # only ESRCH from a signal-0 probe proves it exited. Callers never signal an uninspectable pid.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return None
                except PermissionError:
                    pass
                return UNINSPECTABLE
            raise OSError(error, 'cannot verify e2e process identity')
        if int.from_bytes(info.raw[4:8], sys.byteorder) == 5:  # SZOMB
            return None
        return f'{int.from_bytes(info.raw[120:128], sys.byteorder)}:{int.from_bytes(info.raw[128:136], sys.byteorder)}'
    if sys.platform.startswith('linux'):
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
        except FileNotFoundError:
            return None
        return None if not fields or fields[0] == 'Z' else fields[19]
    raise RuntimeError('e2e process identity requires macOS or Linux')


def read_json(path):
    try:
        # No symlinks or partial records; malformed input never grants ownership.
        if path.is_symlink() or path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + os.urandom(6).hex())
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
    temporary.replace(path)


@contextlib.contextmanager
def locked(base, name='.e2e-register.lock', *, shared=False):
    # A shared lock only needs a read-only descriptor (flock does not require write access), so a confined
    # product helper (macOS sandbox profile that denies every file write) can still take it. initialize()
    # creates the file; only the exclusive owner (World close) opens for append.
    if shared:
        descriptor = os.open(base / name, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)
        return
    with (base / name).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def initialize(base, *, registry=None):
    base = Path(base).resolve()
    (base / PROCESSES).mkdir(mode=0o700, exist_ok=True)
    with locked(base):
        record = read_json(base / MANIFEST)
        if record is None:
            record = {'base': str(base), 'marker': os.urandom(24).hex(),
                      'owner_pid': os.getpid(), 'owner_start': identity(os.getpid())}
            write_json(base / MANIFEST, record)
    registry = registry or os.environ.get('COCOLO_E2E_REGISTRY')
    if registry:
        registry = Path(registry)
        registry.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(registry / (record['marker'] + '.json'), record)
    return record['marker']


def _register(base, pid):
    if (base / '.e2e-closing').exists():
        raise RuntimeError('e2e World is closing')
    owner = read_json(base / MANIFEST)
    if not owner or owner.get('base') != str(base.resolve()):
        raise RuntimeError('e2e World has no ownership marker')
    start = identity(pid)
    if start is None or start == UNINSPECTABLE:
        # Exited already, or a privileged image whose start time cannot be confirmed: no record can be
        # trusted for a later signal, and this user cannot signal it anyway.
        return None
    record = {'pid': pid, 'start': start, 'base': owner['base'], 'marker': owner['marker']}
    write_json(base / PROCESSES / f'{pid}.json', record)
    return record


def register(base, pid):
    base = Path(base).resolve()
    with locked(base, shared=True):
        return _register(base, pid)


def _spawn_note(base, nonce):
    return base / PROCESSES / f'{nonce}.spawn'


def _spawned_by_tracked_parent(base):
    """True only for a child that its direct parent's tracked Popen started and therefore records itself."""
    nonce = os.environ.get(SPAWN_ENV, '')
    if len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        return False
    note = read_json(_spawn_note(base, nonce))
    owner = read_json(base / MANIFEST)
    return bool(note and owner and note.get('marker') == owner.get('marker') and note.get('parent') == os.getppid())


def install_tracking(base):
    """Called before any product code, including in every detached Python host."""
    base = Path(base).resolve()
    try:
        register(base, os.getpid())
    except PermissionError:
        # A confined product helper (the signature worker runs under a macOS sandbox profile that denies every
        # file write) cannot write its own record. That is tolerated only when its direct parent's tracked Popen
        # started it (it carries that spawn's nonce, whose note names this parent) and so recorded it. A process
        # started any other way (os.posix_spawn) or a grandchild inheriting the nonce fails closed.
        if not _spawned_by_tracked_parent(base):
            raise
    original = subprocess.Popen
    # A subclass preserves Popen's class interface used by some clients.
    class OwnedPopen(original):
        def __init__(self, *args, **kwargs):
            with locked(base, shared=True):
                if (base / '.e2e-closing').exists():
                    raise RuntimeError('e2e World is closing')
                owner = read_json(base / MANIFEST)
                nonce = os.urandom(16).hex()
                note = _spawn_note(base, nonce)
                write_json(note, {'marker': owner and owner.get('marker'), 'parent': os.getpid()})
                self._e2e_note = note
                kwargs['env'] = {**(kwargs.get('env') if kwargs.get('env') is not None else os.environ), SPAWN_ENV: nonce}
                try:
                    super().__init__(*args, **kwargs)
                except BaseException:
                    note.unlink(missing_ok=True)
                    raise
                try:
                    self._e2e_record = _register(base, self.pid)
                except BaseException:
                    self.kill()
                    self.wait()
                    note.unlink(missing_ok=True)
                    raise
        def wait(self, *args, **kwargs):
            result = super().wait(*args, **kwargs)
            forget(base, getattr(self, '_e2e_record', None))
            self._e2e_note.unlink(missing_ok=True)
            return result

        def poll(self):
            result = super().poll()
            if result is not None:
                forget(base, getattr(self, '_e2e_record', None))
                self._e2e_note.unlink(missing_ok=True)
            return result
    subprocess.Popen = OwnedPopen


def forget(base, record):
    """Only prune the record belonging to the child this Popen actually reaped."""
    if record:
        path = base / PROCESSES / f"{record['pid']}.json"
        if read_json(path) == record:
            path.unlink(missing_ok=True)


def survivors(base, owner):
    found = []
    for path in (base / PROCESSES).glob('*.json'):
        record = read_json(path)
        if (record and record.get('marker') == owner.get('marker') and record.get('base') == str(base)
                and type(record.get('pid')) is int and record['pid'] not in (os.getpid(), owner.get('owner_pid'))
                and isinstance(record.get('start'), str)):
            current = identity(record['pid'])
            if current == record['start'] or current == UNINSPECTABLE:
                found.append(record)  # uninspectable but alive: fail closed, report it, never signal it
            else:
                forget(base, record)
    return found


def drained_stop(base):
    """Use an installed client's own digest/protocol and explicit World socket; never auto-start."""
    socket = base / 'h/.local/state/codex-agents/collector.sock'
    if not socket.exists():
        return
    client = _CLIENT_PATHS.get(base)
    if client is None or not client.is_file():
        client = next((path for path in base.rglob('collector_client.py')
                       if path.parent.name == 'codex_agents'), None)
        if client is None:
            return
        _CLIENT_PATHS[base] = client
    runtime = client.parent.parent
    code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from codex_agents.collector_client import CollectorClient; '
            'CollectorClient(Path(sys.argv[2]), timeout=.5).shutdown(only_if_drained=True)')
    try:
        subprocess.run([sys.executable, '-I', '-B', '-S', '-c', code, str(runtime), str(socket)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={'PATH': os.defpath}, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _signal(record, sig):
    # Recheck immediately before every signal, rather than trusting a previous polling pass.
    if identity(record['pid']) == record['start']:
        with contextlib.suppress(ProcessLookupError):
            os.kill(record['pid'], sig)


def _wait(base, owner, seconds, pending=()):
    end = time.monotonic() + seconds
    verified = {record['pid']: record for record in pending}
    while True:
        live = {record['pid']: record for record in survivors(base, owner)}
        verified.update(live)
        # proc_pidinfo can report an exiting child gone before waitpid can reap it. Keep
        # previously verified direct children pending until the kernel actually releases them.
        for pid, record in verified.items():
            current = identity(pid)
            if current is not None and current != record['start']:
                continue  # Reused pid: never wait for or signal its replacement.
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue  # A detached grandchild belongs to init; its kernel identity suffices.
            if reaped:
                forget(base, record)
                live.pop(pid, None)
            elif current is None:
                live[pid] = record
        if not live or time.monotonic() >= end:
            return list(live.values())
        time.sleep(.02)


def cleanup(base, *, remove=True, marker=None):
    base = Path(base).resolve()
    if not base.exists():
        return
    try:
        with locked(base, '.e2e-cleanup.lock'):
            owner = read_json(base / MANIFEST)
            if not owner or owner.get('base') != str(base.resolve()) or (marker and owner.get('marker') != marker):
                return
            drained_stop(base)
            live = _wait(base, owner, .3)
            for record in live:
                _signal(record, signal.SIGTERM)
            live = _wait(base, owner, 1., live)
            # Wait for in-flight spawners, then prevent any new registration. Draining
            # and TERM handlers may still spawn until this final escalation boundary.
            with locked(base):
                (base / '.e2e-closing').touch()
            live = _wait(base, owner, 0, live)
            for record in live:
                _signal(record, signal.SIGKILL)
            if _wait(base, owner, 2., live):
                raise RuntimeError('e2e World still has live recorded processes')
            reap_worlds(base / 'worlds')
            if remove:
                remove_world(base)
    except FileNotFoundError:
        if base.exists():
            raise


def safe_world_base(base):
    base = Path(base).resolve()
    cache = (Path.home() / '.cache').resolve()
    return base.parent == cache and base.name.startswith('e2e-')


def remove_world(base):
    base = Path(base).resolve()
    if safe_world_base(base):
        shutil.rmtree(base)
        _CLIENT_PATHS.pop(base, None)


def reap_worlds(registry):
    """Only inspect the runner-assigned registry; never enumerate the account's cache."""
    registry = Path(registry)
    if not registry.is_dir():
        return
    for path in registry.glob('*.json'):
        owner = read_json(path)
        if not owner or not isinstance(owner.get('base'), str) or not isinstance(owner.get('marker'), str):
            continue
        base = Path(owner['base'])
        if not safe_world_base(base):
            continue
        actual = read_json(base / MANIFEST)
        if actual == owner:
            cleanup(base, marker=owner['marker'])
        if not base.exists():
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    # Only the owning World holds this pipe's write end. EOF survives SIGKILL and os._exit.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdin.buffer.read()
    cleanup(Path(sys.argv[1]))
