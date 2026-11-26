"""HM-2a: leftover CoCoLo preparation folders (docs/requirements/s11-hm2a.md; plan step S11, SW-8, decision 309).

A health check, `workspaces`, over CoCoLo's scratch area (the real scratch container, `bridge.scratch_base()` plus
`bridge.SCRATCH_CONTAINER`). It removes only a `prepare-*` folder whose owner is PROVEN gone, and reports everything
else it finds odd. It reads what `codex_preparation.py` and the driver keep; it never edits it.

Removal needs positive proof (fix round, review r1). Missing records plus age are not proof. A folder is removed only
when all of these hold:

- its owner marker (`host_paths.PREPARATION_OWNER_MARKER`, written when the folder was made) names this check's state
  root, a project and a launch-journal instance, and that instance's lock file is free (a non-blocking flock
  succeeds: `launch_journal.instance_stopped`). The service holds that lock while it runs, and the kernel frees it
  when that process ends. A folder with no marker, a marker of another state root, or an instance whose lock file is
  missing is reported and never removed. A held lock means a running owner and the folder is kept. A clock jump
  cannot cause a removal, because the proof is the lock, not the time;
- no launch-journal entry of any project of this state root names it, whatever the entry's state or service instance
  (`journal_live_workspaces`: read only, no lock, nothing written);
- no driver recorded it as a removal it still owes (decision 309, PREPCLEAN-4). That record lives in the ledger, so
  this module is GIVEN a reader for it (`pending`) and never opens the ledger or history itself: the caller makes that
  reader ask the owner thread (E1b-1, decision 311);
- its marker is at least `MIN_ORPHAN_AGE_SECONDS` old.

Each pass lists the folders first, then reads both record sets, so a folder a driver recorded meanwhile is kept. A
record set that cannot be read completely (an error, or any truncation: past a listing, entry or path limit) means
nothing is removed that pass, and the pass says so. A check that has not been given both readers does nothing.

Path safety is the test `codex_preparation.remove_recorded_workspace` makes (`workspace_problem`), applied through
descriptors: the container is opened once per pass with O_DIRECTORY|O_NOFOLLOW and checked by fstat (a directory of
this user, the same one its path names); every folder is looked at, opened and removed relative to that pinned
descriptor, and every level below through its own pinned, fstat-checked descriptor (unlinkat, rmdir and fchmod
relative to it). No link is ever followed and no path is walked by name, so swapping the container or a subfolder for
a link mid-removal cannot redirect it. No mode is changed before the owner is proven gone (the marker is read through a
search-only descriptor where a folder is not listable); after that, a folder of ours made unlistable gets its mode
back only through fchmod on a descriptor opened O_NOFOLLOW and verified by fstat, never by name. A link, a file or
another user's folder is reported and left exactly as it is.
A folder is proven gone only when looking for it in the pinned container raises FileNotFoundError; any other error
means not proven.

Bounds (each listed in tests/lifetime_caps.json with a past-the-limit test): one pass examines at most
`MAX_ENTRIES_SCANNED` entries of the container (every entry read counts, and the time share is checked while listing)
and `MAX_FOLDERS_LISTED` `prepare-*` folders, going on with the listing the last pass stopped in (kept between passes,
so leftovers behind live or other entries are reached however large the container is), removes at most
`MAX_REMOVALS_PER_PASS` folders and `MAX_REMOVAL_ENTRIES_PER_PASS` entries in them, never deeper than
`MAX_REMOVAL_DEPTH`, and stops at its deadline (checked before every removal step, so a cut pass leaves nothing
deleting). A folder not finished is kept with its marker and continued at the next pass: the last check comes
before the marker goes, the folder's rmdir follows it at once, and a failed rmdir puts the marker back. A reader may
hand back at most `MAX_RECORD_ITEMS` items and `MAX_RECORD_PATHS` distinct paths; the journal reader takes at most
`MAX_ENTRIES_SCANNED` entries of the projects folder, `MAX_JOURNAL_PROJECTS` projects, `MAX_JOURNAL_NAMES` names of
launch folders and `MAX_JOURNAL_ENTRIES` entries, all counted while it lists. Standard library only.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import stat
from pathlib import Path

from . import host_paths, launch_journal
from .health_monitor import MAX_TEXT, PROJECT_ID, SUBJECT, Check, Finding

CHECK_NAME = 'workspaces'
FOLDER_PREFIX = 'prepare-'
MARKER = host_paths.PREPARATION_OWNER_MARKER
MAX_MARKER_BYTES = 4096
# A folder whose marker is younger than this is never an orphan (an extra guard; the proof is its owner's free lock).
MIN_ORPHAN_AGE_SECONDS = 3600
# A folder still pending (owed to its driver) after this many passes is reported.
PENDING_REPORT_PASSES = 10
# One pass examines at most this many entries of the container, and at most this many `prepare-*` folders.
MAX_ENTRIES_SCANNED = 1024
MAX_FOLDERS_LISTED = 256
# One pass removes at most this many folders, and at most this many entries inside them, never deeper than this.
MAX_REMOVALS_PER_PASS = 4
MAX_REMOVAL_ENTRIES_PER_PASS = 4096
MAX_REMOVAL_DEPTH = 64
# A record reader may hand back this many distinct paths, and this many items in all (repeats included).
MAX_RECORD_PATHS = 8192
MAX_RECORD_ITEMS = 4 * MAX_RECORD_PATHS
MAX_JOURNAL_PROJECTS = 256
MAX_JOURNAL_ENTRIES = 4096
MAX_JOURNAL_NAMES = 2 * MAX_JOURNAL_ENTRIES
# Entries named in one listing of a folder being removed (it is listed again until it is empty).
_REMOVAL_BATCH = 256
# The container listing checks the time share after every this many entries read (so a pass whose share is over
# still lists a small container, and reads at most this many entries of a large one).
_LISTING_DEADLINE_EVERY = 64
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
# Open a folder for search only (no listing): needs only its search bit, so an unlistable folder of ours can be
# verified by fstat and its mode changed through that descriptor. None where the platform has no O_SEARCH.
_SEARCH_DIRECTORY = (None if getattr(os, 'O_SEARCH', None) is None
                     else os.O_SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
_MARKER_READ = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0)
_INSTANCE = re.compile(r'[0-9a-f]{32}')
_MARKER_FIELDS = {'instance', 'kind', 'project_id', 'state_root', 'version'}


def workspace_problem(path, container, *, uid=None):
    """Why this path is not a folder of ours to remove (a reason code), or None when it is.

    The same test as `codex_preparation.remove_recorded_workspace` (decision 309): absolute, no `..`, a `prepare-*`
    name, the real scratch container as parent, a real folder owned by this user, and a container that is real.
    Reads the path with lstat only; nothing is followed or changed. A pass applies the same test through its pinned
    container descriptor (`_entry_problem`)."""
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
    problem = _entry_problem(info, uid)
    if problem is not None:
        return problem
    if os.path.realpath(container) != str(container):
        return 'container_unsafe'
    return None


def _entry_problem(info, uid):
    if stat.S_ISLNK(info.st_mode):
        return 'folder_is_a_link'
    if not stat.S_ISDIR(info.st_mode):
        return 'folder_not_a_directory'
    if info.st_uid != uid:
        return 'folder_foreign_owner'
    return None


def journal_live_workspaces(state_root):
    """A reader of the folders every launch-journal entry of this state root names: set of paths, or None when the
    set is not complete (a journal folder that is not owner-only or cannot be listed, an entry that cannot be read,
    or more entries, projects, names or journal entries than the bounds, each counted while listing). Read only: no
    lock is taken and nothing is written."""
    projects = Path(state_root) / 'projects'

    def read():
        names, scanned = [], 0
        try:
            with os.scandir(projects) as listing:
                for item in listing:
                    scanned += 1
                    if scanned > MAX_ENTRIES_SCANNED:
                        return None  # a project past the limit could name any folder
                    if PROJECT_ID.fullmatch(item.name):
                        names.append(item.name)
                        if len(names) > MAX_JOURNAL_PROJECTS:
                            return None
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        found, count, seen = set(), 0, 0
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
            files = []
            try:
                with os.scandir(directory) as listing:
                    for item in listing:
                        seen += 1
                        if seen > MAX_JOURNAL_NAMES:
                            return None
                        if launch_journal._entry_name(item.name):
                            count += 1
                            if count > MAX_JOURNAL_ENTRIES:
                                return None
                            files.append(item.name)
            except OSError:
                return None
            for entry in sorted(files):
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


def _unproven_finding(count, container):
    """PREP-1: all leftover folders whose owner cannot be proven, as ONE finding (a count, never one line each).
    The step is safe: look in the scratch folder, and delete only when no CoCoLo service runs for any project (a folder may belong to a
    running service, which is why nothing here removes it)."""
    many = count != 1
    head = (f'{count} leftover CoCoLo preparation folder{"s are" if many else " is"} not in use, but nothing proves '
            f'the service{"s that made them have" if many else " that made it has"} stopped, so '
            f'{"they were" if many else "it was"} left alone.')
    step = (f' Look in {container}; delete {"them" if many else "it"} only when no CoCoLo service is running '
            'on this Mac (check every project).')
    short = ' In the scratch folder; delete only when no CoCoLo service runs on this Mac (any project).'
    text = head + step
    if _sentence(text) != text:  # past the finding bound: say where in general words instead of the path
        text = head + short
    return _finding('folders', 'reported', 'owner_unknown', text)


_UNSAFE = {
    'folder_is_a_link': 'is a link, not a real folder. It was not followed or removed; look at it by hand.',
    'folder_not_a_directory': 'is a file, not a folder. It was left alone; look at it by hand.',
    'folder_foreign_owner': 'belongs to another user of this machine. It was left alone; look at it by hand.',
    'unreadable': 'could not be looked at. It was left alone; it is checked again at the next pass.',
}


# Decision 365: what the person can do about each unknown reason of the pending records (kept short: findings are 240 characters).
_STEPS = {
    'deadline': 'No action needed; it is tried again next pass.',
    'too_many_projects': 'No action needed yet; report it if it lasts.',
    'too_many_pending': 'It stays until the extra records clear; report it if it lasts.',
    'projects_unreadable': 'Check the CoCoLo state folder is readable; report it if it lasts.',
    'history_unreadable': 'That project history cannot be read; report it if it lasts.',
    'evidence_unreadable': 'A removal record is damaged; report it if it lasts.',
}


class _Budget:
    """What one pass may still spend on removal: entries removed so far, and its deadline."""

    def __init__(self, context):
        self.used, self.context = 0, context

    def spent(self):
        return self.used >= MAX_REMOVAL_ENTRIES_PER_PASS or self.context.expired()


def _verified(parent, name, flags, ident, uid):
    """A descriptor of `name` in the pinned `parent` opened with `flags` (never following a link), if fstat shows it
    is still the folder that was looked at (same device and inode, a directory of `uid`); None otherwise. A refused
    open raises (PermissionError and the rest), so the caller can tell 'not readable' from 'not ours'."""
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
    except OSError:
        info = None
    if info is None or (info.st_dev, info.st_ino) != ident or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        os.close(descriptor)
        return None
    return descriptor


def _look_folder(parent, name, ident, uid):
    """A descriptor to read the owner marker through, BEFORE any owner proof: nothing is changed. A folder that is
    not listable is opened for search only where the platform can (O_SEARCH); otherwise None (owner not proven)."""
    for flags in (_DIRECTORY, _SEARCH_DIRECTORY):
        if flags is None:
            continue
        try:
            return _verified(parent, name, flags, ident, uid)
        except PermissionError:
            continue
        except OSError:
            return None
    return None


def _open_folder(parent, name, ident, uid):
    """A listable descriptor of `name` in the pinned `parent`, if it is still the folder that was looked at; None
    otherwise. Only called once the owner is proven gone. An owner-only folder made unlistable gets its mode back
    only through fchmod on a descriptor opened O_NOFOLLOW and verified by fstat (the same device, inode and user),
    never by name; where the platform cannot open a folder for search (no O_SEARCH) it stays as it is (not
    removed, reported)."""
    try:
        return _verified(parent, name, _DIRECTORY, ident, uid)
    except PermissionError:
        pass
    except OSError:
        return None
    if _SEARCH_DIRECTORY is None:
        return None
    try:
        search = _verified(parent, name, _SEARCH_DIRECTORY, ident, uid)
    except OSError:
        return None
    if search is None:
        return None  # another folder stands there now: it is left exactly as it is
    try:
        os.fchmod(search, 0o700)  # the verified folder itself, through its own descriptor
    except OSError:
        return None
    finally:
        os.close(search)
    try:
        return _verified(parent, name, _DIRECTORY, ident, uid)
    except OSError:
        return None


def _remove_tree(parent, name, ident, uid, budget, depth=0):
    """Remove the folder `name` of the pinned `parent` and everything in it, through descriptors only.

    True once it is removed; False when the budget or deadline stopped it, or it could not be (a level that is not
    the folder looked at, too deep, an error). At the top level the owner marker is removed last, so a folder that is
    only partly removed keeps its owner proof for the next pass."""
    if depth > MAX_REMOVAL_DEPTH:
        return False
    descriptor = _open_folder(parent, name, ident, uid)
    if descriptor is None:
        return False
    try:
        os.fchmod(descriptor, 0o700)  # through the pinned descriptor: a read-only level of ours can be emptied
        while True:
            if budget.spent():
                return False
            with os.scandir(descriptor) as listing:
                batch = [entry.name for entry in itertools.islice(listing, _REMOVAL_BATCH)]
            if depth == 0:
                batch = [child for child in batch if child != MARKER]
            if not batch:
                break
            for child in batch:
                if budget.spent():
                    return False
                try:
                    info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if not _remove_tree(descriptor, child, (info.st_dev, info.st_ino), uid, budget, depth + 1):
                        return False
                    continue
                try:
                    os.unlink(child, dir_fd=descriptor)  # a link is removed, never followed
                except FileNotFoundError:
                    pass
                budget.used += 1
        if depth == 0:
            # The last budget check comes BEFORE the marker goes; the marker and the folder then go back to back
            # (`_finish`), so a pass that stops never leaves a folder without its owner proof.
            if budget.spent():
                return False
            return _finish(parent, name, descriptor, budget)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    if budget.spent():
        return False
    try:
        os.rmdir(name, dir_fd=parent)
    except OSError:
        return False
    budget.used += 1
    return True


def _finish(parent, name, folder, budget):
    """Remove the emptied top folder's owner marker, then the folder itself at once: no check, no other step
    between. If the rmdir fails, the marker is put back as it was (the same bytes and times), so the next pass can
    still prove the owner gone and finish. A marker that is missing or not a marker now removes nothing."""
    descriptor = os.open(MARKER, _MARKER_READ, dir_fd=folder)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKER_BYTES:
            return False
        data = os.read(descriptor, MAX_MARKER_BYTES + 1)
    finally:
        os.close(descriptor)
    os.unlink(MARKER, dir_fd=folder)
    try:
        os.rmdir(name, dir_fd=parent)
    except OSError:
        _restore_marker(folder, data, info)
        return False
    budget.used += 2
    return True


def _restore_marker(folder, data, info):
    try:
        descriptor = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=folder)
    except OSError:
        return
    try:
        os.write(descriptor, data)
        if os.utime in os.supports_fd:
            os.utime(descriptor, ns=(info.st_atime_ns, info.st_mtime_ns))
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


class WorkspaceCheck(Check):
    """HM-2a's check (see the module notes). `container` is the scratch container (default: the real one, resolved at
    each pass), `live` and `pending` are the readers of the two record sets (callables returning paths, or None when
    unreadable), `state_root` is the learning state whose launch-journal instances owner markers must name (without
    it no owner is ever proven, so nothing is removed). `remove` replaces the pinned remover with a callable given the
    folder's path (a test seam) and `uid` is the user the folders must belong to. Without both readers the check does
    nothing."""

    name = CHECK_NAME

    def __init__(self, *, container=None, live=None, pending=None, state_root=None, remove=None, uid=None):
        self._container = None if container is None else Path(container)
        self._live, self._pending = live, pending
        self._state_root = None if state_root is None else Path(state_root)
        self._remove = remove
        self._uid = os.geteuid() if uid is None else uid
        # Passes a folder has been pending: only folders pending now are kept, so it ends with them.
        self.pending_passes = {}
        # The container listing the next pass goes on with: (container device and inode, its open listing), or None
        # to start a new one. Kept between passes so every pass reads at most MAX_ENTRIES_SCANNED entries and still
        # reaches the end of a huge container (rotating: a listing that ends is closed and the next starts again).
        self._scan = None

    def _container_path(self):
        if self._container is not None:
            return self._container
        try:
            from . import bridge
            return Path(os.path.realpath(bridge.scratch_base())) / bridge.SCRATCH_CONTAINER
        except Exception:
            return None

    def close(self):
        """Close the kept container listing, if any (the next pass starts a new one)."""
        scan, self._scan = getattr(self, '_scan', None), None
        if scan is not None:
            try:
                scan[1].close()
            except Exception:
                pass

    def __del__(self):
        self.close()

    def _listing(self, container, ident, context):
        """(`prepare-*` names, whether entries were left unexamined) for this pass, going on from where the last
        pass stopped. Every entry read counts toward MAX_ENTRIES_SCANNED and the time share is checked every
        `_LISTING_DEADLINE_EVERY` entries, so a pass never reads more than the cap, however far into the container
        it is. Names only; nothing is followed."""
        if self._scan is not None and self._scan[0] != ident:
            self.close()  # another folder stands at the container's path now: start again there
        resumed = self._scan is not None
        names, read = [], 0
        try:
            while True:
                if self._scan is None:
                    listing = os.scandir(container)
                    self._scan = (ident, listing, iter(listing))
                entries, pulled, ended = self._scan[2], 0, False
                while read < MAX_ENTRIES_SCANNED and len(names) < MAX_FOLDERS_LISTED:
                    if read and read % _LISTING_DEADLINE_EVERY == 0 and context.expired():
                        break
                    try:
                        entry = next(entries)
                    except StopIteration:
                        ended = True
                        break
                    read += 1
                    pulled += 1
                    if entry.name.startswith(FOLDER_PREFIX):
                        names.append(entry.name)
                if ended:
                    self.close()
                    if resumed and pulled == 0:
                        resumed = False  # the kept listing was at its end: start again from the first entry
                        continue
                break
        except BaseException:
            self.close()
            raise
        return sorted(names), not ended or resumed

    @staticmethod
    def _records(reader):
        """The distinct paths a reader gives, or (None, the exception type) when they are not all there: an error, or
        more items or distinct paths than the bounds (any truncation means the set is not complete)."""
        try:
            value = reader()
            if value is None:
                reason = getattr(reader, 'reason', None)  # decision 365: a reader may say why (a plain code)
                return None, (reason if type(reason) is str and SUBJECT.fullmatch(reason) else None)
            paths, items = set(), 0
            for item in value:
                items += 1
                if items > MAX_RECORD_ITEMS:
                    return None, None
                paths.add(str(item))
                if len(paths) > MAX_RECORD_PATHS:
                    return None, None
        except Exception as error:
            return None, type(error).__name__
        return paths, None

    @staticmethod
    def _records_finding(what, error):
        """The one 'records unreadable' finding. `error` is an exception class name or, from the pending reader
        (decision 365), a reason code; a reason code is also the subject, so the status line shows what is left."""
        step = _STEPS.get((error or '').split(':')[0])
        if step:  # a named reason: short, so the longest (a project id and an error class) fits 240 characters
            text = f'Owed folder removals could not be fully read ({error}), so nothing was removed. {step}'
        else:
            text = (f'The records of {what} are incomplete or could not be read'
                    + (f' ({error})' if error else '') + ', so no leftover preparation folder was removed. '
                    'It is tried again at the next pass.')
        return _finding(error if step else 'records', 'reported', 'records_unreadable', text)

    def _owner(self, container, name, ident):
        """('stopped', marker time) when the folder's owner is proven gone, ('running', None) when its owner holds its
        lock, (None, None) when nothing is proven (no or unreadable marker, another state root, no lock file)."""
        if self._state_root is None:
            return None, None
        folder = _look_folder(container, name, ident, self._uid)  # no mode is changed before owner proof
        if folder is None:
            return None, None
        try:
            descriptor = os.open(MARKER, _MARKER_READ, dir_fd=folder)
        except OSError:
            return None, None
        finally:
            os.close(folder)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid or info.st_size > MAX_MARKER_BYTES:
                return None, None
            data = os.read(descriptor, MAX_MARKER_BYTES + 1)
        except OSError:
            return None, None
        finally:
            os.close(descriptor)
        try:
            value = json.loads(data)
        except (ValueError, RecursionError):
            return None, None
        root = os.path.realpath(self._state_root)
        if (type(value) is not dict or set(value) != _MARKER_FIELDS or value['version'] != 1
                or value['kind'] != host_paths.PREPARATION_OWNER_KIND or value['state_root'] != root
                or type(value['project_id']) is not str or not PROJECT_ID.fullmatch(value['project_id'])
                or type(value['instance']) is not str or not _INSTANCE.fullmatch(value['instance'])):
            return None, None
        directory = Path(root) / 'projects' / value['project_id'] / 'launches'
        if launch_journal.instance_stopped(directory, value['instance']):
            return 'stopped', info.st_mtime
        if launch_journal._lock_held(directory / (value['instance'] + '.lock')):
            return 'running', None
        return None, None

    def run(self, context):
        if self._live is None or self._pending is None:
            return []
        container = self._container_path()
        if container is None:
            return []
        try:
            descriptor = os.open(container, _DIRECTORY)
        except FileNotFoundError:
            self.pending_passes.clear()
            return []  # no scratch area yet: nothing to tidy
        except OSError:
            descriptor = None
        if descriptor is None:
            return [self._unsafe_container()]
        try:
            return self._pass(descriptor, container, context)
        finally:
            os.close(descriptor)

    @staticmethod
    def _unsafe_container():
        return _finding('container', 'reported', 'container_unsafe',
                        'The CoCoLo scratch folder is not a real folder of this user, so no leftover preparation '
                        'folder was removed. Look at it by hand.')

    def _pass(self, fd, container, context):
        try:
            pinned, named = os.fstat(fd), os.lstat(container)
        except OSError:
            return [self._unsafe_container()]
        if (not stat.S_ISDIR(pinned.st_mode) or pinned.st_uid != os.geteuid()
                or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
                or os.path.realpath(container) != str(container)):
            return [self._unsafe_container()]
        try:
            names, more = self._listing(fd, (pinned.st_dev, pinned.st_ino), context)  # listed first: the records below are read after it
        except OSError:
            return [_finding('container', 'reported', 'container_unsafe',
                             'The CoCoLo scratch folder could not be listed, so no leftover preparation folder '
                             'was removed. It is tried again at the next pass.')]
        findings = []
        if more:
            findings.append(_finding('container', 'reported', 'too_many_folders',
                                     'The CoCoLo scratch folder holds more entries than one check looks at; the '
                                     'rest are looked at in the next checks.'))
        live, live_error = self._records(self._live)
        pending, pending_error = self._records(self._pending)
        if live is None or pending is None:
            what = 'which preparation folders are in use' if live is None else 'which removals a driver still owes'
            error = live_error if live is None else pending_error
            findings.append(self._records_finding(what, error))
        now = context.now_ms / 1000
        candidates, seen_pending, unproven = [], {}, 0
        for name in names:
            path = container / name
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                problem = _entry_problem(info, self._uid)
            except FileNotFoundError:
                continue
            except OSError:
                problem = 'unreadable'
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
            ident = (info.st_dev, info.st_ino)
            owner, made = self._owner(fd, name, ident)
            if owner == 'running':
                continue
            if owner is None:
                if now - info.st_mtime >= MIN_ORPHAN_AGE_SECONDS:
                    unproven += 1  # PREP-1: reported once, below, with a count
                continue
            if now - made >= MIN_ORPHAN_AGE_SECONDS:
                candidates.append((name, ident))
        if pending is not None:
            self.pending_passes = seen_pending
        if unproven:
            findings.append(_unproven_finding(unproven, container))
        budget, removed, waiting = _Budget(context), 0, 0
        for name, ident in candidates:
            if removed >= MAX_REMOVALS_PER_PASS or budget.spent():
                waiting += 1
                continue
            removed += 1
            findings.append(self._remove_one(fd, container, name, ident, budget))
        if waiting:
            findings.append(_finding('container', 'reported', 'folders_waiting',
                                     f'{waiting} more leftover preparation folder(s) wait for the next check; at '
                                     f'most {MAX_REMOVALS_PER_PASS} are removed in one.'))
        return findings

    def _remove_one(self, fd, container, name, ident, budget):
        stopped = False
        if self._remove is not None:
            try:
                self._remove(container / name)
            except Exception:
                pass
        else:
            try:
                done = _remove_tree(fd, name, ident, self._uid, budget)
            except Exception:
                done = False
            stopped = not done and budget.spent()
        # Only the folder's observed absence in the pinned container is proof, whatever the remover did.
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return _finding(_subject(name), 'fixed', 'orphan_removed',
                            f'A leftover CoCoLo preparation folder ({name}) belonged to no running service and '
                            'was owed to none; it was removed.')
        except OSError:
            pass
        if stopped:
            return _finding(_subject(name), 'reported', 'orphan_removal_continues',
                            f'A leftover CoCoLo preparation folder ({name}) is being removed; it is large, so its '
                            'removal goes on at the next check.')
        return _finding(_subject(name), 'reported', 'orphan_removal_failed',
                        f'A leftover CoCoLo preparation folder ({name}) belongs to no running service but could '
                        'not be removed. It is tried again at the next check.')


def register(monitor, state_root, *, pending, **options):
    """Add the check to a health monitor: its live set from the launch journals under `state_root`, its owner proof
    against that state root's journal instances, its pending removals from `pending` (a reader the caller makes ask
    the owner thread, E1b-1)."""
    check = WorkspaceCheck(live=journal_live_workspaces(state_root), pending=pending, state_root=state_root,
                           **options)
    monitor.register(check)
    return check
