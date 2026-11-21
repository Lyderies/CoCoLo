"""Bounded dispatch client for an original prepared project execution.

Endpoint/scope values are routing claims, never native authority. No admission,
model launch, persistence owner or direct fallback exists in this client.
"""
from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path
import re
import time

from .control_api import ControlScope, scope_data
from .contracts import RESULT_VALIDATION_REASON_CODES
from .history_contracts import AttemptBinding, TerminalDelivery, _DIGEST, _RUN, matches, valid_integer
from .learning_contracts import (
    SUPPORTED_EFFORTS, SUPPORTED_MODELS, canonical_json, parse_bounded_json, validate_client_diagnostics,
)
from .result_contracts import MAX_RESULT_BODY_BYTES
from .result_read_api import ResultReadClient
from .service_transport import MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

MAX_DISPATCH_FRAME_BYTES = 16_384
DISPATCH_OPERATIONS = frozenset({'dispatch.submit', 'dispatch.status'})
DISPATCH_STATES = frozenset({'queued', 'running', 'publishing', 'published', 'unknown', 'retired'})
DISPATCH_REASONS = frozenset({'dispatch_invalid_request', 'dispatch_invalid_response', 'dispatch_not_authorized',
    'dispatch_unavailable', 'dispatch_conflict', 'dispatch_full', 'dispatch_not_found'})
_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_OUTCOMES = frozenset({'result', 'launch_failed', 'process_tracking_unavailable', 'watchdog_unavailable',
    'unparseable_stream', 'stream_too_large', 'timed_out', 'cancelled', 'startup_silence', 'work_silence',
    'monitor_unavailable', 'codex_activity_invalid', 'child_reported_error', 'missing_result',
    'unparseable_result', 'invalid_result', 'result_too_large',
    'codex_tool_not_allowed:mcp_tool_call', 'codex_tool_not_allowed:web_search',
    'codex_tool_not_allowed:collab_tool_call', 'codex_tool_not_allowed:unknown'})
_CLAUDE_OUTCOMES = frozenset({'claude_activity_invalid', 'claude_tool_not_allowed', 'missing_init_frame',
    'paid_usage_observed', 'provider_rejected', 'rate_limit_event_invalid', 'init_api_key_source',
    'init_model_mismatch', 'init_plugin_not_allowed', 'init_mcp_server_loaded', 'init_tool_not_allowed',
    'init_client_unsupported', 'init_permission_mode_mismatch', 'init_additional_directories_present',
    'init_plugin_not_disabled'})  # O10 VF-6
_USAGE = {'codex': {'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'},
          'claude': {'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'}}
_RATE_LIMIT_FIELDS = {'rate_limit_events', 'rate_limit_records_kept', 'rate_limit_record_failed'}


class DispatchError(RuntimeError):
    def __init__(self, reason='dispatch_unavailable', *, known=frozenset()):
        """`known` adds the fixed codes a caller may pass on, such as why a run could not start."""
        allowed = type(reason) is str and (reason in DISPATCH_REASONS or reason in known)
        self.reason_code = reason if allowed else 'dispatch_unavailable'
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class DispatchRequest:
    protocol_version: int
    operation: str
    request_id: str
    scope: ControlScope
    run_handle: str
    attempt: int
    inputs_digest: str
    expected_export_commit: str | None = None


def _data(request):
    if (type(request) is not DispatchRequest or type(request.scope) is not ControlScope
            or type(request.protocol_version) is not int or request.protocol_version != 1
            or type(request.operation) is not str or request.operation not in DISPATCH_OPERATIONS
            or not matches(request.run_handle, _RUN) or not valid_integer(request.attempt, minimum=1, maximum=(1 << 31)-1)
            or not matches(request.inputs_digest, _DIGEST)
            or (request.expected_export_commit is not None
                and (type(request.expected_export_commit) is not str or _COMMIT.fullmatch(request.expected_export_commit) is None))):
        raise DispatchError('dispatch_invalid_request')
    return {**asdict(request), 'scope': scope_data(request.scope)}


def dispatch_request(scope, operation, run_handle, attempt, inputs_digest, expected_export_commit=None):
    value = DispatchRequest(1, operation, 'pending', scope, run_handle, attempt, inputs_digest, expected_export_commit)
    data = _data(value)
    data['request_id'] = 'dispatch-identity'
    identity = 'dsp_' + hashlib.sha256(canonical_json(data)).hexdigest()
    return DispatchRequest(1, operation, identity, scope, run_handle, attempt, inputs_digest, expected_export_commit)


def encode_dispatch_request(request):
    try:
        data = _data(request)
        expected = dict(data, request_id='dispatch-identity')
        if request.request_id != 'dsp_' + hashlib.sha256(canonical_json(expected)).hexdigest():
            raise ValueError()
        raw = canonical_json(data)
        if len(raw) + 1 > MAX_DISPATCH_FRAME_BYTES:
            raise ValueError()
    except Exception:
        raise DispatchError('dispatch_invalid_request') from None
    return raw


def decode_dispatch_request(raw):
    try:
        if type(raw) is not bytes or len(raw) + 1 > MAX_DISPATCH_FRAME_BYTES:
            raise ValueError()
        data = parse_bounded_json(raw, max_bytes=MAX_DISPATCH_FRAME_BYTES-1)
        if (type(data) is not dict or data.keys() != {'protocol_version', 'operation', 'request_id', 'scope',
                'run_handle', 'attempt', 'inputs_digest', 'expected_export_commit'} or type(data['scope']) is not dict):
            raise ValueError()
        request = DispatchRequest(**{**data, 'scope': ControlScope(**data['scope'])})
        encode_dispatch_request(request)
    except Exception:
        raise DispatchError('dispatch_invalid_request') from None
    return request


def dispatch_request_digest(request):
    return hashlib.sha256(encode_dispatch_request(request)).hexdigest()


def dispatch_reply(request=None, *, result=None, reason=None):
    return canonical_json({'protocol_version': 1, 'request_id': None if request is None else request.request_id,
        'request_digest': None if request is None else dispatch_request_digest(request),
        'ready': reason is None, 'result': result, 'reason_code': reason})


def decode_dispatch_response(raw, request):
    try:
        if type(raw) is not bytes or len(raw)+1 > MAX_DISPATCH_FRAME_BYTES:
            raise ValueError()
        value = parse_bounded_json(raw, max_bytes=MAX_DISPATCH_FRAME_BYTES-1)
        if (value.keys() != {'protocol_version', 'request_id', 'request_digest', 'ready', 'result', 'reason_code'}
                or type(value['protocol_version']) is not int or value['protocol_version'] != 1
                or value['request_id'] != request.request_id or value['request_digest'] != dispatch_request_digest(request)
                or type(value['ready']) is not bool):
            raise ValueError()
        if not value['ready']:
            if value['result'] is not None or value['reason_code'] not in DISPATCH_REASONS:
                raise ValueError()
            reason = value['reason_code']
        else:
            reason = None
            result = value['result']
            if (value['reason_code'] is not None or type(result) is not dict
                    or result.keys() != {'state', 'known', 'binding', 'delivery', 'report', 'inputs_digest'}
                    or result['state'] not in DISPATCH_STATES or type(result['known']) is not bool
                    or result['inputs_digest'] != request.inputs_digest or type(result['binding']) is not dict):
                raise ValueError()
            binding = AttemptBinding(**result['binding'])
            if (any(getattr(binding, name) != item for name, item in scope_data(request.scope).items())
                    or binding.run_handle != request.run_handle or binding.attempt != request.attempt):
                raise ValueError()
            if result['state'] == 'published':
                delivery, report = result['delivery'], result['report']
                if type(delivery) is not dict or type(report) is not dict:
                    raise ValueError()
                actual = TerminalDelivery(**{**delivery, 'binding': AttemptBinding(**delivery['binding'])})
                provider = report.get('provider')
                claude = provider == 'claude'
                if (actual.binding != binding or provider not in _USAGE or report.get('bridge_run_id') != binding.claim_id
                        or (claude and (not _RATE_LIMIT_FIELDS <= set(report)
                                        or type(report['rate_limit_record_failed']) is not bool
                                        or not valid_integer(report['rate_limit_events'])
                                        or not valid_integer(report['rate_limit_records_kept'])))):
                    raise ValueError()
                if claude and report.get('client_diagnostics') is not None:
                    validate_client_diagnostics(report['client_diagnostics'])
                if (set(report) - (_RATE_LIMIT_FIELDS | {'client_diagnostics'} if claude else set()) - {'provider', 'requested_model', 'observed_model', 'effort', 'wall_seconds',
                        'read_containment', 'rerouted', 'outcome', 'usage', 'result_validation_reason_code',
                        'bridge_run_id', 'reviewed', 'terminal_state', 'capacity_state', 'export_file_count',
                        'export_commit', 'export_manifest_digest'}
                        or type(report.get('reviewed')) is not bool or report.get('capacity_state') != 'retained'
                        or report.get('terminal_state') not in {None, 'timed_out', 'cancelled', 'failed', 'interrupted'}
                        or report.get('result_validation_reason_code') not in {None, *RESULT_VALIDATION_REASON_CODES}):
                    raise ValueError()
                if (report.get('requested_model') not in SUPPORTED_MODELS
                        or report.get('observed_model') not in {None, *SUPPORTED_MODELS}
                        or report.get('effort') not in SUPPORTED_EFFORTS
                        or report.get('outcome') not in (_OUTCOMES | _CLAUDE_OUTCOMES if claude else _OUTCOMES)
                        or report.get('read_containment') != ('enforced' if claude else 'advisory')
                        or type(report.get('rerouted')) is not bool
                        or type(report.get('wall_seconds')) not in {int, float}
                        or not math.isfinite(report['wall_seconds']) or not 0 <= report['wall_seconds'] <= 1_000_000
                        or not valid_integer(report.get('export_file_count'), maximum=2000)
                        or type(report.get('export_commit')) is not str or _COMMIT.fullmatch(report['export_commit']) is None
                        or not matches(report.get('export_manifest_digest'), _DIGEST)):
                    raise ValueError()
                usage = report.get('usage')
                if usage is not None and (type(usage) is not dict or usage.keys() != _USAGE[provider] or any(v is not None and not valid_integer(v) for v in usage.values())):
                    raise ValueError()
            elif result['delivery'] is not None or result['report'] is not None:
                raise ValueError()
    except Exception:
        raise DispatchError('dispatch_invalid_response') from None
    if reason is not None:
        raise DispatchError(reason)
    return result


class DispatchClient:
    def __init__(self, endpoint, result_endpoint, scope, *, timeout):
        if type(scope) is not ControlScope or type(timeout) not in {int, float} or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise DispatchError('dispatch_invalid_request')
        scope_data(scope)
        self._endpoint, self._result_endpoint, self._scope, self._timeout = Path(endpoint), Path(result_endpoint), scope, timeout
        self.last_binding = None

    def call(self, operation, run_handle, attempt, inputs_digest, *, expected_export_commit=None, timeout=None):
        request = dispatch_request(self._scope, operation, run_handle, attempt, inputs_digest, expected_export_commit)
        try:
            raw = exchange_frame(self._endpoint, encode_dispatch_request(request), timeout=self._timeout if timeout is None else timeout,
                                 request_limit=MAX_DISPATCH_FRAME_BYTES, response_limit=MAX_DISPATCH_FRAME_BYTES)
        except ServiceTransportError as transport:
            error = DispatchError('dispatch_unavailable')
            # O10 DR-1: a service that did not answer in time is slow, not gone; only that is retried.
            error.timed_out = transport.reason_code == 'deadline_exceeded'
            raise error from None
        return decode_dispatch_response(raw, request)

    def _status(self, view, run_handle, attempt, inputs_digest, expected_export_commit, timeout):
        """One status check. O10 DR-1: a check the service was too slow to answer keeps the last view, so
        the wait goes on to its own deadline; a service that is gone, or any refusal, ends it."""
        try:
            return self.call('dispatch.status', run_handle, attempt, inputs_digest,
                             expected_export_commit=expected_export_commit, timeout=timeout)
        except DispatchError as error:
            if getattr(error, 'timed_out', False):
                return view
            raise

    def dispatch(self, run_handle, attempt, inputs_digest, *, wait_seconds, expected_export_commit=None):
        if type(wait_seconds) not in {int, float} or not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 86_460:
            raise DispatchError('dispatch_invalid_request')
        deadline = time.monotonic() + wait_seconds
        view = self.call('dispatch.submit', run_handle, attempt, inputs_digest,
                         expected_export_commit=expected_export_commit, timeout=min(self._timeout, wait_seconds))
        self.last_binding = AttemptBinding(**view['binding'])
        while view['state'] != 'published':
            if view['state'] in {'unknown', 'retired'} or time.monotonic() >= deadline:
                raise DispatchError('dispatch_unavailable')
            time.sleep(min(.05, max(0, deadline-time.monotonic())))
            remaining = min(self._timeout, deadline-time.monotonic())
            if remaining <= 0:
                raise DispatchError('dispatch_unavailable')
            view = self._status(view, run_handle, attempt, inputs_digest, expected_export_commit, remaining)
        binding = AttemptBinding(**view['binding'])
        while True:
            remaining = min(self._timeout, deadline-time.monotonic())
            if remaining <= 0:
                raise DispatchError('dispatch_unavailable')
            try:
                body = ResultReadClient(self._result_endpoint, timeout=remaining).read_result(
                    binding, view['delivery']['result_digest'])
            except Exception:
                # O10 DR-2: read again only after a fresh answer that it is still published; a re-check
                # that times out is retried on its own, never taken as permission (review 040-vf-dr-code).
                fresh = None
                while fresh is None:
                    time.sleep(min(.05, max(0, deadline-time.monotonic())))
                    remaining = min(self._timeout, deadline-time.monotonic())
                    if remaining <= 0:
                        raise DispatchError('dispatch_unavailable') from None
                    fresh = self._status(None, run_handle, attempt, inputs_digest, expected_export_commit, remaining)
                view = fresh
                if view['state'] != 'published':
                    raise DispatchError('dispatch_unavailable') from None
                continue
            try:
                result = parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)
            except Exception:
                raise DispatchError('dispatch_unavailable') from None
            return {'result': result, 'bridge_report': view['report']}
