"""Denial settlement metadata; native account/project identity is separate.

A permanent no-launch fence must commit before a project retires an armed lease.
MACs bind these data, never authenticate a sender or prove process cleanup.
"""
from dataclasses import asdict, dataclass
import secrets

from .account_claim_contracts import (
    ClaimError, LaunchClaimInput, _DIGEST, _REQUEST_ID, checked_claim_receipt, matches,
)
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json
from .result_contracts import ResultLeaseBinding

MAX_DENIAL_RECEIPT_BYTES = 16_384
DENIAL_RECORD_TYPE = 'claim_denial_settlement'
# Trusted cooperative SQL limits; actual OS-I/O and installed cadence remain gates. The check for an
# earlier dispatch record of the run reads every record of the project, so it has the account's
# ordinary read budget (0.3.3, C2): 2,000 callbacks and 16 KiB values refused every fence on a project
# with about 200,000 records (4,573 callbacks) or one record over 16 KiB (records reach 64 KiB).
DENIAL_LEGACY_READ_LIMITS = (100_000, 1_000, 2 * 1_048_576)


@dataclass(frozen=True)
class CloseDeniedInput:
    run_handle: str
    identity_digest: str
    claim_request_id: str
    denial_receipt_digest: str
    lease_digest: str

    def __post_init__(self):
        LaunchClaimInput(self.run_handle, self.identity_digest)
        if (not matches(self.claim_request_id, _REQUEST_ID)
                or any(not matches(v, _DIGEST) for v in (self.denial_receipt_digest, self.lease_digest))):
            raise ClaimError('claim_denial_invalid')

    def payload(self):
        self.__post_init__()
        return asdict(self)


@dataclass(frozen=True)
class FenceClaimInput:
    """The run's own fixed claim request, to be closed for good unless the account admitted it.

    `identity_digest` is None from the next service of the project, which fences a lease an
    earlier one left armed (0.3.3, C3) and never saw its identity: the account takes it from the
    run's own launch observation and still checks the whole original claim request against it.
    """
    run_handle: str
    identity_digest: str | None
    claim_request_id: str
    lease_digest: str

    def __post_init__(self):
        LaunchClaimInput(self.run_handle, '0' * 64 if self.identity_digest is None else self.identity_digest)
        if not matches(self.claim_request_id, _REQUEST_ID) or not matches(self.lease_digest, _DIGEST):
            raise ClaimError('claim_denial_invalid')

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_fence_claim(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        value = FenceClaimInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_denial_invalid')
    return value


def checked_fence_reply(value, request):
    """The receipt the account holds for the request and, unless it was admitted, the closure."""
    invalid = False
    try:
        if type(value) is not dict or set(value) != {'receipt', 'closure'} or type(request) is not FenceClaimInput:
            raise ValueError()
        receipt = checked_claim_receipt(value['receipt'])
        if receipt['known'] is not True:
            raise ValueError()
        if receipt['state'] == 'claimed':
            if value['closure'] is not None:
                raise ValueError()
            closure = None
        else:
            closure = checked_denial_closure(value['closure'])
            binding = closure['binding']
            if (closure['denial'] != receipt or binding['run_handle'] != request.run_handle
                    or binding['claim_request_id'] != request.claim_request_id
                    or closure['lease_digest'] != request.lease_digest):
                raise ValueError()
        result = {'receipt': receipt, 'closure': closure}
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_denial_invalid')
    return result


def decode_close_denied(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        value = CloseDeniedInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_denial_invalid')
    return value


def denial_record_id(key, project, lease_digest):
    return DENIAL_RECORD_TYPE + ':' + keyed_digest(key, canonical_json({
        'domain': 'account-denial-settlement-id-v1', 'project_id': project, 'lease_digest': lease_digest,
    }))


def binding_digest(key, binding):
    return keyed_digest(key, canonical_json(binding.as_data()))


def _receipt_digest(key, material):
    return keyed_digest(key, canonical_json({'domain': 'account-denial-settlement-v1', **material}))


def checked_denial_closure(value, request=None):
    invalid = False
    try:
        fields = {'state', 'binding', 'lease_digest', 'assignment_digest', 'denial', 'record_id',
                  'record_content_digest', 'fenced_at_ms', 'receipt_digest', 'known'}
        if (type(value) is not dict or set(value) != fields or value['state'] != 'denied_closed'
                or type(value['known']) is not bool or type(value['fenced_at_ms']) is not int
                or not 0 <= value['fenced_at_ms'] <= (1 << 63) - 1):
            raise ValueError()
        binding = ResultLeaseBinding(**value['binding'])
        denial = checked_claim_receipt(value['denial'])
        prefix = DENIAL_RECORD_TYPE + ':'
        if (denial['state'] != 'denied' or denial['known'] is not True or denial['run_id'] != binding.run_id
                or denial['recorded_at_ms'] > value['fenced_at_ms']
                or any(not matches(value[n], _DIGEST) for n in ('lease_digest', 'assignment_digest', 'record_content_digest', 'receipt_digest'))
                or type(value['record_id']) is not str or not value['record_id'].startswith(prefix)
                or not matches(value['record_id'][len(prefix):], _DIGEST)):
            raise ValueError()
        if request is not None:
            if (type(request) is not CloseDeniedInput or binding.run_handle != request.run_handle
                    or binding.claim_request_id != request.claim_request_id
                    or denial['receipt_digest'] != request.denial_receipt_digest or value['lease_digest'] != request.lease_digest):
                raise ValueError()
        result = {**value, 'binding': binding.as_data(), 'denial': denial}
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_denial_invalid')
    return result


def denial_closure(key, binding, record, *, known):
    from .learning_contracts import content_digest
    material = {'state': 'denied_closed', 'binding': binding.as_data(), 'lease_digest': record['lease_digest'],
                'assignment_digest': record['assignment_digest'], 'denial': record['denial'], 'record_id': record['record_id'],
                'record_content_digest': content_digest(record), 'fenced_at_ms': record['observed_at_ms']}
    return checked_denial_closure({**material, 'receipt_digest': _receipt_digest(key, material), 'known': known})


def verify_denial_closure(key, lease, raw):
    invalid = False
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_DENIAL_RECEIPT_BYTES:
            raise ValueError()
        value = checked_denial_closure(parse_bounded_json(raw, max_bytes=MAX_DENIAL_RECEIPT_BYTES))
        material = {k: v for k, v in value.items() if k not in {'known', 'receipt_digest'}}
        invalid = (raw != canonical_json(value) or value['binding'] != lease.binding.as_data()
                   or value['lease_digest'] != lease.lease_digest or value['assignment_digest'] != lease.assignment_digest
                   or value['denial']['recorded_at_ms'] < lease.created_at_ms
                   or not secrets.compare_digest(value['receipt_digest'], _receipt_digest(key, material)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_denial_invalid')
    return value
