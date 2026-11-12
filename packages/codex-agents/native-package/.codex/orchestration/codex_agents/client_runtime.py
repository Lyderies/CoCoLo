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
import time

from .collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient, empty_ingestion_snapshot
from .account_api import ACCOUNT_REASON_CODES, AccountClient, AccountError, integer as account_integer, _ID as _ACCOUNT_ID
from .account_open_contracts import AccountOpenInput, OpenError
from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .collector_contracts import CollectorError, CollectorTransportError
from .collector_runtime import client_health_snapshot
from .control_api import ControlClient, ControlScope
from .contracts import effective_dispatch_bounds, effective_dispatch_limits
from .admission_metadata import api_keys_present
from .dispatch_api import DISPATCH_REASONS, DispatchClient, DispatchError
from .history_api import HistoryClient
from .history_contracts import HISTORY_REASON_CODES, AttemptBinding, HistoryError, _ID, matches
from .host_result_contracts import HostResultReference
from .hosts import HOST_CLIENTS, HostClientMismatch, HostClientUnknown, detect_host_client
from .learning_contracts import ADMISSION_REASON_CODES, canonical_json, content_digest, keyed_digest, parse_bounded_json
from .billing_probe_contracts import BILLING_PROBE_REASONS, PROBE_COMPLETION_REASONS
from .open_scope_contracts import open_inputs_digest
from .orchestration_api import OrchestrationClient, OrchestrationRequest, OrchestrationScope
from .provider_observation_contracts import ObservationError, ObservationInput
from .result_contracts import MAX_RESULT_BODY_BYTES
from .result_read_api import ResultReadClient
from .service_enrollment import (
    EnrollmentError, account_endpoint_path, enrollment_preamble, proof_directory, read_capability, withdraw_proof,
)
from .service_protocol import ServiceTarget
from .service_transport import exchange_frame, install_enrollment


ACCOUNT_GENERATION = 1  # account_host.GENERATION; that module is a persistence owner.


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


def enroll_clients(state, project_root, scope, endpoints=(), *, account=None, host_client=None):
    """Enroll this checked CLI process for the hinted endpoints it is about to use.

    Each key is read once from the owner-only state root, and each request leaves
    a single-use proof file beside its endpoint. The account preamble also names
    this project's root, which the account owner matches to the registered
    project. A process with no host class, or a missing key, stays unenrolled
    for that endpoint and its host refuses; there is no database fallback.

    Returns a mapping from endpoint to the protected context for its next
    request; each entry is used once.
    """
    if host_client is None:
        try:
            host_client = detect_host_client(os.environ)
        except HostClientUnknown:
            host_client = None
    preambles, contexts = {}, {}
    if host_client is not None:
        try:
            host_key = read_capability(state, project_root, 'host') if endpoints else None
        except EnrollmentError:
            host_key = None
        if host_key is not None:
            for endpoint in endpoints:
                preambles[Path(endpoint)] = ('host', host_key, scope.supervisor_generation, None)
        if account is not None:
            try:
                account_key = read_capability(state, None, 'account')
                root = str(_canonical_project_root(Path(project_root)))
            except (EnrollmentError, StateError, OSError):
                account_key = None
            if account_key is not None:
                preambles[Path(account)] = ('account', account_key, ACCOUNT_GENERATION, root)

    def preamble(socket_path, message):
        entry = preambles.get(Path(socket_path))
        if entry is None:
            return None
        role, key, generation, subject = entry
        proofs = proof_directory(socket_path)
        try:
            line = enrollment_preamble(key, role, host_client, generation, message, proofs,
                                       subject=subject, context=contexts.pop(Path(socket_path), None))
        except EnrollmentError:
            raise OSError() from None
        return line, lambda: withdraw_proof(proofs, line)

    install_enrollment(preamble if preambles else None)
    return contexts


def _account_target(state, project_root):
    """The account endpoint and this project's identifier, with or without a project host.

    A published project hint names both. Without one, the identifier comes from
    the collector's registry lookup and the endpoint is the fixed account path.
    """
    try:
        scope, endpoints = _read_runtime_endpoints(state, project_root, account=True, account_only=True)
        if len(endpoints) == 3:
            return scope.project_id, endpoints[2]
    except DispatchError:
        pass
    try:
        project_id = CollectorClient(state.socket, timeout=2).project(_canonical_project_root(Path(project_root)))
    except (CollectorError, StateError, OSError):
        raise DispatchError('dispatch_unavailable') from None
    return project_id, account_endpoint_path(state)


def _status_ingestion_unavailable():
    value = empty_ingestion_snapshot(registered=False)
    value.update(registered_project_state='unavailable', ingestion_state='unavailable',
                 freshness={'state': 'unavailable', 'age_ms': None,
                            'message': 'Hook delivery is unknown while the collector is unavailable.'})
    return value


def _status_collector_fields(health):
    """Closed display metadata only; a health view grants no execution authority."""
    template = empty_ingestion_snapshot(registered=False)
    ingestion = health['ingestion']
    if (type(ingestion) is not dict or ingestion.keys() != template.keys()
            or ingestion['registered_project_state'] not in {'active', 'unregistered'}
            or ingestion['ingestion_state'] != ('active' if ingestion['registered_project_state'] == 'active' else 'inactive')
            or not account_integer(health['started_at_ms']) or not account_integer(health['schema_version'])):
        raise ValueError()
    for name in ('accepted_hook_event_count', 'rejected_hook_event_count'):
        if not account_integer(ingestion[name]):
            raise ValueError()
    for name in ('last_accepted_hook_event_at_ms', 'last_rejected_hook_event_at_ms'):
        if ingestion[name] is not None and not account_integer(ingestion[name]):
            raise ValueError()
    for name in ('hook_event_counts', 'managed_lifecycle', 'coverage'):
        values = ingestion[name]
        if type(values) is not dict or values.keys() != template[name].keys():
            raise ValueError()
        if any((type(v) is not bool if name == 'coverage' else not account_integer(v)) for v in values.values()):
            raise ValueError()
    freshness = ingestion['freshness']
    messages = {
        'unregistered': 'Project is not registered.',
        'awaiting_hook_review_or_first_event': 'No events since the hooks were installed; open /hooks on the main checkout.',
        'fresh': 'Hooks are delivering events.',
        'idle': 'Hooks have delivered events; no recent activity.',
    }
    if (type(freshness) is not dict or freshness.keys() != {'state', 'age_ms', 'message'}
            or type(freshness['state']) is not str or freshness['state'] not in messages
            or freshness['message'] != messages[freshness['state']]
            or (freshness['state'] in {'fresh', 'idle'} and not account_integer(freshness['age_ms']))
            or (freshness['state'] not in {'fresh', 'idle'} and freshness['age_ms'] is not None)):
        raise ValueError()
    attestations = health['provider_attestations']
    if (type(attestations) is not dict or attestations.keys() != {'codex', 'claude'}
            or any(type(v) is not dict or v.keys() != {'state'} or v['state'] not in
                   {'present', 'missing', 'identity_missing'} for v in attestations.values())):
        raise ValueError()
    counters = health.get('collector_health', {})
    if (type(counters) is not dict or counters.keys() - {'admission_rejected', 'queue_full', 'collector_error', 'conflict'}
            or any(type(v) is not dict or v.keys() != {'count', 'updated_at_ms'}
                   or any(not account_integer(n) for n in v.values()) for v in counters.values())):
        raise ValueError()
    return {'collector_schema_version': health['schema_version'],
            'collector_runtime_version': health['runtime_version'], 'collector_runtime_digest': health['runtime_digest'],
            'started_at_ms': health['started_at_ms'], 'collector_health': counters,
            'ingestion': ingestion, 'provider_attestations': attestations}


def status_client(state_root, project_root):
    """Observe existing collector/account APIs once each; never start or open a DB."""
    report = {'ready': False, 'active': False, 'client_failures': {},
              'ingestion': _status_ingestion_unavailable(),
              'provider_attestations': {p: {'state': 'unavailable'} for p in ('codex', 'claude')},
              'owner_key': {'state': 'unavailable', 'fingerprint': None,
                            'replacements': None, 'last_replaced_at_ms': None}}
    try:
        state = CollectorPaths(state_root)
        report['client_failures'] = client_health_snapshot(state, project_root)
    except (StateError, OSError, ValueError):
        return report | {'reason_code': 'status_runtime_unavailable', 'error': 'Status services are unavailable.'}
    collector_ready = False
    try:
        report.update(_status_collector_fields(CollectorClient(state.socket, timeout=1).health(project_root)))
        report['active'] = collector_ready = True
    except CollectorError as error:
        reason = ('status_collector_transport_unavailable' if isinstance(error, CollectorTransportError)
                  else 'status_collector_unavailable')
        report.update(reason_code=reason, error='Collector status is unavailable.')
        if error.code == 'project_unregistered':
            report['ingestion'] = empty_ingestion_snapshot(registered=False)
    except OSError:
        report.update(reason_code='status_collector_transport_unavailable', error='Collector status is unavailable.')
    except (KeyError, TypeError, ValueError):
        report.update(reason_code='status_collector_unavailable', error='Collector status is unavailable.')
    owner_ready = False
    try:
        project_id, endpoint = _account_target(state, project_root)
        enroll_clients(state, project_root, None, account=endpoint)
        owner = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=1).owner_key()
        report['owner_key'] = {name: owner[name] for name in ('state', 'fingerprint', 'replacements', 'last_replaced_at_ms')}
        owner_ready = True
        if not collector_ready and report['ingestion']['registered_project_state'] == 'unavailable':
            report['ingestion']['registered_project_state'] = 'active'
    except AccountError as error:
        report['account_reason_code'] = error.reason_code
    except (DispatchError, StateError, OSError, ValueError):
        pass
    if not owner_ready and collector_ready:
        report.update(reason_code='status_account_unavailable', error='Account status is unavailable.')
    report['ready'] = collector_ready and owner_ready
    return report


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
        state = CollectorPaths(state_root)
        project_id, endpoint = _account_target(state, project_root)
        enroll_clients(state, project_root, None, account=endpoint)
        result = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=60).billing_probe(
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
        state = CollectorPaths(state_root)
        project_id, endpoint = _account_target(state, project_root)
        enroll_clients(state, project_root, None, account=endpoint)
        result = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=60).observe_provider(
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
        state = CollectorPaths(state_root)
        scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (endpoint, result_endpoint), host_client=host_client)
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
        try:
            # Tell the project host the result is admitted, so it may retire the run's custody.
            contexts = enroll_clients(state, project_root, scope, (endpoint, Path(endpoint).parent / 'run.sock'),
                                      host_client=host_client)
            if (Path(endpoint).parent / 'run.sock').exists():
                _run_driver(contexts, endpoint, 'run.recorded', binding.run_handle, binding.attempt, inputs, {},
                            timeout=5)
        except Exception:
            pass  # The host keeps custody for its grace period instead.
        return {'ready': True, **admitted, **records,
                'host_outcome_digest': admitted['admission']['host_outcome_digest'], 'capacity_state': 'retained'}
    except (DispatchError, HistoryError, HostClientMismatch) as error:
        raise RuntimeResultError(error.reason_code) from None
    except Exception:
        raise RuntimeResultError('result_client_unavailable') from None


def _goal_usage(snapshot):
    """The goal part of the legacy report; per-run totals come only from recorded runs."""
    tokens, seconds = snapshot['goal_tokens_used'], snapshot['goal_time_used_seconds']
    return {'goal_total_tokens': tokens, 'goal_time_used_seconds': seconds,
            'goal_usage_source': 'unavailable' if tokens is None and seconds is None else 'host_reported'}


def orchestration_client(state_root, project_root, operation, *, host_client, orchestration_handle=None,
                         request_id=None, expected_sequence=None, goal_tokens_used=None, goal_time_used_seconds=None,
                         status=None):
    """One lifecycle mutation, with explicit identity/sequence for reconciliation."""
    identity = 'cli-orc-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ID):
        raise RuntimeOrchestrationError('history_invalid_request')
    captured_sequence = None
    try:
        state = CollectorPaths(state_root)
        scope, control, endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (control, endpoint), host_client=host_client)
        actor = OrchestrationScope(**vars(scope))
        client = OrchestrationClient(endpoint, actor, timeout=10)
        if operation == 'orchestration.open':
            payload = {}
        else:
            payload = {'orchestration_handle': orchestration_handle}
            try:
                current = client.call('orchestration.status', payload,
                    request_id=identity if operation == 'orchestration.status' else 'orc-read-'+content_digest({'request': identity}))
            except HistoryError as error:
                # An enrolled read of an existing orchestration is refused only when
                # another host class owns it: keep the documented lifecycle reason.
                if error.reason_code == 'history_not_authorized':
                    raise HostClientMismatch('host_client_mismatch') from None
                raise
            snapshot = current['current']['snapshot']
            if snapshot['client_version'] != host_client:
                raise HostClientMismatch('host_client_mismatch')
            if operation == 'orchestration.status':
                return {'ready': True, **snapshot, **current, 'usage': _goal_usage(snapshot), 'request_id': identity}
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
    return {'ready': True, **snapshot, **result, 'usage': _goal_usage(snapshot), 'request_id': identity,
            **({'expected_sequence': captured_sequence} if captured_sequence is not None else {})}


def _run_driver(contexts, control, operation, run_handle, attempt, inputs_digest, context, *, timeout):
    """One exchange with the project host's run driver; its reply is metadata only."""
    driver = Path(control).parent / 'run.sock'
    contexts[driver] = canonical_json(context)
    raw = exchange_frame(driver, canonical_json({'protocol': 1, 'operation': operation, 'run_handle': run_handle,
        'attempt': attempt, 'inputs_digest': inputs_digest}), timeout=timeout, response_limit=4_096)
    value = parse_bounded_json(raw, max_bytes=4_096)
    if (value.keys() != {'protocol', 'state', 'reason_code'} or value['protocol'] != 1
            or value['state'] not in {'preparing', 'ready', 'failed', 'refused'}):
        raise ValueError()
    return value


def run_control_client(state_root, project_root, operation, orchestration_handle, run_handle, attempt, *, host_client):
    """One live status read or cancellation request for a run this host class holds.

    The reply is the project host's own observation: health, ages and remaining
    bounds, never content. An accepted cancellation is a request, not confirmed
    cleanup; a lost reply never proves that no stop was requested.
    """
    try:
        state = CollectorPaths(state_root)
        scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        client = ControlClient(endpoint, scope, timeout=5)
        target = ServiceTarget(orchestration_handle, run_handle, attempt)
        view = client.status(target) if operation == 'project.status' else client.cancel(target)
    except DispatchError:
        return {'ready': False, 'reason_code': 'control_unavailable', 'error': 'run control is unavailable'}
    except Exception as error:
        reason = getattr(error, 'reason_code', 'control_unavailable')
        return {'ready': False, 'reason_code': reason if type(reason) is str else 'control_unavailable',
                'error': 'run control refused'}
    return {'ready': True, 'observed_at_ns': view.observed_at_ns, **view.result}


def bridged_run_owner(state_root, project_root, run_handle, *, host_client):
    """'owned' or 'other' when this project's host holds the bridged run, else None.

    Commands that know only a run handle use it to refuse a run the host drives:
    the other host class gets the lifecycle mismatch, the owner a fixed refusal.
    """
    try:
        state = CollectorPaths(state_root)
        scope, control, result = read_runtime_hint(state, project_root)
        driver = Path(control).parent / 'run.sock'
        if not driver.exists():
            return None
        contexts = enroll_clients(state, project_root, scope, (control, driver), host_client=host_client)
        reply = _run_driver(contexts, control, 'run.describe', run_handle, 1, '0' * 64, {}, timeout=5)
    except Exception:
        return None
    if reply['state'] == 'refused':
        return 'other' if reply['reason_code'] == 'history_not_authorized' else None
    return 'owned'


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
        state = CollectorPaths(state_root)
        scope, endpoints = _read_runtime_endpoints(state, project_root, account=True)
        if len(endpoints) != 3:
            raise RuntimeOpenError('open_unavailable')
        contexts = enroll_clients(state, project_root, scope, endpoints[:2], account=endpoints[2], host_client=host_client)
        prospective = OrchestrationScope(**vars(scope))
        digest = open_inputs_digest(assignment, task, route, parent_grant)
        originals = {'assignment': assignment, 'task': task, 'route': route, 'parent_grant': parent_grant}
        # The trusted CLI hands each owner the originals for exactly this request.
        contexts[endpoints[1]] = canonical_json({'purpose': 'open', 'orchestration_handle': orchestration_handle,
                                                 **originals})
        project_scope = HistoryClient(endpoints[1], scope.project_id, scope.runtime_digest, timeout=10).original_open_scope(
            prospective, orchestration_handle, digest)
        if project_scope['facts']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        opening = AccountOpenInput(prospective, orchestration_handle, digest)
        contexts[endpoints[2]] = canonical_json({'purpose': 'open', 'project_scope': project_scope,
            'prospective': {'scope': prospective.as_data(), 'orchestration_handle': orchestration_handle,
                            **originals, 'worker': None}})
        identity = 'cli-open-' + content_digest(opening.payload())
        reply = AccountClient(endpoints[2], scope.project_id, scope.runtime_digest, timeout=10).open_host_run(
            opening, request_id=identity)
    except DispatchError:
        raise RuntimeOpenError('open_unavailable') from None
    except (AccountError, HistoryError, OpenError) as error:
        raise RuntimeOpenError(error.reason_code) from None
    opened = reply['state'] == 'opened'
    registered = False
    if opened:
        # The project host keeps the run's originals for its dispatch; the account
        # run is open either way, and an unregistered run is refused at dispatch.
        try:
            contexts = enroll_clients(state, project_root, scope, (endpoints[0], Path(endpoints[0]).parent / 'run.sock'),
                                      host_client=host_client)
            registered = _run_driver(contexts, endpoints[0], 'run.register', reply['run_handle'], assignment['attempt'],
                digest, {'purpose': 'register', 'orchestration_handle': orchestration_handle, 'run_id': reply['run_id'],
                         'host_client': host_client, **originals}, timeout=10)['state'] != 'refused'
        except Exception:
            registered = False
    # The legacy open report's fields, projected from the account receipt.
    return {'ready': opened, **reply, 'admission': 'admitted' if opened else 'denied',
            'status': 'started' if opened else 'denied',
            'observation_started_at_ms': reply['recorded_at_ms'] if opened else None, 'read_containment': 'advisory',
            'project_host_registered': registered}


def dispatch_codex_client(state_root, project_root, run_handle, task, route, assignment, parent_grant, *, expected_export_commit=None):
    """Actual CLI path: submit once, poll metadata, read original result chunks."""
    if (route.get('provider') != 'codex' or route.get('dispatch') != 'bridge'
            or route.get('sandbox_mode') != 'read-only' or assignment.get('permissions', {}).get('network') is not False
            or any(not p.endswith(':read') for p in assignment.get('permissions', {}).get('filesystem', []))
            or api_keys_present(os.environ)):
        raise DispatchError('dispatch_not_authorized')
    state = CollectorPaths(state_root)
    scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
    contexts = enroll_clients(state, project_root, scope, (endpoint, result_endpoint, Path(endpoint).parent / 'run.sock'))
    client = DispatchClient(endpoint, result_endpoint, scope, timeout=2)
    seconds, _ = effective_dispatch_limits(route, assignment)
    digest = open_inputs_digest(assignment, task, route, parent_grant)
    # The project host prepares the admitted run, claims it and consumes its grant
    # before dispatch can name it. Waiting here never extends a run's own bounds.
    # A runtime without a run driver has prepared the run itself.
    deadline = time.monotonic() + 600
    while (Path(endpoint).parent / 'run.sock').exists():
        try:
            prepared = _run_driver(contexts, endpoint, 'run.prepare', run_handle, assignment['attempt'], digest,
                {'purpose': 'prepare', 'expected_export_commit': expected_export_commit}, timeout=30)
        except Exception:
            raise DispatchError('dispatch_unavailable') from None
        if prepared['state'] == 'ready':
            break
        if prepared['state'] == 'refused' and prepared['reason_code'] == 'history_not_authorized':
            raise HostClientMismatch('host_client_mismatch')
        if prepared['state'] in {'failed', 'refused'} or time.monotonic() >= deadline:
            raise DispatchError('dispatch_unavailable')
    old = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGINT, signal.SIGTERM):
            old[signum] = signal.signal(signum, interrupted)
    try:
        result = client.dispatch(run_handle, assignment['attempt'], digest,
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
