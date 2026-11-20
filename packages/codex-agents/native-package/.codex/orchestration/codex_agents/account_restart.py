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


def decide(notes, idle, previous, *, now_ms):
    """`ok`, `restart`, `wait` (something is running: `held` names it) or `suppressed` (H1c-5)."""
    notes = _checked(notes)
    stuck = None if notes is None else _stuck(notes, now_ms)
    if stuck is None:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}
    lane, cause = stuck
    idle = idle if type(idle) is dict else {}
    held = {'run_slots': list(idle.get('run_slots') or []), 'probes': list(idle.get('probes') or []),
            # Kept work is what makes it stuck; busy or queued work is still running (H1c-2).
            # C3: kept work counts too, except the stuck lane's own (and billing's when the probe worker
            # is stuck, since its kept jobs are what hold it).
            'lanes': {name: count for name in LANES
                      if (count := notes[name]['busy'] + notes[name]['queued']
                          + (0 if name == lane or (lane == 'probe_worker' and name == 'billing')
                             else notes[name]['kept']))}}
    if type(idle) is not dict or 'run_slots' not in idle or 'probes' not in idle:
        held['unknown'] = True  # the ledger could not say: never restart blind
    if any(held.values()):
        return {'action': 'wait', 'lane': lane, 'cause': cause, 'held': held}
    # C4: `previous` is the last self-restart (record_restart), not the latest exit record.
    if type(previous) is dict and type(previous.get('at_ms')) is int and now_ms - previous['at_ms'] < RESTART_EVERY_MS:
        return {'action': 'suppressed', 'lane': lane, 'cause': cause, 'held': held}
    return {'action': 'restart', 'lane': lane, 'cause': cause, 'held': held}


def check(read_notes, read_idle, previous, *, now_ms):
    """The decision from callables that may fail: notes that cannot be read never restart; a ledger
    that cannot say means wait (H1c-6). Never raises."""
    try:
        notes = read_notes()
    except Exception:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}
    try:
        idle = read_idle()
    except Exception:
        idle = None
    try:
        return decide(notes, idle, previous, now_ms=now_ms)
    except Exception:
        return {'action': 'ok', 'lane': None, 'cause': None, 'held': None}


def record_restart(state_root, *, lane, cause, now_ms) -> bool:
    """C4: keep this self-restart (time, lane, fixed cause) durably before stopping; False if it could
    not be kept, and then the restart does not happen."""
    try:
        if not (type(lane) is str and lane in LANES and type(cause) is str and _CODE.fullmatch(cause)
                and type(now_ms) is int):
            return False
        target = Path(state_root) / RECORD_NAME
        temporary = target.with_name(f'.{target.name}.{os.getpid()}.{secrets.token_hex(8)}')
        data = json.dumps({'at_ms': now_ms, 'lane': lane, 'cause': cause}, sort_keys=True).encode()
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
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        return True
    except Exception:
        return False


def last_restart(state_root):
    """C4/C5: the last self-restart kept, or None when there is none or it is malformed. Never raises."""
    try:
        path = Path(state_root) / RECORD_NAME
        if path.is_symlink() or not path.is_file():
            return None
        record = json.loads(path.read_bytes()[:4096])
        if (type(record) is dict and set(record) == {'at_ms', 'lane', 'cause'} and type(record['at_ms']) is int
                and record['lane'] in LANES and type(record['cause']) is str and _CODE.fullmatch(record['cause'])):
            return record
    except Exception:
        pass
    return None
