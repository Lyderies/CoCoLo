"""Bounded original account release acknowledgement, never native authority."""
import secrets

from .account_claim_contracts import ClaimError, MAX_CLAIM_INTEGER
from .cleanup_contracts import CleanupEvidence, MAX_CLEANUP_EVIDENCE_BYTES
from .history_contracts import _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json
from .terminal_delivery import checked_terminal_receipt


def capture_cleanup_evidence(raw):
    if raw is not None and (type(raw) is not bytes or not 0 < len(raw) <= MAX_CLEANUP_EVIDENCE_BYTES):
        raise ClaimError('claim_cleanup_invalid')
    return raw


def _material(evidence, cleanup_digest, released_at_ms, receipt_digest):
    return {'state': 'released', 'binding': evidence.grant.binding.as_data(),
        'registration_digest': evidence.grant.registration_digest,
        'terminal_record_id': evidence.delivery.record_id,
        'terminal_content_digest': evidence.delivery.record_content_digest,
        'result_digest': evidence.delivery.result_digest,
        'cleanup_digest': cleanup_digest, 'released_at_ms': released_at_ms, 'receipt_digest': receipt_digest}


def _seal(key, material):
    return keyed_digest(key, b'account-project-release-ack-v1\0' + canonical_json(material))


def release_receipt(key, evidence, cleanup_digest, released_at_ms, receipt_digest, *, known):
    if type(key) is not bytes or len(key) != 32 or type(evidence) is not CleanupEvidence:
        raise ClaimError('claim_cleanup_invalid')
    evidence.material()
    material = _material(evidence, cleanup_digest, released_at_ms, receipt_digest)
    return checked_release_receipt({**material, 'acknowledgement_digest': _seal(key, material), 'known': known})


def checked_release_receipt(value, submission=None):
    invalid = False
    try:
        fields = {'state', 'binding', 'registration_digest', 'terminal_record_id', 'terminal_content_digest',
                  'result_digest', 'cleanup_digest', 'released_at_ms', 'receipt_digest', 'acknowledgement_digest', 'known'}
        if type(value) is not dict or set(value) != fields or value['state'] != 'released':
            raise ValueError()
        terminal = {n: value[n] for n in ('binding', 'registration_digest', 'terminal_record_id',
                    'terminal_content_digest', 'result_digest', 'receipt_digest', 'known')}
        checked_terminal_receipt({**terminal, 'state': 'recorded'}, submission)
        if (not valid_integer(value['released_at_ms'], maximum=MAX_CLAIM_INTEGER)
                or any(not matches(value[n], _DIGEST) for n in ('cleanup_digest', 'acknowledgement_digest'))):
            raise ValueError()
        return {**value, 'binding': terminal['binding'].copy()}
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError('claim_cleanup_invalid')


def verify_release_receipt(key, evidence, cleanup_digest, raw):
    try:
        if (type(key) is not bytes or len(key) != 32
                or capture_cleanup_evidence(raw) is None or type(evidence) is not CleanupEvidence):
            raise ValueError()
        evidence.material()
        value = checked_release_receipt(parse_bounded_json(raw, max_bytes=MAX_CLEANUP_EVIDENCE_BYTES))
        material = {k: v for k, v in value.items() if k not in {'known', 'acknowledgement_digest'}}
        if (raw != canonical_json(value)
                or value['released_at_ms'] < evidence.grant.claimed_at_ms
                or material != _material(evidence, cleanup_digest, value['released_at_ms'], value['receipt_digest'])
                or not secrets.compare_digest(value['acknowledgement_digest'], _seal(key, material))):
            raise ValueError()
        return value
    except Exception:
        pass
    raise ClaimError('claim_cleanup_invalid')
