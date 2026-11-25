"""HM-2a: leftover CoCoLo preparation folders (docs/requirements/s11-hm2a.md; plan step S11, SW-8, decision 309).

A health check, `workspaces`, over CoCoLo's scratch area (the real scratch container, `bridge.scratch_base()` plus
`bridge.SCRATCH_CONTAINER`). It removes only a `prepare-*` folder whose owning run and driver no longer exist, and
reports everything else it finds odd. It reads what `codex_preparation.py` and the driver keep; it never edits it.

What counts as owned (a folder is kept if any of these holds):

- a launch-journal entry of any project of this state root names it, whatever the entry's state or service instance
  (`journal_live_workspaces`: read only, no lock, nothing written);
- a driver recorded it as a removal it still owes (decision 309, PREPCLEAN-4, the preparation hold row's evidence).
  That record lives in the ledger, so this module is GIVEN a reader for it (`pending`) and never opens the ledger or
  history itself: the caller makes that reader ask the owner thread (E1b-1, decision 311);
- it is younger than `MIN_ORPHAN_AGE_SECONDS` (a driver may not have recorded it yet).

Each pass lists the folders first, then reads both record sets, so a folder a driver recorded meanwhile is kept. A
record set that cannot be read completely means nothing is removed that pass, and the pass says so. A check that has
not been given both readers does nothing at all.

Path safety is the test `codex_preparation.remove_recorded_workspace` makes, applied to every folder before it is
removed (`workspace_problem`): an absolute path, no `..`, a `prepare-*` name, the real scratch container as parent, a
real folder owned by this user, and a container that is itself real. A link, a file or another user's folder is
reported and left exactly as it is; a link is never followed.

Bounds (each listed in tests/lifetime_caps.json with a past-the-limit test): `MAX_ENTRIES_SCANNED` and
`MAX_FOLDERS_LISTED` bound what one pass reads of the container, `MAX_REMOVALS_PER_PASS` what it removes (the rest wait
for the next pass), `MAX_RECORD_PATHS` what a reader may hand back, `MAX_JOURNAL_PROJECTS` and `MAX_JOURNAL_ENTRIES`
what the journal reader takes. Removal stops when the check's time share ends. Standard library only.
"""
from __future__ import annotations

import itertools
import os
import stat
from pathlib import Path

from . import host_paths, launch_journal
from .health_monitor import MAX_TEXT, PROJECT_ID, SUBJECT, Check, Finding

CHECK_NAME = 'workspaces'
FOLDER_PREFIX = 'prepare-'
# A folder younger than this is never an orphan: its driver may not have recorded it yet.
MIN_ORPHAN_AGE_SECONDS = 3600
# A folder still pending (owed to its driver) after this many passes is reported.
PENDING_REPORT_PASSES = 10
# One pass reads at most this many entries of the container, and at most this many `prepare-*` folders.
MAX_ENTRIES_SCANNED = 1024
MAX_FOLDERS_LISTED = 256
# One pass removes at most this many folders (each removal may take a while); the rest wait for the next pass.
MAX_REMOVALS_PER_PASS = 4
# A record reader may hand back this many paths at most (the journal reader's own bounds are below).
MAX_RECORD_PATHS = 8192
MAX_JOURNAL_PROJECTS = 256
MAX_JOURNAL_ENTRIES = 4096


def workspace_problem(path, container, *, uid=None):
    """Why this path is not a folder of ours to remove (a reason code), or None when it is.

    The same test as `codex_preparation.remove_recorded_workspace` (decision 309): absolute, no `..`, a `prepare-*`
    name, the real scratch container as parent, a real folder owned by this user, and a container that is real.
    Reads the path with lstat only; nothing is followed or changed."""
    path, container = Path(path), Path(container)
    uid = os.geteuid() if uid is None else uid
    if not path.is_absolute():
        return 'not_absolute'
    if '..' in path.parts:
        return 'dot_dot'
    if not path.name.startswith(FOLDER_PREFIX):
        return 'not_a_preparation_name'
    if path.parent != container:
        return 'not_in_container'
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'
    if stat.S_ISLNK(info.st_mode):
        return 'folder_is_a_link'
    if not stat.S_ISDIR(info.st_mode):
        return 'folder_not_a_directory'
    if info.st_uid != uid:
        return 'folder_foreign_owner'
    if os.path.realpath(container) != str(container):
        return 'container_unsafe'
    return None


def journal_live_workspaces(state_root):
    """A reader of the folders every launch-journal entry of this state root names: set of paths, or None when the
    set is not complete (a journal folder that is not owner-only or cannot be listed, an entry that cannot be read,
    more projects or entries than the bounds). Read only: no lock is taken and nothing is written."""
    projects = Path(state_root) / 'projects'

    def read():
        try:
            names = []
            with os.scandir(projects) as listing:
                for item in itertools.islice(listing, MAX_ENTRIES_SCANNED):
                    if PROJECT_ID.fullmatch(item.name):
                        names.append(item.name)
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        if len(names) > MAX_JOURNAL_PROJECTS:
            return None
        found, count = set(), 0
        for name in sorted(names):
            directory = projects / name / 'launches'
            try:
                info = os.lstat(directory)
            except FileNotFoundError:
                continue  # a project that never launched anything
            except OSError:
                return None
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            try:
                files = sorted(entry for entry in os.listdir(directory) if launch_journal._entry_name(entry))
            except OSError:
                return None
            count += len(files)
            if count > MAX_JOURNAL_ENTRIES:
                return None
            for entry in files:
                value = launch_journal._read_entry_or_none(directory / entry)
                if value is None:
                    return None  # an entry that cannot be read may name any folder
                if value.get('workspace') is not None:
                    found.add(str(value['workspace']))
        return found

    return read


def _sentence(text):
    clean = ''.join(character if character.isprintable() else ' ' for character in str(text))
    return clean[:MAX_TEXT]


def _subject(name):
    return name if type(name) is str and SUBJECT.fullmatch(name) else 'folder'


def _finding(subject, outcome, reason, text):
    return Finding(CHECK_NAME, 'machine', subject, outcome, reason, _sentence(text))


_UNSAFE = {
    'folder_is_a_link': 'is a link, not a real folder. It was not followed or removed; look at it by hand.',
    'folder_not_a_directory': 'is a file, not a folder. It was left alone; look at it by hand.',
    'folder_foreign_owner': 'belongs to another user of this machine. It was left alone; look at it by hand.',
    'unreadable': 'could not be looked at. It was left alone; it is checked again at the next pass.',
}


class WorkspaceCheck(Check):
    """HM-2a's check (see the module notes). `container` is the scratch container (default: the real one, resolved at
    each pass), `live` and `pending` are the readers of the two record sets (callables returning paths, or None when
    unreadable); `remove` removes a folder (default: the host's own private-folder remover) and `uid` is the user the
    folders must belong to. Without both readers the check does nothing."""

    name = CHECK_NAME

    def __init__(self, *, container=None, live=None, pending=None, remove=None, uid=None):
        self._container = None if container is None else Path(container)
        self._live, self._pending = live, pending
        self._remove = remove if remove is not None else host_paths.remove_private_directory
        self._uid = os.geteuid() if uid is None else uid
        # Passes a folder has been pending: only folders pending now are kept, so it ends with them.
        self.pending_passes = {}

    def _container_path(self):
        if self._container is not None:
            return self._container
        try:
            from . import bridge
            return Path(os.path.realpath(bridge.scratch_base())) / bridge.SCRATCH_CONTAINER
        except Exception:
            return None

    def _listing(self, container):
        """(`prepare-*` names, whether more entries or folders were left unread). Names only; nothing is followed."""
        names, scanned_all = [], True
        with os.scandir(container) as listing:
            entries = list(itertools.islice(listing, MAX_ENTRIES_SCANNED + 1))
            scanned_all = len(entries) <= MAX_ENTRIES_SCANNED
            for entry in entries[:MAX_ENTRIES_SCANNED]:
                if entry.name.startswith(FOLDER_PREFIX):
                    names.append(entry.name)
        names.sort()
        return names[:MAX_FOLDERS_LISTED], not scanned_all or len(names) > MAX_FOLDERS_LISTED

    @staticmethod
    def _records(reader):
        """The paths a reader gives, or (None, the exception type) when they are not all there."""
        try:
            value = reader()
            if value is None:
                return None, None
            paths = set(str(item) for item in itertools.islice(value, MAX_RECORD_PATHS + 1))
        except Exception as error:
            return None, type(error).__name__
        if len(paths) > MAX_RECORD_PATHS:
            return None, None
        return paths, None

    def run(self, context):
        if self._live is None or self._pending is None:
            return []
        container = self._container_path()
        if container is None:
            return []
        try:
            info = os.lstat(container)
        except FileNotFoundError:
            self.pending_passes.clear()
            return []  # no scratch area yet: nothing to tidy
        except OSError:
            info = None
        if (info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or os.path.realpath(container) != str(container)):
            return [_finding('container', 'reported', 'container_unsafe',
                             'The CoCoLo scratch folder is not a real folder of this user, so no leftover '
                             'preparation folder was removed. Look at it by hand.')]
        try:
            names, more = self._listing(container)  # listed first: the records below are read after it
        except OSError:
            return [_finding('container', 'reported', 'container_unsafe',
                             'The CoCoLo scratch folder could not be listed, so no leftover preparation folder '
                             'was removed. It is tried again at the next pass.')]
        findings = []
        if more:
            findings.append(_finding('container', 'reported', 'too_many_folders',
                                     f'The CoCoLo scratch folder holds more than {MAX_FOLDERS_LISTED} preparation '
                                     'folders; the rest are looked at once these are gone.'))
        live, live_error = self._records(self._live)
        pending, pending_error = self._records(self._pending)
        if live is None or pending is None:
            what = 'which preparation folders are in use' if live is None else 'which removals a driver still owes'
            error = live_error if live is None else pending_error
            findings.append(_finding('records', 'reported', 'records_unreadable',
                                     f'The records of {what} could not be read'
                                     + (f' ({error})' if error else '')
                                     + ', so no leftover preparation folder was removed. It is tried again at '
                                       'the next pass.'))
        candidates, seen_pending = [], {}
        for name in names:
            path = container / name
            problem = workspace_problem(path, container, uid=self._uid)
            if problem == 'missing':
                continue
            if problem is not None:
                findings.append(_finding(_subject(name), 'reported', problem,
                                         f'A CoCoLo preparation folder ({name}) ' + _UNSAFE.get(
                                             problem, 'cannot be checked. It was left alone.')))
                continue
            if pending is not None and str(path) in pending:
                passes = min(self.pending_passes.get(name, 0) + 1, PENDING_REPORT_PASSES)
                seen_pending[name] = passes
                if passes >= PENDING_REPORT_PASSES:
                    findings.append(_finding(_subject(name), 'reported', 'removal_still_pending',
                                             f'A CoCoLo preparation folder ({name}) has been waiting to be removed '
                                             f'for {PENDING_REPORT_PASSES} checks. Its service keeps retrying; '
                                             'look at it by hand if it stays.'))
                continue
            if live is None or pending is None or str(path) in live:
                continue
            try:
                age = context.now_ms / 1000 - os.lstat(path).st_mtime
            except OSError:
                continue
            if age >= MIN_ORPHAN_AGE_SECONDS:
                candidates.append(path)
        if pending is not None:
            self.pending_passes = seen_pending
        removed, waiting = 0, 0
        for path in candidates:
            if removed >= MAX_REMOVALS_PER_PASS or context.expired():
                waiting += 1
                continue
            removed += 1
            findings.append(self._remove_one(path, container))
        if waiting:
            findings.append(_finding('container', 'reported', 'folders_waiting',
                                     f'{waiting} more leftover preparation folder(s) wait for the next check; at '
                                     f'most {MAX_REMOVALS_PER_PASS} are removed in one.'))
        return findings

    def _remove_one(self, path, container):
        name = path.name
        if workspace_problem(path, container, uid=self._uid) is None:  # looked at again just before removing
            try:
                self._remove(path)
            except Exception:
                pass
        # Only the folder's observed absence is proof, whatever the remover returned or raised.
        if not os.path.lexists(path):
            return _finding(_subject(name), 'fixed', 'orphan_removed',
                            f'A leftover CoCoLo preparation folder ({name}) belonged to no running service and '
                            'was owed to none; it was removed.')
        return _finding(_subject(name), 'reported', 'orphan_removal_failed',
                        f'A leftover CoCoLo preparation folder ({name}) belongs to no running service but could '
                        'not be removed. It is tried again at the next check.')


def register(monitor, state_root, *, pending, **options):
    """Add the check to a health monitor: its live set from the launch journals under `state_root`, its pending
    removals from `pending` (a reader the caller makes ask the owner thread, E1b-1)."""
    check = WorkspaceCheck(live=journal_live_workspaces(state_root), pending=pending, **options)
    monitor.register(check)
    return check
