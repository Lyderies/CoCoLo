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
from .account_api import ACCOUNT_REASON_CODES, AccountClient, AccountError, _ID as _ACCOUNT_ID
from .account_open_contracts import AccountOpenInput, OpenError
from .collector_paths import CollectorPaths, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .control_api import ControlClient, ControlScope
from .contracts import effective_dispatch_bounds, effective_dispatch_limits
from .admission_metadata import api_keys_present
from .dispatch_api import DISPATCH_REASONS, DispatchClient, DispatchError
from .history_api import HistoryClient
from .history_contracts import HISTORY_REASON_CODES, AttemptBinding, HistoryError, _ID, matches
from .host_result_contracts import HostResultReference
from .hosts import HOST_CLIENTS, HostClientMismatch
from .learning_contracts import ADMISSION_REASON_CODES, canonical_json, content_digest, keyed_digest, parse_bounded_json
from .billing_probe_contracts import BILLING_PROBE_REASONS, PROBE_COMPLETION_REASONS
from .open_scope_contracts import open_inputs_digest
from .orchestration_api import OrchestrationClient, OrchestrationRequest, OrchestrationScope
from .provider_observation_contracts import ObservationError, ObservationInput
from .result_contracts import MAX_RESULT_BODY_BYTES
from .result_read_api import ResultReadClient
from .service_protocol import ServiceTarget


def runtime_hint_path(state, project_root):
    if type(state) is not CollectorPaths:
        raise DispatchError('dispatch_unavailable')
    root = _canonical_project_root(project_root)
    return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.json')


def _read_runtime_endpoints(state, project_root, *, account=False, account_only=False):
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
            if (name == 'account_endpoint' and not account) or (account_only and name != 'account_endpoint'):
                endpoints.append(endpoint)
                continue  # Only endpoints used by this operation must be live.
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


class RuntimeObservationError(RuntimeError):
    def __init__(self, reason, *, request_id=None):
        self.reason_code = reason if type(reason) is str and reason in ACCOUNT_REASON_CODES else 'observation_unavailable'
        self.request_id = request_id if matches(request_id, _ACCOUNT_ID) else None
        super().__init__(self.reason_code)


class RuntimeBillingProbeError(RuntimeError):
    def __init__(self, reason, *, request_id=None):
        allowed = ACCOUNT_REASON_CODES | BILLING_PROBE_REASONS | PROBE_COMPLETION_REASONS | ADMISSION_REASON_CODES
        self.reason_code = reason if type(reason) is str and reason in allowed else 'probe_unavailable'
        self.request_id = request_id if matches(request_id, _ACCOUNT_ID) else None
        super().__init__(self.reason_code)


def billing_probe_client(state_root, project_root, *, request_id=None):
    """One fixed account probe; retain the ID for explicit lost-reply recovery."""
    identity = 'cli-billing-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ACCOUNT_ID):
        raise RuntimeBillingProbeError('probe_invalid_request')
    if api_keys_present(os.environ):
        raise RuntimeBillingProbeError('provider_api_key_present', request_id=identity)
    try:
        scope, endpoints = _read_runtime_endpoints(CollectorPaths(state_root), project_root,
                                                   account=True, account_only=True)
        if len(endpoints) != 3:
            raise RuntimeBillingProbeError('probe_unavailable', request_id=identity)
        result = AccountClient(endpoints[2], scope.project_id, scope.runtime_digest, timeout=60).billing_probe(
            request_id=identity)
    except DispatchError:
        raise RuntimeBillingProbeError('probe_unavailable', request_id=identity) from None
    except AccountError as error:
        raise RuntimeBillingProbeError(error.reason_code, request_id=identity) from None
    if result['probe'] == 'denied' or result['reason_code'] is not None:
        raise RuntimeBillingProbeError(result['reason_code'], request_id=identity)
    return {'ready': True, **result, 'request_id': identity,
        'probe': 'launched' if result['probe'] == 'completed' else result['probe'],
        'records': [{**{k: v for k, v in record.items() if k != 'client_path'},
            **({'paid_usage_waived_by_attestation': result['billing_waivers'][record['record_id']]}
                if record['record_type'] == 'provider_billing' and 'billing_waivers' in result else {})}
            for record in result['records']]}


def observe_provider_client(state_root, project_root, provider, *, request_id=None, run_handle=None, phase=None):
    """One non-spending account request; no local probe, persistence or retry."""
    identity = 'cli-observe-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ACCOUNT_ID):
        raise RuntimeObservationError('observation_invalid_request')
    try:
        request = ObservationInput(provider, run_handle, phase)
        scope, endpoints = _read_runtime_endpoints(CollectorPaths(state_root), project_root,
                                                   account=True, account_only=True)
        if len(endpoints) != 3:
            raise RuntimeObservationError('observation_unavailable', request_id=identity)
        result = AccountClient(endpoints[2], scope.project_id, scope.runtime_digest, timeout=60).observe_provider(
            request.provider, request_id=identity, run_handle=request.run_handle, phase=request.phase)
    except DispatchError:
        raise RuntimeObservationError('observation_unavailable', request_id=identity) from None
    except (AccountError, ObservationError) as error:
        raise RuntimeObservationError(error.reason_code, request_id=identity) from None
    # Provider identity may include its local executable path in the owner
    # journal. The existing CLI projection must never print that path.
    return {'ready': True, **result, 'request_id': identity,
            'records': [{**{key: value for key, value in record.items() if key != 'client_path'},
                         **({'paid_usage_waived_by_attestation': result['billing_waivers'][record['record_id']]}
                            if record['record_type'] == 'provider_billing' and 'billing_waivers' in result else {})}
                        for record in result['records']]}


class RuntimeResultError(RuntimeError):
    def __init__(self, reason):
        allowed = HISTORY_REASON_CODES | DISPATCH_REASONS | {'result_client_unavailable', 'host_client_mismatch'}
        self.reason_code = reason if type(reason) is str and reason in allowed else 'result_client_unavailable'
        super().__init__(self.reason_code)


def record_codex_result_client(state_root, project_root, task, route, assignment, parent_grant, result, observation,
                               *, host_client):
    """Admit only the original published terminal; never transmit a replacement body."""
    try:
        if (type(route) is not dict or route.get('provider') != 'codex' or route.get('dispatch') != 'bridge'
                or type(observation) is not dict
                or observation.keys() - {'run_handle', 'result_attempts', 'terminal_state'}
                or observation.get('result_attempts') != [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]):
            raise HistoryError('history_invalid_input')
        submitted = canonical_json(result)
        if not 0 < len(submitted) <= MAX_RESULT_BODY_BYTES:
            raise HistoryError('history_invalid_input')
        inputs = open_inputs_digest(assignment, task, route, parent_grant)
        scope, endpoint, result_endpoint = read_runtime_hint(CollectorPaths(state_root), project_root)
        view = DispatchClient(endpoint, result_endpoint, scope, timeout=10).call('dispatch.status',
            observation.get('run_handle'), assignment.get('attempt'), inputs)
        if view['state'] != 'published':
            raise HistoryError('history_result_not_ready')
        binding = AttemptBinding(**view['binding'])
        current = OrchestrationClient(result_endpoint, OrchestrationScope(**vars(scope)), timeout=10).call(
            'orchestration.status', {'orchestration_handle': binding.orchestration_handle})
        if current['current']['snapshot']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        delivery = view['delivery']
        reference = HostResultReference(binding, delivery['record_id'], delivery['record_content_digest'], delivery['result_digest'])
        history = HistoryClient(result_endpoint, scope.project_id, scope.runtime_digest, timeout=10)
        terminal = history.read_record(reference.terminal_record_id)
        record, metadata = terminal['record'], terminal['metadata']
        if (metadata['source'] != 'host' or metadata['content_digest'] != reference.terminal_content_digest
                or record['result_digest'] != reference.result_digest
                or record['bridge_run_id'] != binding.claim_id
                or observation.get('terminal_state') != record['terminal_state']):
            raise HistoryError('history_conflict')
        body = ResultReadClient(result_endpoint, timeout=10).read_result(binding, reference.result_digest)
        if submitted != canonical_json(parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)):
            raise HistoryError('history_conflict')
        # Only this durable reference crosses the mutation boundary. The writer
        # rechecks original inputs, result, current authority and expiry itself.
        admitted = history.admit_host_result(reference)
        records = {}
        for kind in ('event', 'outcome'):
            commit = admitted[kind+'_commit']
            entry = history.read_record(commit['record_id'])
            if (entry['metadata'] != commit or entry['record']['record_type'] != kind
                    or entry['record']['run_id'] != binding.run_id):
                raise HistoryError('history_conflict')
            records[kind] = entry['record']
        return {'ready': True, **admitted, **records,
                'host_outcome_digest': admitted['admission']['host_outcome_digest'], 'capacity_state': 'retained'}
    except (DispatchError, HistoryError, HostClientMismatch) as error:
        raise RuntimeResultError(error.reason_code) from None
    except Exception:
        raise RuntimeResultError('result_client_unavailable') from None


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
