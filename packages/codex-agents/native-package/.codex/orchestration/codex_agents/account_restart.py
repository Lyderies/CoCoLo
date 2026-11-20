"""S1-H1c: whether the account restarts itself (PLAN H1c-1 to H1c-6; owner decision 2026-11-20).

A pure decision from the lane notes (H1a), what the ledger says is still running, and the previous
exit record. It restarts only when the account is stuck and nothing is running anywhere, at most
once an hour; when something is running it waits and names what is held.
"""
from __future__ import annotations

LANES = ('signature', 'claims', 'observations', 'ingress', 'billing', 'probe_worker')
_LANE_KEYS = {'faulted', 'kept', 'busy', 'queued', 'last_failure', 'held_since_ms'}
PROBE_HELD_MS = 10 * 60_000
CHECK_EVERY_SECONDS = 60  # H1c-6: at most once a minute
RESTART_EVERY_MS = 60 * 60_000


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
            'lanes': {name: notes[name]['busy'] + notes[name]['queued'] for name in LANES
                      if notes[name]['busy'] + notes[name]['queued']}}
    if type(idle) is not dict or 'run_slots' not in idle or 'probes' not in idle:
        held['unknown'] = True  # the ledger could not say: never restart blind
    if any(held.values()):
        return {'action': 'wait', 'lane': lane, 'cause': cause, 'held': held}
    if (type(previous) is dict and previous.get('cause') == 'account_restart'
            and type(previous.get('stopped_at_ms')) is int and now_ms - previous['stopped_at_ms'] < RESTART_EVERY_MS):
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
