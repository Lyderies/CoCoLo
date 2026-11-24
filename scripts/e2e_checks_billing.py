"""L3: observation and billing."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import PROBE, _SCENES, Phase, check  # noqa: E402


def reason_of(reply: dict | None) -> str | None:
    return (reply or {}).get("reason_code")


def observe_scene():
    """L3: observations of both fake clients and the Claude billing probe, with the refusals that must come
    first (a probe that completed is reused for a while, and a reused probe is not admitted again)."""
    if "observe" in _SCENES:
        return _SCENES["observe"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["observe"] = scene
    scene.setup = world.setup(world.answers())
    world.claude_frames(e2e_support.claude_probe_frames())
    scene.codex = world.cli("provider-observe", "--provider", "codex")
    scene.claude = world.cli("provider-observe", "--provider", "claude")
    # Refusals. Each is an expected refusal of its own step (exit 2 with a reason code).
    scene.api_key = world.host_cli("claude", *PROBE, refuses=True, environment={"ANTHROPIC_API_KEY": "e2e-not-a-key"})
    world.claude_version("9.9.9 (Claude Code)")
    scene.unreviewed = world.host_cli("claude", *PROBE, refuses=True)
    world.claude_version(None)
    world.claude_signed_in(False)
    scene.signed_out = world.cli("provider-observe", "--provider", "claude", refuses=True)
    world.claude_signed_in(True)
    scene.signed_in_again = world.cli("provider-observe", "--provider", "claude")
    # Another account that never attested: the probe is not admitted (the control for L3.2).
    world.claude_account("someone-else@example.invalid")
    scene.unattested = world.host_cli("claude", *PROBE, refuses=True)
    world.claude_account(None)
    # The probe itself, then the attestations the admission rested on.
    scene.probe = world.host_cli("claude", *PROBE)
    scene.status = world.cli("learning-status")
    return scene


def records_of(reply: dict | None, record_type: str) -> list[dict]:
    return [r for r in (reply or {}).get("records", []) if r.get("record_type") == record_type]


@check("L3.1", group="observe")
def l3_1_observe_both_clients_and_claude_billing_probe(phase: Phase) -> None:
    scene = observe_scene()
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    for provider, (code, reply, _text) in (("codex", scene.codex), ("claude", scene.claude)):
        phase.expect(code == 0 and reply is not None and reply.get("ready") is True,
                     f"provider-observe succeeds for the fake {provider} client")
        phase.expect(reply.get("provider") == provider, f"the {provider} observation names its provider")
        identity = records_of(reply, "provider_identity")
        phase.expect(len(identity) == 1 and identity[0].get("logged_in") is True,
                     f"the {provider} observation holds one signed-in identity")
        phase.expect(len(records_of(reply, "provider_billing")) >= 1 or provider == "claude",
                     "the codex observation holds its billing record")
    code, reply, _ = scene.probe
    if reply is not None and reason_of(reply):
        phase.reason(reason_of(reply))
    phase.expect(code == 0 and reply is not None and reply.get("ready") is True, "the Claude billing probe succeeds")
    phase.expect(reply.get("probe") == "launched", "the probe really ran the client")
    billing = records_of(reply, "provider_billing")
    phase.expect(len(billing) == 1 and billing[0].get("paid_usage_usable") is False
                 and billing[0].get("is_using_overage") is False,
                 "the probe's billing record shows no paid usage")
    phase.expect(reply.get("request_id", "").startswith("cli-billing-"), "the probe reply carries its request id")


@check("L3.2", group="observe")
def l3_2_billing_attestation_recorded_and_admission_accepts_it(phase: Phase) -> None:
    scene = observe_scene()
    code, status, _ = scene.status
    phase.expect(code == 0 and status is not None, "the collector answers its status")
    attested = {name: (value or {}).get("state") for name, value in (status.get("provider_attestations") or {}).items()}
    phase.expect(attested == {"codex": "present", "claude": "present"}, "a billing attestation is recorded for each provider")
    # Admission accepts it: the probe was admitted on the attested account ...
    code, reply, _ = scene.probe
    phase.expect(code == 0 and reply is not None and reply.get("probe") == "launched",
                 "admission accepted the attested Claude account (the probe was launched)")
    # ... and the same admission refuses an account with no attestation, so the attestation is what it accepted.
    code, reply, _ = scene.unattested
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "billing_attestation_missing",
                 "an account with no attestation is refused with billing_attestation_missing")


@check("L3.3", group="observe")
def l3_3_client_version_unreviewed_refused(phase: Phase) -> None:
    code, reply, _ = observe_scene().unreviewed
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "client_version_unreviewed",
                 "an unreviewed client version is refused with client_version_unreviewed")


@check("L3.3", group="observe")
def l3_3_api_key_in_environment_refused(phase: Phase) -> None:
    code, reply, _ = observe_scene().api_key
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "provider_api_key_present",
                 "an API key in the environment is refused with provider_api_key_present")


@check("L3.3", group="observe")
def l3_3_signed_out_client_refused(phase: Phase) -> None:
    scene = observe_scene()
    code, reply, _ = scene.signed_out
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "observation_unavailable",
                 "a signed-out client is refused with observation_unavailable")
    code, reply, _ = scene.signed_in_again
    phase.expect(code == 0 and reply is not None and reply.get("ready") is True,
                 "the same client observes again once it is signed in")


def paid_scene():
    """L3: an account whose Claude client reports paid usage (extra usage in use), on its own machine."""
    if "paid" not in _SCENES:
        world = e2e_support.World().__enter__()
        scene = SimpleNamespace(world=world)
        _SCENES["paid"] = scene
        scene.setup = world.setup(world.answers())
        world.claude_frames(e2e_support.claude_probe_frames(isUsingOverage=True, overageStatus="allowed"))
        scene.first = world.host_cli("claude", *PROBE, refuses=True)
        scene.second = world.host_cli("claude", *PROBE, refuses=True)
    return _SCENES["paid"]


@check("L3.3", group="paid")
def l3_3_paid_usage_on_refused(phase: Phase) -> None:
    scene = paid_scene()
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    first_code, first, _ = scene.first
    phase.reason(reason_of(first) or "probe_ready")
    code, reply, _ = scene.second
    phase.reason(reason_of(reply) or "probe_ready")
    billing = records_of(reply, "provider_billing")
    shown = [b for b in billing if b.get("paid_usage_usable") is True or b.get("is_using_overage") is True]
    phase.expect(code == 2 and reason_of(reply) == "paid_usage_usable",
                 "a probe after paid usage was seen is refused with paid_usage_usable"
                 + (" (it was answered ready with the paid-usage record reused)" if shown and code == 0 else ""))
