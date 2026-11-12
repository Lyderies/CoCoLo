"""Fresh CLI with database/takeover traps; native enrollment is synthetic."""
import builtins
import io
import os
from pathlib import Path
import runpy
import sqlite3
import sys
from unittest import mock

_CALLER_ENV = {name: os.environ.get(name) for name in
              ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_USE_BEDROCK')}
from tests.support import ORCHESTRATION_ROOT  # noqa: E402 -- capture markers before test bootstrap changes them.


def main():
    # tests.support selects a default host for in-process tests. This fresh CLI
    # must instead exercise the markers explicitly supplied by its caller.
    for name, value in _CALLER_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    def forbidden(*args, **kwargs):
        raise AssertionError('normal Codex client touched persistence')
    database_names = {'learning.sqlite', 'learning.sqlite3', 'ledger.sqlite', 'history.sqlite', 'history.sqlite3'}
    def checked(function):
        def invoke(path, *args, **kwargs):
            name = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ''
            basename = Path(name).name
            if any(basename == value or basename.startswith(value+'-') for value in database_names):
                forbidden()
            return function(path, *args, **kwargs)
        return invoke
    with mock.patch.object(sqlite3, 'connect', new=forbidden), \
            mock.patch.object(os, 'stat', new=checked(os.stat)), \
            mock.patch.object(os, 'lstat', new=checked(os.lstat)), \
            mock.patch.object(os, 'open', new=checked(os.open)), \
            mock.patch.object(builtins, 'open', new=checked(builtins.open)), \
            mock.patch.object(io, 'open', new=checked(io.open)):
        namespace = runpy.run_path(str(ORCHESTRATION_ROOT / 'cli.py'), run_name='dispatch_cli_guard_fixture')
        # Deliberate fake enrollment isolates the client/database boundary.
        # No claim of a reviewed native caller or protected-image bootstrap.
        namespace['main'].__globals__['_require_protected_runtime'] = lambda args: None
        namespace['main'].__globals__['_require_own_roots'] = lambda args: None
        namespace['main'].__globals__['_learning_ledger'] = forbidden
        namespace['main'].__globals__['CollectorMaintenance'].acquire = forbidden
        from codex_agents import providers
        with mock.patch.object(providers, 'observe_codex', new=forbidden), \
                mock.patch.object(providers, 'observe_claude', new=forbidden):
            return namespace['main']()


if __name__ == '__main__':
    sys.exit(main())
