"""Legacy admission calculations over trusted metadata values.

These calculations import no provider client, persistence, key or process code.
Outputs are unsaved data, not caller authentication, verified signatures, fresh
owner checks or permission to launch. Callers holding arbitrary Python objects
remain outside any serialization/authority boundary. Future fixed helper inputs
must come from closed bounded data decoding; no normal API is enabled here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from copy import deepcopy
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


# USAGE-RECHECK-1 (decision 134 pending): an exhausted provider whose reset is far away (or unmeasured) is
# re-read at most once per interval, so an early provider reset is seen. Owner to confirm both numbers.
USAGE_RECHECK_INTERVAL_MS = 3_600_000
USAGE_RECHECK_FAR_RESET_MS = 3_600_000


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


def select_provider_identity(provider, identities, now_ms):
    """Open and advisory routing use the same freshness and ledger tie order."""
    return _latest([item for item in identities if item[0]["provider"] == provider
                    and 0 <= now_ms - item[0]["observed_at_ms"] <= IDENTITY_MAX_AGE_MS])


def _window_live(window: dict[str, Any], observed_at_ms: int, now_ms: int) -> bool:
    # A window counts until it resets; one with no reset time counts for five hours.
    if window["resets_at"] is not None:
        return window["resets_at"] > now_ms
    # Future observations still restrict after a clock rollback, just like
    # future paid-usage evidence; a favourable older reading cannot clear them.
    return now_ms - observed_at_ms <= FIVE_HOURS_MS


def _headroom_state(
    provider: str, record: dict[str, Any], now_ms: int, *, unknown_when_unmeasured: bool = False,
) -> str | None:
    """Rate one record; keep legacy ample for unmeasured live windows by default.

    The usage classifier requests unknown for those windows, so they neither
    establish availability nor clear a resetless restriction.
    """

    windows = [
        window for window in record["windows"]
        if _window_live(window, record["observed_at_ms"], now_ms)
    ]
    if not windows:
        # A producer may report only an aggregate Claude status, including an
        # empty unifiedWindows map. With no reset information, use the same
        # fallback age bound. Explicitly expired windows never use this path.
        if (provider == "claude" and not record["windows"]
                and now_ms - record["observed_at_ms"] <= FIVE_HOURS_MS):
            if record["status"] == "rejected":
                return "exhausted"
            if record["status"] == "allowed_warning":
                return "constrained"
            if record["status"] == "allowed":
                return "ample"
        return None
    used = [window["used_percent"] for window in windows if window["used_percent"] is not None]
    if provider == "codex":
        if any(value >= CODEX_EXHAUSTED_PERCENT for value in used):
            return "exhausted"
        if any(value >= CODEX_CONSTRAINED_PERCENT for value in used):
            return "constrained"
        return "ample" if used or not unknown_when_unmeasured else None
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
    return "ample" if used or not unknown_when_unmeasured else None


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
    recheck_attempts_ms: tuple[int, ...] | list[int] = (),
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
    identity_item = select_provider_identity(provider, identities, now_ms)
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
    if state == "exhausted" and not (probe and usage_recheck_due(provider, fingerprint, headroom, billing, now_ms,
                                                                recheck_attempts_ms=recheck_attempts_ms)):
        # USAGE-RECHECK-2: only a due re-check probe passes an exhausted account; every other gate still applies.
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


@dataclass(frozen=True)
class ProviderStateSnapshot:
    """Service-built advisory state. Never deserialized from task/assignment data."""

    project_id: str
    evaluated_at_ms: int
    providers: dict[str, Any]
    # USAGE-RECHECK-5: the route's own re-check audit, added by the CLI after its fresh reading; never on the wire.
    recheck: Any = None

    def audit(self) -> dict[str, Any]:
        from .learning_contracts import content_digest
        body = {"format_version": 1, "classifier": "RT-RX-1", "project_id": self.project_id,
                "evaluated_at_ms": self.evaluated_at_ms, "providers": deepcopy(self.providers)}
        return {**body, "snapshot_ref": content_digest(body), "status": "checked",
                **({"recheck": deepcopy(self.recheck)} if self.recheck else {})}


def _headroom_components(record):
    # Merges retain original chronology and reset provenance outside legacy windows.
    return record.get("restriction_provenance", [record])


def classify_provider_state(provider, fingerprint, headroom, billing, now_ms, *, recheck_attempts_ms=()):
    """One pure usage classifier for routing, admission and the launch recheck.

    A clearing reading replaces only older resetless components. Measured resets,
    billing restrictions and future observations remain independently applicable.
    `recheck_attempts_ms` are the times of this account's re-check attempts (USAGE-RECHECK-5): each starts the
    re-check interval like a reading, whether or not it produced one.
    """
    rank = {"unknown": -1, "ample": 0, "constrained": 1, "exhausted": 2}
    own = []
    # USAGE-RECHECK-3: the reset each window's own reading reported (id of the window -> reset). A merge lends every
    # component the latest reset of its window; that inherited reset is never proof of an early reset.
    reported = {}
    for record, digest in headroom:
        if record["provider"] != provider or record["account_fingerprint"] != fingerprint:
            continue
        if "restriction_provenance" not in record:
            own.append((record, digest))
            continue
        components = _headroom_components(record)
        # Preserve the conservative overflow merge for measured resets: each
        # window's restriction lasts through the latest measured reset for it.
        # Synthesized resetless expiries never extend a measured restriction.
        resets = {}
        for component in components:
            for window in component["windows"]:
                if window["resets_at"] is not None:
                    key = (window["scope"], window["window_minutes"])
                    resets[key] = max(resets.get(key, window["resets_at"]), window["resets_at"])
        for component in components:
            windows = [
                {**window, "resets_at": resets[(window["scope"], window["window_minutes"])]}
                if window["resets_at"] is not None else window
                for window in component["windows"]
            ]
            reported.update((id(merged), window["resets_at"]) for merged, window in zip(windows, component["windows"]))
            own.append(({**component, "windows": windows}, digest))
    def reset_early(record, window):
        # USAGE-RECHECK-3 (decision 134 pending): a strictly later reading of the same window that rates it
        # clear and reports its own, different reset proves the provider reset that window early.
        # Only a far reset (more than USAGE_RECHECK_FAR_RESET_MS away when the clearing reading was taken) can be
        # proved early; a near one is waited for, as RT-RX-5 and the fixed admission order (test_providers)
        # require. A proof, once made, does not lapse as the old reset nears.
        # Only a window of known identity (a scope or a duration) can be matched: two unnamed windows may be
        # different windows. Both resets compared are the ones the readings themselves reported.
        key = (window.get("scope"), window.get("window_minutes"))
        if key == (None, None):
            return False
        blocking = {window["resets_at"], reported.get(id(window), window["resets_at"])}
        for later, _ in own:
            if not record["observed_at_ms"] < later["observed_at_ms"] <= now_ms:
                continue
            if window["resets_at"] - later["observed_at_ms"] <= USAGE_RECHECK_FAR_RESET_MS:
                continue
            for candidate in later["windows"]:
                own_reset = reported.get(id(candidate), candidate["resets_at"])
                if ((candidate.get("scope"), candidate.get("window_minutes")) == key
                        and own_reset is not None and own_reset not in blocking
                        and _headroom_state(provider, {**later, "windows": [candidate]}, later["observed_at_ms"],
                                            unknown_when_unmeasured=True) in {"ample", "constrained"}):
                    return True
        return False

    clearing = [(record, digest) for record, digest in own
                if record["observed_at_ms"] <= now_ms
                and _headroom_state(provider, record, now_ms, unknown_when_unmeasured=True) in {"ample", "constrained"}]
    newest_clear = _latest(clearing)
    rated = []
    cleared = []
    for record, digest in own:
        explicit = [w for w in record["windows"] if w["resets_at"] is not None]
        if explicit:
            remaining = [w for w in explicit if not reset_early(record, w)]
            if not remaining and _headroom_state(provider, {**record, "windows": explicit}, now_ms,
                                                 unknown_when_unmeasured=True) in {"exhausted", "constrained"}:
                cleared.append(digest)
            explicit = remaining
        resetless = [w for w in record["windows"] if w["resets_at"] is None]
        components = []
        if explicit:
            components.append(({**record, "windows": explicit}, False))
        if resetless or not record["windows"]:
            components.append(({**record, "windows": resetless}, True))
        for component, fallback in components:
            state = _headroom_state(provider, component, now_ms, unknown_when_unmeasured=True)
            if state is None:
                continue
            if (fallback and state in {"exhausted", "constrained"} and newest_clear is not None
                    and newest_clear[0]["observed_at_ms"] > record["observed_at_ms"]):
                cleared.append(digest)
                continue
            resets = [w["resets_at"] for w in component["windows"]
                      if w["resets_at"] is not None and w["resets_at"] > now_ms
                      and _headroom_state(provider, {**component, "windows": [w]}, now_ms,
                                          unknown_when_unmeasured=True) == state]
            rated.append((state, digest, resets,
                          record["observed_at_ms"] + FIVE_HOURS_MS + 1 if fallback else None,
                          "headroom"))
    if provider == "codex":
        own_billing = [(r, d) for r, d in billing
                       if r["provider"] == provider and r["account_fingerprint"] == fingerprint]
        latest = _latest([(r, d) for r, d in own_billing if r["observed_at_ms"] <= now_ms])
        for record, digest in own_billing:
            if record.get("ordinary_usage_allowed") is False and (
                    record["observed_at_ms"] > now_ms or (latest is not None and (record, digest) == latest)):
                rated.append(("exhausted", digest, [], None, "ordinary_usage_denied"))
    state = max((item[0] for item in rated), key=rank.__getitem__, default="unknown")
    deciding = [item for item in rated if item[0] == state]
    # Admission cites only one deciding record. The route additionally explains clearing.
    digests = list(dict.fromkeys(item[1] for item in deciding))[-1:]
    if cleared and newest_clear is not None:
        digests = list(dict.fromkeys([*digests, cleared[-1], newest_clear[1]]))
    resets = sorted({reset for item in deciding for reset in item[2]})
    expiries = [item[3] for item in deciding if item[3] is not None]
    # USAGE-RECHECK-1: the newest reading of this account (any window, or Codex billing) starts the interval.
    readings = [r["observed_at_ms"] for r, _ in own if r["observed_at_ms"] <= now_ms] + [
        r["observed_at_ms"] for r, _ in billing
        if r["provider"] == provider and r["account_fingerprint"] == fingerprint and r["observed_at_ms"] <= now_ms]
    attempts = [at for at in recheck_attempts_ms if type(at) is int and at <= now_ms]
    recheck_due = None
    if (state == "exhausted" and readings
            and (not resets or min(resets) - now_ms > USAGE_RECHECK_FAR_RESET_MS)):
        recheck_due = max(readings + attempts) + USAGE_RECHECK_INTERVAL_MS
    return {"state": state, "known": state != "unknown", "evidence_digests": digests,
            "reasons": list(dict.fromkeys(item[4] for item in deciding)),
            "next_reset_ms": min(resets, default=None), "blocking_resets_ms": resets,
            "fallback_expiry_ms": max(expiries, default=None),
            "decision_digest": deciding[-1][1] if deciding else None, "recheck_due_at_ms": recheck_due,
            "last_reading_at_ms": max(readings, default=None)}


def usage_recheck_due(provider, fingerprint, headroom, billing, now_ms, *, recheck_attempts_ms=()):
    """USAGE-RECHECK-1: whether an exhausted account is due its bounded fresh reading."""
    due = classify_provider_state(provider, fingerprint, headroom, billing, now_ms,
                                  recheck_attempts_ms=recheck_attempts_ms)["recheck_due_at_ms"]
    return due is not None and due <= now_ms


def headroom_state(provider, fingerprint, headroom, billing, now_ms):
    """Admission retains its validated three-rating vocabulary; unknown passes usage."""
    result = classify_provider_state(provider, fingerprint, headroom, billing, now_ms)
    return (result["state"] if result["known"] else "ample", result["decision_digest"])


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


def probe_retry_at(passed_at_ms: int) -> str:
    """The account-wide cooldown ends five minutes after the latest qualifying pass."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp((passed_at_ms + 300_000) / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def probe_head(
    headroom: list[tuple[dict[str, Any], str]], fingerprint: str, latest: dict[str, Any],
) -> dict[str, Any] | None:
    """Only a unique headroom row with the reused billing row's probe provenance is its own."""
    probe_id = latest.get("probe_id")
    if probe_id is None:
        return None
    own = [record for record, _ in headroom
           if record["account_fingerprint"] == fingerprint and record.get("run_id") is None
           and record.get("phase") is None and record.get("probe_id") == probe_id]
    return own[0] if len(own) == 1 else None


def newer_run_restricts(
    latest: dict[str, Any],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    fingerprint: str,
    now_ms: int,
    order: dict[str, int],
) -> bool:
    """R2: a run record newer than the probe's latest billing that is more restrictive (paid usage usable,
    overage in use, or headroom more restrictive than the probe's own) means the probe's reading is not enough.

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
    if not newer:
        return False
    # PC-1: a newer run record blocks the reuse only when it is MORE restrictive than the reused probe's own
    # reading; one as constrained as the probe's own changes nothing the probe did not already show (the
    # re-evaluation and the final gate still judge it). Equal rank is left to that re-evaluation even when the
    # restriction sits on another window (LOW-3): it reads every headroom record, the run's included.
    #
    # PC-5: timestamps cannot establish ownership (complementary partial failures can share a stamp).
    # Missing or ambiguous provenance keeps the legacy ample baseline, so any constrained run blocks.
    rank = {"ample": 0, "constrained": 1, "exhausted": 2}
    head = probe_head(headroom, fingerprint, latest)
    own = [] if head is None else [(head, "")]
    return (rank[headroom_state("claude", fingerprint, newer, [], now_ms)[0]]
            > rank[headroom_state("claude", fingerprint, own, [], now_ms)[0]])


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
