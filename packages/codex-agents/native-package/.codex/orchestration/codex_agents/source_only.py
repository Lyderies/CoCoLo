"""COCOLO's own code always loads from source; the standard library may use its installed bytecode.

Product processes start with `-I -B -S -X pycache_prefix=/dev/null`, so nothing a session could
write, such as a `__pycache__` folder in a project, is ever run as bytecode. That prefix also hides
the interpreter's own standard-library bytecode, so every start recompiled the standard library
(about 90 ms of CPU). Each entry script calls `use_installed_stdlib_bytecode` first, before it
imports any other runtime module:

- every module under the runtime root is loaded from its source by a loader that never reads or
  writes bytecode, whatever prefix is set and whatever `__pycache__` or `.pyc` lies there;
- then the prefix is cleared, so modules outside the runtime root (the standard library) load as
  Python normally loads them, from bytecode beside their source in the interpreter's installation,
  which has the same protection as that source and the interpreter itself.

`-B` stays, so nothing is written. This module imports only the standard library, and is itself
loaded under the `/dev/null` prefix, so from source.
"""

from __future__ import annotations

import os
import sys
from importlib.machinery import SOURCE_SUFFIXES, FileFinder, SourceFileLoader


class SourceOnlyLoader(SourceFileLoader):
    """Compiles a module from its source every time; never reads or writes bytecode."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _source_only_hook(root: str):
    """A path hook that claims the runtime root and the folders below it, and nothing else."""

    def hook(path):
        try:
            real = os.path.realpath(path)
        except (OSError, TypeError, ValueError):
            raise ImportError("not the runtime root") from None
        if (real != root and not real.startswith(root + os.sep)) or not os.path.isdir(real):
            # A file (such as `runpy.run_path` of an entry script) is left to the usual hooks.
            raise ImportError("not a folder of the runtime root")
        # Source suffixes only: a bare `.pyc` or an extension module here is not a module.
        return FileFinder(path, (SourceOnlyLoader, SOURCE_SUFFIXES))

    hook.cocolo_source_only_root = root
    return hook


def use_installed_stdlib_bytecode(root) -> None:
    """Load modules under `root` from source only, then let the standard library use its bytecode."""

    real = os.path.realpath(os.fspath(root))
    if not any(getattr(hook, "cocolo_source_only_root", None) == real for hook in sys.path_hooks):
        sys.path_hooks.insert(0, _source_only_hook(real))
    for entry in list(sys.path_importer_cache):
        try:
            resolved = os.path.realpath(entry)
        except (OSError, TypeError, ValueError):
            continue
        if resolved == real or resolved.startswith(real + os.sep):
            del sys.path_importer_cache[entry]
    sys.pycache_prefix = None
