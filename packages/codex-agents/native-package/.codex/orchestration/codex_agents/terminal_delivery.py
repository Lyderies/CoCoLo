"""Closed project terminal metadata and account acknowledgements.

Native peer identity, original actor/epoch and current permissions are separate
requirements. These data and MACs never establish cleanup or release capacity.
MACs bind metadata; they do not prove a sender or an account commit on their own.
No result body, rejected content, process identity or caller flag is transferred.
"""
from dataclasses import asdict, dataclass
import secrets

from .account_claim_contracts import ClaimError, _DIGEST, matches
from .execution_grant import ExecutionRegistrationInput, checked_execution_grant, verify_execution_grant
from .history_api import delivery_from_data
from .history_contracts import AttemptBinding, TerminalDelivery
from .learning_contracts import DISPATCH_RESULT_SCHEMA_VERSION, canonical_json, content_digest, keyed_digest, parse_bounded_json
from .result_contracts import ResultLeaseBinding

MAX_TERMINAL_EVIDENCE_BYTES = 16_384
_RECORD_PREFIX = 'dispatch_result:'


def _record_id(value):
    return type(value) is str and value.startswith(_RECORD_PREFIX) and matches(value[len(_RECORD_PREFIX):], _DIGEST)


@dataclass(frozen=True)
class TerminalSubmissionInput:
    registration: ExecutionRegistrationInput
    registration_digest: str
    terminal_record_id: str
    terminal_content_digest: str
    result_digest: str

    def __post_init__(self):
        if (type(self.registration) is not ExecutionRegistrationInput or not _record_id(self.terminal_record_id)
                or any(not matches(v, _DIGEST) for v in (self.registration_digest, self.terminal_content_digest, self.result_digest))):
            raise ClaimError('claim_terminal_invalid')
        self.registration.payload()

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_terminal_submission(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        fields = dict(payload)
        fields['registration'] = ExecutionRegistrationInput(**fields['registration'])
        value = TerminalSubmissionInput(**fields)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_terminal_invalid')
    return value


@dataclass(frozen=True)
class TerminalEvidence:
    grant: object
    delivery: TerminalDelivery
    record: bytes

    def material(self):
        grant = checked_execution_grant(self.grant.as_data())
        if type(self.delivery) is not TerminalDelivery or type(self.record) is not bytes:
            raise ClaimError('claim_terminal_invalid')
        record = parse_bounded_json(self.record, max_bytes=MAX_TERMINAL_EVIDENCE_BYTES)
        data = {**self.delivery.as_data(), 'dispatch_result': record}
        delivery_from_data(data)
        if (self.record != canonical_json(record) or grant.binding != self.delivery.binding
                or record['schema_version'] != DISPATCH_RESULT_SCHEMA_VERSION
                or record['bridge_run_id'] != grant.binding.claim_id
                or record['observed_at_ms'] < grant.claimed_at_ms
                or record['record_id'] != _RECORD_PREFIX + content_digest({k: v for k, v in record.items() if k != 'record_id'})):
            raise ClaimError('claim_terminal_invalid')
        return {'grant': grant.as_data(), 'delivery': data}


def capture_terminal_evidence(raw):
    if raw is not None and (type(raw) is not bytes or not 0 < len(raw) <= MAX_TERMINAL_EVIDENCE_BYTES):
        raise ClaimError('claim_terminal_invalid')
    return raw


def _evidence_digest(key, material):
    return keyed_digest(key, canonical_json({'domain': 'project-terminal-evidence-v1', **material}))


def encode_terminal_evidence(key, grant, delivery, record):
    evidence = TerminalEvidence(grant, delivery, record)
    material = evidence.material()
    raw = canonical_json({'protocol_version': 1, 'evidence': material, 'seal': _evidence_digest(key, material)})
    capture_terminal_evidence(raw)
    return raw


def verify_terminal_evidence(key, expected, raw):
    invalid = False
    try:
        if type(expected) is not ResultLeaseBinding or capture_terminal_evidence(raw) is None:
            raise ValueError()
        expected.as_data()
        data = parse_bounded_json(raw, max_bytes=MAX_TERMINAL_EVIDENCE_BYTES)
        if (type(data) is not dict or set(data) != {'protocol_version', 'evidence', 'seal'}
                or type(data['protocol_version']) is not int or data['protocol_version'] != 1
                or type(data['evidence']) is not dict or set(data['evidence']) != {'grant', 'delivery'}
                or not matches(data['seal'], _DIGEST)):
            raise ValueError()
        grant = checked_execution_grant(data['evidence']['grant'])
        verify_execution_grant(key, grant.binding, grant.encode())
        if not expected.matches_attempt(grant.binding):
            raise ValueError()
        delivery = delivery_from_data(data['evidence']['delivery'])
        evidence = TerminalEvidence(grant, delivery, canonical_json(data['evidence']['delivery']['dispatch_result']))
        material = evidence.material()
        invalid = (raw != canonical_json(data) or data['evidence'] != material
                   or not secrets.compare_digest(data['seal'], _evidence_digest(key, material)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_terminal_invalid')
    return evidence


def _receipt_material(grant, delivery):
    return {'state': 'recorded', 'binding': grant.binding.as_data(), 'registration_digest': grant.registration_digest,
            'terminal_record_id': delivery.record_id, 'terminal_content_digest': delivery.record_content_digest,
            'result_digest': delivery.result_digest}


def _receipt_digest(key, material):
    return keyed_digest(key, canonical_json({'domain': 'account-terminal-acceptance-v1', **material}))


def terminal_receipt(key, grant, delivery, *, known):
    material = _receipt_material(grant, delivery)
    return checked_terminal_receipt({**material, 'receipt_digest': _receipt_digest(key, material), 'known': known})


def checked_terminal_receipt(value, submission=None):
    invalid = False
    try:
        fields = {'state', 'binding', 'registration_digest', 'terminal_record_id', 'terminal_content_digest',
                  'result_digest', 'receipt_digest', 'known'}
        if type(value) is not dict or set(value) != fields or value['state'] != 'recorded' or type(value['known']) is not bool:
            raise ValueError()
        binding = AttemptBinding(**value['binding'])
        if (not _record_id(value['terminal_record_id']) or any(not matches(value[n], _DIGEST) for n in (
                'registration_digest', 'terminal_content_digest', 'result_digest', 'receipt_digest'))):
            raise ValueError()
        if submission is not None:
            if type(submission) is not TerminalSubmissionInput:
                raise ValueError()
            submission.__post_init__()
            registration = submission.registration
            if (binding.run_handle != registration.run_handle or binding.claim_id != registration.bridge_run_id
                    or binding.claim_digest != registration.claim_digest
                    or any(value[n] != getattr(submission, n) for n in (
                        'registration_digest', 'terminal_record_id', 'terminal_content_digest', 'result_digest'))):
                raise ValueError()
        result = {**value, 'binding': binding.as_data()}
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_terminal_invalid')
    return result


def verify_terminal_receipt(key, grant, delivery, raw):
    invalid = False
    try:
        if capture_terminal_evidence(raw) is None:
            raise ValueError()
        value = checked_terminal_receipt(parse_bounded_json(raw, max_bytes=MAX_TERMINAL_EVIDENCE_BYTES))
        material = {n: v for n, v in value.items() if n not in {'known', 'receipt_digest'}}
        invalid = (raw != canonical_json(value) or material != _receipt_material(grant, delivery)
                   or not secrets.compare_digest(value['receipt_digest'], _receipt_digest(key, material)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_terminal_invalid')
    return value
