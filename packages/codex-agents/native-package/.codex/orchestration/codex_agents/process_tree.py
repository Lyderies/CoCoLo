"""Track and kill the descendants of one bridged child, and never anything else.

`ps` only lists candidate pids. Every decision rests on one kernel record per
process, read in a single call, that gives its parent, process group and birth
time together (kinfo_proc on macOS, /proc/<pid>/stat on Linux). A process is
adopted only when its own record names a tracked parent and that parent's
record, read afterwards, still has the tracked birth time; its own birth time
comes from the same record as its parent. A process group never adopts. Every
signal is preceded by a fresh record read, so a pid that now belongs to another
process is not signalled, and a group is signalled only through a verified
tracked leader, which includes the child itself while it is unreaped.

Tracking is by polling, so it is conservative rather than complete: a
descendant whose parent exits before any snapshot has seen it is lost, and it
is left running rather than risk signalling an unrelated process. So that such
a process cannot let cleanup be confirmed, every child is started with a
per-run marker in its environment (`RUN_TAG_VARIABLE`, an unguessable value
kept with the tree), which its descendants inherit. Cleanup is confirmed only
while no process of this user, born since the child, still carries it
(`tagged_process_running`), and none is still in the child's own session (the
child starts one, so a descendant that never called setsid stays in it). The
known limits: a process that clears or rewrites its own environment, or (on
macOS, which withholds the environment of its own signed tools such as /bin/sh
and /bin/sleep from other processes) runs one of those tools, and that also
starts a new session and detaches between two snapshots, is not found. A
process whose environment cannot be read at all is judged by the nearest
readable parent its environment came from; detached under launchd/init with
no such parent, it keeps cleanup unconfirmed. Either way it is still confined
to the child's read-only sandbox.

Standard library only: the deadline watchdog runs this file as a script.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import selectors
import signal
import struct
import subprocess
import sys
import threading
from threading import local as thread_local
import time

# Fixed paths, never a search path: macOS has /bin/ps, Linux distributions one or both.
PS_PATHS = ("/bin/ps", "/usr/bin/ps")
PS_COLUMNS = "pid=,ppid="
PS_ENVIRONMENT = {"PATH": os.defpath, "LC_ALL": "C"}
SNAPSHOT_TIMEOUT_SECONDS = 5
TRACK_INTERVAL_SECONDS = 0.25
WATCHDOG_INTERVAL_SECONDS = 0.5
# The watchdog retries identification for this long, while the bridge holds the
# child unreaped and waits for its acknowledgement a little longer.
WATCHDOG_IDENTIFY_SECONDS = 5
WATCHDOG_READY_SECONDS = 10
WATCHDOG_READY = b"ready\n"
# C3: an orphaned watchdog asks the container primitive to stop the container for at most one
# monotonic budget: WATCHDOG_STOP_BUDGET_SECONDS plus the container's own grace (a stop needs the
# grace to finish its SIGTERM wait). Inside it, at most WATCHDOG_STOP_ATTEMPTS tries with the pauses
# below; a try still running when the budget ends is abandoned (it runs in a helper thread), so
# neither a hanging podman nor the pauses can hold the tree alive past the budget. The tree is then
# ended (its own bound: MAX_KILL_ROUNDS rounds) and one last stop is tried for at most
# WATCHDOG_LAST_STOP_SECONDS. 15 s + grace fits the real test's 20 s allowance (grace 1 s there).
WATCHDOG_STOP_BUDGET_SECONDS = 15.0
WATCHDOG_LAST_STOP_SECONDS = 5.0
WATCHDOG_STOP_ATTEMPTS = 4
WATCHDOG_STOP_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
# Kill rounds: each takes a fresh snapshot, adopts what the tree started since and
# kills every live tracked process, until a round finds none left alive.
MAX_KILL_ROUNDS = 5
# Identities not yet proven stopped that cleanup evidence holds at once; one proven stopped
# is dropped (except the root's), so a long run that starts many short processes never reaches it.
MAX_CLEANUP_IDENTITIES = 4096
# A failed process listing is retried this often, this far apart, before cleanup is refused.
SNAPSHOT_RETRY_ATTEMPTS = 5
SNAPSHOT_RETRY_PAUSE_SECONDS = 0.1
KILL_ROUND_PAUSE_SECONDS = 0.05

# macOS: sysctl(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid) fills one 648-byte
# struct kinfo_proc. kp_proc starts with p_starttime (struct timeval); p_stat is
# at 36 and p_pid at 40; kp_eproc starts at 296 with e_ppid at 560 and e_pgid at
# 564. Every record must name the pid asked for, and this process's own record
# must give its own parent and group, so a layout change fails closed.
DARWIN_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
DARWIN_KINFO_PROC_SIZE = 648
DARWIN_STAT_OFFSET = 36
DARWIN_PID_OFFSET = 40
DARWIN_PPID_OFFSET = 560
DARWIN_PGID_OFFSET = 564
DARWIN_ZOMBIE = 5

# macOS: sysctl(CTL_KERN, KERN_PROCARGS2, pid) gives argc, the executable path,
# then the argument and environment strings, each NUL-terminated.
DARWIN_KERN_PROCARGS2 = 49
# Each child's environment carries this marker with a fresh 128-bit value.
RUN_TAG_VARIABLE = "COCOLO_RUN_TAG"
RUN_TAG = re.compile(r"[0-9a-f]{32}")
# Bounds on one scan of the process table for the marker: more processes, or
# one environment larger than this, makes the answer unknown, never "none".
MAX_TAG_SCAN_PROCESSES = 8192
MAX_ENVIRONMENT_BYTES = 4 * 1024 * 1024
# How far up its listed parents an unreadable process is judged (`_judged_by_parents`).
MAX_PARENT_DEPTH = 16
# A process that is being killed still shows its environment for a moment.
TAG_SCAN_ATTEMPTS = 3
# A scan lists again until a listing adds no process it has not read, so one that
# forks and exits between the listing and its read cannot hide its descendant.
# One still growing after this many listings is not proof.
MAX_SCAN_ROUNDS = 8

# pid -> parent pid, as `ps` listed it: a hint for which records to read, never trusted.
Table = dict[int, int]
# One kernel record: (parent pid, process group, birth time, exited)
Record = tuple[int, int, str, bool]


def process_table() -> Table | None:
    """This user's pids and their listed parents from a fixed-path `ps`; None when unavailable."""

    path = next((candidate for candidate in PS_PATHS if os.path.isfile(candidate)), None)
    if path is None:
        return None
    arguments = [path, "-U", str(os.getuid()), "-o", PS_COLUMNS]
    try:
        with subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=PS_ENVIRONMENT,
        ) as lister:
            try:
                output, _ = lister.communicate(timeout=SNAPSHOT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                lister.kill()
                lister.communicate()
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    if lister.returncode != 0:
        return None
    table: Table = {}
    for line in output.decode("ascii", "replace").splitlines():
        fields = line.split()
        if not fields:
            continue
        # A line that does not parse makes the whole snapshot unusable.
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            return None
        table[int(fields[0])] = int(fields[1])
    # `ps` lists itself: a new pid in every listing, which would never look settled.
    table.pop(lister.pid, None)
    return table or None


_SYSCTL: list = []


def _darwin_raw(pid: int) -> bytes | None:
    import ctypes

    if not _SYSCTL:
        library = ctypes.CDLL(DARWIN_LIBSYSTEM, use_errno=True)
        function = library.sysctl
        function.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        function.restype = ctypes.c_int
        _SYSCTL.append(function)
    name = (ctypes.c_int * 4)(1, 14, 1, pid)
    buffer = ctypes.create_string_buffer(DARWIN_KINFO_PROC_SIZE)
    size = ctypes.c_size_t(DARWIN_KINFO_PROC_SIZE)
    if _SYSCTL[0](name, 4, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.raw if size.value == DARWIN_KINFO_PROC_SIZE else None


def _darwin_gone(pid: int) -> bool:
    """The kernel answered and has no process with this pid (an empty record), not an error."""
    import ctypes

    if not _SYSCTL:
        _darwin_raw(os.getpid())  # Binds sysctl once, as the record read does.
    name = (ctypes.c_int * 4)(1, 14, 1, pid)
    buffer = ctypes.create_string_buffer(DARWIN_KINFO_PROC_SIZE)
    size = ctypes.c_size_t(DARWIN_KINFO_PROC_SIZE)
    return _SYSCTL[0](name, 4, buffer, ctypes.byref(size), None, 0) == 0 and size.value == 0


def parse_kinfo_proc(raw: bytes, pid: int) -> Record | None:
    """A macOS kinfo_proc as a record; None unless it is the record for `pid`."""

    if len(raw) != DARWIN_KINFO_PROC_SIZE:
        return None
    if struct.unpack_from("=i", raw, DARWIN_PID_OFFSET)[0] != pid:
        return None
    seconds, microseconds = struct.unpack_from("=qi", raw, 0)
    parent = struct.unpack_from("=i", raw, DARWIN_PPID_OFFSET)[0]
    group = struct.unpack_from("=i", raw, DARWIN_PGID_OFFSET)[0]
    exited = raw[DARWIN_STAT_OFFSET] == DARWIN_ZOMBIE
    return parent, group, f"{seconds}.{microseconds:06d}", exited


_DARWIN_LAYOUT: list[bool] = []


def _darwin_record(pid: int) -> Record | None:
    if not _DARWIN_LAYOUT:
        own_pid = os.getpid()
        raw = _darwin_raw(own_pid)
        own = parse_kinfo_proc(raw, own_pid) if raw is not None else None
        _DARWIN_LAYOUT.append(
            own is not None and own[:2] == (os.getppid(), os.getpgrp()) and not own[3]
        )
    if not _DARWIN_LAYOUT[0]:
        return None
    raw = _darwin_raw(pid)
    return parse_kinfo_proc(raw, pid) if raw is not None else None


def parse_proc_stat(content: bytes) -> Record | None:
    """A Linux /proc/<pid>/stat line as a record: state, ppid, pgrp and starttime."""

    # The command name may hold spaces and parentheses; the fields follow the last ")".
    end = content.rfind(b")")
    if end < 0:
        return None
    fields = content[end + 2:].split()
    if len(fields) < 20 or not all(fields[index].isdigit() for index in (1, 2, 19)):
        return None
    return int(fields[1]), int(fields[2]), fields[19].decode("ascii"), fields[0] == b"Z"


def _linux_record(pid: int) -> Record | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            content = handle.read()
    except OSError:
        return None
    return parse_proc_stat(content)


def process_record(pid: int) -> Record | None:
    """One process's parent, group, birth time and exit state, from one kernel read.

    None when the process is gone (reaped), unreadable, or the platform is not
    macOS or Linux.
    """

    if pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_record(pid)
        if sys.platform.startswith("linux"):
            return _linux_record(pid)
    except (OSError, ValueError, AttributeError, IndexError, struct.error):
        return None
    return None


def process_gone(pid: int) -> bool:
    """Positive evidence that no process has this pid now: it exited and was reaped.

    A record that cannot be read (permissions, an unknown platform, any error) is not
    evidence, and neither is a pid reused by another process (that one has a record).
    A process that escaped its tree is still running, so it is never "gone".
    """

    if pid <= 1:
        return False
    try:
        if sys.platform == "darwin":
            return _darwin_gone(pid)
        if sys.platform.startswith("linux"):
            try:
                os.stat(f"/proc/{pid}")
            except (FileNotFoundError, ProcessLookupError):
                return True
            return False
    except (OSError, ValueError, AttributeError):
        return False
    return False


def create_child(factory, arguments, options):
    """O0c review M2: run the process factory (a `subprocess.Popen` class) and say what a failure left.

    Returns (process, error, child_left). On success: (process, None, True). On failure, `child_left`
    is False only when no child can exist: the factory raised an ordinary error before it created one
    (refused before the fork, or the fork itself failed), or it created one and reaped it itself
    (CPython's Popen waits for a child whose exec failed before raising). A child it created and did
    not reap is returned with the error, so the caller can stop and reap it. An interruption with no
    child recorded, a factory that is not a class (what it left cannot be read), or any doubt, leaves
    `child_left` True. Never raises.
    """

    if not isinstance(factory, type):
        try:
            return factory(arguments, **options), None, True
        except BaseException as error:  # noqa: BLE001 - reported to the caller, which re-raises it
            return None, error, True
    try:
        process = factory.__new__(factory)
        process.__init__(arguments, **options)
    except BaseException as error:  # noqa: BLE001 - reported to the caller, which re-raises it
        process = locals().get("process")
        created = getattr(process, "_child_created", False) is True
        if created and getattr(process, "returncode", None) is None:
            return process, error, True
        return None, error, not (created or isinstance(error, Exception))
    return process, None, True


def group_empty(group: int) -> bool:
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


def end_child(process, *, wait_s: float, end_group_after_reap: bool = False) -> str | None:
    """O0c review M4: end one child started in a session of its own; None once it is proven gone.

    Proof: the child is this process's own, reaped by its wait, and no process is left in its group
    (the group it led; any descendant that did not change group is in it). While the child is not
    reaped its pid, and so its group id, cannot belong to anyone else, so the whole group is killed
    first. `end_group_after_reap` (only right after the child was reaped, where its group cannot yet
    have been replaced) also kills what is left in the group and waits up to `wait_s` for it to empty.
    Otherwise the reason it is not proven: `not_reaped` or `group_not_empty`. Never raises.
    """

    try:
        pid = process.pid
        if process.returncode is None:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                process.kill()
            try:
                process.wait(timeout=wait_s)
            except subprocess.TimeoutExpired:
                return "not_reaped"
            end_group_after_reap = True  # reaped just now, after its group was killed
        if group_empty(pid):
            return None
        if not end_group_after_reap:
            return "group_not_empty"
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
        end = time.monotonic() + wait_s
        while not group_empty(pid):
            if time.monotonic() >= end:
                return "group_not_empty"
            time.sleep(0.01)
        return None
    except Exception:  # noqa: BLE001 - nothing proven
        return "unchecked"


def new_run_tag() -> str:
    """A fresh, unguessable per-run marker value for a child's environment."""
    return secrets.token_hex(16)


def run_tag_entry(tag: str) -> bytes:
    """The exact environment entry a tagged child and its descendants carry."""
    return (RUN_TAG_VARIABLE + "=" + tag).encode("ascii")


def parse_procargs2(raw: bytes) -> tuple[bytes, list[bytes]] | None:
    """The executable path and environment strings of a macOS KERN_PROCARGS2 buffer.

    None when it does not parse. macOS withholds the environment of an
    Apple-signed (platform) executable from other processes, so for one of
    those the list is empty.
    """

    if len(raw) < 4:
        return None
    argc = struct.unpack_from("=i", raw, 0)[0]
    if argc < 0:
        return None
    # The executable path, NUL-padded so argv starts 8-byte aligned after argc (so
    # on every process checked), then argv and the environment. Skipping every NUL
    # instead would also swallow an empty argv[0] and shift the environment by one.
    end = raw.find(b"\0", 4)
    if end < 0:
        return None
    path = raw[4:end]
    position = 4 + -(-(len(path) + 1) // 8) * 8
    if position > len(raw) or raw[end:position].strip(b"\0"):
        return None
    strings = raw[position:].split(b"\0")
    if len(strings) < argc:
        return None
    environment = []
    for string in strings[argc:]:
        if not string:
            break  # The environment ends at its first empty string.
        environment.append(string)
    return path, environment


def parse_environ(raw: bytes) -> list[bytes]:
    """The environment strings of a Linux /proc/<pid>/environ buffer."""
    return [string for string in raw.split(b"\0") if string]


_ARGMAX: list[int] = []


_ARGUMENT_BUFFERS = thread_local()


def _darwin_arguments(pid: int) -> tuple[bytes, list[bytes]] | None:
    import ctypes

    if not _SYSCTL:
        _darwin_raw(os.getpid())  # Binds sysctl once, as the record read does.
    if not _ARGMAX:
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if _SYSCTL[0]((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return None
        _ARGMAX.append(min(max(value.value, 0), MAX_ENVIRONMENT_BYTES))
    # One buffer per thread, reused: a scan reads every process, and kern.argmax can be megabytes.
    buffer = getattr(_ARGUMENT_BUFFERS, "buffer", None)
    if buffer is None:
        buffer = _ARGUMENT_BUFFERS.buffer = ctypes.create_string_buffer(_ARGMAX[0])
    size = ctypes.c_size_t(_ARGMAX[0])
    name = (ctypes.c_int * 3)(1, DARWIN_KERN_PROCARGS2, pid)
    if _SYSCTL[0](name, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return parse_procargs2(ctypes.string_at(buffer, min(size.value, _ARGMAX[0])))


def _linux_environment(pid: int) -> list[bytes] | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read(MAX_ENVIRONMENT_BYTES + 1)
    except OSError:
        return None
    return None if len(raw) > MAX_ENVIRONMENT_BYTES else parse_environ(raw)


def process_arguments(pid: int) -> tuple[bytes | None, list[bytes]] | None:
    """One process's executable path (macOS only) and environment strings; None when unreadable."""

    if pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_arguments(pid)
        if sys.platform.startswith("linux"):
            environment = _linux_environment(pid)
            return None if environment is None else (None, environment)
    except (OSError, ValueError, AttributeError, IndexError, struct.error):
        return None
    return None


def process_environment(pid: int) -> list[bytes] | None:
    """One process's environment strings from the kernel; None when unreadable."""
    arguments = process_arguments(pid)
    return None if arguments is None else arguments[1]


def _birth_order(birth: str) -> tuple[int, ...] | None:
    """A birth time as comparable numbers: seconds and microseconds on macOS, ticks on Linux."""
    parts = birth.split(".")
    return tuple(int(part) for part in parts) if all(part.isdigit() for part in parts) else None


def _born_before(birth: str, reference: str) -> bool:
    first, second = _birth_order(birth), _birth_order(reference)
    return first is not None and second is not None and len(first) == len(second) and first < second


def session_reused_at(session: int | None, born: str) -> str | None:
    """The birth of a later process that now holds the child's pid, the child's session id.

    That process may lead a new, unrelated session with the same id; a member
    born after it may belong to that one instead.
    """
    if session is None or session <= 1:
        return None
    record = process_record(session)
    return record[2] if record is not None and record[2] != born else None


def _in_session(pid: int, session: int | None, birth: str | None = None,
                reused_at: str | None = None) -> bool:
    """Whether this process is still in the child's session (whose id is the child's pid).

    When that pid now belongs to a later process (`reused_at` is its birth), only
    a member born before it can be in the child's session.
    """
    if session is None or session <= 1:
        return False
    if reused_at is not None and (birth is None or not _born_before(birth, reused_at)):
        return False
    try:
        return os.getsid(pid) == session
    except OSError:
        return False  # Gone since it was listed, or not readable: its record decides.


def _subreaper(pid: int) -> bool:
    """Linux: a per-user service manager adopts orphans in place of init."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
            return handle.read(64).strip() == b"systemd"
    except OSError:
        return True  # Cannot tell: treated as the adopter, so never as proof.


def _judged_by_parents(record: Record, entry: bytes, born: str, session: int | None,
                       table: Table, reused_at: str | None = None) -> bool | None:
    """An unreadable process born since the child, judged by the parent its environment came from.

    Its listed parents are walked up (at most MAX_PARENT_DEPTH): the first one
    born before the child, or readable, decides, as does one in the child's
    session. Each parent must still be the listed process, born no later than
    its child, so a reused pid cannot vouch for it. Reaching launchd/init (or a
    Linux subreaper), an unlisted parent, or the bound while every step was
    unreadable is not proof: that is how a detached escapee looks.
    """
    child = record
    for _ in range(MAX_PARENT_DEPTH):
        parent = child[0]
        if parent <= 1 or parent not in table or _excluded_pid(parent) or _subreaper(parent):
            return None
        found = process_record(parent)
        if found is None or found[3] or _born_before(child[2], found[2]):
            return None
        if _born_before(found[2], born):
            return False  # Born before the child: it never had the marker to pass on.
        if _in_session(parent, session, found[2], reused_at):
            return True
        arguments = process_arguments(parent)
        if arguments is not None:
            return entry in arguments[1]
        child = found
    return None


def _carries_tag(pid: int, entry: bytes, born: str, session: int | None = None,
                 table: Table | None = None, reused_at: str | None = None) -> bool | None:
    """Whether this pid is a live process born since `born` with `entry` in its environment.

    With `session`, a live process still in that session counts too. One whose
    environment cannot be read (a setuid `login`, say) is judged by its parents.

    macOS withholds the environment of its own signed tools (an empty list
    here), which therefore never match: the documented limit.
    """
    record = process_record(pid)
    if record is None:
        return False if process_gone(pid) else None
    if record[3] or _born_before(record[2], born):
        # An exited process runs nothing; one born before the child cannot have inherited the tag.
        return False
    if _in_session(pid, session, record[2], reused_at):
        return True
    arguments = process_arguments(pid)
    if arguments is not None:
        return entry in arguments[1]
    after = process_record(pid)
    if after is None and process_gone(pid):
        return False
    if after is not None and (after[2] != record[2] or after[3]):
        return False  # It exited, or its pid passed to another process, while being read.
    return None if table is None else _judged_by_parents(record, entry, born, session, table, reused_at)


# A check's answer for a process that ended before it was read (reaped, or a zombie).
GONE = "gone"


def _vanished_candidates(new: list[int], table: Table, results: dict) -> set[int]:
    """The processes of this listing that ended unread and could have passed the marker on.

    The environment follows parentage, so one that ended unread could have carried
    the marker only if its listed parent could: launchd/init or a Linux subreaper
    (it was already an orphan), a parent whose answer was unknown or that is not
    listed, or another such process. One under a parent read as clean (a compiler
    under `make`, say) could not.
    """
    candidates: set[int] = set()
    grew = True
    while grew:
        grew = False
        for pid in new:
            if pid in candidates or results.get(pid) != GONE:
                continue
            parent = table.get(pid)
            if (parent is None or parent <= 1 or parent not in table or parent in candidates
                    or results.get(parent, False) is None or _subreaper(parent)):
                candidates.add(pid)
                grew = True
    return candidates


def scan_until_stable(listing, check, limit: int | None = None) -> bool | None:
    """Check every listed process, listing again while one that ended unread could have hidden another.

    `listing()` returns {pid: listed parent} (None when unavailable); `check(pid,
    listed)` returns True (found), False, None (cannot tell) or GONE (ended before
    it was read). A process that forks and ends between a listing and its read
    leaves its descendant orphaned and unlisted, so after such a round the next
    listing's new processes are checked too. True as soon as one is found; False
    once a round leaves no such candidate and every answer was clean; None
    otherwise, including candidates still appearing after MAX_SCAN_ROUNDS.
    """
    limit = MAX_TAG_SCAN_PROCESSES if limit is None else limit
    results: dict = {}
    unknown = False
    for _ in range(MAX_SCAN_ROUNDS):
        listed = listing()
        if listed is None:
            return None
        new = sorted(pid for pid in listed if pid not in results)
        if len(results) + len(new) > limit:
            return None
        for pid in new:
            found = check(pid, listed)
            if found is True:
                return True
            results[pid] = found
            unknown = unknown or found is None
        if not _vanished_candidates(new, listed, results):
            return None if unknown else False
    return None


def _tag_scan(tag: str, born: str, session: int | None) -> bool | None:
    entry = run_tag_entry(tag)
    reused_at = session_reused_at(session, born)

    def check(pid, table):
        if _excluded_pid(pid):
            return False
        found = _carries_tag(pid, entry, born, session, table, reused_at)
        if found is False:
            record = process_record(pid)
            if (record is None and process_gone(pid)) or (record is not None and record[3]):
                return GONE  # Ended before or while it was read: its children are orphans now.
        return found

    return scan_until_stable(process_table, check)


def tagged_process_running(tag: str, born: str, session: int | None = None) -> bool | None:
    """Whether a process other than this one still carries the run's marker.

    Only this user's processes born at or after `born` (the child's birth) are
    read; with `session`, one still in that session (the child's) counts too. True when one is found, False only when every one could be read and
    none carries it, None (not proof) otherwise. A process seen with it is looked
    at again a moment later, since one being killed still shows its environment.
    """

    if type(tag) is not str or RUN_TAG.fullmatch(tag) is None or type(born) is not str:
        return None
    for attempt in range(TAG_SCAN_ATTEMPTS):
        if attempt:
            time.sleep(KILL_ROUND_PAUSE_SECONDS)
        found = _tag_scan(tag, born, session)
        if found is False:
            return False
    return found


def tracking_available() -> bool:
    """Whether both the process table and kernel records can be read here."""

    return process_table() is not None and process_record(os.getpid()) is not None


def _excluded_pid(pid: int) -> bool:
    return pid <= 1 or pid == os.getpid()


def _excluded_group(group: int) -> bool:
    return group <= 1 or group == os.getpgrp()


class ProcessTree:
    """One child and every descendant seen so far, each known by pid and birth time."""

    def __init__(self, root: int, run_tag: str | None = None) -> None:
        self.root = root
        # The marker in the child's environment; with it, cleanup is confirmed
        # only once no process still carries it.
        self.run_tag = run_tag
        self.root_birth: str | None = None
        self.tracked: dict[int, str] = {}
        # Until the root's birth time is known nothing is tracked or signalled.
        self.identified = False
        # Unlike the signalling set, cleanup evidence never forgets an
        # unreadable identity. None is not proof that a process has stopped.
        # An identity proven stopped is dropped (the root's stays, as the anchor of the
        # evidence), so only identities not yet proven stopped count toward the limit.
        self._cleanup_seen: dict[tuple[int, str], bool] = {}
        self._cleanup_incomplete = False
        # A process listing failed since the last good one. With a run marker a later good
        # listing, or the marker scan, covers what the gap hid; without one it is permanent.
        self._snapshot_missed = False

    def snapshot_failed(self) -> None:
        """A process listing failed: cleanup is unproven until a later listing succeeds."""
        if self.run_tag is None:
            self._cleanup_incomplete = True
        else:
            self._snapshot_missed = True

    def _retry_snapshot(self) -> bool:
        """After a failed listing, bounded fresh attempts; True once a listing succeeded."""
        for attempt in range(SNAPSHOT_RETRY_ATTEMPTS):
            if attempt:
                time.sleep(SNAPSHOT_RETRY_PAUSE_SECONDS)
            table = process_table()
            if table is not None:
                self.update(table)
                return True
        return False

    def _snapshot_covered(self) -> bool:
        return not self._snapshot_missed or self._retry_snapshot() and not self._snapshot_missed

    def _mark_stopped(self, identity: tuple[int, str]) -> None:
        if identity == (self.root, self.root_birth):
            self._cleanup_seen[identity] = True
        else:
            self._cleanup_seen.pop(identity, None)

    def _remember_cleanup(self, pid: int, birth: str, record: Record | None) -> None:
        identity = (pid, birth)
        if record is not None and (record[2] != birth or record[3]):
            self._mark_stopped(identity)
            return
        if identity not in self._cleanup_seen:
            unproven = sum(1 for stopped in self._cleanup_seen.values() if not stopped)
            if unproven >= MAX_CLEANUP_IDENTITIES:
                self._cleanup_incomplete = True
                return
            self._cleanup_seen[identity] = False
        if record is None and not self._cleanup_seen[identity]:
            # Gone means it exited and its parent reaped it (a child's quick shell command,
            # say); anything else unreadable stays unproven.
            if process_gone(pid):
                self._mark_stopped(identity)
            else:
                self._cleanup_incomplete = True

    def cleanup_after_owned_wait(self, process) -> bool:
        """Positive tracked-tree metadata, not a cleanup credential.

        A registered execution owner must additionally prove that this is its
        actual retained Popen/tree pair. The direct root's owned wait proves
        that root stopped; other generations need positive kernel observations.
        An unreadable/missing record, tracking loss or overflow remains unknown.
        """
        root = (self.root, self.root_birth)
        if (not self.identified or self._cleanup_incomplete or root not in self._cleanup_seen
                or process.pid != self.root or type(process.returncode) is not int
                or not self._snapshot_covered()):
            return False
        for (pid, birth), stopped in tuple(self._cleanup_seen.items()):
            if not stopped and (pid, birth) != root:
                self._remember_cleanup(pid, birth, process_record(pid))
        return not self._cleanup_incomplete and all(
            stopped or identity == root for identity, stopped in self._cleanup_seen.items()
        ) and self._no_tagged_process()

    def _no_tagged_process(self) -> bool:
        """No process outside the tracked tree still carries this run's marker.

        Catches a descendant whose parent exited between two snapshots, so it
        was never adopted: by the marker it inherited, or because it is still
        in the child's session. The child started its session, so its id is
        the child's pid.
        """
        return (self.run_tag is None
                or tagged_process_running(self.run_tag, self.root_birth, self.root) is False)

    def cleanup_confirmed(self) -> bool:
        """Positive evidence that every process this tree ever tracked has stopped.

        The watchdog's form of `cleanup_after_owned_wait`: it does not own the
        root, so the root must also be seen stopped (a zombie, reaped, or its
        pid reborn). An unreadable record, a lost snapshot or overflow is unknown.
        """
        if (not self.identified or self._cleanup_incomplete or (self.root, self.root_birth) not in self._cleanup_seen
                or not self._snapshot_covered()):
            return False
        for (pid, birth), stopped in tuple(self._cleanup_seen.items()):
            if not stopped:
                self._remember_cleanup(pid, birth, process_record(pid))
        return (not self._cleanup_incomplete and all(self._cleanup_seen.values())
                and self._no_tagged_process())

    def identify(self, table: Table | None, birth: str | None = None) -> bool:
        """Record the root, which must be unreaped, and adopt what is already visible.

        With `birth` the root must still be that same process.
        """

        if table is None:
            self.snapshot_failed()
            return False
        record = process_record(self.root)
        if record is None or (birth is not None and record[2] != birth):
            return False
        self.root_birth = record[2]
        self.tracked = {self.root: record[2]}
        self.identified = True
        self._remember_cleanup(self.root, record[2], record)
        self.update(table)
        return True

    def _record(self, pid: int) -> Record | None:
        """The pid's current record, only while it is still the tracked process."""

        birth = self.tracked.get(pid)
        if birth is None or _excluded_pid(pid):
            return None
        record = process_record(pid)
        self._remember_cleanup(pid, birth, record)
        return record if record is not None and record[2] == birth else None

    def update(self, table: Table) -> set[int]:
        """Adopt the children of tracked processes; return the tracked pids still present.

        The listed parent only chooses which records to read. A candidate, even a
        pid tracked before as another process, is adopted only when its own record
        names a tracked parent whose record, read afterwards, is still that process.
        """

        live = {pid for pid in self.tracked if self._record(pid) is not None}
        grew = True
        while grew:
            grew = False
            for pid, listed_parent in table.items():
                if pid in live or listed_parent not in live or _excluded_pid(pid):
                    continue
                record = process_record(pid)
                if record is None and process_gone(pid):
                    continue  # Started and already reaped between the snapshot and this read.
                if record is None or record[0] not in live:
                    # The snapshot put this candidate under an owned parent.
                    # It may have escaped/reparented before this read; an
                    # unreadable identity cannot prove it stopped.
                    self._cleanup_incomplete = True
                    continue
                if self._record(record[0]) is None:
                    self._cleanup_incomplete = True
                    continue
                self.tracked[pid] = record[2]
                self._remember_cleanup(pid, record[2], record)
                live.add(pid)
                grew = True
        # Exited processes are forgotten; their pids may belong to anything later.
        self.tracked = {pid: self.tracked[pid] for pid in live}
        self._snapshot_missed = False  # A listing succeeded and what it showed is adopted.
        return live

    def root_exited(self) -> bool:
        """Whether the unreaped root has exited: a zombie, or no longer the tracked root."""

        record = self._record(self.root)
        return record is None or record[3]

    def terminate(self) -> None:
        """Kill every tracked process in rounds, each verified just before its signal.

        A group is killed only through a verified leader whose record shows it
        leads that group: the root, whose group is its own for life, or another
        tracked process. Nothing is stopped, so nothing is ever left stopped.
        """

        if not self.identified:
            return
        for round_number in range(MAX_KILL_ROUNDS):
            if round_number:
                time.sleep(KILL_ROUND_PAUSE_SECONDS)
            table = process_table()
            if table is not None:
                self.update(table)
            else:
                self.snapshot_failed()
            records = {pid: self._record(pid) for pid in sorted(self.tracked)}
            running = [pid for pid, record in records.items() if record and not record[3]]
            if round_number and not running:
                return
            for group, record in records.items():
                if record is None or record[1] != group or _excluded_group(group):
                    continue
                leader = self._record(group)
                if leader is not None and leader[1] == group:
                    with contextlib.suppress(OSError):
                        os.killpg(group, signal.SIGKILL)
            for pid in running:
                if self._record(pid) is not None:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
            # Without a snapshot nothing new can be adopted.
            if table is None:
                return


def await_ready(stream, seconds: float) -> bool:
    """Whether the watchdog acknowledged on `stream` within `seconds`."""

    received = b""
    deadline = time.monotonic() + seconds
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(received) < len(WATCHDOG_READY):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return False
            chunk = os.read(stream.fileno(), len(WATCHDOG_READY) - len(received))
            if not chunk:
                return False
            received += chunk
    return received == WATCHDOG_READY


def write_cleanup_marker(path: str, bridge_run_id: str, pid: int, birth: str) -> bool:
    """Record, owner-only and atomically, that the watched tree of this child is confirmed empty.

    A project service's launch journal reads it before it settles a run whose
    service was lost. Any failure leaves no marker, which is never evidence.
    """
    directory, name = os.path.split(path)
    body = json.dumps({"version": 1, "bridge_run_id": bridge_run_id, "pid": pid, "birth": birth,
                       "confirmed": True}, sort_keys=True, separators=(",", ":")).encode("ascii")
    temporary = os.path.join(directory, ".%s.%d.tmp" % (name, os.getpid()))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(descriptor, body)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return False
    return True


class _ContainerWatch:
    """C3: the run's container, as the detached watchdog sees it (container mode)."""

    def __init__(self, runtime, name: str, grace: int, cgroup: str | None, parent: int) -> None:
        self.runtime, self.name, self.grace, self.cgroup, self.parent = runtime, name, grace, cgroup, parent

    def parent_alive(self) -> bool:
        # The service that started this watchdog is its parent; once it dies the watchdog is
        # reparented, so its parent pid changes and can never be the service's again.
        return os.getppid() == self.parent

    def stop(self):
        return self.runtime.stop(self.name, grace_seconds=self.grace, cgroup_path=self.cgroup)


_CONTAINER_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}-[0-9a-f]{32}")


def container_watcher(argument: str) -> _ContainerWatch:
    """C3: the container this watchdog stops through the container primitive (`container_runtime`,
    loaded from this file's own folder: the watchdog runs this file as a script). ValueError when
    the bridge's argument is not exactly what `container_launch.watchdog_config` writes."""
    try:
        config = json.loads(argument)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("the container argument is not JSON") from None
    keys = {"project", "prefix", "podman", "global", "name", "grace", "cgroup", "parent"}
    if (type(config) is not dict or config.keys() != keys or type(config["podman"]) is not str
            or not os.path.isabs(config["podman"]) or type(config["global"]) is not list
            or not all(type(item) is str for item in config["global"])
            or type(config["name"]) is not str or _CONTAINER_NAME.fullmatch(config["name"]) is None
            or type(config["grace"]) is not int or not 0 <= config["grace"] <= 3600
            or (config["cgroup"] is not None and type(config["cgroup"]) is not str)
            or type(config["parent"]) is not int or config["parent"] <= 1
            or type(config["project"]) is not str or type(config["prefix"]) is not str):
        raise ValueError("the container argument is invalid")
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "container_runtime.py")
    spec = importlib.util.spec_from_file_location("cocolo_watchdog_container_runtime", path)
    if spec is None or spec.loader is None:
        raise ValueError("the container primitive is missing")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    runner = module.SubprocessPodman(config["podman"], global_args=config["global"])
    runtime = module.ContainerRuntime(config["project"], name_prefix=config["prefix"], runner=runner)
    return _ContainerWatch(runtime, config["name"], config["grace"], config["cgroup"], config["parent"])


def _stop_once(container, seconds: float) -> bool:
    """One stop try, abandoned (False) when it has not answered within `seconds`; True only when gone."""
    answer: list[bool] = []

    def attempt() -> None:
        try:
            answer.append(bool(container.stop().gone))
        except Exception:
            answer.append(False)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(max(seconds, 0.0))
    return bool(answer and answer[0])


def _stop_container(container, budget: float | None = None) -> bool:
    """Stop the container until it is gone: a refused or stop_failed answer, an exception or a try that
    hangs is retried after a back-off, at most WATCHDOG_STOP_ATTEMPTS times and never past the budget
    (default WATCHDOG_STOP_BUDGET_SECONDS plus the container's grace). True once gone; False when the
    bound ends it."""
    if budget is None:
        budget = WATCHDOG_STOP_BUDGET_SECONDS + float(getattr(container, "grace", 0) or 0)
    end = time.monotonic() + budget
    for attempt in range(WATCHDOG_STOP_ATTEMPTS):
        if attempt:
            pause = WATCHDOG_STOP_BACKOFF_SECONDS[min(attempt, len(WATCHDOG_STOP_BACKOFF_SECONDS)) - 1]
            time.sleep(max(min(pause, end - time.monotonic()), 0.0))
        left = end - time.monotonic()
        if left <= 0:
            break
        if _stop_once(container, left):
            return True
    return False


def watch(root: int, birth: str, deadline: float, announce, marker: str | None = None,
          bridge_run_id: str | None = None, run_tag: str | None = None, *, container=None) -> bool:
    """The detached deadline watchdog for one bridged child.

    It verifies the child's identity from the bridge, which keeps the child
    unreaped until `announce` acknowledges, and gives up silently if it cannot.
    It then tracks the tree on its own, so a crash of the bridge still ends it:
    the tree is killed at the absolute monotonic deadline supplied by the bridge,
    or once the child itself has exited. Wall-clock corrections cannot shorten or
    extend that budget. Supported Python versions share this clock across processes.
    With `marker`, once the tree it tracked is confirmed empty (and, with
    `run_tag`, no process still carries the run's marker) it records that there
    before exiting (`write_cleanup_marker`).

    C3, with `container` (container mode: the child streams a container's output): once the
    service that started it has died, the container is stopped through the container primitive
    (SIGTERM, the grace, the kill, removal, gone asked of the OS) before the tree is killed. While
    the service lives it stops its own runs' containers, so the watchdog leaves them to it. Returns
    False only when the container was not proven gone (see WATCHDOG_STOP_BUDGET_SECONDS).
    """

    tree = ProcessTree(root, run_tag)
    give_up = time.monotonic() + WATCHDOG_IDENTIFY_SECONDS
    while not tree.identify(process_table(), birth):
        if time.monotonic() >= give_up:
            return True
        time.sleep(0.1)
    announce()
    while True:
        table = process_table()
        if table is not None:
            tree.update(table)
        orphaned = container is not None and not container.parent_alive()
        if orphaned or tree.root_exited() or time.monotonic() >= deadline:
            stopped = True
            if orphaned:
                stopped = _stop_container(container)  # Nothing records it here: recovery asks the OS again.
            tree.terminate()
            if not stopped:
                # Hand-off: one last bounded try after the tree is gone, and a nonzero exit (the bridge's
                # reap() reads it as a backstop that did not finish) for whatever next asks the OS.
                stopped = _stop_once(container, WATCHDOG_LAST_STOP_SECONDS)
            if marker is not None and bridge_run_id is not None and tree.cleanup_confirmed():
                write_cleanup_marker(marker, bridge_run_id, root, tree.root_birth)
            return stopped
        time.sleep(WATCHDOG_INTERVAL_SECONDS)


def _announce() -> None:
    # A bridge that died before reading this leaves the watchdog to clean up alone.
    with contextlib.suppress(OSError):
        os.write(1, WATCHDOG_READY)
    # Replace the pipe with /dev/null so the bridge sees it close.
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.close(null)


if __name__ == "__main__":
    # root birth deadline [marker bridge_run_id [run_tag]] | root birth deadline --container <json>
    if len(sys.argv) == 6 and sys.argv[4] == "--container":
        # A container it cannot take on is never left unguarded quietly: it exits without
        # acknowledging, so the bridge refuses the run.
        sys.exit(0 if watch(int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), _announce,
                            container=container_watcher(sys.argv[5])) else 1)
    else:
        watch(int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), _announce,
              *(sys.argv[4:7] if len(sys.argv) in (6, 7) else ()))
