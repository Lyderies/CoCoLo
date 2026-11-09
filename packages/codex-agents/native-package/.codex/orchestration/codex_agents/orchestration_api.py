"""Closed project lifecycle protocol; no SQLite or native caller authority.

The endpoint and owning primary must be independently authenticated. A committed
receipt, including a known old active snapshot, is metadata, never a new launch
grant. Current execution checks read the persistence owner's current state.
"""
from dataclasses import asdict, dataclass
import hashlib
import secrets
from pathlib import Path

from .history_contracts import (HistoryError, HISTORY_REASON_CODES, MAX_SEQUENCE, _ID, _PROJECT, _DIGEST, _ORCHESTRATION,
                                matches, valid_integer)
from .learning_contracts import canonical_json, parse_bounded_json
from .service_transport import MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

ORCHESTRATION_PROTOCOL_VERSION = 1
MAX_ORCHESTRATION_FRAME_BYTES = 16_384
MAX_ORCHESTRATION_CHECKPOINT_BYTES = 8192
ORCHESTRATION_OPERATIONS = frozenset('orchestration.' + name for name in ('open', 'pause', 'resume', 'checkpoint', 'close', 'status'))
ORCHESTRATION_TERMINAL_STATES = frozenset({'completed', 'failed', 'interrupted', 'cancelled'})
ORCHESTRATION_STATES = ORCHESTRATION_TERMINAL_STATES | {'active', 'paused'}
HOST_CLIENT_VERSIONS = frozenset({'codex-host-v1', 'claude-host-v1'})
SCOPE_FIELDS = ('project_id', 'primary_session_id', 'runtime_digest', 'supervisor_instance_id', 'supervisor_generation')


@dataclass(frozen=True)
class OrchestrationScope:
    project_id: str
    primary_session_id: str
    runtime_digest: str
    supervisor_instance_id: str
    supervisor_generation: int

    def as_data(self):
        if (not matches(self.project_id, _PROJECT) or not matches(self.primary_session_id, _ID)
                or not matches(self.runtime_digest, _DIGEST) or not matches(self.supervisor_instance_id, _ID)
                or not valid_integer(self.supervisor_generation, minimum=1)):
            raise HistoryError('history_invalid_input')
        return asdict(self)

    def __post_init__(self):
        self.as_data()

    def matches_attempt(self, binding):
        return self.as_data() == {name: getattr(binding, name) for name in SCOPE_FIELDS}


@dataclass(frozen=True)
class OrchestrationLimits:
    max_orchestrations: int
    max_events: int

    def __post_init__(self):
        if (not valid_integer(self.max_orchestrations, minimum=1, maximum=65_536)
                or not valid_integer(self.max_events, minimum=3, maximum=65_536)):
            raise HistoryError('history_invalid_input')


@dataclass(frozen=True)
class OrchestrationRequest:
    protocol_version: int
    operation: str
    request_id: str
    scope: OrchestrationScope
    payload: dict

    def __post_init__(self):
        # Hold a private canonical copy so a caller alias cannot change identity.
        raw = encode_orchestration_request(self)
        object.__setattr__(self, 'payload', parse_bounded_json(raw, max_bytes=MAX_ORCHESTRATION_FRAME_BYTES - 1)['payload'])


def _closed(value, fields):
    return type(value) is dict and value.keys() == fields


def encode_orchestration_request(request):
    invalid = False
    try:
        if (type(request) is not OrchestrationRequest or type(request.protocol_version) is not int
                or request.protocol_version != 1 or request.operation not in ORCHESTRATION_OPERATIONS
                or not matches(request.request_id, _ID) or type(request.scope) is not OrchestrationScope):
            raise ValueError()
        scope = request.scope.as_data()
        operation, payload = request.operation, request.payload
        if operation == 'orchestration.open':
            if not _closed(payload, set()):
                raise ValueError()
        else:
            fields = {'orchestration_handle'}
            if operation != 'orchestration.status':
                fields.add('expected_sequence')
            if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                fields |= {'goal_tokens_used', 'goal_time_used_seconds'}
            if operation == 'orchestration.close':
                fields.add('status')
            if not _closed(payload, fields) or not matches(payload['orchestration_handle'], _ORCHESTRATION):
                raise ValueError()
            if (operation != 'orchestration.status'
                    and not valid_integer(payload['expected_sequence'], minimum=1, maximum=MAX_SEQUENCE - 1)):
                raise ValueError()
            if operation == 'orchestration.close' and payload['status'] not in ORCHESTRATION_TERMINAL_STATES:
                raise ValueError()
            if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                for name in ('goal_tokens_used', 'goal_time_used_seconds'):
                    if not (operation == 'orchestration.close' and payload[name] is None) and not valid_integer(payload[name]):
                        raise ValueError()
        raw = canonical_json({'protocol_version': 1, 'operation': operation, 'request_id': request.request_id,
                              'scope': scope, 'payload': payload})
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_request')
    if len(raw) + 1 > MAX_ORCHESTRATION_FRAME_BYTES:
        raise HistoryError('history_request_too_large')
    return raw


def decode_orchestration_request(raw):
    invalid = False
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_ORCHESTRATION_FRAME_BYTES:
            raise ValueError()
        data = parse_bounded_json(raw, max_bytes=MAX_ORCHESTRATION_FRAME_BYTES - 1)
        if not _closed(data, {'protocol_version', 'operation', 'request_id', 'scope', 'payload'}) or type(data['scope']) is not dict:
            raise ValueError()
        request = OrchestrationRequest(**{**data, 'scope': OrchestrationScope(**data['scope'])})
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_request')
    return request


def orchestration_request_digest(request):
    return hashlib.sha256(encode_orchestration_request(request)).hexdigest()


def checked_orchestration_view(value):
    if not _closed(value, {'snapshot', 'checkpoint_digest'}) or not matches(value['checkpoint_digest'], _DIGEST):
        raise HistoryError('history_invalid_response')
    s = value['snapshot']
    fields = {'scope', 'orchestration_handle', 'orchestration_id', 'client_version', 'status', 'sequence',
              'started_at_ms', 'observed_at_ms', 'ended_at_ms', 'goal_tokens_used', 'goal_time_used_seconds'}
    if not _closed(s, fields) or not _closed(s['scope'], set(SCOPE_FIELDS)):
        raise HistoryError('history_invalid_response')
    try:
        OrchestrationScope(**s['scope'])
    except HistoryError:
        raise HistoryError('history_invalid_response') from None
    if (not matches(s['orchestration_handle'], _ORCHESTRATION) or not matches(s['orchestration_id'], _ID)
            or type(s['client_version']) is not str or s['client_version'] not in HOST_CLIENT_VERSIONS
            or type(s['status']) is not str or s['status'] not in ORCHESTRATION_STATES
            or not valid_integer(s['sequence'], minimum=1) or not valid_integer(s['started_at_ms'])
            or not valid_integer(s['observed_at_ms'], minimum=s['started_at_ms'])
            or (s['status'] in {'active', 'paused'} and s['ended_at_ms'] is not None)
            or (s['status'] in ORCHESTRATION_TERMINAL_STATES
                and (not valid_integer(s['ended_at_ms']) or s['ended_at_ms'] != s['observed_at_ms']))
            or any(s[n] is not None and not valid_integer(s[n]) for n in ('goal_tokens_used', 'goal_time_used_seconds'))
            # Only open commits sequence 1: active, unmeasured and observed at start.
            or (s['sequence'] == 1 and (s['status'] != 'active' or s['observed_at_ms'] != s['started_at_ms']
                                        or s['goal_tokens_used'] is not None or s['goal_time_used_seconds'] is not None))):
        raise HistoryError('history_invalid_response')
    return value


def _same(left, right):
    # JSON equality without Python's bool/int/float coercion.
    return canonical_json(left) == canonical_json(right)


_RECEIPT_STATUS = {'orchestration.open': {'active'}, 'orchestration.pause': {'paused'},
                   'orchestration.resume': {'active'}, 'orchestration.checkpoint': {'active', 'paused'}}


def _check_receipt(request, receipt, current, known):
    # The receipt is the original committed result; current is separately read
    # state. A known receipt is metadata only and never a new execution grant.
    committed, now, payload = receipt['snapshot'], current['snapshot'], request.payload
    expected = {payload['status']} if request.operation == 'orchestration.close' else _RECEIPT_STATUS[request.operation]
    sequence = 1 if request.operation == 'orchestration.open' else payload['expected_sequence'] + 1
    if committed['status'] not in expected or committed['sequence'] != sequence:
        raise ValueError()
    for name in ('goal_tokens_used', 'goal_time_used_seconds'):
        if payload.get(name) is not None and committed[name] != payload[name]:
            raise ValueError()
    if not known:
        if not _same(receipt, current):
            raise ValueError()
    elif (any(not _same(committed[n], now[n])
              for n in ('scope', 'orchestration_handle', 'orchestration_id', 'client_version', 'started_at_ms'))
            or now['sequence'] < committed['sequence'] or now['observed_at_ms'] < committed['observed_at_ms']
            or (now['sequence'] == committed['sequence']) != _same(receipt, current)
            or (committed['status'] in ORCHESTRATION_TERMINAL_STATES and not _same(receipt, current))
            or any(committed[n] is not None and (now[n] is None or now[n] < committed[n])
                   for n in ('goal_tokens_used', 'goal_time_used_seconds'))):
        raise ValueError()


def decode_orchestration_response(raw, request):
    invalid = False
    try:
        encode_orchestration_request(request)
        if type(raw) is not bytes or len(raw) + 1 > MAX_ORCHESTRATION_FRAME_BYTES:
            raise ValueError()
        response = parse_bounded_json(raw, max_bytes=MAX_ORCHESTRATION_FRAME_BYTES - 1)
        scope = request.scope.as_data()
        if (not _closed(response, {'protocol_version', 'request_id', 'request_digest', 'scope', 'ready', 'result', 'reason_code'})
                or type(response['protocol_version']) is not int or response['protocol_version'] != 1
                or not matches(response['request_id'], _ID) or response['request_id'] != request.request_id
                or not _same(response['scope'], scope) or not matches(response['request_digest'], _DIGEST)
                or response['request_digest'] != orchestration_request_digest(request) or type(response['ready']) is not bool):
            raise ValueError()
        if response['ready']:
            result = response['result']
            if response['reason_code'] is not None or not _closed(result, {'receipt', 'current', 'known'}) or type(result['known']) is not bool:
                raise ValueError()
            current = checked_orchestration_view(result['current'])
            if (not _same(current['snapshot']['scope'], scope)
                    or (request.operation != 'orchestration.open'
                        and current['snapshot']['orchestration_handle'] != request.payload['orchestration_handle'])):
                raise ValueError()
            if request.operation == 'orchestration.status':
                if result['receipt'] is not None or result['known'] is not False:
                    raise ValueError()
            else:
                _check_receipt(request, checked_orchestration_view(result['receipt']), current, result['known'])
        elif (response['result'] is not None or type(response['reason_code']) is not str
              or response['reason_code'] not in HISTORY_REASON_CODES):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_response')
    if not response['ready']:
        raise HistoryError(response['reason_code'])
    return response['result']


class OrchestrationClient:
    """One bounded exchange per call; no startup, retries or database fallback."""
    def __init__(self, endpoint: Path, scope: OrchestrationScope, *, timeout: float):
        if (not isinstance(endpoint, Path) or not endpoint.is_absolute() or type(scope) is not OrchestrationScope
                or type(timeout) not in {int, float} or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
            raise HistoryError('history_invalid_input')
        self._endpoint, self._scope, self._timeout = endpoint, OrchestrationScope(**scope.as_data()), timeout

    def call(self, operation, payload, *, request_id=None):
        # Exactly one exchange. A lost or ambiguous reply may follow a commit and
        # is never resent; reconcile by explicitly reusing the same request_id.
        if request_id is None:
            request_id = 'orc-request:' + secrets.token_hex(16)
        request = OrchestrationRequest(1, operation, request_id, self._scope, payload)
        raw = encode_orchestration_request(request)
        request = decode_orchestration_request(raw)
        failed = False
        try:
            reply = exchange_frame(self._endpoint, raw, timeout=self._timeout, request_limit=MAX_ORCHESTRATION_FRAME_BYTES,
                                   response_limit=MAX_ORCHESTRATION_FRAME_BYTES)
        except ServiceTransportError:
            failed = True
        if failed:
            raise HistoryError('history_service_unavailable')
        return decode_orchestration_response(reply, request)
