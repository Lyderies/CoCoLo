"""L2: the owner key (configure, wrong passphrase, replacement)."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import _SCENES, Phase, check  # noqa: E402
from e2e_checks_setup import changes_between  # noqa: E402


# L2: owner key

ATTEST = ROOT / "scripts" / "attest.py"
WRONG_PASSPHRASE = "not-the-passphrase-" + "e2e"
# The owner's tool offers "Try again or Exit? [T/e]" after a wrong passphrase; the test owner exits.
GIVE_UP = ((r"Try again or Exit\? \[T/e\]\s*$", "e"),)


def key_scene():
    """L2: a fresh install (its own machine), then the owner's attestation tool with a wrong passphrase."""
    if "key" not in _SCENES:
        world = e2e_support.World().__enter__()
        scene = SimpleNamespace(world=world)
        _SCENES["key"] = scene
        scene.setup = world.setup(world.answers())
        scene.before = world.snapshot()
        scene.status = world.cli("learning-status")
        scene.before_key_files = sorted(p.name for p in (world.home / ".ssh").iterdir())
        argv = [str(world.shim), "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", str(ATTEST),
                "--target", str(world.project)]
        # The tool's own output names the key and its fingerprint (it is the owner's tool, not a product
        # output the G8 scan judges), so it is kept out of the collected outputs.
        scene.wrong = world.terminal(argv, label="attest_wrong_passphrase", refuses=True,
                                     passphrase=WRONG_PASSPHRASE, answers=GIVE_UP, record=False, timeout=90)
        scene.after = world.snapshot()
    return _SCENES["key"]


@check("L2.1", group="key")
def l2_1_owner_key_configured_and_wrong_passphrase_refused(phase: Phase) -> None:
    scene = key_scene()
    world = scene.world
    phase.expect(scene.setup.code == 0, "the scripted setup that configures the owner key exits 0")
    code, status, _ = scene.status
    phase.expect(code == 0 and status is not None, "the collector answers its status")
    phase.expect((status.get("owner_key") or {}).get("state") == "ok", "the owner key is configured (state ok)")
    phase.expect((world.home / ".local" / "state" / "codex-agents" / "owner-allowed-signers").is_file()
                 or any((world.home / ".local" / "state").rglob("owner-allowed-signers")),
                 "the owner's allowed-signers record is in the state folder")
    phase.expect({"codex-agents-owner", "codex-agents-owner.pub"} <= set(scene.before_key_files),
                 "the owner key pair is in the owner's ssh folder")
    wrong = scene.wrong
    phase.expect(not wrong.timed_out, "the attestation tool did not wait for ever on a wrong passphrase")
    phase.expect(wrong.code == 1, "a wrong passphrase is refused (exit 1)")
    phase.expect("That passphrase did not unlock the key." in wrong.text, "the refusal says the passphrase did not unlock the key")
    phase.expect("Stopped: stopped: the passphrase did not unlock the key" in wrong.text,
                 "the stop carries its fixed reason")
    phase.expect("Attested:" not in wrong.text and "attested." not in wrong.text, "nothing was attested")
    phase.expect(WRONG_PASSPHRASE not in wrong.text, "the wrong passphrase is never shown")
    phase.reason("passphrase_did_not_unlock_key")
    changed = changes_between(scene.before, scene.after)
    phase.expect(not changed["files"], "a wrong passphrase changed no file or digest (key, records, state)")
    phase.expect(not changed["records"], "a wrong passphrase added no record")
    phase.expect(scene.before["ledger"] == scene.after["ledger"], "a wrong passphrase added no ledger row at all")
    phase.expect(scene.before_key_files == sorted(p.name for p in (world.home / ".ssh").iterdir()),
                 "a wrong passphrase made no key file")
    code, status, _ = world.cli("learning-status")
    phase.expect(code == 0 and (status.get("owner_key") or {}).get("state") == "ok", "the owner key is still configured")


# L2.2: replacing the owner key (owner decision 39). Throwaway keys under the harness's scratch home only.

SIGNATURE_NAMESPACE = "codex-agents-owner-v1"
REPLACE_ANSWERS = ((r"Type REPLACE to continue:\s*$", "REPLACE"),)


def sign_with_key(world, key: Path, payload: bytes) -> Path | None:
    """Sign `payload` as the owner would, with a scratch key and its scratch passphrase (the tool's own
    ssh-keygen step, asked for the passphrase through an askpass script instead of a terminal); the
    signature file, or None if ssh-keygen refused."""
    folder = world.base / "tmp"
    askpass = folder / "askpass.sh"
    askpass.write_text("#!/bin/sh\nprintf '%s\\n' '" + world.passphrase + "'\n")
    askpass.chmod(0o700)
    signed = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(key), "-n", SIGNATURE_NAMESPACE],
                            input=payload, capture_output=True, stdin=None, start_new_session=True,
                            env={**world.env, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                                 "DISPLAY": "e2e:0"})
    if signed.returncode != 0 or not signed.stdout.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        return None
    path = folder / f"owner-{time.monotonic_ns()}.sig"
    path.write_bytes(signed.stdout)
    return path


def billing_attempt(world, fingerprint: str, key: Path, *, refuses: bool):
    """One billing statement for the fake Codex account, signed with `key`: (prepare reply, signature made,
    (exit, reply, text) of the record step)."""
    grant = world.base / "tmp" / "grant.json"
    grant.write_text(json.dumps({"version": 1, "provider": "codex", "account_fingerprint": fingerprint,
                                 "no_paid_overage": True, "no_usage_credits": True,
                                 "issued_at": int(time.time() * 1000)}))
    base = ("learning-record-billing-attestation", "--provider", "codex", "--grant", str(grant))
    code, prepared, _ = world.cli(*base)
    if code != 0 or not prepared or "signing_payload_b64" not in prepared:
        return prepared, None, (code, prepared, "")
    signature = sign_with_key(world, key, base64.b64decode(prepared["signing_payload_b64"]))
    if signature is None:
        return prepared, None, (-1, None, "")
    return prepared, signature, world.cli(*base, "--signature", str(signature), refuses=refuses)


def replace_scene():
    """L2.2: a fresh install, a statement signed with the old key (the control), the owner replacing the key
    at a terminal, then a statement signed with the old key and one signed with the new key."""
    if "replace" in _SCENES:
        return _SCENES["replace"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["replace"] = scene
    scene.setup = world.setup(world.answers())
    ssh_folder = world.home / ".ssh"
    scene.before_pubs = {p.name for p in ssh_folder.glob("*.pub")}
    scene.old_keys = world.all_key_values()
    old_private = ssh_folder / "codex-agents-owner"
    scene.old_private = old_private
    scene.status_before = world.cli("learning-status")
    code, observed, _ = world.cli("provider-observe", "--provider", "codex")
    identity = [r for r in (observed or {}).get("records", []) if r.get("record_type") == "provider_identity"]
    scene.fingerprint = (identity[0].get("account_fingerprint") if identity else None) or ""
    scene.control = billing_attempt(world, scene.fingerprint, old_private, refuses=False)
    argv = [str(world.shim), "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", str(ATTEST),
            "--target", str(world.project), "--replace-owner-key"]
    scene.replace = world.terminal(argv, label="attest_replace_owner_key", answers=REPLACE_ANSWERS,
                                   record=False, timeout=170)
    scene.status_after = world.cli("learning-status")
    scene.new_pubs = sorted(p.name for p in ssh_folder.glob("*.pub") if p.name not in scene.before_pubs)
    scene.signers = next(iter((world.home / ".local" / "state").rglob("owner-allowed-signers")), None)
    scene.ledger_before_old = world.ledger_counts()
    scene.old_after = billing_attempt(world, scene.fingerprint, old_private, refuses=True)
    scene.ledger_after_old = world.ledger_counts()
    new_private = ssh_folder / scene.new_pubs[0][:-4] if scene.new_pubs else None
    scene.new_private = new_private
    scene.new_after = (billing_attempt(world, scene.fingerprint, new_private, refuses=False)
                       if new_private else (None, None, (-1, None, "")))
    scene.status_final = world.cli("learning-status")
    return scene


@check("L2.2", group="replace")
def l2_2_replacing_owner_key_refuses_old_signatures_accepts_new(phase: Phase) -> None:
    scene = replace_scene()
    world = scene.world
    phase.expect(scene.setup.code == 0, "the scripted setup that configures the owner key exits 0")
    phase.expect(len(scene.old_keys) == 1 and scene.old_private.is_file(), "the first owner key pair exists before the replacement")
    code, status, _ = scene.status_before
    before = (status or {}).get("owner_key") or {}
    phase.expect(code == 0 and before.get("state") == "ok" and before.get("replacements") == 0
                 and before.get("last_replaced_at_ms") is None, "the owner key is configured before the replacement")
    phase.expect("fingerprint" not in before, "the status shows no key fingerprint (decision 36)")
    phase.expect(bool(scene.fingerprint), "the fake Codex account has an observed identity to sign a statement for")
    # Control: before the replacement, a statement signed with the old key is accepted, so a later refusal
    # of the old key is the replacement's doing and not a faulty signing step.
    _prepared, signature, (code, reply, _text) = scene.control
    phase.expect(signature is not None, "a statement was prepared and signed with the old key (control)")
    phase.expect(code == 0 and (reply or {}).get("stored") is True,
                 "before the replacement the old key's signature is accepted (control)")
    # The replacement, at a terminal.
    run = scene.replace
    phase.expect(not run.timed_out, "the owner key replacement did not wait for ever")
    phase.expect(run.code == 0, "the owner key replacement finishes (exit 0)")
    phase.expect("The owner key was replaced." in run.text, "the tool says the owner key was replaced")
    phase.expect(world.passphrase not in run.text, "the passphrase is never shown")
    phase.expect(len(scene.new_pubs) == 1, "exactly one new owner key pair was made beside the old one")
    phase.expect(scene.old_private.is_file(), "the old key file stays where it was")
    code, status, _ = scene.status_after
    after = (status or {}).get("owner_key") or {}
    phase.expect(code == 0 and after.get("state") == "ok", "after the replacement the owner key is configured (state ok)")
    phase.expect(after.get("replacements") == (before.get("replacements") or 0) + 1, "the replacement is counted once")
    phase.expect(isinstance(after.get("last_replaced_at_ms"), int), "the status says when the owner key was replaced")
    old_blob = scene.old_keys[0][1]
    new_blob = ""
    if scene.new_pubs:
        new_blob = (world.home / ".ssh" / scene.new_pubs[0]).read_text().split()[1]
    signers = scene.signers.read_text() if scene.signers else ""
    phase.expect(bool(new_blob) and new_blob in signers and old_blob not in signers,
                 "the allowed-signers record holds the new key and no longer the old one")
    # The old key's signature is refused, with nothing recorded.
    _prepared, signature, (code, reply, _text) = scene.old_after
    phase.expect(signature is not None, "a new statement was prepared and signed with the old key")
    phase.expect(code != 0 and (reply or {}).get("stored") is not True,
                 "after the replacement the old key's signature is refused")
    phase.expect(scene.ledger_before_old == scene.ledger_after_old, "the refused old-key signature added no record")
    # The new key's signature is accepted.
    _prepared, signature, (code, reply, _text) = scene.new_after
    phase.expect(signature is not None, "a new statement was prepared and signed with the new key")
    phase.expect(code == 0 and (reply or {}).get("stored") is True,
                 "after the replacement the new key's signature is accepted")
    phase.expect(((reply or {}).get("record") or {}).get("record_type") == "billing_attestation",
                 "the accepted statement is stored as a billing attestation")
    code, status, _ = scene.status_final
    phase.expect(code == 0 and ((status or {}).get("provider_attestations") or {}).get("codex", {}).get("state") == "present",
                 "the collector shows a Codex billing attestation present, signed by the new key")
