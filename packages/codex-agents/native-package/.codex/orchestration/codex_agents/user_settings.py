"""One bounded, user-editable settings source for S9, S10 and S35.

No persistence access at import. Call load(account_state_root) to reload. All
consumers use this file, typed values, validation and whole-file fallback.
"""
from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import stat

SETTINGS_BYTES = 4096
MAX_SETTING_INTEGER = 2 ** 63 - 1
MIN_BYTES = 1024 ** 2
FILENAME = 'user-settings.json'
WARNING = 'The user settings could not be read; the default limits are in force.'


@dataclass(frozen=True)
class UserSettings:
    store_size_bytes: int = 8 * 1024 ** 3
    store_growth_bytes_per_day: int = 1024 ** 3
    disk_free_bytes: int = 2 * 1024 ** 3
    disk_free_percent: int = 5
    no_merge_warning_ms: int = 90 * 60 * 1000
    memory_available_percent: int = 10
    warning: str | None = None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def load(root, *, path=None):
    """Missing file uses defaults; invalid file returns defaults and plain warning."""
    try:
        fd = os.open(Path(path) if path is not None else Path(root) / FILENAME,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('unavailable settings')
            raw = stream.read(SETTINGS_BYTES + 1)
        if len(raw) > SETTINGS_BYTES:
            raise ValueError('oversized settings')
        data = json.loads(raw, object_pairs_hook=_unique)
        keys = {field.name for field in fields(UserSettings)} - {'warning'}
        if type(data) is not dict or set(data) - keys:
            raise ValueError('unknown settings')
        for key, value in data.items():
            minimum, maximum = MIN_BYTES, MAX_SETTING_INTEGER
            if key in ('disk_free_percent', 'memory_available_percent'):
                minimum, maximum = 1, 99
            elif key == 'no_merge_warning_ms':
                minimum = 60_000
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError('invalid limit')
        return UserSettings(**data)
    except FileNotFoundError:
        return UserSettings()
    except (OSError, ValueError, RecursionError):
        return UserSettings(warning=WARNING)
