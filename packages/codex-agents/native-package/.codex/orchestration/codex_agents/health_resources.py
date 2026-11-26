"""HM-6 (S10): free disk space and memory pressure.

New runs are refused when free space on the workspace root or on the state folder is below the larger of
`disk_free_bytes` and `disk_free_percent` of that disk (defaults 2 GiB and 5%, owner decision 340). Memory pressure is
only reported. All limits come from the one shared user-settings file (`user_settings`). A disk probe that fails
refuses (fail safe: a run that might fill the disk is worse than a short pause). The VM disk is added in S19.

Probes are injectable; nothing here touches the disk at import.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from . import user_settings
from .health_monitor import Check, CheckContext, Finding

CHECK_NAME = 'resources'
GIB = 1024 ** 3
MAX_TEXT = 240
# Called as disk_usage(path); the default is looked up at call time so a test can replace it.
disk_usage = shutil.disk_usage


@dataclass(frozen=True)
class DiskProblem:
    label: str  # 'workspace' or 'state'
    path: str
    free: int | None
    total: int | None
    needed: int | None
    unreadable: bool = False


@dataclass(frozen=True)
class Refusal:
    text: str
    problems: tuple


def needed_bytes(total: int, settings) -> int:
    """The larger of the byte limit and the percentage of the disk (rounded up)."""
    percent = -(-total * settings.disk_free_percent // 100)
    return max(settings.disk_free_bytes, percent)


def _amount(value: int, other: int) -> str:
    text = '%.2f GiB' % (value / GIB)
    if text == '%.2f GiB' % (other / GIB) and value != other:
        return '%d bytes' % value
    return text


def _check_folder(label, path, settings, usage):
    try:
        reading = usage(path)
        total, free = reading[0], reading[2]
        if (any(type(v) is not int for v in (total, free)) or total <= 0 or free < 0 or free > total):
            raise ValueError('unusable reading')
    except Exception:  # The text never repeats what the system said; it can hold private paths.
        return DiskProblem(label, str(path), None, None, None, True)
    needed = needed_bytes(total, settings)
    if free < needed:
        return DiskProblem(label, str(path), free, total, needed)
    return None


def problems(workspace_root, state_root, settings, usage=None) -> tuple:
    usage = usage or disk_usage
    # The collector serves every project and knows no workspace root; each run's own root is checked at registration.
    found = (None if workspace_root is None else _check_folder('workspace', Path(workspace_root), settings, usage),
             _check_folder('state', Path(state_root), settings, usage))
    return tuple(p for p in found if p is not None)


_LIMITS = 'Free some space, or change disk_free_bytes / disk_free_percent in user-settings.json (see docs/configuration.md).'
_SHORT_LIMITS = 'Free space, or change disk_free_bytes or disk_free_percent in user-settings.json.'
_NAMES = {'workspace': 'workspace folder', 'state': 'state folder'}


def _sentence(problem: DiskProblem, *, short=False) -> str:
    name = _NAMES[problem.label]
    path = problem.path
    if short and len(path) > 50:
        path = path[:14] + '...' + path[-33:]
    if problem.unreadable:
        return 'CoCoLo could not read the free space of the disk holding the %s (%s)' % (name, path)
    if short:
        return ('only %s is free on the disk of the %s (%s); %s is needed'
                % (_amount(problem.free, problem.needed), name, path, _amount(problem.needed, problem.free)))
    return ('only %s is free on the disk holding the %s (%s) and at least %s is needed'
            % (_amount(problem.free, problem.needed), name, path, _amount(problem.needed, problem.free)))


def refusal(workspace_root, state_root, settings, usage=None) -> Refusal | None:
    """None when new runs may start; else a plain reason naming each low folder."""
    found = problems(workspace_root, state_root, settings, usage)
    if not found:
        return None
    body = '; and '.join(_sentence(p) for p in found)
    return Refusal('No new run was started: %s. %s' % (body, _LIMITS), found)


def read_memory():
    """(total, available) bytes from /proc/meminfo, or None when this machine does not offer it."""
    try:
        values = {}
        with open('/proc/meminfo', 'rb') as stream:
            for line in stream.read(65536).decode('ascii', 'replace').splitlines()[:64]:
                name, _, rest = line.partition(':')
                if name in ('MemTotal', 'MemAvailable'):
                    values[name] = int(rest.split()[0]) * 1024
        return values['MemTotal'], values['MemAvailable']
    except (OSError, KeyError, ValueError, IndexError):
        return None


def memory_pressure(settings, probe=None) -> tuple | None:
    """(available, total) when available memory is below the limit; None when fine or unknown. Never refuses."""
    try:
        reading = (probe or read_memory)()
        if reading is None:
            return None
        total, available = reading
        if type(total) is not int or type(available) is not int or total <= 0 or not 0 <= available <= total:
            return None
    except Exception:
        return None
    return (available, total) if available * 100 < total * settings.memory_available_percent else None


class ResourceCheck(Check):
    """Reports low or unreadable disks and memory pressure to the status warning list; fixes nothing."""

    name = CHECK_NAME

    def __init__(self, workspace_root, state_root, *, settings: Callable | None = None, usage=None, memory=None):
        self.workspace_root = None if workspace_root is None else Path(workspace_root)
        self.state_root = Path(state_root)
        self._settings = settings or (lambda: user_settings.load(self.state_root))
        self._usage, self._memory = usage, memory

    def run(self, context: CheckContext) -> Sequence[Finding]:
        settings = self._settings()
        found = []
        for problem in problems(self.workspace_root, self.state_root, settings, self._usage):
            code = ('disk_unreadable_' if problem.unreadable else 'disk_low_') + problem.label
            text = 'New runs are refused: %s. %s' % (_sentence(problem, short=True), _SHORT_LIMITS)
            found.append(Finding(CHECK_NAME, 'machine', problem.label, 'reported', code, text[:MAX_TEXT]))
        low = memory_pressure(settings, self._memory)
        if low is not None:
            text = ('This computer is short of memory (%.1f of %.1f GiB available). Runs are not stopped, '
                    'but they may be slow.' % (low[0] / GIB, low[1] / GIB))
            found.append(Finding(CHECK_NAME, 'machine', 'memory', 'reported', 'memory_pressure', text))
        return found


def register(monitor, state, workspace_root=None, **options):
    """Wiring (CollectorServer.__init__): health_resources.register(monitor, state); no workspace root there."""
    check = ResourceCheck(workspace_root, Path(state.root), **options)
    monitor.register(check)
    return check
