"""Closed launch-claim metadata. These values confer no execution authority."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .learning_contracts import ADMISSION_REASON_CODES

CLAIM_ERROR_CODES = frozenset({
    "claim_invalid_request", "claim_conflict", "claim_unavailable", "claim_busy",
    "claim_cancelled", "claim_preparation_failed",
})
CLAIM_DENIAL_CODES = frozenset(ADMISSION_REASON_CODES - {"admitted"}) | frozenset({
    "claim_not_open", "claim_already_consumed", "claim_context_missing",
    "claim_runtime_mismatch", "claim_observation_missing", "claim_client_changed",
})
_DIGEST = re.compile(r"[0-9a-f]{64}")
_HANDLE = re.compile(r"hrn_[0-9a-f]{48}")
_RUN = re.compile(r"run:[0-9a-f]{64}")
_BRIDGE = re.compile(r"brg_[0-9a-f]{32}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
MAX_CLAIM_INTEGER = (1 << 63) - 1


class ClaimError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in CLAIM_ERROR_CODES:
            reason_code = "claim_invalid_request"
        self.reason_code = reason_code
        super().__init__(reason_code)


def matches(value: object, pattern: re.Pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class LaunchClaimInput:
    run_handle: str
    identity_digest: str

    def __post_init__(self) -> None:
        if not matches(self.run_handle, _HANDLE) or not matches(self.identity_digest, _DIGEST):
            raise ClaimError("claim_invalid_request")

    def payload(self) -> dict:
        self.__post_init__()
        return {"run_handle": self.run_handle, "identity_digest": self.identity_digest}


def decode_claim_input(payload: object) -> LaunchClaimInput:
    if type(payload) is not dict or set(payload) != {"run_handle", "identity_digest"}:
        raise ClaimError("claim_invalid_request")
    return LaunchClaimInput(payload["run_handle"], payload["identity_digest"])


def checked_claim_receipt(value: object) -> dict:
    fields = {"state", "receipt_digest", "run_id", "bridge_run_id", "deadline_at_ms",
              "reason_code", "recorded_at_ms", "known"}
    if (type(value) is not dict or set(value) != fields or type(value["state"]) is not str
            or value["state"] not in {"claimed", "denied"} or not matches(value["receipt_digest"], _DIGEST)
            or (value["run_id"] is not None and not matches(value["run_id"], _RUN))
            or type(value["recorded_at_ms"]) is not int or not 0 <= value["recorded_at_ms"] <= MAX_CLAIM_INTEGER
            or type(value["known"]) is not bool):
        raise ClaimError("claim_invalid_request")
    if value["state"] == "claimed":
        if (value["run_id"] is None or not matches(value["bridge_run_id"], _BRIDGE)
                or type(value["deadline_at_ms"]) is not int
                or not value["recorded_at_ms"] < value["deadline_at_ms"] <= MAX_CLAIM_INTEGER
                or value["reason_code"] is not None):
            raise ClaimError("claim_invalid_request")
    elif (value["bridge_run_id"] is not None or value["deadline_at_ms"] is not None
          or type(value["reason_code"]) is not str or value["reason_code"] not in CLAIM_DENIAL_CODES):
        raise ClaimError("claim_invalid_request")
    return dict(value)
