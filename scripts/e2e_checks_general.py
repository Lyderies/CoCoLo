"""G1 and G8: judged over every step of the installed project's life (the L1 scene)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import Phase, check  # noqa: E402
from e2e_checks_setup import update_scene  # noqa: E402


@check("G1", group="fresh")
def g1_every_step_exits_zero_or_refuses_as_expected(phase: Phase) -> None:
    scene = update_scene()
    world = scene.world
    # A refusal that is expected: an answers file missing an answer is refused by name before anything changes.
    broken = world.answers(commit=None)
    refused = world.setup(broken, refuses=True)
    phase.expect(refused.code == 2, "an answers file missing an answer is refused with exit 2")
    phase.expect("answers file: commit: " in refused.text and "Nothing was changed." in refused.text,
                 "the refusal names the missing answer and says nothing changed")
    phase.reason("answers_file_refused")
    steps = world.steps
    phase.expect(len(steps) >= 6, "the run has setup runs and owner commands to judge")
    for step in steps:
        if step["refuses"]:
            continue
        phase.expect(not step["timed_out"], "no step waited for ever")
        phase.expect(step["code"] == 0, "every step that is not an expected refusal exits 0")
    seen = e2e_support.bare_unavailable("\n".join(world.outputs + world.screens))
    for code in seen:
        phase.reason("bare_" + code)
    phase.expect(not seen, "no step answered with a bare run_unavailable, account_unavailable or probe_unavailable")
    # The detector itself: it flags a bare code and leaves a code with a cause alone.
    phase.expect(e2e_support.bare_unavailable('{"reason_code": "run_unavailable"}') == ["run_unavailable"],
                 "the check sees a bare run_unavailable")
    phase.expect(not e2e_support.bare_unavailable('{"reason_code": "run_unavailable_busy_probe"}'),
                 "the check leaves a reason that carries its cause")
    # G1 is also judged at capture, for every World and every step (e2e_support.capture_breaks): the judge itself.
    def judged(text, code, refuses=False, timed_out=False):
        return e2e_support.capture_breaks(text, kind="output", private={}, code=code, refuses=refuses, timed_out=timed_out)
    phase.expect(judged("", 2) == ["g1_unexpected_exit"], "an unexpected refusal breaks the rule at capture")
    phase.expect(not judged("", 2, refuses=True), "an expected refusal does not")
    phase.expect(judged("", -9, timed_out=True) == ["g1_step_timed_out"], "a step that waited for ever breaks it")
    phase.expect(judged('{"reason_code": "probe_unavailable"}', 0) == ["g1_bare_probe_unavailable"],
                 "a bare probe_unavailable breaks it")


@check("G8", group="fresh")
def g8_no_private_value_in_any_output(phase: Phase) -> None:
    """Owner decision 36: what is saved, logged or read by an AI helper (every owner command's output, the status
    command included) carries no private value; a screen shown to the owner at a terminal (setup, the owner's own
    tools) may show what the owner needs to proceed or troubleshoot, such as the key tool's own fingerprint line or
    the path of a file setup installed, but never the passphrase or key material."""
    scene = update_scene()
    world = scene.world
    leaked = set()
    for output in world.outputs:
        leaked.update(world.leaks(output))
    for name in sorted(leaked):
        phase.reason("leaks_" + name)
    phase.expect(len(world.outputs) + len(world.screens) >= 6, "the outputs of every step were collected")
    phase.expect(not leaked, "no output carries a key, a fingerprint, the passphrase or a path under the temporary home")
    phase.expect(world.passphrase not in "\n".join(world.outputs), "the passphrase is never shown")
    phase.expect(len(world.screens) >= 3, "the owner's screens were collected")
    secret = {name for screen in world.screens for name in world.leaks(screen)
              if name.startswith(("passphrase", "owner_key_material"))}
    for name in sorted(secret):
        phase.reason("screen_leaks_" + name)
    phase.expect(not secret, "no owner screen shows the passphrase or the key material")
    # The rule is also judged at capture, for every World and every step (e2e_support.capture_breaks; a break
    # fails the check that captured it). The judge itself, on this World's real values:
    private = world.private_values()
    fingerprint, private_line = private.get("owner_key_fingerprint", ""), private.get("owner_private_key_0_1", "")
    phase.expect(bool(fingerprint) and bool(private_line), "the run's key fingerprint and private key are known to the judge")

    def judged(text, kind):
        return e2e_support.capture_breaks(text, kind=kind, private=private, code=0, refuses=False, timed_out=False)
    phase.expect(not judged("Fingerprint " + fingerprint + " in " + str(world.home), "screen"),
                 "an owner screen may show the fingerprint and a path")
    phase.expect(judged(fingerprint, "output") == ["g8_output_leaks_owner_key_fingerprint"],
                 "a saved output with the fingerprint breaks the rule")
    phase.expect("g8_screen_leaks_owner_private_key" in judged(private_line, "screen"),
                 "an owner screen with a line of the private key breaks the rule")
    phase.expect("g8_screen_leaks_private_key_material" in judged("-----BEGIN OPENSSH PRIVATE KEY-----", "screen"),
                 "an owner screen with a private key block breaks the rule")
    phase.expect("g8_screen_leaks_passphrase" in judged(world.passphrase, "screen"),
                 "an owner screen with the passphrase breaks the rule")
