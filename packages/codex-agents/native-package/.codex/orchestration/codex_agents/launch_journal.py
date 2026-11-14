"""Durable launch evidence for the runs one project service claimed.

The project service writes one owner-only file per admitted claim, in the
project's own state folder, and moves it forward as the run goes:

- ``claimed``: the account admitted the claim; nothing was started. Written
  before the run can reach its launch, so a run still ``claimed`` never
  started a child.
- ``spawning``: written (and flushed to disk) immediately before the child is
  created. A launch whose ``spawning`` write fails never happens.
- ``launched``: the child's pid and kernel birth time, once it is identified,
  and the per-run marker in its environment (`process_tree.RUN_TAG_VARIABLE`).
- ``watched``: the detached watchdog's pid and birth time, once it has
  acknowledged that it tracks the child's tree.

Beside a watched entry the watchdog itself writes ``<bridge_run_id>.done``
before it exits, but only once the process tree it tracked is confirmed
empty (`process_tree.ProcessTree.cleanup_confirmed`, the confirmation the
normal release relies on). A watchdog that was killed or could not confirm
writes nothing, and the run is then left to the account's timed rule.

A normal release removes both files. Each service instance holds an exclusive
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

from .process_tree import (GONE, RUN_TAG, _born_before, process_gone, process_record, scan_until_stable,
                           session_reused_at, tagged_process_running)

STATES = frozenset({'claimed', 'spawning', 'launched', 'watched', 'settled'})
MAX_ENTRY_BYTES = 65_536
MAX_ENTRIES = 256
# A settled run's tombstone answers status and abandonment for this long.
TOMBSTONE_MS = 86_400_000
# The kernel checks must find nothing twice, this far apart: a process that joins a
# group or session between one look and the next is seen by the second.
RECHECK_SECONDS = 0.2
MAX_MARKER_BYTES = 4_096
PS_PATHS = ('/bin/ps', '/usr/bin/ps')
PS_TIMEOUT_SECONDS = 5
_BRIDGE = re.compile(r'brg_[0-9a-f]{32}')
_INSTANCE = re.compile(r'[0-9a-f]{32}')
_HANDLE = re.compile(r'hrn_[0-9a-f]{48}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'version', 'state', 'instance', 'bridge_run_id', 'run_handle', 'attempt', 'host_client', 'lease',
           'identity_digest', 'claim_request_id', 'storage_evidence', 'workspace', 'child', 'watchdog',
           'run_tag', 'outcome', 'settled_at_ms'}


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
    if state in {'launched', 'watched'} and (not _identity(value['child'])
                                             or not _matches(value['run_tag'], RUN_TAG)):
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
            # Locked under a name no other instance looks at, then linked into place:
            # `<instance>.lock` never exists unlocked, so nothing can take it for a stopped one.
            temporary = self.directory / ('.%s.lock.tmp' % self.instance)
            self._lock_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                                    0o600)
        except OSError as error:
            raise JournalError('launch journal is unavailable') from error
        self._released = False
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.link(temporary, self._lock_path(self.instance))
            os.unlink(temporary)
            self._sync_directory()
        except OSError as error:
            os.close(self._lock_fd)
            self._lock_fd = None
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise JournalError('launch journal is unavailable') from error

    def _lock_path(self, instance):
        return self.directory / (instance + '.lock')

    def _path(self, bridge_run_id):
        if not _matches(bridge_run_id, _BRIDGE):
            raise JournalError('bridge run id is invalid')
        return self.directory / (bridge_run_id + '.json')

    def marker_path(self, bridge_run_id):
        """Where the run's watchdog confirms that the tree it tracked is empty."""
        if not _matches(bridge_run_id, _BRIDGE):
            raise JournalError('bridge run id is invalid')
        return self.directory / (bridge_run_id + '.done')

    def _writable(self):
        if self._released:
            raise JournalError('launch journal is released')

    def _sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, bridge_run_id, value):
        """Replace the entry atomically and durably: the file, then the folder, reach the disk."""
        self._writable()
        value = {name: item for name, item in value.items() if name != 'marker_path'}
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
        # Not stored: where `tree_state` reads the watchdog's confirmation, at the time it checks.
        return {**value, 'marker_path': str(self.marker_path(value['bridge_run_id']))}

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
                 'child': None, 'watchdog': None, 'run_tag': None, 'outcome': None, 'settled_at_ms': None}
        self._writable()
        known = self.entry(bridge_run_id)
        if known is not None:
            known.pop('marker_path')
            if known != value:
                raise JournalError('launch entry already exists')
            return
        if self._path(bridge_run_id).exists():
            raise JournalError('launch entry already exists')
        self._write(bridge_run_id, value)

    def spawning(self, bridge_run_id):
        """Immediately before the child is created; a failure here means it is never created."""
        self._advance(bridge_run_id, 'claimed', state='spawning')

    def launched(self, bridge_run_id, pid, birth, run_tag):
        self._advance(bridge_run_id, 'spawning', state='launched', child={'pid': pid, 'birth': birth},
                      run_tag=run_tag)

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
        with contextlib.suppress(OSError):
            self.marker_path(bridge_run_id).unlink()

    def remove(self, bridge_run_id):
        """A run whose capacity its own service released: nothing is left to settle."""
        self._writable()
        with contextlib.suppress(OSError):
            self.marker_path(bridge_run_id).unlink()
        try:
            self._path(bridge_run_id).unlink()
            self._sync_directory()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise JournalError('launch entry was not removed') from error

    def entries(self):
        """Every entry, at most MAX_ENTRIES; one that cannot be read is {'bridge_run_id', 'state': None}."""
        return self._listing()[0]

    def _listing(self):
        """(entries, complete): complete only when every entry file was listed and could be read."""
        found = []
        try:
            names = sorted(name for name in os.listdir(self.directory)
                           if name.endswith('.json') and _matches(name[:-5], _BRIDGE))
        except OSError:
            return found, False
        for name in names[:MAX_ENTRIES]:
            value = self._read(self.directory / name)
            found.append(value if value is not None else {'bridge_run_id': name[:-5], 'state': None})
        complete = len(names) <= MAX_ENTRIES and all(entry['state'] is not None for entry in found)
        return found, complete

    def _unsettled(self, instance):
        """Whether that instance may still have an unsettled run here (an unreadable entry may be one)."""
        entries, complete = self._listing()
        return not complete or any(entry['state'] != 'settled' and entry.get('instance') == instance
                                   for entry in entries)

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
        if self._released or not self.instance_stopped(instance) or self._unsettled(instance):
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
        if self._released:
            return
        for entry in self.entries():
            if entry['state'] == 'settled' and now_ms - entry['settled_at_ms'] > TOMBSTONE_MS:
                with contextlib.suppress(JournalError):
                    self.remove(entry['bridge_run_id'])

    def release(self):
        """This instance's threads have stopped: later instances may settle its runs."""
        if self._lock_fd is None:
            return
        if not self._unsettled(self.instance):
            with contextlib.suppress(OSError):
                self._lock_path(self.instance).unlink()
        self._released = True  # Nothing is written here after the lock is gone.
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


# Every user's processes are listed, so the cap is wider than the marker scan's.
MAX_SESSION_SCAN_PROCESSES = 32768


def _listed_pids():
    """Every process's pid and listed parent from a fixed-path `ps`, without that `ps`; None when unavailable."""
    path = next((candidate for candidate in PS_PATHS if os.path.isfile(candidate)), None)
    if path is None:
        return None
    try:
        with subprocess.Popen([path, '-A', '-o', 'pid=,ppid='], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env={'PATH': os.defpath, 'LC_ALL': 'C'}) as lister:
            try:
                output, _ = lister.communicate(timeout=PS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                lister.kill()
                lister.communicate()
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    if lister.returncode != 0:
        return None
    listed = {}
    for line in output.decode('ascii', 'replace').splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            return None
        listed[int(fields[0])] = int(fields[1])
    # `ps` lists itself, a new pid every time; it is this process's own child, never a member.
    listed.pop(lister.pid, None)
    return listed or None


def _session_members(sessions, births=None):
    """Whether any process is in one of these sessions; None when that cannot be read.

    A member that forks and ends between a listing and its read leaves its
    descendant unlisted, so the listing is repeated while such a process may
    have been one (see `scan_until_stable`). With
    `births` (session id -> the birth of the process that started it): when that
    pid now belongs to a later process, which may lead a new session with the same
    id, only a member born before that process counts.
    """
    reused = {session: session_reused_at(session, (births or {}).get(session, '')) if births else None
              for session in sessions}

    def check(pid, listed):
        try:
            session = os.getsid(pid)
        except ProcessLookupError:
            return GONE  # Ended since the listing; anything it forked shows in the next one.
        except OSError:
            return None
        if session not in reused:
            return False
        if reused[session] is None:
            return True
        record = process_record(pid)
        if record is None:
            return GONE if process_gone(pid) else None
        return _born_before(record[2], reused[session])

    return scan_until_stable(_listed_pids, check, limit=MAX_SESSION_SCAN_PROCESSES)


def _confirmed(value, path):
    """Whether the run's own watchdog confirmed, for exactly this child, that its tracked tree is empty."""
    if type(path) is not str:
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > MAX_MARKER_BYTES):
            return False
        raw = os.read(descriptor, MAX_MARKER_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    try:
        marker = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return marker == {'version': 1, 'bridge_run_id': value['bridge_run_id'], 'pid': value['child']['pid'],
                      'birth': value['child']['birth'], 'confirmed': True}


def _kernel_empty(identities, run_tag, born):
    """True when no process is in either group or session, and none carries the run's marker.

    None when that cannot be read. The marker finds a descendant that detached
    after its parent exited between two of the watchdog's snapshots.
    """
    for identity in identities:
        if not _group_empty(identity['pid']):
            return False
    members = _session_members({identity['pid'] for identity in identities},
                               {identity['pid']: identity['birth'] for identity in identities})
    if members is None or members:
        return None if members is None else False
    tagged = tagged_process_running(run_tag, born)
    return None if tagged is None else not tagged


def tree_state(entry):
    """'gone', 'alive' or 'unknown' for a journal entry; only 'gone' is evidence.

    Gone means: the run never spawned (still ``claimed``), or it was watched
    and now the child and the watchdog are each reaped (or their pids belong
    to processes born later), the watchdog confirmed before it exited that
    the whole tree it tracked is empty, and, twice, a short interval apart,
    no process is in either one's process group or session and no process of
    this user still carries the run's environment marker. The child and
    the watchdog each started a new session, so their group and session ids
    are their pids. Nothing is signalled.
    """
    if entry.get('state') is None:
        return 'unknown'
    value = checked_entry({name: item for name, item in entry.items() if name != 'marker_path'})
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
        evidence = (identities, value['run_tag'], value['child']['birth'])
        first = _kernel_empty(*evidence)
        if first is not True:
            return 'unknown' if first is None else 'alive'
        if not _confirmed(value, entry.get('marker_path')):
            # The watchdog was killed, or could not confirm its tree: a descendant may
            # have escaped into a session of its own. Not evidence.
            return 'unknown'
        time.sleep(RECHECK_SECONDS)
        second = _kernel_empty(*evidence)
        if second is not True:
            return 'unknown' if second is None else 'alive'
        return 'gone'
    except Exception:
        return 'unknown'
