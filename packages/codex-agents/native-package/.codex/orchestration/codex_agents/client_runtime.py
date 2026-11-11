"""Read existing project-runtime routing hints and use bounded service clients.

These private metadata files do not authenticate a native peer or confer launch
authority. Reviewed native bootstrap must supply/authorize the endpoints; this
client never creates state, starts a runtime or falls back to a database.
"""
import os
from pathlib import Path
import signal
import stat
import threading

from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .collector_paths import CollectorPaths, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .control_api import ControlClient, ControlScope
from .contracts import effective_dispatch_bounds, effective_dispatch_limits
from .admission_metadata import api_keys_present
from .dispatch_api import DispatchClient, DispatchError
from .learning_contracts import keyed_digest, parse_bounded_json
from .open_scope_contracts import open_inputs_digest
from .service_protocol import ServiceTarget


def runtime_hint_path(state, project_root):
    if type(state) is not CollectorPaths:
        raise DispatchError('dispatch_unavailable')
    root = _canonical_project_root(project_root)
    return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.json')


def read_runtime_hint(state, project_root):
    descriptor = None
    try:
        root = _canonical_project_root(project_root)
        path = runtime_hint_path(state, root)
        _assert_no_symlink_components(path)
        _check_owner_mode(path.parent, directory=True)
        _check_owner_mode(path, directory=False)
        expected = path.lstat()
        if not 0 < expected.st_size <= 8192 or not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_NONBLOCK', 0):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
                or (hasattr(os, 'getuid') and opened.st_uid != os.getuid())
                or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError()
        raw = os.read(descriptor, 8193)
        after, current = os.fstat(descriptor), path.lstat()
        if (len(raw) != opened.st_size or after.st_size != opened.st_size
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError()
        value = parse_bounded_json(raw, max_bytes=8192)
        if (value.keys() != {'schema_version', 'project_root_digest', 'scope', 'dispatch_endpoint', 'result_endpoint'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['project_root_digest'] != keyed_digest(state.digest_key(), str(root))
                or type(value['scope']) is not dict):
            raise ValueError()
        scope = ControlScope(**value['scope'])
        if scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST:
            raise ValueError()
        endpoints = []
        for name in ('dispatch_endpoint', 'result_endpoint'):
            endpoint = value[name]
            if (type(endpoint) is not str or not 0 < len(endpoint) <= 4096 or '\0' in endpoint
                    or not Path(endpoint).is_absolute() or '..' in Path(endpoint).parts):
                raise ValueError()
            endpoint = Path(endpoint)
            _assert_no_symlink_components(endpoint)
            if not endpoint.is_relative_to(state.root):
                raise ValueError()
            actual = endpoint.lstat()
            if (not stat.S_ISSOCK(actual.st_mode) or actual.st_nlink != 1 or stat.S_IMODE(actual.st_mode) != 0o600
                    or (hasattr(os, 'getuid') and actual.st_uid != os.getuid())):
                raise ValueError()
            endpoints.append(endpoint)
        return scope, endpoints[0], endpoints[1]
    except Exception:
        raise DispatchError('dispatch_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def dispatch_codex_client(state_root, project_root, run_handle, task, route, assignment, parent_grant, *, expected_export_commit=None):
    """Actual CLI path: submit once, poll metadata, read original result chunks."""
    if (route.get('provider') != 'codex' or route.get('dispatch') != 'bridge'
            or route.get('sandbox_mode') != 'read-only' or assignment.get('permissions', {}).get('network') is not False
            or any(not p.endswith(':read') for p in assignment.get('permissions', {}).get('filesystem', []))
            or api_keys_present(os.environ)):
        raise DispatchError('dispatch_not_authorized')
    state = CollectorPaths(state_root)
    scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
    client = DispatchClient(endpoint, result_endpoint, scope, timeout=2)
    seconds, _ = effective_dispatch_limits(route, assignment)
    old = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGINT, signal.SIGTERM):
            old[signum] = signal.signal(signum, interrupted)
    try:
        result = client.dispatch(run_handle, assignment['attempt'], open_inputs_digest(assignment, task, route, parent_grant),
                                 wait_seconds=seconds+60, expected_export_commit=expected_export_commit)
        if result['bridge_report']['requested_model'] != assignment['model'] or result['bridge_report']['effort'] != assignment['effort']:
            raise DispatchError('dispatch_invalid_response')
        result['bridge_report']['effective_bounds'] = effective_dispatch_bounds(route, assignment)
        return result
    except KeyboardInterrupt:
        binding = client.last_binding
        if binding is not None:
            try:
                ControlClient(endpoint, scope, timeout=2).cancel(
                    ServiceTarget(binding.orchestration_handle, binding.run_handle, binding.attempt))
            except Exception:
                pass  # A missing cancellation acknowledgement never confirms cleanup.
        raise DispatchError('dispatch_unavailable') from None
    finally:
        for signum, handler in old.items():
            signal.signal(signum, handler)
