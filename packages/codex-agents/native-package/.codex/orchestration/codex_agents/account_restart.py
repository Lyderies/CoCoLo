"""S1-H1c: whether the account restarts itself (PLAN H1c-1 to H1c-6; owner decision 2026-11-20).

A pure decision from the lane notes (H1a), what the ledger says is still running, and the previous
exit record. It restarts only when the account is stuck and nothing is running anywhere, at most
once an hour; when something is running it waits and names what is held.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets

LANES = ('signature', 'claims', 'observations', 'ingress', 'billing', 'probe_worker')
_LANE_KEYS = {'faulted', 'kept', 'busy', 'queued', 'last_failure', 'held_since_ms'}
PROBE_HELD_MS = 10 * 60_000
CHECK_EVERY_SECONDS = 60  # H1c-6: at most once a minute
RESTART_EVERY_MS = 60 * 60_000
RECORD_NAME = 'account-restart.json'  # C4: the last self-restart, owner-only, kept apart from exit records
_CODE = re.compile(r'[a-z][a-z_]{2,63}')


def _checked(notes):
    if type(notes) is not dict or set(notes) != set(LANES):
        return None
    for lane in notes.values():
        if type(lane) is not dict or lane.keys() != _LANE_KEYS:
            return None
    return notes


def _stuck(notes, now_ms):
    """(lane, cause) of the first stuck lane, or None (H1c-1)."""
    for name in LANES:
        lane = notes[name]
        if lane['faulted'] is True:
            failure = lane['last_failure']
            cause = failure.get('cause') if type(failure) is dict else None
            return name, cause if type(cause) is str else 'lane_faulted'
    held = notes['probe_worker']['held_since_ms']
    if type(held) is int and now_ms - held > PROBE_HELD_MS:
        return 'probe_worker', 'probe_worker_held'
    return None


def decide(notes, idle, previous, *, now_ms, mono_ns=None):
    """`ok`, `restart`, `wait` (something is running: `held` names it) or `suppressed` (H1c-5)."""
    notes = _checked(notes)
    stuck = None if notes is None else _stuck(notes, now_ms)
    if stuck is None:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}
    lane, cause = stuck
    idle = idle if type(idle) is dict else {}
    held = {'run_slots': list(idle.get('run_slots') or []), 'probes': list(idle.get('probes') or []),
            # Busy or queued work is still running (H1c-2). O0c M8 (replacing C3): kept work, in any lane,
            # is held unknown work the restart exists to end, so it never blocks one; what C3 guarded
            # against, a child that may still run, is checked by `check` before any restart.
            'lanes': {name: count for name in LANES if (count := notes[name]['busy'] + notes[name]['queued'])}}
    if type(idle) is not dict or 'run_slots' not in idle or 'probes' not in idle:
        held['unknown'] = True  # the ledger could not say: never restart blind
    if any(held.values()):
        return {'action': 'wait', 'lane': lane, 'cause': cause, 'held': held}
    # C4: `previous` is the last self-restart (last_restart), not the latest exit record. D2: history that
    # cannot be read suppresses. D3: either clock saying less than an hour suppresses.
    if previous == 'unreadable':
        return {'action': 'suppressed', 'reason': 'history_unreadable', 'lane': lane, 'cause': cause, 'held': held}
    if type(previous) is dict:
        wall_recent = type(previous.get('at_ms')) is int and now_ms - previous['at_ms'] < RESTART_EVERY_MS
        recorded = previous.get('mono_ns')
        mono_recent = (type(mono_ns) is int and type(recorded) is int and mono_ns >= recorded
                       and mono_ns - recorded < RESTART_EVERY_MS * 1_000_000)
        if wall_recent or mono_recent:
            return {'action': 'suppressed', 'reason': 'recent', 'lane': lane, 'cause': cause, 'held': held}
    return {'action': 'restart', 'lane': lane, 'cause': cause, 'held': held}


def check(read_notes, read_idle, previous, *, now_ms, mono_ns=None, read_children=None):
    """The decision from callables that may fail: notes that cannot be read never restart; a ledger
    that cannot say means wait (H1c-6). Never raises.

    O0c M8-M10: a restart frees every slot the account holds in memory, so it happens only once
    `read_children` (the account host's `unended_children`) says every child its lanes started is
    proven gone. It is asked only when a restart is due; it returns the lanes whose children are not
    (review M3: with each process and why, `held['processes']`), and while any is (or it cannot say)
    the decision is `wait`, naming them in `held['children']`."""
    try:
        notes = read_notes()
    except Exception:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}
    try:
        idle = read_idle()
    except Exception:
        idle = None
    try:
        decision = decide(notes, idle, previous, now_ms=now_ms, mono_ns=mono_ns)
    except Exception:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}
    if decision['action'] != 'restart':
        return decision
    try:
        pending, processes = _children(read_children())
    except Exception:
        pending, processes = ['unknown'], []  # never restart blind
    if pending:
        return {**decision, 'action': 'wait',
                'held': {**decision['held'], 'children': pending, 'processes': processes}}
    return decision


# Why a process a lane started is not proven gone (review M3: each is shown with what to check).
CHILD_REASONS = frozenset({'running', 'not_reaped', 'group_not_empty', 'session_not_empty',
                           'tree_not_confirmed', 'no_handle', 'unchecked'})


def _children(answer):
    """(lanes, processes) from the account host's answer: a list of lanes, or {lane: [{'pid', 'why'}]}.
    Anything else raises, and the decision waits."""
    if type(answer) in (list, tuple):
        if any(name not in LANES for name in answer):
            raise ValueError()
        return sorted(set(answer)), []
    if type(answer) is not dict or any(name not in LANES or type(items) is not list for name, items in answer.items()):
        raise ValueError()
    processes = []
    for name, items in answer.items():
        for item in items:
            if (type(item) is not dict or set(item) != {'pid', 'why'} or item['why'] not in CHILD_REASONS
                    or not (item['pid'] is None or (type(item['pid']) is int and item['pid'] > 1))):
                raise ValueError()
            processes.append({'lane': name, 'pid': item['pid'], 'why': item['why']})
    processes.sort(key=lambda item: (item['lane'], -1 if item['pid'] is None else item['pid'], item['why']))
    return sorted(name for name, items in answer.items() if items), processes[:16]


def record_restart(state_root, *, lane, cause, now_ms, mono_ns) -> bool:
    """C4: keep this self-restart (time, lane, fixed cause) durably before stopping; False if it could
    not be kept, and then the restart does not happen."""
    try:
        if not (type(lane) is str and lane in LANES and type(cause) is str and _CODE.fullmatch(cause)
                and type(now_ms) is int and type(mono_ns) is int):
            return False
        target = Path(state_root) / RECORD_NAME
        temporary = target.with_name(f'.{target.name}.{os.getpid()}.{secrets.token_hex(8)}')
        data = json.dumps({'at_ms': now_ms, 'mono_ns': mono_ns, 'lane': lane, 'cause': cause}, sort_keys=True).encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            try:
                folder = os.open(target.parent, os.O_RDONLY)  # D4: the rename itself is durable
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            except BaseException:
                try:
                    target.unlink()  # E2: not durable, so not a restart: never reported as one
                except OSError:
                    pass
                raise
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        return True
    except Exception:
        return False


def last_restart(state_root):
    """C4/C5: the last self-restart kept; None when there is none; 'unreadable' when one exists but cannot
    be read or checked (D2: that suppresses a restart). Never raises."""
    try:
        path = Path(state_root) / RECORD_NAME
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            return 'unreadable'
        with open(path, 'rb') as handle:  # E3: at most 4 KiB is read
            raw = handle.read(4097)
        if len(raw) > 4096:
            return 'unreadable'
        record = json.loads(raw)
        if (type(record) is dict and set(record) == {'at_ms', 'mono_ns', 'lane', 'cause'}
                and type(record['at_ms']) is int and type(record['mono_ns']) is int
                and record['lane'] in LANES and type(record['cause']) is str and _CODE.fullmatch(record['cause'])):
            return record
    except Exception:
        pass
    return 'unreadable'
