"""Bounded original account admission metadata, never native or launch authority.

The enrolled account owner derives these facts from its original registered
claim, admission and start records. A MAC only binds bytes; native peer identity,
current permission and protected publication remain independent activation gates.
No active flag, prompt, result body, cleanup assertion or new execution is carried.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import secrets

from .account_claim_contracts import ClaimError, _DIGEST, matches
from .contracts import TASK_FAMILIES
from .execution_grant import ExecutionGrant, ExecutionRegistrationInput
from .history_contracts import AttemptBinding, _ID, valid_integer
from .learning_contracts import canonical_json, external_id, keyed_digest

MAX_RUN_PROVENANCE_BYTES = 8192
GRANT_DIGEST_FIELDS = ('registration_digest', 'assignment_digest', 'task_digest', 'route_digest',
                       'configuration_digest', 'lease_digest')
_FIELDS = {'binding', 'registration', 'assignment_id', 'provider', 'task_family', 'client_version',
           'registered_assignment_id', 'admission_record_id', 'admission_content_digest',
           'admission_observed_at_ms', 'start_record_id', 'start_content_digest', 'started_at_ms',
           'observation_deadline_at_ms', *GRANT_DIGEST_FIELDS}
_DOMAIN = b'account-original-run-provenance-v1\0'


@dataclass(frozen=True)
class RunProvenanceInput:
    registration: ExecutionRegistrationInput
    registration_digest: str
    assignment_id: str

    def __post_init__(self):
        if (type(self.registration) is not ExecutionRegistrationInput
                or not matches(self.registration_digest, _DIGEST) or not matches(self.assignment_id, _ID)):
            raise ClaimError('claim_execution_invalid')
        self.registration.payload()

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_run_provenance_input(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        fields = dict(payload)
        fields['registration'] = ExecutionRegistrationInput(**fields['registration'])
        value = RunProvenanceInput(**fields)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return value


def checked_run_provenance(value, request, project_id, runtime_digest):
    """Closed shape/request binding only; a public client cannot verify the MAC."""
    invalid = False
    try:
        if (type(request) is not RunProvenanceInput or type(value) is not dict
                or value.keys() != {'schema_version', 'provenance', 'seal'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not matches(value['seal'], _DIGEST)):
            raise ValueError()
        request = decode_run_provenance_input(request.payload())
        facts = value['provenance']
        if type(facts) is not dict or facts.keys() != _FIELDS or type(facts['binding']) is not dict:
            raise ValueError()
        binding = AttemptBinding(**facts['binding'])
        registration = ExecutionRegistrationInput(**facts['registration'])
        if (binding.project_id != project_id or binding.runtime_digest != runtime_digest
                or registration.payload() != request.registration.payload()
                or binding.run_handle != registration.run_handle or binding.claim_id != registration.bridge_run_id
                or binding.claim_digest != registration.claim_digest or facts['registration_digest'] != request.registration_digest
                or facts['assignment_id'] != request.assignment_id
                or any(not matches(facts[name], _DIGEST) for name in (*GRANT_DIGEST_FIELDS,
                                                                 'admission_content_digest', 'start_content_digest'))
                or any(not matches(facts[name], _ID) for name in ('registered_assignment_id', 'admission_record_id', 'start_record_id'))
                or type(facts['provider']) is not str or facts['provider'] not in {'codex', 'claude'}
                or type(facts['task_family']) is not str or facts['task_family'] not in TASK_FAMILIES
                or facts['client_version'] != 'bridge-v1'
                or not valid_integer(facts['admission_observed_at_ms']) or not valid_integer(facts['started_at_ms'])
                or not valid_integer(facts['observation_deadline_at_ms'], minimum=facts['started_at_ms'] + 1)
                or len(canonical_json(value)) > MAX_RUN_PROVENANCE_BYTES):
            raise ValueError()
        captured = deepcopy(value)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return captured


def seal_run_provenance(key, facts):
    """Persistence-owner construction only; no caller or grant is authenticated."""
    if type(key) is not bytes or len(key) != 32:
        raise ClaimError('claim_execution_invalid')
    invalid = False
    try:
        request = decode_run_provenance_input({name: facts[name] for name in ('registration', 'registration_digest', 'assignment_id')})
        binding = AttemptBinding(**facts['binding'])
        raw = canonical_json({'schema_version': 1, 'provenance': facts})
        value = {**{'schema_version': 1, 'provenance': facts}, 'seal': keyed_digest(key, _DOMAIN + raw)}
        value = checked_run_provenance(value, request, binding.project_id, binding.runtime_digest)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return value


def verify_run_provenance(key, request, grant, value):
    """Project writer checks the original consumed grant and exact account facts."""
    invalid = False
    try:
        if type(key) is not bytes or len(key) != 32 or type(grant) is not ExecutionGrant:
            raise ValueError()
        grant.__post_init__()
        checked = checked_run_provenance(value, request, grant.binding.project_id, grant.binding.runtime_digest)
        facts = checked['provenance']
        raw = canonical_json({'schema_version': 1, 'provenance': facts})
        if (not secrets.compare_digest(checked['seal'], keyed_digest(key, _DOMAIN + raw))
                or canonical_json(facts['binding']) != canonical_json(grant.binding.as_data())
                or any(facts[name] != getattr(grant, name) for name in GRANT_DIGEST_FIELDS)
                or facts['registered_assignment_id'] != external_id(key, 'asg', grant.binding.claim_id)
                or facts['observation_deadline_at_ms'] < grant.deadline_at_ms):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_execution_invalid')
    return checked
