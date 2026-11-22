"""Legacy admission calculations over trusted metadata values.

These calculations import no provider client, persistence, key or process code.
Outputs are unsaved data, not caller authentication, verified signatures, fresh
owner checks or permission to launch. Callers holding arbitrary Python objects
remain outside any serialization/authority boundary. Future fixed helper inputs
must come from closed bounded data decoding; no normal API is enabled here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .learning_contracts import CLAUDE_SUBSCRIPTION_TYPES, CODEX_SUBSCRIPTION_PLAN_TYPES, parse_semver

API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


IDENTITY_MAX_AGE_MS = 30 * 60 * 1000


BILLING_MAX_AGE_MS = 30 * 60 * 1000


EVIDENCE_HORIZON_MS = 24 * 60 * 60 * 1000


CODEX_EXHAUSTED_PERCENT = 90


CODEX_CONSTRAINED_PERCENT = 75


FIVE_HOUR_WINDOW_MINUTES = 300


FIVE_HOURS_MS = FIVE_HOUR_WINDOW_MINUTES * 60 * 1000


def api_keys_present(environ: Mapping[str, str]) -> bool:
    return any(environ.get(name) for name in API_KEY_VARIABLES)


def _latest(records: list[tuple[dict[str, Any], str]]) -> tuple[dict[str, Any], str] | None:
    # Records arrive in ledger order, so a later record wins an observation-time tie.
    ranked = max(
        enumerate(records),
        key=lambda item: (item[1][0]["observed_at_ms"], item[0]),
        default=None,
    )
    return None if ranked is None else ranked[1]


def _window_live(window: dict[str, Any], observed_at_ms: int, now_ms: int) -> bool:
    # A window counts until it resets; one with no reset time counts for a day.
    if window["resets_at"] is not None:
        return window["resets_at"] > now_ms
    # Future observations still restrict after a clock rollback, just like
    # future paid-usage evidence; a favourable older reading cannot clear them.
    return now_ms - observed_at_ms <= EVIDENCE_HORIZON_MS


def _headroom_state(provider: str, record: dict[str, Any], now_ms: int) -> str | None:
    """Rate one headroom record: exhausted, constrained or ample; None once reset."""

    windows = [
        window for window in record["windows"]
        if _window_live(window, record["observed_at_ms"], now_ms)
    ]
    if not windows:
        # A producer may report only an aggregate Claude status, including an
        # empty unifiedWindows map. With no reset information, use the same
        # fallback age bound. Explicitly expired windows never use this path.
        if (provider == "claude" and not record["windows"]
                and now_ms - record["observed_at_ms"] <= EVIDENCE_HORIZON_MS):
            if record["status"] == "rejected":
                return "exhausted"
            if record["status"] == "allowed_warning":
                return "constrained"
        return None
    used = [window["used_percent"] for window in windows if window["used_percent"] is not None]
    if provider == "codex":
        if any(value >= CODEX_EXHAUSTED_PERCENT for value in used):
            return "exhausted"
        if any(value >= CODEX_CONSTRAINED_PERCENT for value in used):
            return "constrained"
        return "ample"
    # Under a Claude primary, Claude children spend the primary's own allowance,
    # so Claude windows use the same thresholds as Codex.
    if record["status"] == "rejected" or any(
        value >= CODEX_EXHAUSTED_PERCENT for value in used
    ):
        return "exhausted"
    if record["status"] == "allowed_warning" or any(
        value >= CODEX_CONSTRAINED_PERCENT for value in used
    ):
        return "constrained"
    return "ample"


def _admission_limits(settings: Any) -> tuple[tuple[int, int, int], tuple[int, int, int], int | None] | None:
    """The reviewed version range and run cap, or None when the settings are unusable."""

    if not isinstance(settings, dict):
        return None
    versions = settings.get("client_versions")
    cap = settings.get("max_admissions_per_window", False)
    if not isinstance(versions, dict):
        return None
    minimum = parse_semver(versions.get("minimum"))
    maximum = parse_semver(versions.get("maximum_exclusive"))
    if minimum is None or maximum is None or not minimum < maximum:
        return None
    if cap is not None and (
        not isinstance(cap, int) or isinstance(cap, bool) or cap < 1
    ):
        return None
    return minimum, maximum, cap


def window_start_ms(
    provider: str,
    fingerprint: str,
    headroom: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> tuple[int, str | None]:
    """Where the current five-hour usage window began, and the record that says so.

    A recorded five-hour reset still ahead, and no more than one window away,
    places a window that began five hours before it. The earliest such start
    among every observation still live, including future-stamped observations
    after clock rollback, is used. A later reading cannot free places while an
    earlier window has not reset. With no such reset known, use the last five hours.
    """

    starts = [
        (window["resets_at"] - FIVE_HOURS_MS, digest)
        for record, digest in headroom
        if record["provider"] == provider
        and record["account_fingerprint"] == fingerprint
        for window in record["windows"]
        if window["window_minutes"] == FIVE_HOUR_WINDOW_MINUTES
        and window["resets_at"] is not None
        and now_ms < window["resets_at"] <= now_ms + FIVE_HOURS_MS
    ]
    if starts:
        return min(starts, key=lambda item: item[0])
    return now_ms - FIVE_HOURS_MS + 1, None


def evaluate_admission(
    *,
    provider: str,
    identities: list[tuple[dict[str, Any], str]],
    attestations: list[tuple[dict[str, Any], str]],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    prior_claude_admissions: set[str],
    environ: Mapping[str, str],
    now_ms: int,
    admission_policy: Any = None,
    prior_run_admissions: list[tuple[str, int]] | None = None,
    window_headroom: list[tuple[dict[str, Any], str]] | None = None,
    cited_digests: set[str] | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Apply the admission checks in their fixed order; first failure wins.

    Each input is a list of (record, content digest) pairs for one project.
    Restrictive evidence wins: a later favourable record never clears an earlier
    restriction that still applies. `prior_claude_admissions` holds the account
    fingerprints that already have an admitted Claude run.
    `admission_policy` is the provider's committed admission settings: its
    reviewed client range and its run cap. `prior_run_admissions` lists the
    (account fingerprint, decision time) of earlier passing run admissions for
    this provider; billing probes are not runs and are not in it.
    `window_headroom` is the account's headroom from every project, which
    places the cap window; both cap inputs are ledger-wide.
    Billing, headroom and prior admissions are the account's, from every
    project; `cited_digests` lists the records this project holds, and only
    those are cited as evidence. Attestations stay per project.
    `probe` evaluates a Claude billing probe, whose purpose is to refresh
    stale billing, so it does not need fresh billing itself.
    The result is an unsaved decision; the ledger binds it to a run.
    """

    def decision(
        reason: str,
        fingerprint: str | None = None,
        evidence: list[str] | None = None,
        basis: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "decision": "pass" if reason == "admitted" else "deny",
            "reason_code": reason,
            "admission_basis": basis,
            "account_fingerprint": fingerprint,
            "evidence_digests": sorted(
                set(evidence or [])
                if cited_digests is None else set(evidence or []) & cited_digests
            ),
            "observed_at_ms": now_ms,
        }

    if api_keys_present(environ):
        return decision("provider_api_key_present")
    limits = _admission_limits(admission_policy)
    if limits is None or prior_run_admissions is None or window_headroom is None:
        return decision("admission_policy_unavailable")
    minimum_version, maximum_version, run_cap = limits
    current = [
        item for item in identities
        if item[0]["provider"] == provider
        and 0 <= now_ms - item[0]["observed_at_ms"] <= IDENTITY_MAX_AGE_MS
    ]
    identity_item = _latest(current)
    if identity_item is None:
        return decision("provider_identity_missing")
    identity, identity_digest = identity_item
    fingerprint = identity["account_fingerprint"]
    evidence = [identity_digest]
    version = parse_semver(identity.get("client_version"))
    if version is None or not minimum_version <= version < maximum_version:
        return decision("client_version_unreviewed", fingerprint, evidence)
    attestation = _latest([
        item for item in attestations
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ])
    if attestation is None:
        return decision("billing_attestation_missing", fingerprint, evidence)
    evidence.append(attestation[1])
    if provider == "claude" and not (
        identity["logged_in"]
        and identity["auth_method"] == "claude.ai"
        and identity["subscription_type"] in CLAUDE_SUBSCRIPTION_TYPES
    ):
        return decision("claude_subscription_missing", fingerprint, evidence)
    if provider == "codex" and identity["plan_type"] not in CODEX_SUBSCRIPTION_PLAN_TYPES:
        return decision("codex_plan_not_subscription", fingerprint, evidence)
    spent = spend_control_digest(provider, fingerprint, billing, now_ms)
    if spent is not None:
        return decision("codex_spend_control_reached", fingerprint, [*evidence, spent])
    paid = paid_usage_digest(provider, fingerprint, billing, now_ms, attestation[0])
    if paid is not None:
        # Any recent sign of paid usage denies; a later favourable record
        # cannot clear it.
        return decision("paid_usage_usable", fingerprint, [*evidence, paid])
    latest_billing = _latest([
        item for item in billing
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
        and item[0]["observed_at_ms"] <= now_ms
    ])
    fresh = (
        latest_billing is not None
        and now_ms - latest_billing[0]["observed_at_ms"] <= BILLING_MAX_AGE_MS
    )
    basis = "evidence"
    if provider == "codex":
        # Codex billing comes from the same non-spending read, so it is required.
        if not fresh:
            return decision("billing_unverified", fingerprint, evidence)
    elif probe and latest_billing is not None:
        # A probe refreshes billing, so older evidence is enough to admit it;
        # that evidence must still show paid usage off.
        fresh = True
    elif not fresh:
        # Claude has no non-spending billing source. One bootstrap run per
        # account may open on the attestation and identity; its own rate-limit
        # event, or a probe, must record fresh billing before another Claude
        # run is admitted.
        if fingerprint in prior_claude_admissions:
            return decision("billing_unverified", fingerprint, evidence)
        basis = "bootstrap"
    if fresh:
        evidence.append(latest_billing[1])
        if latest_billing[0]["paid_usage_usable"] is not False and not credits_waived(
            provider, latest_billing[0], attestation[0]
        ):
            return decision("billing_unverified", fingerprint, evidence)
    state, state_digest = headroom_state(provider, fingerprint, headroom, billing, now_ms)
    if state_digest is not None:
        evidence.append(state_digest)
    if state == "exhausted":
        return decision("provider_exhausted", fingerprint, evidence)
    if run_cap is not None:
        # A backstop on run count per usage window, whatever headroom says.
        # A reading from another project still places the window; the
        # decision cites only this project's records.
        start, window_digest = window_start_ms(
            provider, fingerprint, window_headroom, now_ms
        )
        # An admission stamped after now still counts, so a clock that steps
        # back never frees places under the cap.
        counted = sum(
            1 for admitted, at in prior_run_admissions
            if admitted == fingerprint and start <= at
        )
        if counted >= run_cap:
            return decision(
                "window_run_cap_reached", fingerprint,
                [*evidence, *([window_digest] if window_digest else [])],
            )
    return decision("admitted", fingerprint, evidence, basis)


def credits_waived(provider: str, record: dict[str, Any], attestation: Any) -> bool:
    """Whether the owner's attestation turns a Codex credits reading into unusable.

    Only when the current attestation says credit spending is disabled, the
    reading shows credits that are not unlimited, and spend control has not been
    reached. Any other reading keeps its observed billing meaning.
    """

    return (
        provider == "codex"
        and attestation is not None
        and attestation.get("codex_credits_spending_disabled") is True
        and record.get("has_credits") is True
        and record.get("unlimited") is False
        and record.get("spend_control_reached") is not True
    )


def spend_control_digest(
    provider: str,
    fingerprint: str,
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> str | None:
    """The digest of a Codex billing record from the last day reporting spend control reached."""

    if provider != "codex":
        return None
    for record, digest in billing:
        if (
            record["provider"] == provider
            and record["account_fingerprint"] == fingerprint
            and now_ms - record["observed_at_ms"] <= EVIDENCE_HORIZON_MS
            and record.get("spend_control_reached") is True
        ):
            return digest
    return None


def paid_usage_digest(
    provider: str,
    fingerprint: str,
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
    attestation: Any = None,
) -> str | None:
    """The digest of a billing record from the last day showing paid usage, if any.

    A record stamped after now also counts, so a clock that steps back never
    hides paid usage.
    """

    for record, digest in billing:
        if (
            record["provider"] == provider
            and record["account_fingerprint"] == fingerprint
            and now_ms - record["observed_at_ms"] <= EVIDENCE_HORIZON_MS
            and (
                (
                    record["paid_usage_usable"] is True
                    and not credits_waived(provider, record, attestation)
                )
                or record["is_using_overage"] is True
            )
        ):
            return digest
    return None


def headroom_state(
    provider: str,
    fingerprint: str,
    headroom: list[tuple[dict[str, Any], str]],
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> tuple[str, str | None]:
    """The most restrictive unreset headroom, and the latest record at that level.

    Only the latest record at the deciding level is named: a decision's digest
    list is bounded, and runs keep adding headroom records. For Codex, the
    latest billing reading disallowing ordinary usage also exhausts it, and so
    does any such reading stamped after now, so a clock step back never hides it.
    """

    rank = {"ample": 0, "constrained": 1, "exhausted": 2}
    state = "ample"
    state_digest: str | None = None
    for record, digest in headroom:
        # A record stamped after now still restricts, so a clock step back
        # never hides an exhausted window.
        if record["provider"] != provider or record["account_fingerprint"] != fingerprint:
            continue
        rated = _headroom_state(provider, record, now_ms)
        if rated is None:
            continue
        if rank[rated] >= rank[state]:
            state, state_digest = rated, digest
    if provider == "codex":
        own = [
            item for item in billing
            if item[0]["provider"] == provider and item[0]["account_fingerprint"] == fingerprint
        ]
        latest = _latest([item for item in own if item[0]["observed_at_ms"] <= now_ms])
        if (latest is not None and latest[0]["ordinary_usage_allowed"] is False) or any(
            record["observed_at_ms"] > now_ms and record["ordinary_usage_allowed"] is False
            for record, _ in own
        ):
            state = "exhausted"
    return state, state_digest


def evaluate_launch(
    *,
    provider: str,
    identity: dict[str, Any],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    now_ms: int,
    bootstrap: bool = False,
    attestations: list[tuple[dict[str, Any], str]] | None = None,
) -> str | None:
    """Re-check an admitted run's eligibility just before its child launches.

    The account and client were already compared with the admitted ones. This
    re-reads the account-wide billing and headroom, including the fresh
    before-launch reading, and the plan the fresh identity reports. The run
    cap is not counted again. Billing must be from the last 30 minutes, for
    either provider; only a run admitted as the Claude bootstrap, on an account
    with no billing at all yet, launches without it. Returns a denial reason,
    or None to launch.
    """

    fingerprint = identity["account_fingerprint"]
    if provider == "codex" and identity["plan_type"] not in CODEX_SUBSCRIPTION_PLAN_TYPES:
        return "codex_plan_not_subscription"
    if provider == "claude" and not (
        identity["logged_in"]
        and identity["auth_method"] == "claude.ai"
        and identity["subscription_type"] in CLAUDE_SUBSCRIPTION_TYPES
    ):
        return "claude_subscription_missing"
    attestation = _latest([
        item for item in attestations or []
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ])
    current = None if attestation is None else attestation[0]
    if spend_control_digest(provider, fingerprint, billing, now_ms) is not None:
        return "codex_spend_control_reached"
    if paid_usage_digest(provider, fingerprint, billing, now_ms, current) is not None:
        return "paid_usage_usable"
    if attestation is None:
        # A replaced or swapped owner key cancels the statement a run was admitted on.
        return "billing_attestation_missing"
    latest = _latest([
        item for item in billing
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
        and item[0]["observed_at_ms"] <= now_ms
    ])
    if latest is None:
        if not (provider == "claude" and bootstrap):
            return "billing_unverified"
    elif now_ms - latest[0]["observed_at_ms"] > BILLING_MAX_AGE_MS:
        # A dispatch long after its open must refresh billing first (for
        # Claude, with a billing probe).
        return "billing_unverified"
    elif latest[0]["paid_usage_usable"] is not False and not credits_waived(
        provider, latest[0], current
    ):
        return "billing_unverified"
    state, _ = headroom_state(provider, fingerprint, headroom, billing, now_ms)
    if state == "exhausted":
        return "provider_exhausted"
    return None


def probe_latest(billing: list[tuple[dict[str, Any], str]], fingerprint: str) -> dict[str, Any] | None:
    """The newest billing reading of this account that a probe itself took (no run, no phase).

    A finished child's records are not probe evidence: they neither stand in for a probe's reading nor
    refresh its age (decision 50, BR-21)."""

    return max(
        (record for record, _ in billing
         if record["account_fingerprint"] == fingerprint
         and record.get("run_id") is None and record.get("phase") is None),
        key=lambda record: record["observed_at_ms"], default=None,
    )


def probe_head(headroom: list[tuple[dict[str, Any], str]], fingerprint: str) -> dict[str, Any] | None:
    """The newest observed headroom reading of this account that a probe itself took (not the last one stored)."""

    return max(
        (record for record, _ in headroom
         if record["account_fingerprint"] == fingerprint
         and record.get("run_id") is None and record.get("phase") is None),
        key=lambda record: record["observed_at_ms"], default=None,
    )


def newer_run_restricts(
    latest: dict[str, Any],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    fingerprint: str,
    now_ms: int,
    order: dict[str, int],
) -> bool:
    """R2: a run record newer than the probe's latest billing that is more restrictive (paid usage usable,
    overage in use, or headroom that is not ample) means the probe's favourable reading is not enough.

    BR-2 (decision 44): "newer" is serialized insertion order (`order`: record id to its row's insertion
    number) OR a later-or-equal observation time, so a restriction stored late with an earlier stamp still
    counts; freshness itself always uses the real observation time."""

    since, seen = latest["observed_at_ms"], order.get(latest["record_id"])

    def newer_than(record: dict[str, Any]) -> bool:
        return record["observed_at_ms"] >= since or (
            seen is not None and order.get(record["record_id"], -1) > seen)

    if any(
        record.get("run_id") is not None and newer_than(record)
        and (record.get("paid_usage_usable") is True or record.get("is_using_overage") is True)
        for record, _ in billing
    ):
        return True
    newer = [
        (record, digest) for record, digest in headroom
        if record["account_fingerprint"] == fingerprint
        and record.get("run_id") is not None and newer_than(record)
    ]
    return bool(newer) and headroom_state("claude", fingerprint, newer, [], now_ms)[0] != "ample"


def reuse_gate(
    *,
    environ: Mapping[str, str],
    process_environ: Mapping[str, str],
    identity_at: int | None,
    reuse_at: int | None,
    now_ms: int,
    since_ms: int,
    reuse_ms: int | None,
) -> str | None:
    """The shared last test of a yes (decision 50, BR-18, BR-21): a refusal reason, or None.

    Run after every caller callback, with nothing external after it. Whatever environment the caller passed,
    the process's own is looked at too: a provider API key in either refuses (subscription-only). The
    identity reading must lie in its window (since..now: a reading stamped after now is no evidence of now),
    and, for a reuse, the billing reading must be younger than the reuse age and not stamped after now."""

    if api_keys_present(environ) or api_keys_present(process_environ):
        return "provider_api_key_present"
    if identity_at is None or not since_ms <= identity_at <= now_ms:
        return "provider_identity_missing"
    if reuse_ms is not None and (reuse_at is None or not 0 <= now_ms - reuse_at < reuse_ms):
        return "billing_unverified"
    return None
