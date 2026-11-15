"""Pure provider restriction comparisons and conservative headroom merging."""

from __future__ import annotations

from typing import Any

from .admission_metadata import EVIDENCE_HORIZON_MS
from .learning_contracts import content_digest, validate_provider_headroom

CLAUDE_STATUS_RANK = {None: 0, "allowed": 0, "allowed_warning": 1, "rejected": 2}
BILLING_RANK = {False: 0, None: 1, True: 2}


def _headroom_record(**material: Any) -> dict[str, Any]:
    body = {"schema_version": 1, "record_type": "provider_headroom", **material}
    return validate_provider_headroom({
        **body, "record_id": f"provider_headroom:{content_digest(body)}",
    })


def _restriction_windows(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Include an unscoped fallback window for an aggregate Claude restriction."""

    if record["windows"]:
        return record["windows"]
    if record["status"] in {"rejected", "allowed_warning"}:
        return [{"scope": None, "window_minutes": None,
                 "used_percent": None, "resets_at": None}]
    return []


def _effective_window_reset(window: dict[str, Any], observed_at_ms: int) -> int:
    # Explicit resets are exclusive; the reset-less fallback is live through
    # observed + horizon, inclusively, so its equivalent reset is one ms later.
    return (window["resets_at"] if window["resets_at"] is not None
            else observed_at_ms + EVIDENCE_HORIZON_MS + 1)


def _window_covered(
    window: dict[str, Any], earlier: dict[str, Any], *,
    observed_at_ms: int, earlier_observed_at_ms: int,
) -> bool:
    """Whether an earlier window already restricts at least as much, as long."""

    def used(item: dict[str, Any]) -> float:
        return -1.0 if item["used_percent"] is None else float(item["used_percent"])

    if (earlier["scope"], earlier["window_minutes"]) != (
        window["scope"], window["window_minutes"]
    ) or used(earlier) < used(window):
        return False
    return _effective_window_reset(earlier, earlier_observed_at_ms) >= _effective_window_reset(
        window, observed_at_ms
    )


def _more_restrictive(record: dict[str, Any], prior: list[dict[str, Any]]) -> bool:
    """Whether a run's new rate-limit record restricts more than its earlier ones.

    Billing is ranked usable over unknown over unusable, and only a higher rank
    is kept, because admission reads the latest billing record. A headroom
    record is kept for a higher status, or for any window that no earlier
    record covers. Only an earlier record at least as restrictive in status can
    cover a window, so a rejection is never hidden behind an allowed reading
    that happened to last longer.
    """

    if not prior:
        return True
    if record["record_type"] == "provider_billing":
        return BILLING_RANK[record["paid_usage_usable"]] > max(
            BILLING_RANK[item["paid_usage_usable"]] for item in prior
        )
    status = CLAUDE_STATUS_RANK[record["status"]]
    covering = [item for item in prior if CLAUDE_STATUS_RANK[item["status"]] >= status]
    if not covering:
        return True
    return any(
        not any(
            _window_covered(window, other, observed_at_ms=record["observed_at_ms"],
                            earlier_observed_at_ms=item["observed_at_ms"])
            for item in covering for other in _restriction_windows(item)
        )
        for window in _restriction_windows(record)
    )


def _merged_restriction(
    records: list[dict[str, Any]], *, observed_at_ms: int
) -> dict[str, Any]:
    """One headroom record as restrictive as all of a run's: the highest status,
    and per window the highest use and the latest effective reset.

    A reset-less or empty aggregate restriction counts through a day from its
    own observation. Its equivalent exclusive reset is one millisecond later.
    Every merged window has an explicit reset at least as late as its inputs,
    so neither merging nor a clock rollback can shorten that restriction.
    """

    status = max((item["status"] for item in records), key=CLAUDE_STATUS_RANK.__getitem__)
    windows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in records:
        for window in _restriction_windows(item):
            key = (window["scope"], window["window_minutes"])
            effective_reset = _effective_window_reset(window, item["observed_at_ms"])
            merged = windows.get(key)
            if merged is None:
                windows[key] = {**window, "resets_at": effective_reset}
                continue
            used = [value for value in (merged["used_percent"], window["used_percent"])
                    if value is not None]
            merged["used_percent"] = max(used) if used else None
            merged["resets_at"] = max(merged["resets_at"], effective_reset)
    last = records[-1]
    return _headroom_record(
        provider=last["provider"],
        account_fingerprint=last["account_fingerprint"],
        windows=[windows[key] for key in sorted(windows, key=repr)],
        status=status,
        run_id=last["run_id"],
        phase=last["phase"],
        observed_at_ms=observed_at_ms,
    )


more_restrictive = _more_restrictive
merged_restriction = _merged_restriction
