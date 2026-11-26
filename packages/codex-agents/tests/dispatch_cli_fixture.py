"""Fresh CLI with database/takeover traps; native enrollment is synthetic."""
import builtins
import contextlib
import io
import os
from pathlib import Path
import runpy
import sqlite3
import sys
import types
from unittest import mock

_CALLER_ENV = {name: os.environ.get(name) for name in
              ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID',
               'CLAUDE_CODE_USE_BEDROCK', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY')}
from tests.support import ORCHESTRATION_ROOT  # noqa: E402 -- capture markers before test bootstrap changes them.


# TESTSPEED-3: a fixture with no collector and no project service makes the CLI start both, best effort, and
# both starts always end at once (the collector with status 2, the service without serving), each after a
# full isolated interpreter start of about a second. A test that opts in (this variable) gets those two spawns
# replaced by an immediate end; the product code around them (the wait, the health counter, the refusal) still
# runs. Real starts keep their own tests (test_project_start, test_learning_cli, test_host_commands).
NO_SERVICE_SPAWN = 'COCOLO_TEST_ENDED_SERVICE_STARTS'
# S10 HM-6 (decision 363): a new run's open asks the project host's run driver `run.check` and fails closed without
# one. A fixture with no run driver opts in (this variable) to a protocol-valid `ready` answer for that one
# operation; the real `_run_driver` and its reply validation still run, and every other driver call still fails.
RUN_CHECK_READY = 'COCOLO_TEST_RUN_CHECK_READY'


class _EndedProcess:
    returncode = 2

    def poll(self):
        return 2


def _ended_service_starts(stack):
    import subprocess
    from codex_agents import collector_runtime, project_host_client
    real_popen, real_spawn = subprocess.Popen, os.posix_spawn

    def popen(argv, *args, **kwargs):
        return _EndedProcess() if 'learning-serve' in argv else real_popen(argv, *args, **kwargs)

    def spawn(path, argv, env, **kwargs):
        # The host's own start becomes a process that ends at once; its file actions (report file) are kept.
        return real_spawn(path, [path, '-I', '-S', '-B', '-c', 'pass'], env, **kwargs)

    class Shim(types.ModuleType):
        def __init__(self, module, **replaced):
            super().__init__(module.__name__)
            self._module, self._replaced = module, replaced

        def __getattr__(self, name):
            return self._replaced[name] if name in self._replaced else getattr(self._module, name)

    stack.enter_context(mock.patch.object(collector_runtime, 'subprocess', new=Shim(subprocess, Popen=popen)))
    stack.enter_context(mock.patch.object(project_host_client, 'os', new=Shim(os, posix_spawn=spawn)))


def _run_check_ready(stack):
    from codex_agents import client_runtime
    from codex_agents.learning_contracts import canonical_json, parse_bounded_json
    real_enroll = client_runtime.enroll_clients

    def enroll(*args, **kwargs):
        endpoints = args[3] if len(args) > 3 else ()
        if 'account' not in kwargs and len(endpoints) == 2 and Path(endpoints[1]).name == 'run.sock':
            return {}
        return real_enroll(*args, **kwargs)

    def transport(path, raw, **options):
        if parse_bounded_json(raw, max_bytes=4_096)['operation'] != 'run.check':
            raise OSError('no run driver in this fixture')
        return canonical_json({'protocol': 1, 'state': 'ready', 'reason_code': None})

    stack.enter_context(mock.patch.object(client_runtime, 'enroll_clients', new=enroll))
    stack.enter_context(mock.patch.object(client_runtime, 'exchange_frame', new=transport))


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
        # The fixture's routes are synthetic: the one supplied stands for the installed
        # route, so these tests reach the client boundary they are about.
        json_input = namespace['main'].__globals__['_json_input']
        namespace['main'].__globals__['_installed_route'] = lambda args, task: json_input(args.route)
        namespace['main'].__globals__['CollectorMaintenance'].acquire = forbidden
        from codex_agents import bridge, providers
        with contextlib.ExitStack() as stack:
            if os.environ.get(NO_SERVICE_SPAWN) == '1':
                _ended_service_starts(stack)
            if os.environ.get(RUN_CHECK_READY) == '1':
                _run_check_ready(stack)
            stack.enter_context(mock.patch.object(providers, 'observe_codex', new=forbidden))
            stack.enter_context(mock.patch.object(providers, 'observe_claude', new=forbidden))
            stack.enter_context(mock.patch.object(bridge, 'claude_billing_probe', new=forbidden))
            stack.enter_context(mock.patch.object(bridge, 'run_child', new=forbidden))
            return namespace['main']()



if __name__ == '__main__':
    sys.exit(main())
