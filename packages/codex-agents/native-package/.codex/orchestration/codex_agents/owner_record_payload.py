"""Owner-record signing data over metadata contracts alone.

The returned bytes preserve the existing owner-record purpose and project scope.
They carry no signature, caller authority or admission decision. Verification and
worker custody remain the responsibility of the actual trusted owner adapter.
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractError
from .learning_contracts import (
    canonical_json, content_digest, validate_billing_attestation, validate_feedback,
    validate_grader_calibration, validate_run_slot_confirmation,
)


def owner_record_payload(project_id: str, record: dict[str, Any]) -> bytes:
    """Return the exact legacy owner feedback/calibration/billing payload."""

    if not isinstance(project_id, str) or not project_id:
        raise ContractError("owner record project is invalid")
    record_type = record.get("record_type") if isinstance(record, dict) else None
    if record_type == "feedback":
        checked = validate_feedback(record)
    elif record_type == "grader_calibration":
        checked = validate_grader_calibration(record)
    elif record_type == "billing_attestation":
        checked = validate_billing_attestation(record)
    elif record_type == "run_slot_confirmation":
        checked = validate_run_slot_confirmation(record)
    else:
        raise ContractError("owner record type is unsupported")
    return canonical_json(
        {
            "purpose": "authenticated_owner_record",
            "project_id": project_id,
            "record_type": record_type,
            "record_digest": content_digest(checked),
        }
    )
