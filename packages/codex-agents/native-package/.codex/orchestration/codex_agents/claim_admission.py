"""Detached bounded admission parsing for an actual registered claim worker.

These values are data, never native, signature, freshness or execution proof.
Only the owner's registered returned-worker registry may supply them for use,
after exact raw recapture and fresh run/clock checks in the final transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .admission_input_decode import ParsedAdmissionRow, decode_admission_inputs
from .admission_inputs import AdmissionCaptureLimits, AdmissionInputSnapshot
from .account_claim_contracts import ClaimError
from .learning_contracts import parse_semver


@dataclass(frozen=True, repr=False)
class PreparedClaimAdmission:
    snapshot: AdmissionInputSnapshot
    rows: tuple[ParsedAdmissionRow, ...]
    billing: tuple[tuple[dict, str], ...]
    headroom: tuple[tuple[dict, str], ...]
    attestations: tuple[tuple[str, dict, str], ...]
    identities: object
    admissions: object
    reviewed_versions: frozenset[str]


def prepare_claim_admission(snapshot, limits: AdmissionCaptureLimits) -> PreparedClaimAdmission:
    rows = decode_admission_inputs(snapshot, limits)
    ordered = sorted(rows, key=lambda item: (item.raw[8], item.raw[9], item.raw[1], item.raw[3]))

    def records(kind):
        return tuple((item.record, item.raw[6]) for item in ordered
                     if item.raw[4] == kind and item.record["provider"] == snapshot.provider)

    attestations = tuple((item.raw[2], item.record, item.raw[6])
                         for item in sorted(rows, key=lambda item: item.raw[3])
                         if item.raw[1] == snapshot.project_id and item.raw[4] == "billing_attestation"
                         and item.raw[7] == "owner" and item.record["provider"] == snapshot.provider)
    identities, admissions, reviewed = {}, {}, set()
    minimum = parse_semver(snapshot.policy.minimum_version)
    maximum = parse_semver(snapshot.policy.maximum_exclusive_version)
    for item in rows:
        if item.raw[1] != snapshot.project_id:
            continue
        if item.raw[4] == "provider_identity" and item.raw[7] == "provider":
            identities.setdefault(item.raw[6], []).append(item.record)
            version = item.record["client_version"]
            parsed = None if version is None else parse_semver(version)
            if item.record["provider"] == snapshot.provider and parsed is not None and minimum <= parsed < maximum:
                reviewed.add(version)
        elif item.raw[4] == "admission_decision" and item.raw[7] == "lifecycle":
            admissions.setdefault(item.record["run_id"], []).append(item.record)
    return PreparedClaimAdmission(
        snapshot, rows, records("provider_billing"), records("provider_headroom"), attestations,
        MappingProxyType({key: tuple(value) for key, value in identities.items()}),
        MappingProxyType({key: tuple(value) for key, value in admissions.items()}), frozenset(reviewed),
    )


def claim_identity(prepared: PreparedClaimAdmission, run_id: str, identity_digest: str):
    """Use validated captured records; no SQL, JSON parsing or signature trust."""
    provider = prepared.snapshot.provider
    current = prepared.identities.get(identity_digest, ())
    if len(current) != 1:
        return "claim_observation_missing", None, None
    admissions = prepared.admissions.get(run_id, ())
    if len(admissions) != 1 or admissions[0]["decision"] != "pass":
        raise ClaimError("claim_preparation_failed")
    admission, identity = admissions[0], current[0]
    # Preserve every evidence occurrence, including duplicates, just as the
    # original admitted-client lookup did. Membership filtering would collapse
    # duplicate identity references and could turn an invalid admission valid.
    admitted = [record for digest in admission["evidence_digests"] for record in prepared.identities.get(digest, ())]
    if (len(admitted) != 1 or admitted[0]["provider"] != admission["provider"]
            or admitted[0]["account_fingerprint"] != admission["account_fingerprint"]):
        raise ClaimError("claim_preparation_failed")
    if (admission["provider"] != provider or identity["provider"] != provider
            or identity["account_fingerprint"] != admission["account_fingerprint"]
            or identity["client_version"] is None or identity["client_version"] != admitted[0]["client_version"]):
        return "claim_client_changed", admission, identity
    return None, admission, identity


def admission_signature_rows(snapshot):
    return tuple((row[2], row[3], row[5], row[6], row[7], row[10])
                 for row in sorted(snapshot.rows, key=lambda row: row[3])
                 if row[1] == snapshot.project_id and row[4] == "billing_attestation")


def same_admission_inputs(before: AdmissionInputSnapshot, after: AdmissionInputSnapshot) -> bool:
    # Capture timestamps differ. Actual rows, policy, public signer and recorded
    # owner generation must match exactly; no mutation counter stands in for them.
    fields = ("project_id", "provider", "runtime_digest", "project_created_at_ms", "policy", "owner", "signer", "rows",
              "anchor_ms")
    return all(getattr(before, name) == getattr(after, name) for name in fields)
