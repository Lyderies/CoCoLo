"""Bounded protected-store evidence; not native caller or launch authority.

The project writer seals its actual armed lease. Account services receive these
bytes through separately authenticated server-side enrollment, never a caller
role flag or an arbitrary endpoint selected in JSON. Verification narrows normal
admission; it cannot create a claim, release capacity or authenticate a primary.
"""
from dataclasses import asdict
import secrets

from .account_claim_contracts import ClaimError
from .history_contracts import HistoryError, _DIGEST, matches, valid_integer
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json
from .result_contracts import ResultLease, ResultLeaseBinding

MAX_STORAGE_EVIDENCE_BYTES = 16_384


def lease_state_seal(key, value):
    return keyed_digest(key, canonical_json({"domain": "project-result-lease-state-v1", **asdict(value)}))


def lease_receipt_digest(key, binding, assignment_digest, created, until):
    return keyed_digest(key, canonical_json({
        "domain": "project-result-lease-v1", "binding": binding.as_data(),
        "assignment_digest": assignment_digest, "created_at_ms": created, "reserved_until_ms": until,
    }))


def capture_storage_evidence(binding, raw):
    invalid = False
    try:
        if binding is not None:
            if type(binding) is not ResultLeaseBinding:
                raise ValueError()
            binding = ResultLeaseBinding(**binding.as_data())
        if raw is not None and (type(raw) is not bytes or not 0 < len(raw) <= MAX_STORAGE_EVIDENCE_BYTES):
            raise ValueError()
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_storage_invalid")
    return binding, raw


def encode_storage_evidence(key, value):
    if type(value) is not ResultLease:
        raise HistoryError("history_invalid_input")
    value.__post_init__()
    if value.state != "claim_pending":
        raise HistoryError("history_result_not_ready")
    raw = canonical_json({"protocol_version": 1, "lease": asdict(value), "seal": lease_state_seal(key, value)})
    if len(raw) > MAX_STORAGE_EVIDENCE_BYTES:
        raise HistoryError("history_invalid_input")
    return raw


def verify_pending_storage_evidence(key, expected, raw):
    """Historical pending evidence; no current launch-readiness assertion."""
    return _verify_historical_storage_evidence(key, expected, raw, state='claim_pending')


def verify_bound_storage_evidence(key, expected, raw):
    return _verify_historical_storage_evidence(key, expected, raw, state='bound')


def _verify_historical_storage_evidence(key, expected, raw, *, state):
    """Historical bound storage metadata; current launch timing is separate."""
    if expected is None or raw is None:
        raise ClaimError("claim_storage_required")
    invalid = False
    try:
        if type(expected) is not ResultLeaseBinding or type(raw) is not bytes or not 0 < len(raw) <= MAX_STORAGE_EVIDENCE_BYTES:
            raise ValueError()
        expected.as_data()
        body = parse_bounded_json(raw, max_bytes=MAX_STORAGE_EVIDENCE_BYTES)
        if (type(body) is not dict or set(body) != {"protocol_version", "lease", "seal"}
                or type(body["protocol_version"]) is not int or body["protocol_version"] != 1
                or not matches(body["seal"], _DIGEST)):
            raise ValueError()
        fields = dict(body["lease"])
        fields["binding"] = ResultLeaseBinding(**fields["binding"])
        value = ResultLease(**fields)
        invalid = (canonical_json(body) != raw or value.state != state or value.binding != expected
                   or not secrets.compare_digest(body["seal"], lease_state_seal(key, value))
                   or not secrets.compare_digest(value.lease_digest, lease_receipt_digest(
                       key, value.binding, value.assignment_digest, value.created_at_ms, value.reserved_until_ms)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_storage_invalid")
    return value


def verify_storage_evidence(key, expected, raw, *, project_id, runtime_digest, run_handle,
                            run_id, request_id, request_digest, assignment_digest,
                            orchestration_digest, now_ms, deadline_ms):
    """Verify the original armed store and authoritative account SQL bindings.

    expected must come from the server's independently authenticated supervisor
    context. Reading it from the evidence itself would remove that actor check.
    A valid HMAC establishes protected key possession, not native provenance.
    """
    if expected is None or raw is None:
        raise ClaimError("claim_storage_required")
    invalid = False
    try:
        if (type(expected) is not ResultLeaseBinding or type(raw) is not bytes or not 0 < len(raw) <= MAX_STORAGE_EVIDENCE_BYTES
                or not valid_integer(now_ms) or not valid_integer(deadline_ms)):
            raise ValueError()
        expected = ResultLeaseBinding(**expected.as_data())
        body = parse_bounded_json(raw, max_bytes=MAX_STORAGE_EVIDENCE_BYTES)
        if (type(body) is not dict or set(body) != {"protocol_version", "lease", "seal"}
                or type(body["protocol_version"]) is not int or body["protocol_version"] != 1
                or type(body["lease"]) is not dict or not matches(body["seal"], _DIGEST)):
            raise ValueError()
        fields = dict(body["lease"])
        fields["binding"] = ResultLeaseBinding(**fields["binding"])
        value = ResultLease(**fields)
        invalid = (
            canonical_json(body) != raw or value.binding != expected or value.state != "claim_pending"
            or not secrets.compare_digest(body["seal"], lease_state_seal(key, value))
            or not secrets.compare_digest(value.lease_digest, lease_receipt_digest(
                key, value.binding, value.assignment_digest, value.created_at_ms, value.reserved_until_ms))
            or expected.project_id != project_id or expected.runtime_digest != runtime_digest
            or expected.run_handle != run_handle or expected.run_id != run_id
            or expected.claim_request_id != request_id or expected.claim_request_digest != request_digest
            or value.assignment_digest != assignment_digest
            or keyed_digest(key, expected.orchestration_handle) != orchestration_digest
        )
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_storage_invalid")
    if not value.created_at_ms <= now_ms < deadline_ms < value.reserved_until_ms:
        raise ClaimError("claim_storage_expired")
    # Immutable metadata only; the caller does not receive a new local execution.
    return value.lease_digest
