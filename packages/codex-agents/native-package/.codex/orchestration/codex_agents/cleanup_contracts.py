"""Bounded cleanup metadata binding; never cleanup or native authority.

A key owner can encode constructed evidence. This proves neither actual cleanup,
sender identity, account commit nor containment. Producers/consumers separately
check the account receipt/protected outbox, original live ExecutionCleanup and
current native peer/profile/epoch/revocation gates. No snapshot or MAC authorizes
release, retirement or execution.
"""
from dataclasses import dataclass
import secrets

from .execution_grant import (
    ExecutionGrant, checked_execution_grant, verify_execution_grant,
)
from .history_contracts import AttemptBinding, TerminalDelivery, _DIGEST, matches
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json
from .result_contracts import LEASE_SCOPE_FIELDS, ResultLeaseBinding

MAX_CLEANUP_EVIDENCE_BYTES = 16384
_DOMAIN = b"project-cleanup-receipt-v1\0"
_MATERIAL_FIELDS = {"binding", "grant", "delivery", "terminal_receipt_digest"}
_LEASE_FIELDS = {*LEASE_SCOPE_FIELDS, "claim_request_id", "claim_request_digest"}
_ATTEMPT_FIELDS = {*LEASE_SCOPE_FIELDS, "claim_id", "claim_digest"}
_GRANT_FIELDS = {
    "binding", "handle_digest", "assignment_digest", "task_digest", "route_digest",
    "configuration_digest", "lease_digest", "clock_owner_digest",
    "clock_reference_ns", "deadline_ns", "claimed_at_ms", "deadline_at_ms",
    "registration_digest",
}
_DELIVERY_FIELDS = {
    "delivery_sequence", "binding", "record_id", "record_content_digest", "result_digest",
}


class CleanupError(RuntimeError):
    def __init__(self, reason_code="cleanup_invalid_input"):
        if type(reason_code) is not str or reason_code not in {
            "cleanup_invalid_input", "cleanup_invalid_evidence",
        }:
            reason_code = "cleanup_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _closed(data, fields):
    if type(data) is not dict or set(data) != fields:
        raise ValueError()


def _key(key):
    if type(key) is not bytes or len(key) != 32:
        raise ValueError()


def _lease(data):
    _closed(data, _LEASE_FIELDS)
    return ResultLeaseBinding(**data)


def _attempt(data):
    _closed(data, _ATTEMPT_FIELDS)
    return AttemptBinding(**data)


def _from_material(data):
    _closed(data, _MATERIAL_FIELDS)
    binding = _lease(data["binding"])
    grant_data = data["grant"]
    _closed(grant_data, _GRANT_FIELDS)
    _attempt(grant_data["binding"])
    grant = checked_execution_grant(grant_data)
    delivery_data = data["delivery"]
    _closed(delivery_data, _DELIVERY_FIELDS)
    delivery_binding = _attempt(delivery_data["binding"])
    record_id = delivery_data["record_id"]
    prefix = "dispatch_result:"
    if (type(record_id) is not str or not record_id.startswith(prefix)
            or not matches(record_id[len(prefix):], _DIGEST)
            or not matches(data["terminal_receipt_digest"], _DIGEST)):
        raise ValueError()
    delivery = TerminalDelivery(**{**delivery_data, "binding": delivery_binding})
    lease_scope = {name: getattr(binding, name) for name in LEASE_SCOPE_FIELDS}
    grant_scope = {name: getattr(grant.binding, name) for name in LEASE_SCOPE_FIELDS}
    if (canonical_json(lease_scope) != canonical_json(grant_scope)
            or canonical_json(delivery.binding.as_data())
            != canonical_json(grant.binding.as_data())):
        raise ValueError()
    return CleanupEvidence(binding, grant, delivery, data["terminal_receipt_digest"])


def _original_grant(key, value):
    verify_execution_grant(key, value.grant.binding, value.grant.encode())


def _seal(key, material):
    return keyed_digest(key, _DOMAIN + canonical_json(material))


@dataclass(frozen=True)
class CleanupEvidence:
    binding: ResultLeaseBinding
    grant: ExecutionGrant
    delivery: TerminalDelivery
    terminal_receipt_digest: str

    def material(self):
        try:
            if (type(self) is not CleanupEvidence
                    or type(self.binding) is not ResultLeaseBinding
                    or type(self.grant) is not ExecutionGrant
                    or type(self.delivery) is not TerminalDelivery):
                raise ValueError()
            data = {
                "binding": self.binding.as_data(),
                "grant": self.grant.as_data(),
                "delivery": self.delivery.as_data(),
                "terminal_receipt_digest": self.terminal_receipt_digest,
            }
            _from_material(data)
            return data
        except Exception:
            pass
        raise CleanupError("cleanup_invalid_input")


def encode_cleanup_evidence(key, value) -> bytes:
    try:
        _key(key)
        if type(value) is not CleanupEvidence:
            raise ValueError()
        material = value.material()
        _original_grant(key, value)
        raw = canonical_json({
            "protocol_version": 1, "cleanup": material, "seal": _seal(key, material),
        })
        if not 0 < len(raw) <= MAX_CLEANUP_EVIDENCE_BYTES:
            raise ValueError()
        return raw
    except Exception:
        pass
    raise CleanupError("cleanup_invalid_input")


def verify_cleanup_evidence(key, expected: ResultLeaseBinding, raw: bytes) -> CleanupEvidence:
    try:
        _key(key)
        if (type(expected) is not ResultLeaseBinding or type(raw) is not bytes
                or not 0 < len(raw) <= MAX_CLEANUP_EVIDENCE_BYTES):
            raise ValueError()
        expected_data = expected.as_data()
        _lease(expected_data)
        data = parse_bounded_json(raw, max_bytes=MAX_CLEANUP_EVIDENCE_BYTES)
        _closed(data, {"protocol_version", "cleanup", "seal"})
        if (type(data["protocol_version"]) is not int or data["protocol_version"] != 1
                or not matches(data["seal"], _DIGEST)
                or raw != canonical_json(data)):
            raise ValueError()
        value = _from_material(data["cleanup"])
        _original_grant(key, value)
        material = value.material()
        if (canonical_json(expected_data) != canonical_json(material["binding"])
                or not secrets.compare_digest(data["seal"], _seal(key, material))):
            raise ValueError()
        return value
    except Exception:
        pass
    raise CleanupError("cleanup_invalid_evidence")
