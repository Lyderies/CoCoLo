"""Read existing project-runtime routing hints and use bounded service clients.

These private metadata files do not authenticate a native peer or confer launch
authority. Reviewed native bootstrap must supply/authorize the endpoints; this
client never creates state, starts a runtime or falls back to a database.
"""
import os
from pathlib import Path
import signal
import secrets
import stat
import threading

from .collector_client import COLLECTOR_RUNTIME_DIGEST
from .account_api import ACCOUNT_REASON_CODES, AccountClient, AccountError
from .account_open_contracts import AccountOpenInput, OpenError
from .collector_paths import CollectorPaths, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .control_api import ControlClient, ControlScope
from .contracts import effective_dispatch_bounds, effective_dispatch_limits
from .admission_metadata import api_keys_present
from .dispatch_api import DispatchClient, DispatchError
from .history_api import HistoryClient
from .history_contracts import HISTORY_REASON_CODES, HistoryError, _ID, matches
from .hosts import HOST_CLIENTS, HostClientMismatch
from .learning_contracts import content_digest, keyed_digest, parse_bounded_json
from .open_scope_contracts import open_inputs_digest
from .orchestration_api import OrchestrationClient, OrchestrationRequest, OrchestrationScope
from .service_protocol import ServiceTarget


def runtime_hint_path(state, project_root):
    if type(state) is not CollectorPaths:
        raise DispatchError('dispatch_unavailable')
    root = _canonical_project_root(project_root)
    return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.json')


def _read_runtime_endpoints(state, project_root, *, account=False):
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
        fields = {'schema_version', 'project_root_digest', 'scope', 'dispatch_endpoint', 'result_endpoint'}
        version = value.get('schema_version')
        if (type(version) is not int or version not in {1, 2}
                or value.keys() != (fields | {'account_endpoint'} if version == 2 else fields)
                or value['project_root_digest'] != keyed_digest(state.digest_key(), str(root))
                or type(value['scope']) is not dict):
            raise ValueError()
        scope = ControlScope(**value['scope'])
        if scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST:
            raise ValueError()
        endpoints = []
        for name in (('dispatch_endpoint', 'result_endpoint', 'account_endpoint') if version == 2
                     else ('dispatch_endpoint', 'result_endpoint')):
            endpoint = value[name]
            if (type(endpoint) is not str or not 0 < len(endpoint) <= 4096 or '\0' in endpoint
                    or not Path(endpoint).is_absolute() or '..' in Path(endpoint).parts):
                raise ValueError()
            endpoint = Path(endpoint)
            if not endpoint.is_relative_to(state.root):
                raise ValueError()
            if name == 'account_endpoint' and not account:
                endpoints.append(endpoint)
                continue  # An admitted dispatch must not depend on account socket availability.
            _assert_no_symlink_components(endpoint)
            actual = endpoint.lstat()
            if (not stat.S_ISSOCK(actual.st_mode) or actual.st_nlink != 1 or stat.S_IMODE(actual.st_mode) != 0o600
                    or (hasattr(os, 'getuid') and actual.st_uid != os.getuid())):
                raise ValueError()
            endpoints.append(endpoint)
        return scope, endpoints
    except Exception:
        raise DispatchError('dispatch_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_runtime_hint(state, project_root):
    scope, endpoints = _read_runtime_endpoints(state, project_root)
    return scope, endpoints[0], endpoints[1]


class RuntimeOpenError(RuntimeError):
    def __init__(self, reason):
        self.reason_code = reason if type(reason) is str and reason in ACCOUNT_REASON_CODES | HISTORY_REASON_CODES else 'open_unavailable'
        super().__init__(self.reason_code)


class RuntimeOrchestrationError(RuntimeError):
    def __init__(self, reason, *, request_id=None, expected_sequence=None):
        allowed = HISTORY_REASON_CODES | {'orchestration_client_unavailable', 'host_client_mismatch'}
        self.reason_code = reason if type(reason) is str and reason in allowed else 'orchestration_client_unavailable'
        self.request_id = request_id if matches(request_id, _ID) else None
        self.expected_sequence = expected_sequence
        super().__init__(self.reason_code)


def orchestration_client(state_root, project_root, operation, *, host_client, orchestration_handle=None,
                         request_id=None, expected_sequence=None, goal_tokens_used=None, goal_time_used_seconds=None,
                         status=None):
    """One lifecycle mutation, with explicit identity/sequence for reconciliation."""
    identity = 'cli-orc-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ID):
        raise RuntimeOrchestrationError('history_invalid_request')
    captured_sequence = None
    try:
        scope, _, endpoint = read_runtime_hint(CollectorPaths(state_root), project_root)
        actor = OrchestrationScope(**vars(scope))
        client = OrchestrationClient(endpoint, actor, timeout=10)
        if operation == 'orchestration.open':
            payload = {}
        else:
            payload = {'orchestration_handle': orchestration_handle}
            current = client.call('orchestration.status', payload,
                request_id=identity if operation == 'orchestration.status' else 'orc-read-'+content_digest({'request': identity}))
            snapshot = current['current']['snapshot']
            if snapshot['client_version'] != host_client:
                raise HostClientMismatch('host_client_mismatch')
            if operation == 'orchestration.status':
                return {'ready': True, **snapshot, **current, 'request_id': identity}
            payload['expected_sequence'] = snapshot['sequence'] if expected_sequence is None else expected_sequence
            if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                payload.update(goal_tokens_used=goal_tokens_used, goal_time_used_seconds=goal_time_used_seconds)
            if operation == 'orchestration.close':
                payload['status'] = status
        request = OrchestrationRequest(1, operation, identity, actor, payload)
        captured_sequence = request.payload.get('expected_sequence')
        result = client.call(operation, request.payload, request_id=identity)
        snapshot = result['current']['snapshot']
        if snapshot['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
    except DispatchError:
        raise RuntimeOrchestrationError('orchestration_client_unavailable', request_id=identity) from None
    except (HistoryError, HostClientMismatch) as error:
        raise RuntimeOrchestrationError(error.reason_code, request_id=identity,
                                       expected_sequence=captured_sequence) from None
    return {'ready': True, **snapshot, **result, 'request_id': identity,
            **({'expected_sequence': captured_sequence} if captured_sequence is not None else {})}


def open_codex_client(state_root, project_root, orchestration_handle, task, route, assignment, parent_grant, *, host_client):
    """Read original project scope, then open once through the account API.

    Trusted native preparation supplies original inputs and reserves its local
    resources independently. Neither this client nor the returned metadata can
    create a launch ticket or replace that protected preparation.
    """
    if (host_client not in HOST_CLIENTS or route.get('provider') != 'codex' or route.get('dispatch') != 'bridge'
            or route.get('sandbox_mode') != 'read-only' or assignment.get('permissions', {}).get('network') is not False
            or any(not p.endswith(':read') for p in assignment.get('permissions', {}).get('filesystem', []))
            or api_keys_present(os.environ)):
        raise RuntimeOpenError('open_not_authorized')
    try:
        scope, endpoints = _read_runtime_endpoints(CollectorPaths(state_root), project_root, account=True)
        if len(endpoints) != 3:
            raise RuntimeOpenError('open_unavailable')
        prospective = OrchestrationScope(**vars(scope))
        digest = open_inputs_digest(assignment, task, route, parent_grant)
        project_scope = HistoryClient(endpoints[1], scope.project_id, scope.runtime_digest, timeout=10).original_open_scope(
            prospective, orchestration_handle, digest)
        if project_scope['facts']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        opening = AccountOpenInput(prospective, orchestration_handle, digest)
        identity = 'cli-open-' + content_digest(opening.payload())
        reply = AccountClient(endpoints[2], scope.project_id, scope.runtime_digest, timeout=10).open_host_run(
            opening, request_id=identity)
    except DispatchError:
        raise RuntimeOpenError('open_unavailable') from None
    except (AccountError, HistoryError, OpenError) as error:
        raise RuntimeOpenError(error.reason_code) from None
    return {'ready': reply['state'] == 'opened', **reply, 'read_containment': 'advisory'}


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
