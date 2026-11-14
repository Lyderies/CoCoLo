"""Durable launch evidence for the runs one project service claimed.

The project service writes one owner-only file per admitted claim, in the
project's own state folder, and moves it forward as the run goes:

- ``claimed``: the account admitted the claim; nothing was started. Written
  before the run can reach its launch, so a run still ``claimed`` never
  started a child.
- ``spawning``: written (and flushed to disk) immediately before the child is
  created. A launch whose ``spawning`` write fails never happens.
- ``launched``: the child's pid and kernel birth time, once it is identified.
- ``watched``: the detached watchdog's pid and birth time, once it has
  acknowledged that it tracks the child's tree.

A normal release removes the file. Each service instance holds an exclusive
lock on its own ``<instance>.lock`` for as long as any of its threads may
launch or own a child; the kernel releases it when the process dies. A later
instance settles an earlier one's run only when that lock is free and
`tree_state` finds positive kernel evidence that nothing of the run can still
be running. Missing, unreadable or ambiguous evidence is never that evidence.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from .process_tree import process_gone, process_record

STATES = frozenset({'claimed', 'spawning', 'launched', 'watched', 'settled'})
MAX_ENTRY_BYTES = 65_536
MAX_ENTRIES = 256
# A settled run's tombstone answers status and abandonment for this long.
TOMBSTONE_MS = 86_400_000
PS_PATHS = ('/bin/ps', '/usr/bin/ps')
PS_TIMEOUT_SECONDS = 5
_BRIDGE = re.compile(r'brg_[0-9a-f]{32}')
_INSTANCE = re.compile(r'[0-9a-f]{32}')
_HANDLE = re.compile(r'hrn_[0-9a-f]{48}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'version', 'state', 'instance', 'bridge_run_id', 'run_handle', 'attempt', 'host_client', 'lease',
           'identity_digest', 'claim_request_id', 'storage_evidence', 'workspace', 'child', 'watchdog',
           'outcome', 'settled_at_ms'}


class JournalError(RuntimeError):
    pass


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _identity(value):
    return (type(value) is dict and value.keys() == {'pid', 'birth'} and type(value['pid']) is int
            and value['pid'] > 1 and type(value['birth']) is str and 0 < len(value['birth']) <= 64)


def checked_entry(value):
    """The entry if every field has its fixed shape, else None."""
    if type(value) is not dict or value.keys() != _FIELDS or value['version'] != 1 or value['state'] not in STATES:
        return None
    if (not _matches(value['instance'], _INSTANCE) or not _matches(value['bridge_run_id'], _BRIDGE)
            or not _matches(value['run_handle'], _HANDLE) or type(value['attempt']) is not int
            or type(value['host_client']) is not str or type(value['lease']) is not dict
            or not _matches(value['identity_digest'], _DIGEST) or type(value['claim_request_id']) is not str
            or type(value['storage_evidence']) is not str or (value['workspace'] is not None
                                                               and type(value['workspace']) is not str)):
        return None
    state = value['state']
    if state in {'launched', 'watched'} and not _identity(value['child']):
        return None
    if state == 'watched' and not _identity(value['watchdog']):
        return None
    if state == 'settled' and (value['outcome'] not in {'settled', 'released'}
                               or type(value['settled_at_ms']) is not int):
        return None
    return value


class LaunchJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = os.lstat(self.directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise JournalError('launch journal folder is not owner-only')
            self.instance = secrets.token_hex(16)
            self._lock_fd = os.open(self._lock_path(self.instance),
                                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except OSError as error:
            raise JournalError('launch journal is unavailable') from error
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise JournalError('launch journal is unavailable') from error

    def _lock_path(self, instance):
        return self.directory / (instance + '.lock')

    def _path(self, bridge_run_id):
        if not _matches(bridge_run_id, _BRIDGE):
            raise JournalError('bridge run id is invalid')
        return self.directory / (bridge_run_id + '.json')

    def _sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, bridge_run_id, value):
        """Replace the entry atomically and durably: the file, then the folder, reach the disk."""
        if checked_entry(value) is None:
            raise JournalError('launch entry is invalid')
        body = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(body) > MAX_ENTRY_BYTES:
            raise JournalError('launch entry is too large')
        temporary = self.directory / ('.%s.%s.tmp' % (bridge_run_id, secrets.token_hex(4)))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._path(bridge_run_id))
            self._sync_directory()
        except OSError as error:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise JournalError('launch entry was not written') from error

    def _read(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except OSError:
            return None
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > MAX_ENTRY_BYTES):
                return None
            raw = os.read(descriptor, MAX_ENTRY_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return None
        value = checked_entry(value)
        if value is None or path.name != value['bridge_run_id'] + '.json':
            return None
        return value

    def entry(self, bridge_run_id):
        return self._read(self._path(bridge_run_id))

    def _advance(self, bridge_run_id, before, **changes):
        value = self.entry(bridge_run_id)
        if value is None or value['state'] != before or value['instance'] != self.instance:
            raise JournalError('launch entry is not ' + before)
        self._write(bridge_run_id, {**value, **changes})

    def claimed(self, bridge_run_id, *, run_handle, attempt, host_client, lease, identity_digest, claim_request_id,
                storage_evidence, workspace):
        """The account admitted this run's claim; written before the run can reach its launch."""
        if type(storage_evidence) is not bytes:
            raise JournalError('storage evidence is invalid')
        value = {'version': 1, 'state': 'claimed', 'instance': self.instance, 'bridge_run_id': bridge_run_id,
                 'run_handle': run_handle, 'attempt': attempt, 'host_client': host_client, 'lease': lease,
                 'identity_digest': identity_digest, 'claim_request_id': claim_request_id,
                 'storage_evidence': storage_evidence.hex(), 'workspace': None if workspace is None else str(workspace),
                 'child': None, 'watchdog': None, 'outcome': None, 'settled_at_ms': None}
        known = self.entry(bridge_run_id)
        if known is not None:
            if known != value:
                raise JournalError('launch entry already exists')
            return
        if self._path(bridge_run_id).exists():
            raise JournalError('launch entry already exists')
        self._write(bridge_run_id, value)

    def spawning(self, bridge_run_id):
        """Immediately before the child is created; a failure here means it is never created."""
        self._advance(bridge_run_id, 'claimed', state='spawning')

    def launched(self, bridge_run_id, pid, birth):
        self._advance(bridge_run_id, 'spawning', state='launched', child={'pid': pid, 'birth': birth})

    def watched(self, bridge_run_id, pid, birth):
        self._advance(bridge_run_id, 'launched', state='watched', watchdog={'pid': pid, 'birth': birth})

    def settled(self, bridge_run_id, outcome, *, now_ms=None):
        """The account no longer counts the run: keep a tombstone that never launches anything."""
        value = self.entry(bridge_run_id)
        if value is None:
            raise JournalError('launch entry is unavailable')
        if value['state'] == 'settled':
            return
        self._write(bridge_run_id, {**value, 'state': 'settled', 'outcome': outcome,
                                    'settled_at_ms': int(time.time() * 1000) if now_ms is None else now_ms})

    def remove(self, bridge_run_id):
        """A run whose capacity its own service released: nothing is left to settle."""
        try:
            self._path(bridge_run_id).unlink()
            self._sync_directory()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise JournalError('launch entry was not removed') from error

    def entries(self):
        """Every entry, at most MAX_ENTRIES; one that cannot be read is {'bridge_run_id', 'state': None}."""
        found = []
        try:
            names = sorted(name for name in os.listdir(self.directory)
                           if name.endswith('.json') and _matches(name[:-5], _BRIDGE))
        except OSError:
            return found
        for name in names[:MAX_ENTRIES]:
            value = self._read(self.directory / name)
            found.append(value if value is not None else {'bridge_run_id': name[:-5], 'state': None})
        return found

    def instance_stopped(self, instance):
        """Whether no thread of that service instance can still launch or own a child.

        True only when its lock file exists and can be locked now: its holder
        released it after its threads stopped, or the kernel released it when
        the process died. A missing lock file proves nothing.
        """
        if not _matches(instance, _INSTANCE):
            return False
        if instance == self.instance:
            return False
        try:
            descriptor = os.open(self._lock_path(instance), os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        finally:
            os.close(descriptor)
        return True

    def forget_instance(self, instance):
        """Remove a stopped instance's lock file once none of its runs is left unsettled."""
        if not self.instance_stopped(instance):
            return
        if any(entry['state'] not in {'settled'} and entry.get('instance') == instance for entry in self.entries()):
            return
        with contextlib.suppress(OSError):
            self._lock_path(instance).unlink()

    def instances(self):
        try:
            return [name[:-5] for name in os.listdir(self.directory)
                    if name.endswith('.lock') and _matches(name[:-5], _INSTANCE)]
        except OSError:
            return []

    def prune(self, *, now_ms=None):
        """Drop tombstones older than TOMBSTONE_MS."""
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        for entry in self.entries():
            if entry['state'] == 'settled' and now_ms - entry['settled_at_ms'] > TOMBSTONE_MS:
                with contextlib.suppress(JournalError):
                    self.remove(entry['bridge_run_id'])

    def release(self):
        """This instance's threads have stopped: later instances may settle its runs."""
        if self._lock_fd is None:
            return
        if not any(entry['state'] not in {'settled'} and entry.get('instance') == self.instance
                   for entry in self.entries()):
            with contextlib.suppress(OSError):
                self._lock_path(self.instance).unlink()
        os.close(self._lock_fd)
        self._lock_fd = None


def _identity_gone(identity):
    """Positive evidence that the process the journal saw is gone: reaped, or its pid reused."""
    record = process_record(identity['pid'])
    if record is not None:
        return record[2] != identity['birth']  # Another process was born with that pid since.
    return process_gone(identity['pid'])


def _group_empty(group):
    """No process (of any user) is in this process group: the kernel answers ESRCH."""
    if type(group) is not int or group <= 1:
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _session_members(sessions):
    """Whether any process is in one of these sessions; None when that cannot be read."""
    path = next((candidate for candidate in PS_PATHS if os.path.isfile(candidate)), None)
    if path is None:
        return None
    try:
        completed = subprocess.run([path, '-A', '-o', 'pid='], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={'PATH': os.defpath, 'LC_ALL': 'C'},
                                   timeout=PS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    pids = completed.stdout.decode('ascii', 'replace').split()
    if not pids or not all(pid.isdigit() for pid in pids):
        return None
    for pid in map(int, pids):
        try:
            if os.getsid(pid) in sessions:
                return True
        except ProcessLookupError:
            continue  # Exited since the listing.
        except OSError:
            return None
    return False


def tree_state(entry):
    """'gone', 'alive' or 'unknown' for a journal entry; only 'gone' is evidence.

    Gone means: the run never spawned (still ``claimed``), or it was watched
    and now the child and the watchdog are each reaped (or their pids belong
    to processes born later), no process is in either one's process group,
    and no process is in either one's session. The child and the watchdog each
    started a new session, so their group and session ids are their pids.
    Nothing is signalled.
    """
    value = checked_entry(entry) if entry.get('state') is not None else None
    if value is None:
        return 'unknown'
    if value['state'] == 'claimed':
        return 'gone'
    if value['state'] != 'watched':
        return 'unknown'
    try:
        identities = (value['child'], value['watchdog'])
        for identity in identities:
            if process_record(identity['pid']) is not None and not _identity_gone(identity):
                return 'alive'
            if not _identity_gone(identity):
                return 'unknown'
        for identity in identities:
            if not _group_empty(identity['pid']):
                return 'alive'
        members = _session_members({identity['pid'] for identity in identities})
        if members is None:
            return 'unknown'
        return 'alive' if members else 'gone'
    except Exception:
        return 'unknown'
