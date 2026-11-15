"""Bounded capacity observations, never launch or cleanup authority."""
from __future__ import annotations

from dataclasses import dataclass

from .account_claim_contracts import MAX_CLAIM_INTEGER, _BRIDGE, _DIGEST, _HANDLE, matches


@dataclass(frozen=True)
class CapacityStatusInput:
    run_handle: str
    bridge_run_id: str

    def __post_init__(self):
        if not matches(self.run_handle, _HANDLE) or not matches(self.bridge_run_id, _BRIDGE):
            raise ValueError("invalid capacity status input")

    def payload(self):
        self.__post_init__()
        return {"run_handle": self.run_handle, "bridge_run_id": self.bridge_run_id}


def decode_capacity_status_input(payload):
    if type(payload) is not dict or set(payload) != {"run_handle", "bridge_run_id"}:
        raise ValueError("invalid capacity status input")
    return CapacityStatusInput(payload["run_handle"], payload["bridge_run_id"])


def checked_capacity_status(value):
    if (type(value) is not dict or set(value) != {"state", "receipt_digest", "released_at_ms"}
            or type(value["state"]) is not str or value["state"] not in {"unknown", "held", "released"}):
        raise ValueError("invalid capacity status")
    if value["state"] == "released":
        if (not matches(value["receipt_digest"], _DIGEST)
                or type(value["released_at_ms"]) is not int
                or not 0 <= value["released_at_ms"] <= MAX_CLAIM_INTEGER):
            raise ValueError("invalid capacity status")
    elif value["receipt_digest"] is not None or value["released_at_ms"] is not None:
        raise ValueError("invalid capacity status")
    return dict(value)
