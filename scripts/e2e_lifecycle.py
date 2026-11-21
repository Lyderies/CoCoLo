#!/usr/bin/env python3
"""COCOLO's strict lifecycle end-to-end test (PLAN-0.4.0.md T7), on a fake project.

One command, no prompts. Each flow (L1 to L15) is a named phase whose checks prove the
requirements in tests/e2e/requirements.json; `--flows L5,L9` runs only those, `--all` every one.
Strict by construction:

- every check must assert at least once (`Phase.expect`), or it fails;
- nothing is skipped: a missing tool, fake client or platform feature fails the check;
- every requirement of a selected flow must be decided by a check that ran;
- the report (G9) holds phases, seconds, reason codes and pass or fail per requirement, and no
  value from the run: it is checked for private values (G8) before it is written.

Usage: python3 -I -B scripts/e2e_lifecycle.py (--all | --flows L1,L5) [--report FILE]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402

REQUIREMENTS = ROOT / "tests" / "e2e" / "requirements.json"
ORCHESTRATION_VERSION = ROOT / "packages" / "codex-agents" / "native-package" / "VERSION"
CHECKS: dict[str, tuple[tuple[str, ...], object]] = {}
# A reason code: what the report may carry from a run, besides counts and seconds.
REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")


class CheckFailed(AssertionError):
    """A check's expectation did not hold."""


def check(*requirement_ids: str):
    """Declare a check and the requirements it proves (read by tests/test_e2e_requirements.py)."""
    def register(function):
        CHECKS[function.__name__] = (requirement_ids, function)
        return function
    return register


class Phase:
    """What one check sees: its isolated environment, and the expectations it records."""

    def __init__(self, name: str, home: Path):
        self.name, self.home, self.expectations, self.reasons = name, home, 0, []

    def expect(self, condition: bool, what: str) -> None:
        """One assertion. `what` is fixed text naming it, never a value from the run."""
        self.expectations += 1
        if not condition:
            raise CheckFailed(what)

    def reason(self, code: str) -> None:
        """Note a reason code a step answered with, for the report."""
        if not REASON.fullmatch(code or ""):
            raise CheckFailed("a step answered with something that is not a reason code")
        self.reasons.append(code)


def requirements() -> dict:
    return json.loads(REQUIREMENTS.read_text())


def run_checks(flows: set[str], *, home_root: Path | None = None, keep_scenes: bool = False) -> dict:
    """Run every check of the selected flows; the report, as written by main()."""
    doc = requirements()
    selected = [item for item in doc["requirements"] if item["flow"] in flows or item["flow"] == "all"]
    wanted = sorted({name for item in selected for name in item["checks"]})
    results, decided = [], {}
    try:
        _run_each(wanted, selected, results, decided, home_root)
    finally:
        if not keep_scenes:
            close_scenes()
    return _verdicts(flows, selected, results, decided)


def _run_each(wanted, selected, results, decided, home_root) -> None:
    for name in wanted:
        entry = {"check": name, "requirements": [], "passed": False, "seconds": 0.0, "reasons": [],
                 "failure": None}
        started = time.monotonic()
        if name not in CHECKS:
            entry["failure"] = "check_missing"
        else:
            ids, function = CHECKS[name]
            entry["requirements"] = list(ids)
            with tempfile.TemporaryDirectory(prefix="cocolo-e2e-", dir=home_root) as home:
                phase = Phase(name, Path(home))
                try:
                    function(phase)
                    if phase.expectations == 0:
                        entry["failure"] = "check_asserted_nothing"
                    else:
                        entry["passed"] = True
                except CheckFailed as error:
                    entry["failure"] = "expectation_failed: " + _fixed(str(error))
                except Exception as error:  # Nothing is optional: anything else fails the check.
                    entry["failure"] = "check_error: " + type(error).__name__
                    traceback.print_exc(file=sys.stderr)
                entry["reasons"] = phase.reasons
        entry["seconds"] = round(time.monotonic() - started, 3)
        results.append(entry)
        for requirement in entry["requirements"] or [i["id"] for i in selected if name in i["checks"]]:
            decided[requirement] = decided.get(requirement, True) and entry["passed"]


def _verdicts(flows, selected, results, decided) -> dict:
    verdicts = {}
    for item in selected:
        if not item["checks"]:
            verdicts[item["id"]] = {"passed": False, "decided_by": [], "failure": "no_check"}
        else:
            verdicts[item["id"]] = {"passed": decided.get(item["id"], False), "decided_by": item["checks"],
                                    "failure": None if decided.get(item["id"], False) else "a_check_failed"}
    return {"schema_version": 1, "flows": sorted(flows), "checks": results, "requirements": verdicts,
            "passed": bool(verdicts) and all(v["passed"] for v in verdicts.values())}


def _fixed(text: str) -> str:
    """Only plain fixed text reaches the report."""
    return re.sub(r"[^A-Za-z0-9 ,.'_-]", "", text)[:160]


def private_values_in(report_text: str, home_roots: list[str], values=()) -> list[str]:
    """G8: names of private values found in the report (never the values themselves).

    `values` are the exact private values a run held (its test key, passphrase and scratch paths)."""
    found = []
    if any(root and root in report_text for root in home_roots):
        found.append("temporary_home_path")
    if any(value and value in report_text for value in values):
        found.append("run_private_value")
    if re.search(r"/Users/|/home/", report_text):
        found.append("home_path")
    if re.search(r"SHA256:[A-Za-z0-9+/]{20,}|ssh-(ed25519|rsa) ", report_text):
        found.append("key_or_fingerprint")
    return found


# ---------------------------------------------------------------------------------------------
# Scenes: the runs several checks read. Each is made once per process, on its own isolated World, and
# every World is stopped and removed when the run ends (run_checks).

_SCENES: dict = {}
SETUP_STEPS = {1: "Checks", 2: "Project", 3: "Providers", 4: "Install", 5: "Collector", 6: "Billing",
               7: "Profile", 8: "Commands", 9: "Done"}
SUMMARY_LINE = re.compile(r"^\s*(OK|FAIL|SKIP)\s+([1-9])\. ([A-Za-z]+): ?(.*)$", re.MULTILINE)
# What a setup run again may add: the observations its own provider checks make, and nothing else.
# `records` is the ledger's own total over its record kinds (observations, admission evidence, attestations
# and the rest), so it is judged through the kinds, not on its own.
OWN_CHECK_TABLES = ("account_observation_origins", "account_observation_requests", "admission_evidence", "records")
OWN_CHECK_RECORDS = ("account_capacity.observations",)


def close_scenes() -> None:
    for scene in _SCENES.values():
        scene.world.__exit__(None, None, None)
    _SCENES.clear()


def summary_states(text: str) -> dict:
    """The setup summary's last word on each step: {number: (state, label, note)}."""
    states = {}
    for state, number, label, note in SUMMARY_LINE.findall(text):
        states[int(number)] = (state, label, note.strip())
    return states


def fresh_scene():
    """L1: a fresh install through the scripted setup (S1), then what a run again is judged against."""
    if "fresh" not in _SCENES:
        world = e2e_support.World().__enter__()
        scene = SimpleNamespace(world=world)
        _SCENES["fresh"] = scene
        scene.run = world.setup(world.answers())
        scene.snapshot = world.snapshot()
    return _SCENES["fresh"]


def again_scene():
    """L1: setup run again with the same answers file on the installed project."""
    scene = fresh_scene()
    if not hasattr(scene, "again"):
        scene.again = scene.world.setup(scene.world.answers())
        scene.again_snapshot = scene.world.snapshot()
    return scene


def update_scene():
    """L1: `./setup.sh --update` on the installed project."""
    scene = again_scene()
    if not hasattr(scene, "update"):
        scene.before_update = scene.world.snapshot()
        scene.update = scene.world.update()
        scene.after_update = scene.world.snapshot()
    return scene


def changes_between(before: dict, after: dict) -> dict:
    """What a second run changed, as fixed names and counts: files, Git, and records by kind."""
    files = sorted(name for name in set(before["files"]) | set(after["files"])
                   if before["files"].get(name) != after["files"].get(name))
    records = sorted(kind for kind in set(before["records"]) | set(after["records"])
                     if before["records"].get(kind) != after["records"].get(kind)
                     and kind not in OWN_CHECK_RECORDS)
    tables = sorted(table for table in set(before["ledger"]) | set(after["ledger"])
                    if before["ledger"].get(table) != after["ledger"].get(table)
                    and table not in OWN_CHECK_TABLES)
    return {"files": files, "head": before["head"] != after["head"], "commits": before["commits"] != after["commits"],
            "dirty": after["dirty"] != "", "records": records, "tables": tables}


def expect_unchanged(phase: Phase, before: dict, after: dict) -> None:
    changed = changes_between(before, after)
    phase.expect(not changed["files"], "a second setup changed no file or digest")
    phase.expect(not changed["head"] and not changed["commits"], "a second setup made no commit")
    phase.expect(not changed["dirty"], "a second setup left the project clean")
    phase.expect(not changed["records"], "a second setup added no record the collector counts")
    for table in changed["tables"]:
        phase.reason("new_" + re.sub(r"[^a-z0-9_]", "_", table.lower()))
    phase.expect(not changed["tables"], "a second setup added no ledger record except its own checks")


# ---------------------------------------------------------------------------------------------
# L1: fresh install and setup again

@check("L1.1")
def l1_fresh_install_scripted_setup(phase: Phase) -> None:
    scene = fresh_scene()
    world, run = scene.world, scene.run
    phase.expect(not run.timed_out, "scripted setup finished by itself")
    phase.expect(run.code == 0, "scripted setup exits 0")
    states = summary_states(run.text)
    phase.expect(sorted(states) == list(SETUP_STEPS), "the summary names all nine steps")
    phase.expect(all(states[number][1] == name for number, name in SETUP_STEPS.items()), "the steps keep their names")
    failed = [number for number, (state, *_rest) in states.items() if state == "FAIL"]
    for number in failed:
        phase.reason("step_failed_" + SETUP_STEPS[number].lower())
    phase.expect(not failed, "no step failed")
    phase.expect({n: s[0] for n, s in states.items() if s[0] != "OK"} == {7: "SKIP"},
                 "every step is ok but the unchosen Codex profile")
    phase.expect(states[7][2] == "not chosen", "the profile step is skipped only because bridging was not chosen")
    phase.expect("installed and verified" in states[4][2] and "committed" in states[4][2],
                 "the install is verified and committed")
    # The protected command rule: its file, mode and protected copy of the runtime.
    rules = sorted((world.home / ".codex" / "rules").glob("agent-orchestration-*.rules"))
    phase.expect(len(rules) == 1, "exactly one protected command rule is installed")
    phase.expect(states[8][0] == "OK" and states[8][2] == "created", "setup says the rule was created")
    rule = rules[0]
    phase.expect(oct(rule.stat().st_mode & 0o777) == "0o600", "the rule file is private to its owner")
    phase.expect("prefix_rule" in rule.read_text(), "the rule file holds a command rule")
    copies = list((world.home / ".codex" / "agent-orchestration").glob("*/*/.codex/orchestration/cli.py"))
    phase.expect(len(copies) == 1, "the rule's protected copy of the orchestrator is in place")
    # The collector: ready on this build, with both providers attested.
    code, status, _ = world.cli("learning-status")
    phase.expect(code == 0 and status is not None, "the collector answers its status")
    version = (ORCHESTRATION_VERSION).read_text().strip()
    phase.expect(status.get("ready") is True, "the collector is ready")
    phase.expect(status.get("collector_runtime_version") == version, "the collector runs this build")
    phase.expect(status.get("collector_code_stale") is False, "the collector's code is not stale")
    attested = {name: (value or {}).get("state") for name, value in (status.get("provider_attestations") or {}).items()}
    phase.expect(attested == {"codex": "present", "claude": "present"}, "both providers are attested")
    phase.expect((status.get("owner_key") or {}).get("state") == "ok", "the owner key is enrolled")
    # The install itself: committed, clean, managed files present.
    phase.expect(scene.snapshot["dirty"] == "", "the project is clean after setup")
    phase.expect(scene.snapshot["commits"] == "2", "the install is one commit on the fixture's")
    manifest = world.project / ".codex" / "orchestration" / "install-manifest.json"
    phase.expect(manifest.is_file(), "the install manifest is in the project")


@check("L1.2")
def l1_setup_again_with_answers_changes_nothing(phase: Phase) -> None:
    scene = again_scene()
    run = scene.again
    phase.expect(not run.timed_out and run.code == 0, "setup run again exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the second run failed")
    expect_unchanged(phase, scene.snapshot, scene.again_snapshot)


@check("L1.2")
def l1_update_on_installed_project_changes_nothing(phase: Phase) -> None:
    scene = update_scene()
    run = scene.update
    phase.expect(not run.timed_out and run.code == 0, "setup --update on the installed project exits 0")
    states = summary_states(run.text)
    phase.expect(not [n for n, s in states.items() if s[0] == "FAIL"], "no step of the update failed")
    expect_unchanged(phase, scene.before_update, scene.after_update)


# ---------------------------------------------------------------------------------------------
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


@check("L2.1")
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


@check("L2.2")
def l2_2_replacing_owner_key_refuses_old_signatures_accepts_new(phase: Phase) -> None:
    scene = replace_scene()
    world = scene.world
    phase.expect(scene.setup.code == 0, "the scripted setup that configures the owner key exits 0")
    phase.expect(len(scene.old_keys) == 1 and scene.old_private.is_file(), "the first owner key pair exists before the replacement")
    code, status, _ = scene.status_before
    before = (status or {}).get("owner_key") or {}
    phase.expect(code == 0 and before.get("state") == "ok" and before.get("fingerprint"), "the owner key is configured before the replacement")
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
    phase.expect(after.get("fingerprint") not in (None, before.get("fingerprint")), "the owner key fingerprint changed")
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


# ---------------------------------------------------------------------------------------------
# L3: observation and billing

PROBE = ("provider-observe", "--provider", "claude", "--billing-probe")


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


@check("L3.1")
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


@check("L3.2")
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


@check("L3.3")
def l3_3_client_version_unreviewed_refused(phase: Phase) -> None:
    code, reply, _ = observe_scene().unreviewed
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "client_version_unreviewed",
                 "an unreviewed client version is refused with client_version_unreviewed")


@check("L3.3")
def l3_3_api_key_in_environment_refused(phase: Phase) -> None:
    code, reply, _ = observe_scene().api_key
    phase.reason(reason_of(reply) or "no_reason")
    phase.expect(code == 2 and reason_of(reply) == "provider_api_key_present",
                 "an API key in the environment is refused with provider_api_key_present")


@check("L3.3")
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


@check("L3.3")
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


# ---------------------------------------------------------------------------------------------
# L4: routing

ROUTE_TASK = {
    "id": "task-1", "objective": "Read one fixture", "family": "inventory", "complexity": "low", "risk": "low",
    "meaningful": True, "operation": "mechanical", "ambiguity": "known", "context_breadth": "narrow",
    "acceptance_strength": "executable", "routing_evidence_refs": [], "deterministic_validation": True,
    "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
    "owned_paths": [], "prohibited_actions": ["network"]}
# The route key the task's shape selects in the policy (a bounded, mechanical, known, narrow, executable task).
ROUTE_KEY = "bounded"
CLAUDE_MODEL = "claude-haiku-5-5"  # The only Claude model this fake project allows (answers: claude_models).


def installed_policy(world) -> dict:
    return json.loads((world.project / ".codex" / "orchestration" / "config" / "routing-policy.json").read_text())


def policy_expectation(policy: dict, primary: str, family: str, key: str) -> dict:
    """What the installed policy says a `primary` session's child is: the first candidate of the family's
    list that the project allows (docs: references/routing.md). Codex is always allowed; a Claude model only
    when the project lists it and the primary is Claude (a Codex primary may not bridge to Claude here)."""
    for candidate in policy["provider_candidates"][family][key]:
        if candidate["provider"] == "codex" or (primary == "claude" and candidate["model"] == CLAUDE_MODEL):
            provider = candidate["provider"]
            role = policy["roles"][family] if provider == "codex" else policy["providers"]["claude"]["roles"][family]
            return {"provider": provider, "model": candidate["model"], "effort": candidate["effort"], "role": role}
    raise e2e_support.HarnessError("the policy lists no allowed candidate")


def world_invocations(world, provider: str | None) -> list[list[str]]:
    """The command lines the fake client of `provider` was started with, in order."""
    log = world.tools / f"{provider}-invocations.jsonl"
    if not log.exists():
        return []
    return [json.loads(line)["argv"] for line in log.read_text().splitlines() if line.strip()]


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def route_scene(primary: str):
    """L4: one primary's route for the task, edited routes refused, then the child run with the valid route.
    The Codex primary is the Codex thread marker; the Claude primary the Claude host markers."""
    name = "route_" + primary
    if name in _SCENES:
        return _SCENES[name]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world, primary=primary)
    _SCENES[name] = scene
    host = (lambda *a, **k: world.host_cli("claude", *a, **k)) if primary == "claude" else world.codex_host_cli
    scene.setup = world.setup(world.answers(providers={
        "codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL, "bridge": "off"}))
    world.claude_frames(e2e_support.claude_probe_frames())
    world.cli("provider-observe", "--provider", "codex")
    world.cli("provider-observe", "--provider", "claude")
    world.host_cli("claude", *PROBE)  # the billing probe the Claude admission rests on (as L3)
    scene.expected = policy_expectation(installed_policy(world), primary, ROUTE_TASK["family"], ROUTE_KEY)
    task_path = world.base / "task.json"
    task_path.write_text(json.dumps(ROUTE_TASK))
    scene.route_step = host("learning-route", "--task", str(task_path))
    route = scene.route = scene.route_step[1] or {}
    assigned = {
        "assignment_id": "assignment-1", "task_id": "task-1", "objective": "Read one fixture",
        "role": route.get("role"), "model": route.get("model"), "effort": route.get("effort"), "attempt": 1,
        "meaningful": True, "owned_paths": [], "context": ["fixture/facts.txt"],
        "permissions": {"tools": ["shell"], "filesystem": ["fixture/facts.txt:read"], "network": False,
                        "mcps": [], "external_effects": False},
        "expected_output": "One marker", "required_evidence": [{"id": "marker", "description": "marker exists"}],
        "verification_required": True, "bounds": route.get("bounds"),
        "stopping_conditions": ["marker found"], "prohibited_actions": ["network"]}
    parent = {"tools": ["shell"], "filesystem": ["fixture/facts.txt:read"], "network": False, "mcps": [],
              "external_effects": False}
    (world.base / "assignment.json").write_text(json.dumps(assigned))
    (world.base / "parent.json").write_text(json.dumps(parent))
    scene.orchestration = host("learning-open-orchestration")
    handle = (scene.orchestration[1] or {}).get("orchestration_handle", "")

    def context(route_file: Path, assignment_file: Path | None = None) -> list[str]:
        return ["--task", str(task_path), "--route", str(route_file),
                "--assignment", str(assignment_file or world.base / "assignment.json"),
                "--parent-grant", str(world.base / "parent.json")]

    def matching_assignment(name: str, change: dict) -> Path:
        """The assignment an owner of the edited route would offer with it: its role, model and effort follow
        the edit, so the only thing wrong with the pair is that the route is not the one the policy gave."""
        path = world.base / f"assignment-{name}.json"
        path.write_text(json.dumps({**assigned, **{k: v for k, v in change.items() if k in ("role", "model", "effort")}}))
        return path

    # Edited routes: each change is made to the route the product returned, then offered to the open.
    other = "codex" if route.get("provider") == "claude" else "claude"
    other_model = "gpt-6-astra" if route.get("provider") == "claude" else "claude-opus-5-5"
    edits = {
        "model": {"model": other_model},
        "effort": {"effort": "max"},
        "role": {"role": "codex_agents_planner", "agent_type": "codex_agents_planner"},
        "sandbox_mode": {"sandbox_mode": "workspace-write"},
        "provider": {"provider": other},
        "dispatch": {"dispatch": "native"}}
    scene.edited = {}
    for field_name, change in edits.items():
        edited = world.base / f"edited-{field_name}.json"
        edited.write_text(json.dumps({**route, **change}))
        scene.edited[field_name] = host("learning-open-host-run", *context(edited, matching_assignment(field_name, change)),
                                        "--orchestration-handle", handle, refuses=True)
    valid = world.base / "route.json"
    valid.write_text(json.dumps(route))
    scene.opened = host("learning-open-host-run", *context(valid), "--orchestration-handle", handle)
    run_handle = (scene.opened[1] or {}).get("run_handle", "")
    before = len(world_invocations(world, route.get("provider")))
    # An edit offered after the open is refused at dispatch as well (before anything is launched).
    edited = world.base / "edited-late.json"
    edited.write_text(json.dumps({**route, "model": other_model}))
    scene.late_edit = host("dispatch", *context(edited, matching_assignment("late", {"model": other_model})),
                           "--run-handle", run_handle, refuses=True)
    if route.get("provider") == "claude":
        world.claude_frames([
            {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none",
             "model": route.get("model"), "plugins": [], "claude_code_version": "2.1.311", "mcp_servers": [],
             "tools": []},
            {"type": "result", "subtype": "success", "is_error": False, "result": "{}",
             "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.1, "permission_denials": []}])
    scene.dispatch = host("dispatch", *context(valid), "--run-handle", run_handle)
    scene.launched = world_invocations(world, route.get("provider"))[before:]
    return scene


def child_run_matches_route(phase: Phase, scene) -> None:
    """The route the primary got is the policy's, and the child ran with exactly it."""
    route, want = scene.route, scene.expected
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    code, reply, _ = scene.route_step
    phase.expect(code == 0 and reply is not None and reply.get("ready") is True, "learning-route answers a ready route")
    for key in ("provider", "model", "effort", "role"):
        phase.expect(route.get(key) == want[key], f"the route's {key} is the policy's first allowed candidate")
    phase.expect(route.get("agent_type") == want["role"], "the route's agent is the policy role's agent")
    phase.expect(route.get("sandbox_mode") == "read-only", "the route's sandbox is read-only")
    code, reply, _ = scene.opened
    phase.expect(code == 0 and reply is not None and reply.get("admission") == "admitted",
                 "the run opens with the unchanged route")
    code, reply, _ = scene.dispatch
    phase.expect(code == 0 and reply is not None, "the dispatch with the unchanged route launches the child")
    phase.expect(len(scene.launched) == 1, "the fake client was started once for the child")
    argv = scene.launched[0] if scene.launched else []
    if want["provider"] == "claude":
        phase.expect(option(argv, "--model") == want["model"], "the child started with the route's model")
        phase.expect(option(argv, "--effort") == want["effort"], "the child started with the route's effort")
        phase.expect(option(argv, "--tools") == "Read,Glob,Grep" and option(argv, "--permission-mode") == "dontAsk",
                     "the child started read-only (read tools only, no permission prompts)")
        phase.expect("--append-system-prompt-file" in argv, "the child started with its role's instructions")
    else:
        phase.expect(option(argv, "-m") == want["model"], "the child started with the route's model")
        phase.expect(f"model_reasoning_effort={want['effort']}" in argv, "the child started with the route's effort")
        phase.expect(option(argv, "-s") == "read-only", "the child started in the route's read-only sandbox")
        agent = (scene.world.project / ".codex" / "agents" / f"{want['role']}.toml").read_text()
        instructions = re.search(r'developer_instructions = """\n(.*?)"""', agent, re.S)
        phase.expect(instructions is not None and
                     ("developer_instructions=" + json.dumps(instructions.group(1))) in argv,
                     "the child started with the instructions of the route's agent")
        phase.expect('sandbox_mode = "read-only"' in agent, "the route's agent file declares the same sandbox")


@check("L4.1")
def l4_1_claude_primary_route_and_child_run_match_policy(phase: Phase) -> None:
    scene = route_scene("claude")
    phase.expect(scene.expected["provider"] == "claude", "a Claude primary's first allowed candidate is a Claude model")
    child_run_matches_route(phase, scene)


@check("L4.1")
def l4_1_codex_primary_route_and_child_run_match_policy(phase: Phase) -> None:
    scene = route_scene("codex")
    phase.expect(scene.expected["provider"] == "codex", "a Codex primary's first allowed candidate is a Codex model")
    child_run_matches_route(phase, scene)


def edited_route_refused(phase: Phase, scene) -> None:
    phase.expect(scene.route_step[0] == 0, "learning-route answers a route to edit")
    for field_name, (code, reply, _) in scene.edited.items():
        if (reply or {}).get("reason_code"):
            phase.reason(reply["reason_code"])
        phase.expect(code == 2 and reply is not None and reply.get("ready") is not True,
                     f"a route with its {field_name} edited is refused when the run opens")
    code, reply, _ = scene.late_edit
    phase.expect(code == 2 and reply is not None and reply.get("ready") is not True,
                 "a route edited after the run opened is refused at dispatch")
    code, reply, _ = scene.opened
    phase.expect(code == 0 and reply is not None and reply.get("admission") == "admitted",
                 "the same route unedited still opens (the refusals were about the edit)")


@check("L4.2")
def l4_2_claude_primary_edited_route_refused(phase: Phase) -> None:
    edited_route_refused(phase, route_scene("claude"))


@check("L4.2")
def l4_2_codex_primary_edited_route_refused(phase: Phase) -> None:
    edited_route_refused(phase, route_scene("codex"))


# ---------------------------------------------------------------------------------------------
# Every flow: G1, G8, G9

@check("G1")
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
    seen = e2e_support.bare_unavailable("\n".join(world.outputs))
    for code in seen:
        phase.reason("bare_" + code)
    phase.expect(not seen, "no step answered with a bare run_unavailable, account_unavailable or probe_unavailable")
    # The detector itself: it flags a bare code and leaves a code with a cause alone.
    phase.expect(e2e_support.bare_unavailable('{"reason_code": "run_unavailable"}') == ["run_unavailable"],
                 "the check sees a bare run_unavailable")
    phase.expect(not e2e_support.bare_unavailable('{"reason_code": "run_unavailable_busy_probe"}'),
                 "the check leaves a reason that carries its cause")


@check("G8")
def g8_no_private_value_in_any_output(phase: Phase) -> None:
    scene = update_scene()
    world = scene.world
    leaked = set()
    for output in world.outputs:
        leaked.update(world.leaks(output))
    for name in sorted(leaked):
        phase.reason("leaks_" + name)
    phase.expect(len(world.outputs) >= 6, "the outputs of every step were collected")
    phase.expect(not leaked, "no output carries a key, a fingerprint, the passphrase or a path under the temporary home")
    phase.expect(world.passphrase not in "\n".join(world.outputs), "the passphrase is never shown")


@check("G9")
def g9_report_is_content_free_json_with_phases_seconds_reasons(phase: Phase) -> None:
    def passing(inner: Phase) -> None:
        inner.expect(True, "it holds")
        inner.reason("claim_busy")

    def failing(inner: Phase) -> None:
        inner.expect(False, "it does not hold")

    document = {"flows": {"L1": "x"}, "requirements": [
        {"id": "L1.1", "flow": "L1", "source": "s", "statement": "t", "checks": ["passing"]},
        {"id": "L1.2", "flow": "L1", "source": "s", "statement": "t", "checks": ["failing"]}]}
    saved = dict(CHECKS)
    try:
        CHECKS.clear()
        CHECKS.update(passing=(("L1.1",), passing), failing=(("L1.2",), failing))
        original = globals()["requirements"]
        globals()["requirements"] = lambda: document
        try:
            report = run_checks({"L1"}, keep_scenes=True)
        finally:
            globals()["requirements"] = original
    finally:
        CHECKS.clear()
        CHECKS.update(saved)
    destination = phase.home / "report.json"
    text = write_report(report, destination)
    written = json.loads(destination.read_text())
    phase.expect(written == json.loads(text) and written["schema_version"] == 1, "the report is written as JSON")
    phase.expect(sorted(written) == ["checks", "flows", "passed", "requirements", "schema_version"],
                 "the report has its fixed keys")
    phase.expect([c["check"] for c in written["checks"]] == ["failing", "passing"], "the report lists every phase")
    phase.expect(all(isinstance(c["seconds"], float) and c["seconds"] >= 0 for c in written["checks"]),
                 "every phase has its seconds")
    phase.expect(written["checks"][1]["reasons"] == ["claim_busy"], "reason codes are carried")
    phase.expect({k: v["passed"] for k, v in written["requirements"].items()} == {"L1.1": True, "L1.2": False},
                 "pass or fail is given per requirement")
    phase.expect(written["passed"] is False, "one failing requirement fails the report")
    phase.expect(not private_values_in(text, [tempfile.gettempdir(), str(phase.home)], e2e_support.PRIVATE_VALUES),
                 "the report carries no private value")
    # A report that did carry one is replaced by a refusal that names the kind only.
    leaky = dict(report, extra=str(phase.home))
    phase.expect(json.loads(write_report(leaky, None)).get("failure") == "private_values_in_report",
                 "a report with a private value in it is never written")
    # Kept as the CI artifact: a workflow step runs this script and uploads its report.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    phase.expect("e2e_lifecycle.py" in workflow and "--report" in workflow, "CI runs the lifecycle script with a report")
    phase.expect("upload-artifact" in workflow.split("e2e_lifecycle.py", 1)[-1], "CI keeps the report as an artifact")


def write_report(report: dict, destination: Path | None) -> str:
    """G9: the content-free JSON report, scanned for private values (G8) before it is written or shown."""
    text = json.dumps(report, indent=1, sort_keys=True)
    home_roots = [tempfile.gettempdir(), str(Path.home()), os.path.realpath(Path.home())]
    leaked = private_values_in(text, home_roots, e2e_support.PRIVATE_VALUES)
    if leaked:
        report = {"schema_version": 1, "passed": False, "failure": "private_values_in_report", "found": leaked}
        text = json.dumps(report, indent=1, sort_keys=True)
    if destination:
        destination.write_text(text + "\n")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true")
    which.add_argument("--flows")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    known = set(requirements()["flows"])
    flows = known if args.all else {flow.strip() for flow in args.flows.split(",")}
    if not flows <= known:
        parser.error("unknown flow: " + ",".join(sorted(flows - known)))
    report = run_checks(flows)
    text = write_report(report, args.report)
    print(text)
    return 0 if json.loads(text)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
