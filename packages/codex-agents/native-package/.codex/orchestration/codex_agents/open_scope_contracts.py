"""Original project-owned inputs before account admission; never launch authority.

No run or lease exists yet. The project owner binds its current journal and an
optional original worker to independently held prospective inputs. A MAC binds
bytes, not freshness, native identity or current permission. Account admission
must recapture current authority separately; this metadata cannot start work.
"""
from dataclasses import dataclass
import hashlib
import secrets

from .contracts import TASK_FAMILIES
from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, _ORCHESTRATION, matches, valid_integer
from .host_result_contracts import HostResultContext, MAX_HOST_RESULT_CONTEXT_BYTES
from .learning_contracts import canonical_json, external_id, keyed_digest, parse_bounded_json
from .orchestration_api import HOST_CLIENT_VERSIONS, OrchestrationScope
from .result_contracts import MAX_RESULT_BODY_BYTES

MAX_OPEN_SCOPE_BYTES = 8192
MAX_OPEN_SCOPE_CONTEXT_BYTES = 2 * MAX_HOST_RESULT_CONTEXT_BYTES
_DOMAIN = b'project-original-open-scope-v1\0'
_FIELDS = {'scope', 'orchestration_handle', 'orchestration_id', 'checkpoint_digest', 'sequence',
           'client_version', 'inputs_digest', 'assignment_id', 'attempt', 'assignment_digest',
           'task_digest', 'route_digest', 'parent_grant_digest', 'configuration_digest', 'provider', 'task_family', 'worker'}
_WORKER_FIELDS = {'binding', 'host_outcome_digest', 'provider', 'task_family', 'host_open_record_id',
                 'host_open_content_digest', 'result_digest', 'body_size', 'terminal_record_id', 'expires_at_ms'}


def open_inputs_digest(assignment, task, route, parent_grant):
    return hashlib.sha256(canonical_json({'assignment': assignment, 'task': task, 'route': route,
                                         'parent_grant': parent_grant})).hexdigest()


@dataclass(frozen=True, repr=False)
class OpenScopeContext:
    """Protected original inputs supplied only by the authenticated server adapter."""
    scope: OrchestrationScope
    orchestration_handle: str
    assignment: dict
    task: dict
    route: dict
    parent_grant: dict
    worker: HostResultContext | None = None

    def snapshot(self):
        invalid = False
        try:
            if (type(self.scope) is not OrchestrationScope or not matches(self.orchestration_handle, _ORCHESTRATION)
                    or any(type(getattr(self, n)) is not dict for n in ('assignment', 'task', 'route', 'parent_grant'))
                    or (self.worker is not None and type(self.worker) is not HostResultContext)):
                raise ValueError()
            value = {'scope': self.scope.as_data(), 'orchestration_handle': self.orchestration_handle,
                **{n: getattr(self, n) for n in ('assignment', 'task', 'route', 'parent_grant')},
                'worker': None if self.worker is None else self.worker.snapshot()[1]}
            raw = canonical_json(value)
            if not 0 < len(raw) <= MAX_OPEN_SCOPE_CONTEXT_BYTES:
                raise ValueError()
            captured = parse_bounded_json(raw, max_bytes=MAX_OPEN_SCOPE_CONTEXT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError('history_not_authorized')
        return raw, captured

    @classmethod
    def from_snapshot(cls, value):
        worker = value['worker']
        if worker is not None:
            worker = HostResultContext(**{**worker, 'binding': AttemptBinding(**worker['binding'])})
        return cls(**{**value, 'scope': OrchestrationScope(**value['scope']), 'worker': worker})


def decode_open_scope_request(value, project_id, runtime_digest):
    invalid = False
    try:
        if type(value) is not dict or value.keys() != {'scope', 'orchestration_handle', 'inputs_digest'}:
            raise ValueError()
        if type(value['scope']) is not dict:
            raise ValueError()
        scope = OrchestrationScope(**value['scope'])
        if (scope.project_id != project_id or scope.runtime_digest != runtime_digest
                or not matches(value['orchestration_handle'], _ORCHESTRATION)
                or not matches(value['inputs_digest'], _DIGEST)):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_request')
    return scope, value['orchestration_handle'], value['inputs_digest']


def checked_open_scope(value, scope, handle, inputs_digest):
    """Closed client shape only, with type-exact nested scope validation."""
    invalid = False
    try:
        if (type(scope) is not OrchestrationScope or type(value) is not dict
                or value.keys() != {'schema_version', 'facts', 'seal'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not matches(value['seal'], _DIGEST)):
            raise ValueError()
        f = value['facts']
        if type(f) is not dict or f.keys() != _FIELDS or type(f['scope']) is not dict:
            raise ValueError()
        actual_scope = OrchestrationScope(**f['scope'])
        if (actual_scope != scope or f['orchestration_handle'] != handle or not matches(handle, _ORCHESTRATION)
                or f['inputs_digest'] != inputs_digest or not matches(inputs_digest, _DIGEST)
                or not matches(f['orchestration_id'], _ID) or not matches(f['assignment_id'], _ID)
                or not valid_integer(f['sequence'], minimum=1, maximum=65_536)
                or not valid_integer(f['attempt'], minimum=1)
                or type(f['client_version']) is not str or f['client_version'] not in HOST_CLIENT_VERSIONS
                or f['provider'] != 'codex' or type(f['task_family']) is not str or f['task_family'] not in TASK_FAMILIES
                or any(not matches(f[n], _DIGEST) for n in ('checkpoint_digest', 'assignment_digest', 'task_digest',
                    'route_digest', 'parent_grant_digest', 'configuration_digest'))):
            raise ValueError()
        w = f['worker']
        if (w is None) != (f['task_family'] != 'verification'):
            raise ValueError()
        if w is not None:
            if type(w) is not dict or w.keys() != _WORKER_FIELDS or type(w['binding']) is not dict:
                raise ValueError()
            binding = AttemptBinding(**w['binding'])
            if (not scope.matches_attempt(binding) or binding.orchestration_handle != handle
                    or w['provider'] != 'codex' or type(w['task_family']) is not str
                    or w['task_family'] not in TASK_FAMILIES - {'verification'}
                    or any(not matches(w[n], _DIGEST) for n in ('host_outcome_digest', 'host_open_content_digest', 'result_digest'))
                    or any(not matches(w[n], _ID) for n in ('host_open_record_id', 'terminal_record_id'))
                    or not valid_integer(w['body_size'], minimum=1, maximum=MAX_RESULT_BODY_BYTES)
                    or not valid_integer(w['expires_at_ms'])):
                raise ValueError()
        raw = canonical_json(value)
        if len(raw) > MAX_OPEN_SCOPE_BYTES:
            raise ValueError()
        captured = parse_bounded_json(raw, max_bytes=MAX_OPEN_SCOPE_BYTES)
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_response')
    return captured


def seal_open_scope(key, facts):
    if type(key) is not bytes or len(key) != 32:
        raise HistoryError('history_invalid_input')
    value = {'schema_version': 1, 'facts': facts}
    value['seal'] = keyed_digest(key, _DOMAIN + canonical_json(value))
    return checked_open_scope(value, OrchestrationScope(**facts['scope']), facts['orchestration_handle'], facts['inputs_digest'])


def verify_open_scope(key, scope, handle, inputs_digest, value):
    """Account-owner byte binding only; never accept this as native/live proof."""
    checked = checked_open_scope(value, scope, handle, inputs_digest)
    if type(key) is not bytes or len(key) != 32:
        raise HistoryError('history_conflict')
    raw = canonical_json({n: checked[n] for n in ('schema_version', 'facts')})
    if (not secrets.compare_digest(checked['seal'], keyed_digest(key, _DOMAIN + raw))
            or checked['facts']['orchestration_id'] != external_id(key, 'orc', handle)):
        raise HistoryError('history_conflict')
    return checked
