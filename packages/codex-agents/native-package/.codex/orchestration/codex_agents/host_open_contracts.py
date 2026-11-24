"""Original pre-claim host-open metadata; no native or execution authority.

Account owners construct these receipts from original rows. Project owners must
check their independently held scope, lease and exact original inputs. Neither a
MAC nor a receipt establishes a native peer, current permission or live state.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import secrets

from .account_claim_contracts import ClaimError, _DIGEST, _HANDLE, matches
from .contracts import TASK_FAMILIES, validate_task
from .history_contracts import HistoryError, _ID, valid_integer
from .learning_contracts import canonical_json, route_identity, content_digest, external_id, keyed_digest, parse_bounded_json, validate_execution_configuration
from .result_contracts import MAX_ASSIGNMENT_CANONICAL_BYTES, ResultLease, ResultLeaseBinding

MAX_OPEN_PROVENANCE_BYTES = 8192
MAX_HOST_OPEN_CONTEXT_BYTES = 3 * MAX_ASSIGNMENT_CANONICAL_BYTES
_DOMAIN = b'account-original-host-open-v1\0'
_DIGEST_FIELDS = ('lease_digest', 'assignment_digest', 'task_digest', 'route_digest', 'configuration_digest',
                  'admission_content_digest', 'start_content_digest')
_FIELDS = {'binding', 'assignment_id', 'provider', 'task_family', 'client_version', *_DIGEST_FIELDS,
           'admission_record_id', 'admission_observed_at_ms', 'start_record_id', 'opened_at_ms',
           'observation_deadline_at_ms', 'worker_outcome_digest', 'verification_record_id', 'verification_content_digest'}


@dataclass(frozen=True)
class OpenProvenanceInput:
    run_handle: str
    assignment_id: str
    worker_outcome_digest: str | None = None

    def __post_init__(self):
        if (not matches(self.run_handle, _HANDLE) or not matches(self.assignment_id, _ID)
                or (self.worker_outcome_digest is not None and not matches(self.worker_outcome_digest, _DIGEST))):
            raise ClaimError('claim_execution_invalid')

    def payload(self):
        self.__post_init__()
        return asdict(self)


@dataclass(frozen=True, repr=False)
class HostOpenContext:
    """Original inputs from an independently authenticated server adapter.

    A request cannot select this context. The adapter must establish native
    identity/current permission and custody of these actual protected inputs.
    """
    binding: ResultLeaseBinding
    assignment: dict
    task: dict
    route: dict

    def snapshot(self):
        invalid = False
        try:
            names = ('assignment', 'task', 'route')
            if (type(self.binding) is not ResultLeaseBinding
                    or any(type(getattr(self, name)) is not dict for name in names)):
                raise ValueError()
            raw = canonical_json({'binding': self.binding.as_data(), **{name: getattr(self, name) for name in names}})
            if not 0 < len(raw) <= MAX_HOST_OPEN_CONTEXT_BYTES:
                raise ValueError()
            value = parse_bounded_json(raw, max_bytes=MAX_HOST_OPEN_CONTEXT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError('history_not_authorized')
        return raw, value


def decode_open_provenance_input(payload):
    invalid = False
    try:
        if type(payload) is not dict or payload.keys() != {'run_handle', 'assignment_id', 'worker_outcome_digest'}:
            raise ValueError()
        request = OpenProvenanceInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return request


def checked_open_provenance(value, request, project_id, runtime_digest):
    """Closed client-side shape only; never authenticate a caller or the MAC."""
    invalid = False
    try:
        if (type(request) is not OpenProvenanceInput or type(value) is not dict
                or value.keys() != {'schema_version', 'provenance', 'seal'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not matches(value['seal'], _DIGEST)):
            raise ValueError()
        request = decode_open_provenance_input(request.payload())
        facts = value['provenance']
        if type(facts) is not dict or facts.keys() != _FIELDS or type(facts['binding']) is not dict:
            raise ValueError()
        binding = ResultLeaseBinding(**facts['binding'])
        if (binding.project_id != project_id or binding.runtime_digest != runtime_digest
                or binding.run_handle != request.run_handle or facts['assignment_id'] != request.assignment_id
                or facts['worker_outcome_digest'] != request.worker_outcome_digest
                or any(not matches(facts[name], _DIGEST) for name in _DIGEST_FIELDS)
                or any(not matches(facts[name], _ID) for name in ('admission_record_id', 'start_record_id'))
                or facts['provider'] not in {'codex', 'claude'} or facts['client_version'] != 'bridge-v1'
                or type(facts['task_family']) is not str or facts['task_family'] not in TASK_FAMILIES
                or not valid_integer(facts['opened_at_ms']) or not valid_integer(facts['admission_observed_at_ms'])
                or not valid_integer(facts['observation_deadline_at_ms'], minimum=facts['opened_at_ms'] + 1)
                or len(canonical_json(value)) > MAX_OPEN_PROVENANCE_BYTES):
            raise ValueError()
        if request.worker_outcome_digest is None:
            if facts['verification_record_id'] is not None or facts['verification_content_digest'] is not None:
                raise ValueError()
        elif (not matches(facts['verification_record_id'], _ID)
                or not matches(facts['verification_content_digest'], _DIGEST)):
            raise ValueError()
        captured = deepcopy(value)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return captured


def decode_host_open_submission(payload, project_id, runtime_digest):
    """Closed wire values only; expected scope and inputs stay server-side."""
    invalid = False
    try:
        if type(payload) is not dict or payload.keys() != {'open_request', 'provenance'}:
            raise ValueError()
        request = decode_open_provenance_input(payload['open_request'])
        provenance = checked_open_provenance(payload['provenance'], request, project_id, runtime_digest)
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError('history_invalid_request')
    return request, provenance


def seal_open_provenance(key, request, facts):
    if type(key) is not bytes or len(key) != 32:
        raise ClaimError('claim_execution_invalid')
    binding = ResultLeaseBinding(**facts['binding'])
    value = {'schema_version': 1, 'provenance': facts}
    value['seal'] = keyed_digest(key, _DOMAIN + canonical_json(value))
    return checked_open_provenance(value, request, binding.project_id, binding.runtime_digest)


def verify_open_provenance(key, request, lease, value, *, assignment, task, route):
    """Bind original bytes on the project writer, without granting fresh work.

    lease and inputs must come from the actual protected writer/original input
    custody, not fields selected from this receipt. Current orchestration,
    permissions, deadlines and unique execution consumption remain separate.
    """
    invalid = False
    try:
        if type(key) is not bytes or len(key) != 32 or type(lease) is not ResultLease:
            raise ValueError()
        lease.__post_init__()
        checked = checked_open_provenance(value, request, lease.binding.project_id, lease.binding.runtime_digest)
        facts = checked['provenance']
        original_task = validate_task(task)
        raw_assignment = canonical_json(assignment)
        raw = canonical_json({'schema_version': 1, 'provenance': facts})
        if request.worker_outcome_digest is not None:
            original_binding = {'schema_version': 1, 'record_type': 'verification_binding',
                'run_id': lease.binding.run_id, 'worker_outcome_digest': request.worker_outcome_digest,
                'observed_at_ms': facts['opened_at_ms']}
            original_binding['record_id'] = 'verification_binding:' + content_digest(original_binding)
            if (facts['verification_record_id'] != original_binding['record_id']
                    or facts['verification_content_digest'] != content_digest(original_binding)):
                raise ValueError()
        if (not secrets.compare_digest(checked['seal'], keyed_digest(key, _DOMAIN + raw))
                or facts['binding'] != lease.binding.as_data() or facts['lease_digest'] != lease.lease_digest
                or facts['assignment_digest'] != lease.assignment_digest
                or not 0 < len(raw_assignment) <= MAX_ASSIGNMENT_CANONICAL_BYTES
                or keyed_digest(key, raw_assignment) != facts['assignment_digest']
                or keyed_digest(key, canonical_json(original_task)) != facts['task_digest']
                or keyed_digest(key, canonical_json(route_identity(route))) != facts['route_digest']
                or content_digest(validate_execution_configuration(route['execution_configuration'])) != facts['configuration_digest']
                or assignment['assignment_id'] != request.assignment_id or assignment['attempt'] != lease.binding.attempt
                or original_task['family'] != facts['task_family'] or route.get('provider', 'codex') != facts['provider']
                or original_task.get('verification_for_outcome') != request.worker_outcome_digest
                or facts['start_record_id'] != external_id(key, 'evt', f'{lease.binding.run_id}\0{request.assignment_id}\0started')
                or lease.binding.run_id != external_id(key, 'run', request.run_handle)):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return checked
