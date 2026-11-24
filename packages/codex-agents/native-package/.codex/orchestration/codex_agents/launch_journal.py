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

Container entries (version 2, C2 of PLAN-0.4.0-supervisor-slices.md) are written
only when the run's child is launched in a container (`container_launch`, behind
its switch). They are keyed by a launch token made before the claim, since the
bridge run id comes with the account's reply, and their states are:

- ``intent``: written (and flushed) before the claim is handed to the account:
  the container's name and labels (``cocolo.project``, ``cocolo.run`` = the
  token, ``cocolo.kind=run``) and everything a later service needs to fence the
  claim. No intent, no claim. The account's admission is recorded on it (the
  bridge run id and the deadline) without changing the state, so an admitted
  claim always has an entry.
- ``started``: the container id and cgroup once podman returned from the launch.
  The container is created only after the admitted intent was read back
  (`check_intent`, the container primitive's ``record``).
- ``stopping``, then ``gone`` (the OS says so) or ``stop_failed`` (not proven
  gone: the slot stays held; a stop may be tried again). C3: each stop records
  the reason the container was first stopped for (`CONTAINER_STOP_REASONS`,
  kept across retries) and counts its attempts.
- ``settled``: the account no longer counts the run (outcome ``settled`` or
  ``released``), or, for an intent whose claim was never admitted, the account
  closed its claim request for good (``fenced``).

The process rule (`tree_state`) never reads a container entry as gone: only the
container's own recovery may settle it. C4: a later service takes custody of a
stopped instance's container entry (`adopt`, only once that instance's lock is
free) before it stops the container and settles the run (`container_recovery`);
its last answer for status is kept beside the entries (`RECOVERY_REPORT`).

Process intents (version 3, B4s r1 HIGH 3, decision 146) are written only in process mode (containers
switched off). Keyed by a process token made before the claim (``pit_<32 hex>``), one is written (and
flushed with its folder) before the run's claim is handed to the account, carrying everything a later
service needs to fence the claim and settle the run (``identity_digest``, the claim request, the lease
and its storage evidence): no intent, no claim. Once the account's admission is recorded in the run's
``claimed`` entry the intent is removed, as it is when the claim was closed unadmitted. A stopped
instance's intent with no ``claimed`` entry is fenced by the next service with the intent's own
identity (never the launch observation's): a claim the account admitted is settled as lost with that
receipt's bridge run id (nothing of it ever spawned: a child is created only from a ``claimed`` entry),
one it never admitted is closed for good (outcome ``fenced``). Its states are ``intent`` and ``settled``.

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
CONTAINER_STATES = frozenset({'intent', 'started', 'stopping', 'gone', 'stop_failed', 'settled'})
CONTAINER_KIND_LABELS = ('cocolo.project', 'cocolo.run', 'cocolo.kind')
# C3: why a run's container was stopped: the liveness clock's reasons (`liveness_clock.LIVENESS_STOP_REASONS`),
# the run's own end, the service closing, a refused launch, and a start that could not be recorded.
CONTAINER_STOP_REASONS = frozenset({
    'cancel_requested', 'startup_silence', 'work_silence', 'absolute_deadline', 'monitor_unavailable',
    'service_close', 'run_ended', 'launch_refused', 'record_failed', 'service_lost',
})
# Plain words for status, one per reason.
STOP_REASON_WORDS = {
    'cancel_requested': 'it was cancelled', 'startup_silence': 'it stayed silent at startup',
    'work_silence': 'it went silent while working', 'absolute_deadline': 'it reached its deadline',
    'monitor_unavailable': 'its monitor stopped', 'service_close': 'its service was closing',
    'run_ended': 'its run had ended', 'launch_refused': 'its launch was refused',
    'record_failed': 'its start could not be recorded', 'service_lost': 'the service that started it had stopped',
}
# The container states status counts (settled entries are no longer held).
CONTAINER_HELD_STATES = ('intent', 'started', 'stopping', 'gone', 'stop_failed')
MAX_STOP_DETAIL = 600
# C4: the container recovery's last answer, for status (beside the entries; never an entry itself).
RECOVERY_REPORT = 'container-recovery.json'
MAX_RECOVERY_REPORT_BYTES = 65_536
# The settled outcomes of a container entry: C4 adds `fenced` (an intent whose claim was closed, never admitted).
CONTAINER_OUTCOMES = frozenset({'settled', 'released', 'fenced'})
MAX_ENTRY_BYTES = 65_536
# Entries read per listing; successive listings go round every file (O10 D6).
MAX_ENTRIES = 256
# Unresolved entries named in status; all are counted.
MAX_UNRESOLVED_LISTED = 16
# Attempts at taking a fresh instance lock: a later instance may remove a temporary
# before its creator holds it (`remove_lock_temporaries`).
LOCK_ATTEMPTS = 3
# A settled run's tombstone answers status and abandonment for this long.
TOMBSTONE_MS = 86_400_000
# The kernel checks must find nothing twice, this far apart: a process that joins a
# group or session between one look and the next is seen by the second.
RECHECK_SECONDS = 0.2
MAX_MARKER_BYTES = 4_096
PS_PATHS = ('/bin/ps', '/usr/bin/ps')
PS_TIMEOUT_SECONDS = 5
_BRIDGE = re.compile(r'brg_[0-9a-f]{32}')
_TOKEN = re.compile(r'lnc_[0-9a-f]{32}')
_PROCESS_TOKEN = re.compile(r'pit_[0-9a-f]{32}')  # A process intent's file name (version 3).
_CONTAINER_NAME = re.compile(r'[a-z][a-z0-9-]{0,30}-[0-9a-f]{32}')
_CONTAINER_PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}')
_CONTAINER_ID = re.compile(r'[0-9a-f]{12,64}')
_CGROUP = re.compile(r'/[A-Za-z0-9@._:/-]{1,1023}')
_INSTANCE = re.compile(r'[0-9a-f]{32}')
_LOCK_TEMPORARY = re.compile(r'\.[0-9a-f]{32}\.lock\.tmp')
_HANDLE = re.compile(r'hrn_[0-9a-f]{48}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'version', 'state', 'instance', 'bridge_run_id', 'run_handle', 'attempt', 'host_client', 'lease',
           'identity_digest', 'claim_request_id', 'storage_evidence', 'workspace', 'child', 'watchdog',
           'run_tag', 'outcome', 'settled_at_ms'}
_CONTAINER_FIELDS = {'version', 'kind', 'state', 'instance', 'token', 'project_id', 'container_name', 'labels',
                     'run_handle', 'attempt', 'host_client', 'lease', 'identity_digest', 'claim_request_id',
                     'storage_evidence', 'workspace', 'bridge_run_id', 'deadline_at_ms', 'container_id',
                     'cgroup_path', 'stop_detail', 'stop_reason', 'stop_attempts', 'outcome', 'settled_at_ms'}
_PROCESS_INTENT_FIELDS = {'version', 'kind', 'state', 'instance', 'token', 'run_handle', 'attempt', 'host_client',
                          'lease', 'identity_digest', 'claim_request_id', 'storage_evidence', 'workspace', 'outcome',
                          'settled_at_ms'}
PROCESS_INTENT_STATES = frozenset({'intent', 'settled'})


def new_token():
    """A fresh launch token: the container's run label, and so its name (RS-12)."""
    return 'lnc_' + secrets.token_hex(16)


def new_process_token():
    """A fresh process intent token (B4s r1 HIGH 3): the name its intent is kept under."""
    return 'pit_' + secrets.token_hex(16)


def entry_key(value):
    """The name an entry's file is kept under: its token (container, process intent) or bridge run id (process)."""
    return value.get('token') if value.get('version') in (2, 3) else value.get('bridge_run_id')


class JournalError(RuntimeError):
    pass


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _identity(value):
    return (type(value) is dict and value.keys() == {'pid', 'birth'} and type(value['pid']) is int
            and value['pid'] > 1 and type(value['birth']) is str and 0 < len(value['birth']) <= 64)


def _checked_container_entry(value):
    if value.keys() != _CONTAINER_FIELDS or value['kind'] != 'container' or value['state'] not in CONTAINER_STATES:
        return None
    if (not _matches(value['instance'], _INSTANCE) or not _matches(value['token'], _TOKEN)
            or not _matches(value['project_id'], _CONTAINER_PROJECT)
            or not _matches(value['container_name'], _CONTAINER_NAME)
            or value['labels'] != {'cocolo.project': value['project_id'], 'cocolo.run': value['token'],
                                   'cocolo.kind': 'run'}
            or not _matches(value['run_handle'], _HANDLE) or type(value['attempt']) is not int
            or type(value['host_client']) is not str or type(value['lease']) is not dict
            or not _matches(value['identity_digest'], _DIGEST) or type(value['claim_request_id']) is not str
            or type(value['storage_evidence']) is not str
            or (value['workspace'] is not None and type(value['workspace']) is not str)):
        return None
    admitted = value['bridge_run_id'] is not None
    if admitted != (value['deadline_at_ms'] is not None):
        return None
    if admitted and (not _matches(value['bridge_run_id'], _BRIDGE) or type(value['deadline_at_ms']) is not int):
        return None
    if value['container_id'] is not None and (not _matches(value['container_id'], _CONTAINER_ID)
                                              or not admitted):
        return None
    if value['cgroup_path'] is not None and not _matches(value['cgroup_path'], _CGROUP):
        return None
    if value['state'] == 'started' and value['container_id'] is None:
        return None
    if value['stop_detail'] is not None and (type(value['stop_detail']) is not str
                                             or len(value['stop_detail']) > MAX_STOP_DETAIL):
        return None
    if (value['stop_reason'] is not None and value['stop_reason'] not in CONTAINER_STOP_REASONS) or (
            type(value['stop_attempts']) is not int or not 0 <= value['stop_attempts'] <= 1_000_000):
        return None
    if value['state'] == 'settled':
        if value['outcome'] not in CONTAINER_OUTCOMES or type(value['settled_at_ms']) is not int:
            return None
    elif value['outcome'] is not None or value['settled_at_ms'] is not None:
        return None
    return value


def _checked_process_intent(value):
    if (value.keys() != _PROCESS_INTENT_FIELDS or value['kind'] != 'process'
            or value['state'] not in PROCESS_INTENT_STATES):
        return None
    if (not _matches(value['instance'], _INSTANCE) or not _matches(value['token'], _PROCESS_TOKEN)
            or not _matches(value['run_handle'], _HANDLE) or type(value['attempt']) is not int
            or type(value['host_client']) is not str or type(value['lease']) is not dict
            or not _matches(value['identity_digest'], _DIGEST) or type(value['claim_request_id']) is not str
            or type(value['storage_evidence']) is not str
            or (value['workspace'] is not None and type(value['workspace']) is not str)):
        return None
    if value['state'] == 'settled':
        if value['outcome'] not in CONTAINER_OUTCOMES or type(value['settled_at_ms']) is not int:
            return None
    elif value['outcome'] is not None or value['settled_at_ms'] is not None:
        return None
    return value


def checked_entry(value):
    """The entry if every field has its fixed shape, else None."""
    if type(value) is dict and value.get('version') == 2:
        return _checked_container_entry(value)
    if type(value) is dict and value.get('version') == 3:
        return _checked_process_intent(value)
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
        except OSError as error:
            raise JournalError('launch journal is unavailable') from error
        self._released = False
        # Where the next bounded listing starts, per purpose: successive passes reach every file.
        self._cursors = {}
        for attempt in range(LOCK_ATTEMPTS):
            try:
                self._take_lock()
                return
            except OSError as error:
                if attempt == LOCK_ATTEMPTS - 1:
                    raise JournalError('launch journal is unavailable') from error

    def _take_lock(self):
        """A fresh instance id whose `<instance>.lock` this process holds.

        Locked under a name no other instance takes for a lock, then linked into
        place: `<instance>.lock` never exists unlocked, so nothing can take it for
        a stopped one. A later instance may remove the temporary between its
        creation and its lock (`remove_lock_temporaries`); the link then fails and
        the caller tries again under a new id.
        """
        self.instance = secrets.token_hex(16)
        temporary = self.directory / ('.%s.lock.tmp' % self.instance)
        self._lock_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.link(temporary, self._lock_path(self.instance))
            os.unlink(temporary)
            self._sync_directory()
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    def _lock_path(self, instance):
        return self.directory / (instance + '.lock')

    def _path(self, key):
        """An entry's file: a process entry's bridge run id, or a container entry's launch token."""
        if not _matches(key, _BRIDGE) and not _matches(key, _TOKEN) and not _matches(key, _PROCESS_TOKEN):
            raise JournalError('bridge run id is invalid')
        return self.directory / (key + '.json')

    def marker_path(self, bridge_run_id):
        """Where the run's watchdog confirms that the tree it tracked is empty."""
        if not _matches(bridge_run_id, _BRIDGE):
            raise JournalError('bridge run id is invalid')
        return self.directory / (bridge_run_id + '.done')

    def watchdog_result_path(self, token):
        """Where a container run's watchdog writes how its stop ended (decision 49), when its service died: one
        owner-only JSON file, written atomically and flushed by the watchdog, the only writer. It is not an
        entry (never listed as one) and goes with the entry (`settled`, `remove`)."""
        if not _matches(token, _TOKEN):
            raise JournalError('launch token is invalid')
        return self.directory / (token + '.watchdog')

    def _forget_watchdog_result(self, key):
        if _matches(key, _TOKEN):
            with contextlib.suppress(OSError):
                self.watchdog_result_path(key).unlink()

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
        if checked_entry(value) is None or entry_key(value) != bridge_run_id:
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
        value = _read_entry(path)
        if value is None:
            return None
        if value['version'] != 1:
            return dict(value)
        # Not stored: where `tree_state` reads the watchdog's confirmation, at the time it checks.
        return {**value, 'marker_path': str(self.marker_path(value['bridge_run_id']))}

    def entry(self, bridge_run_id):
        return self._read(self._path(bridge_run_id))

    def _advance(self, bridge_run_id, before, **changes):
        value = self.entry(bridge_run_id)
        if value is None or value['state'] != before or value['instance'] != self.instance or value['version'] != 1:
            raise JournalError('launch entry is not ' + before)
        self._write(bridge_run_id, {**value, **changes})

    # -- container entries (version 2, C2)
    def intent(self, token, *, project_id, container_name, labels, run_handle, attempt, host_client, lease,
               identity_digest, claim_request_id, storage_evidence, workspace):
        """Before the claim is handed to the account: the run's container, and what a fence needs.

        Written once; a failure here means the claim is never sent.
        """
        self._writable()
        if type(storage_evidence) is not bytes:
            raise JournalError('storage evidence is invalid')
        value = {'version': 2, 'kind': 'container', 'state': 'intent', 'instance': self.instance, 'token': token,
                 'project_id': project_id, 'container_name': container_name, 'labels': labels,
                 'run_handle': run_handle, 'attempt': attempt, 'host_client': host_client, 'lease': lease,
                 'identity_digest': identity_digest, 'claim_request_id': claim_request_id,
                 'storage_evidence': storage_evidence.hex(),
                 'workspace': None if workspace is None else str(workspace), 'bridge_run_id': None,
                 'deadline_at_ms': None, 'container_id': None, 'cgroup_path': None, 'stop_detail': None,
                 'stop_reason': None, 'stop_attempts': 0, 'outcome': None, 'settled_at_ms': None}
        if checked_entry(value) is None:
            raise JournalError('launch entry is invalid')
        if self._path(token).exists():
            raise JournalError('launch entry already exists')
        self._write(token, value)

    # -- process intents (version 3, B4s r1 HIGH 3)
    def process_intent(self, token, *, run_handle, attempt, host_client, lease, identity_digest, claim_request_id,
                       storage_evidence, workspace):
        """Process mode, before the claim is handed to the account: what a later service needs to fence the
        claim and settle the run. Written once (file and folder flushed); a failure here means the claim is
        never sent."""
        self._writable()
        if type(storage_evidence) is not bytes:
            raise JournalError('storage evidence is invalid')
        value = {'version': 3, 'kind': 'process', 'state': 'intent', 'instance': self.instance, 'token': token,
                 'run_handle': run_handle, 'attempt': attempt, 'host_client': host_client, 'lease': lease,
                 'identity_digest': identity_digest, 'claim_request_id': claim_request_id,
                 'storage_evidence': storage_evidence.hex(),
                 'workspace': None if workspace is None else str(workspace), 'outcome': None, 'settled_at_ms': None}
        if checked_entry(value) is None:
            raise JournalError('launch entry is invalid')
        if self._path(token).exists():
            raise JournalError('launch entry already exists')
        self._write(token, value)

    def _container(self, token, states):
        value = self.entry(token)
        if (value is None or value['version'] != 2 or value['state'] not in states
                or value['instance'] != self.instance):
            raise JournalError('launch entry is not ' + ' or '.join(sorted(states)))
        return value

    def admitted(self, token, bridge_run_id, deadline_at_ms):
        """The account admitted the claim: its bridge run id and deadline, on the intent."""
        value = self._container(token, {'intent'})
        if value['bridge_run_id'] is not None:
            if (value['bridge_run_id'], value['deadline_at_ms']) != (bridge_run_id, deadline_at_ms):
                raise JournalError('launch entry was admitted already')
            return
        self._write(token, {**value, 'bridge_run_id': bridge_run_id, 'deadline_at_ms': deadline_at_ms})

    def container_entry(self, bridge_run_id):
        """This instance's admitted container entry for that bridge run id, or None."""
        if not _matches(bridge_run_id, _BRIDGE):
            return None
        for name in self._names() or []:
            if not _matches(name[:-5], _TOKEN):
                continue
            value = self._read(self.directory / name)
            if (value is not None and value['bridge_run_id'] == bridge_run_id
                    and value['instance'] == self.instance):
                return value
        return None

    def check_intent(self, token, container_name):
        """The container primitive's `record`: read back before the container is created.

        Only an admitted intent of this instance, for exactly this container, lets it be created.
        """
        value = self._container(token, {'intent'})
        if value['bridge_run_id'] is None or value['container_name'] != container_name:
            raise JournalError('launch entry is not the admitted intent of this container')

    def started(self, token, container_id, cgroup_path):
        """Podman returned the started (and checked) container."""
        value = self._container(token, {'intent'})
        if value['bridge_run_id'] is None:
            raise JournalError('launch entry is not admitted')
        self._write(token, {**value, 'state': 'started', 'container_id': container_id,
                            'cgroup_path': cgroup_path or None})

    def stopping(self, token, reason='run_ended'):
        """Written before each stop attempt: from a started container, a refused launch's, a failed stop,
        or one interrupted part way. The first reason is kept; the attempts are counted (C3)."""
        if reason not in CONTAINER_STOP_REASONS:
            raise JournalError('stop reason is invalid')
        value = self._container(token, {'intent', 'started', 'stop_failed', 'stopping'})
        self._write(token, {**value, 'state': 'stopping', 'stop_reason': value['stop_reason'] or reason,
                            'stop_attempts': min(value['stop_attempts'] + 1, 1_000_000)})

    def stopped(self, token, gone, detail):
        """The stop's answer: `gone` only when the OS says so, else `stop_failed` (the slot stays held)."""
        value = self._container(token, {'stopping'})
        self._write(token, {**value, 'state': 'gone' if gone is True else 'stop_failed',
                            'stop_detail': str(detail)[:MAX_STOP_DETAIL]})

    def adopt(self, token, instance):
        """C4: take custody of a stopped instance's unsettled container entry; the entry as it now stands.

        Only when that instance's lock is free (none of its threads can launch or own anything any
        more); the entry is then this instance's, so its stops and settlement are written here, and a
        later instance takes it over the same way if this one dies too.
        """
        self._writable()
        value = self.entry(token)
        if (value is None or value['version'] != 2 or value['state'] == 'settled' or value['instance'] != instance
                or not self.instance_stopped(instance)):
            raise JournalError('launch entry is not a stopped instance\'s')
        value = {**value, 'instance': self.instance}
        self._write(token, value)
        return value

    def tokens(self):
        """Every container entry's launch token (from the file names only); None when the folder cannot be listed."""
        names = self._names()
        return None if names is None else {name[:-5] for name in names if _matches(name[:-5], _TOKEN)}

    def write_recovery(self, report):
        """C4: the container recovery's last answer, for status; replaced whole (owner-only, atomic)."""
        self._writable()
        body = json.dumps(report, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(body) > MAX_RECOVERY_REPORT_BYTES:
            raise JournalError('recovery report is too large')
        temporary = self.directory / ('.recovery.%s.tmp' % secrets.token_hex(4))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.directory / RECOVERY_REPORT)
        except OSError as error:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise JournalError('recovery report was not written') from error

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
        if value['version'] == 1:
            with contextlib.suppress(OSError):
                self.marker_path(bridge_run_id).unlink()
        self._forget_watchdog_result(bridge_run_id)

    def remove(self, bridge_run_id):
        """A run whose capacity its own service released: nothing is left to settle."""
        self._writable()
        path = self._path(bridge_run_id)
        if _matches(bridge_run_id, _BRIDGE):
            with contextlib.suppress(OSError):
                self.marker_path(bridge_run_id).unlink()
        self._forget_watchdog_result(bridge_run_id)
        try:
            path.unlink()
            self._sync_directory()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise JournalError('launch entry was not removed') from error

    def entries(self):
        """At most MAX_ENTRIES entries; one that cannot be read is {'bridge_run_id', 'state': None}.

        Each call goes on where the last one stopped, so successive calls go round
        every entry file however many there are (O10 D6).
        """
        return self._window('entries')

    def _names(self):
        """Every entry file's name, sorted; None when the folder cannot be listed."""
        try:
            return sorted(name for name in os.listdir(self.directory) if _entry_name(name))
        except OSError:
            return None

    def _entry_or_unknown(self, name):
        try:
            value = self._read(self.directory / name)
        except JournalError:
            value = None  # An entry that kept changing is unknown here, as an unreadable one always was.
        return value if value is not None else {'bridge_run_id': name[:-5], 'state': None}

    def _window(self, purpose):
        """The next MAX_ENTRIES entries after this purpose's cursor, wrapping round to the first."""
        names = self._names() or []
        if len(names) > MAX_ENTRIES:
            cursor = self._cursors.get(purpose)
            start = 0 if cursor is None else next((index for index, name in enumerate(names) if name > cursor), 0)
            names = (names[start:] + names[:start])[:MAX_ENTRIES]
            self._cursors[purpose] = names[-1] if names else None
        else:
            self._cursors.pop(purpose, None)
        return [self._entry_or_unknown(name) for name in names]

    def _unsettled(self, instance):
        """Whether that instance may still have an unsettled run here (an unreadable entry may be one).

        Every entry file is read, until one answers it.
        """
        names = self._names()
        if names is None:
            return True
        for name in names:
            entry = self._entry_or_unknown(name)
            if entry['state'] is None or (entry['state'] != 'settled' and entry['instance'] == instance):
                return True
        return False

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

    def prune(self, *, now_ms=None, keep=frozenset(), only=None):
        """Drop tombstones older than TOMBSTONE_MS, and lock temporaries whose creator is gone; return the keys
        of the tombstones removed.

        Tombstones are looked at MAX_ENTRIES at a time, going round every file over
        successive calls. B4s r1: the entries named in `keep` (their project settlement is not
        recovered yet) stay. B4s confirming check: with `only` (the keys the caller inspected in
        its own pass, at most one listing's), just those are looked at, each read again here, so an
        entry the caller never saw is never removed.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if self._released:
            return frozenset()
        if only is None:
            entries = self._window('prune')
        else:
            entries = []
            for key in sorted(only)[:MAX_ENTRIES]:
                if _entry_name(str(key) + '.json'):
                    entries.append(self._entry_or_unknown(key + '.json'))
        removed = set()
        for entry in entries:
            if (entry['state'] == 'settled' and now_ms - entry['settled_at_ms'] > TOMBSTONE_MS
                    and entry_key(entry) not in keep):
                with contextlib.suppress(JournalError):
                    self.remove(entry_key(entry))
                    removed.add(entry_key(entry))
        self.remove_lock_temporaries()
        return frozenset(removed)

    def remove_lock_temporaries(self):
        """Remove each `.<instance>.lock.tmp` whose creator is gone (O10 D6).

        Only an owner-only regular file with one link (never linked into place as
        `<instance>.lock`, or that lock is gone already) that no process holds
        locked now; opened without following a link. A creator that is still
        starting and has not locked it yet loses it, and takes a new id
        (`_take_lock`).
        """
        if self._released:
            return
        try:
            names = [name for name in os.listdir(self.directory) if _LOCK_TEMPORARY.fullmatch(name)]
        except OSError:
            return
        for name in names:
            path = self.directory / name
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            except OSError:
                continue
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                        or info.st_nlink != 1):
                    continue
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                now = os.lstat(path)
                if (now.st_dev, now.st_ino) == (info.st_dev, info.st_ino):
                    os.unlink(path)
            except OSError:
                continue
            finally:
                os.close(descriptor)

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


def unresolved(directory, *, now_ms=None):
    """The project's journal entries that no running service can settle, for status (O10 D6).

    An entry is unresolved when it is not settled and its service instance is
    not running (its lock is free or missing), or when it cannot be read. Its
    age is the time since its last state was written. Read only: nothing is
    settled, written or removed, and no process is examined. Every entry is
    counted; at most MAX_UNRESOLVED_LISTED, the oldest first, are named.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    report = {'count': 0, 'oldest_age_ms': None, 'entries': []}
    directory = Path(directory)
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return report
        names = sorted(name for name in os.listdir(directory) if _entry_name(name))
    except OSError:
        return report
    live, found = {}, []
    for name in names:
        path = directory / name
        value = _read_entry_or_none(path)
        if value is not None:
            if value['state'] == 'settled':
                continue
            instance = value['instance']
            if instance not in live:
                live[instance] = _lock_held(directory / (instance + '.lock'))
            if live[instance]:
                continue
        try:
            changed_ms = os.lstat(path).st_mtime_ns // 1_000_000
        except OSError:
            continue  # Removed since it was listed.
        # C4: a container entry is named by its launch token in its own field; its bridge run id is the
        # account's, once admitted.
        container = _matches(name[:-5], _TOKEN) or _matches(name[:-5], _PROCESS_TOKEN)  # or a process intent
        found.append({'bridge_run_id': (value.get('bridge_run_id') if value is not None else None) if container
                      else name[:-5], 'token': name[:-5] if container else None,
                      'state': None if value is None else value['state'], 'age_ms': max(0, now_ms - changed_ms)})
    found.sort(key=lambda item: (-item['age_ms'], item['token'] or item['bridge_run_id']))
    report.update(count=len(found), oldest_age_ms=found[0]['age_ms'] if found else None,
                  entries=found[:MAX_UNRESOLVED_LISTED])
    return report


def container_tokens(directory):
    """HM-1: the launch token of every container entry in that journal folder, read only; None when the
    folder is missing, is not an owner-only folder of this user, or cannot be listed.

    From the file names alone, as `LaunchJournal.tokens` (C4's live set): every entry counts, whatever
    its state and whichever instance wrote it, and one whose file cannot be read counts too. No lock is
    taken and nothing is written. Every intent is written (and its folder synced) before its container
    is created, so a container listed before this is read is never missing from it while its run lives.
    """
    directory = Path(directory)
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None
        names = os.listdir(directory)
    except OSError:
        return None
    return {name[:-5] for name in names if name.endswith('.json') and _matches(name[:-5], _TOKEN)}


def container_holds(directory, *, now_ms=None):
    """C3 (RS-C-12): the project's run containers by state, for status, and an owner alert for each
    one that could not be stopped.

    Every container entry not yet settled is counted, whichever service instance wrote it. Each
    `stop_failed` one is named (at most MAX_UNRESOLVED_LISTED, the oldest first) with the reason it
    was stopped for, the last stop's answer and the attempts, and gets one plain line for the
    owner: its slot stays held, and how to end it by hand. Read only: nothing is written, removed
    or asked of podman.

    C4: `recovery` is the container recovery's last answer (`RECOVERY_REPORT`): whether podman
    answered, how many entries an earlier service left still wait, and each orphan container it
    stopped or could not stop. Entries left waiting because podman does not answer, and each
    orphan that could not be stopped, get one owner line each.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    counts = {state: 0 for state in CONTAINER_HELD_STATES}
    report = {'counts': counts, 'stop_failed': [], 'summary': 'Run containers: none held.', 'attention': [],
              'recovery': None}
    directory = Path(directory)
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return report
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith('.json') and _matches(name[:-5], _TOKEN))
    except OSError:
        return report
    failed = []
    for name in names:
        path = directory / name
        value = _read_entry_or_none(path)
        if value is None or value['version'] != 2 or value['state'] not in counts:
            continue
        counts[value['state']] += 1
        if value['state'] != 'stop_failed':
            continue
        try:
            changed_ms = os.lstat(path).st_mtime_ns // 1_000_000
        except OSError:
            changed_ms = now_ms
        failed.append({'container': value['container_name'], 'reason': value['stop_reason'],
                       'detail': value['stop_detail'], 'attempts': value['stop_attempts'],
                       'age_ms': max(0, now_ms - changed_ms)})
    failed.sort(key=lambda item: (-item['age_ms'], item['container']))
    report['stop_failed'] = failed[:MAX_UNRESOLVED_LISTED]
    words = (('started', 'running'), ('intent', 'being started'), ('stopping', 'stopping'),
             ('gone', 'stopped, their slot not yet settled'), ('stop_failed', 'could not be stopped (slot held)'))
    parts = [f'{counts[state]} {text}' for state, text in words if counts[state]]
    if parts:
        report['summary'] = 'Run containers: ' + ', '.join(parts) + '.'
    for item in report['stop_failed']:
        why = STOP_REASON_WORDS.get(item['reason'], 'it was stopped')
        attempts = item['attempts']
        report['attention'].append(
            f"Run container {item['container']} could not be stopped after {attempts} attempt"
            f"{'s' if attempts != 1 else ''} (stopped because {why}): {item['detail'] or 'no answer'}. "
            f"Its run slot stays held while the project's service keeps trying; to end it yourself: "
            f"podman rm --force {item['container']}")
    if len(failed) > len(report['stop_failed']):
        report['attention'].append(f'{len(failed) - len(report["stop_failed"])} more run container(s) could not '
                                   'be stopped; their slots stay held.')
    recovery = _read_recovery(directory / RECOVERY_REPORT)
    report['recovery'] = recovery
    if recovery is not None:
        if recovery['podman'] == 'unavailable' and recovery['waiting']:
            report['attention'].append(
                f"{recovery['waiting']} run container(s) an earlier service of this project left cannot be checked: "
                f"{recovery['detail'] or 'podman did not answer'}. Their run slots stay held until podman answers; "
                "the project's service keeps asking.")
        for item in recovery['orphans']:
            if item['outcome'] in ('stop_failed', 'refused'):
                report['attention'].append(
                    f"Run container {item['container']} carries this project's label but no run of it is known, "
                    f"and it could not be stopped: {item['detail']}. To end it yourself: podman rm --force "
                    f"{item['container']}")
    return report


_RECOVERY_FIELDS = {'checked_at_ms', 'podman', 'detail', 'waiting', 'orphans'}
_ORPHAN_FIELDS = {'container', 'run', 'outcome', 'detail', 'at_ms'}


def _read_recovery(path):
    """The recovery report in that file if it has its fixed shape, else None (read only)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_size > MAX_RECOVERY_REPORT_BYTES):
            return None
        raw = os.read(descriptor, MAX_RECOVERY_REPORT_BYTES + 1)
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)
    if (type(value) is not dict or value.keys() != _RECOVERY_FIELDS or value['podman'] not in ('ok', 'unavailable')
            or type(value['checked_at_ms']) is not int or type(value['waiting']) is not int
            or (value['detail'] is not None and type(value['detail']) is not str)
            or type(value['orphans']) is not list):
        return None
    for item in value['orphans']:
        if (type(item) is not dict or item.keys() != _ORPHAN_FIELDS
                or any(type(item[name]) is not str for name in ('container', 'run', 'outcome', 'detail'))
                or type(item['at_ms']) is not int):
            return None
    return value


_REREADS = 3  # Times an entry replaced while it was open is opened again by its path.


def _read_entry(path):
    """The checked entry in that file, or None (the same checks as `LaunchJournal._read`).

    A write replaces the entry file, so a reader that opened the old file sees it unlinked
    (st_nlink 0): the entry still exists, so the path is opened again (_REREADS opens in all,
    so two reopens), and a file that is replaced every time raises JournalError. A file with two or more links
    stays refused.
    """
    for _ in range(_REREADS):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except OSError:
            return None
        try:
            info = os.fstat(descriptor)
            if info.st_nlink == 0 and stat.S_ISREG(info.st_mode):
                continue  # Replaced while open: read the new entry.
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > MAX_ENTRY_BYTES):
                return None
            raw = os.read(descriptor, MAX_ENTRY_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        try:
            value = checked_entry(json.loads(raw))
        except (ValueError, RecursionError):
            return None
        if value is None or path.name != entry_key(value) + '.json':
            return None
        return value
    raise JournalError('launch entry kept changing while it was read')


def _read_entry_or_none(path):
    """`_read_entry` for the status readers, which count an entry that cannot be read as unreadable.

    An entry that kept changing on every read is unreadable here too: `unresolved` already lists it
    with state None (unknown, distinct from a missing file, which is not listed), and `container_holds`
    leaves it out of its counts. The status shape has no state for it, and status is best-effort: its
    only caller (client_runtime's status report) displays it and never drives a stop decision.
    """
    try:
        return _read_entry(path)
    except JournalError:
        return None


def _entry_name(name):
    """An entry file's name: a process entry's bridge run id or a container entry's token, then .json."""
    return name.endswith('.json') and (_matches(name[:-5], _BRIDGE) or _matches(name[:-5], _TOKEN)
                                       or _matches(name[:-5], _PROCESS_TOKEN))


def _lock_held(path):
    """Whether a process holds that instance lock now. A lock that cannot be opened is not held."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return False


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
    if entry.get('state') is None or entry.get('version') in (2, 3):
        return 'unknown'  # A container entry is settled only by the container's own recovery; an intent by its fence.
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
