"""L5: the run lifecycle (and G2 to G4, read through the account and project records)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import CLAUDE_MODEL, PROBE, ROUTE_TASK, _SCENES, Phase, check  # noqa: E402


ORACLE = {"alpha": 3, "beta": 5}  # the fake project's committed answer oracle (fixture/oracle.json)
WORK_PATH = "fixture/facts.txt"
WORK_GRANT = {"tools": ["shell"], "filesystem": [f"{WORK_PATH}:read"], "network": False, "mcps": [],
              "external_effects": False}
WORK_EVIDENCE = [{"id": name, "description": f"the value of {name} in {WORK_PATH}"} for name in ORACLE]


def answer_frames(provider: str, world, result: dict) -> None:
    """What the fake `provider` client prints for one child turn that answers with `result`."""
    if provider == "claude":
        world.claude_frames([
            {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none",
             "model": CLAUDE_MODEL, "plugins": [], "claude_code_version": "2.1.311", "mcp_servers": [],
             "tools": ["StructuredOutput", "Read", "Glob", "Grep"]},
            {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(result),
             "structured_output": result, "usage": {"input_tokens": 120, "output_tokens": 30},
             "total_cost_usd": 0.1, "permission_denials": []}])
    else:
        keys = ("execution_state", "semantic_verdict", "admission_state", "retry", "usage")
        (world.tools / "codex-frames.jsonl").write_text(
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}) + "\n")
        (world.tools / "codex-last-message.json").write_text(json.dumps({**{key: None for key in keys}, **result}))


def child_answer(template: dict, verifier: bool, verdict: str = "pass") -> dict:
    """The answer a correct child gives: the result-preflight template filled in. The worker states each oracle
    value in its evidence; the verifier confirms each one against the source."""
    evidence = []
    for entry in template["evidence"]:
        name = entry["criterion_id"]
        detail = f"{name}={ORACLE[name]}" + (" confirmed in the source" if verifier else "")
        evidence.append({**entry, "type": "source", "outcome": "pass", "reference": f"{WORK_PATH}:1", "details": detail})
    return {**template, "evidence": evidence, "semantic_verdict": verdict if verifier else "pass",
            "summary": "values confirmed" if verifier else "found both values"}


def trusted_interpreter_checks(world) -> None:
    """Give the fake project operational checks that can run under the product's macOS check sandbox.

    The fake project's own wrappers use /usr/bin/python3, which on macOS is a developer-tools shim that reads
    /var/select (outside what the sandbox allows), so it cannot run there at all. These wrappers use the trusted
    Python that setup.sh itself chose (a Homebrew one, which the sandbox allows), and are committed before setup."""
    import hashlib
    interpreter = Path(os.path.realpath(world.python))
    digest = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    path = world.project / "qa-check"
    path.write_text(f"#!{interpreter}\nprint(1)\n")
    path.chmod(0o700)
    entries = {kind: [{"check_id": name, "driver": "python-wrapper", "argv": ["./qa-check"],
                       "executable_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "interpreter_sha256": digest, "runtime_tree": None, "timeout_seconds": 20}]
               for kind, name in (("deterministic_checks", "tests"), ("security_checks", "security"))}
    world.checks.write_text(json.dumps({"version": 1, **entries}))
    world.git("add", ".")
    world.git("commit", "-qm", "checks that run in the sandbox")


def lifecycle_scene(primary: str, verifier_verdict: str = "pass"):
    """L5: one primary's whole run on the fake project, every step recorded: readiness, assignment and result
    preflight, export preflight, open, dispatch, record, then the verifier the same way, operational checks and
    QA, close and the report. The children are the fake clients, answering correctly. After a refused step
    nothing later runs except the close (an owner closes what was opened); the checks name the refused step."""
    name = "life_" + primary + ("" if verifier_verdict == "pass" else "_" + verifier_verdict)
    if name in _SCENES:
        return _SCENES[name]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world, primary=primary, steps={}, files={}, digests={}, failed=None)
    _SCENES[name] = scene
    host = (lambda *a, **k: world.host_cli("claude", *a, **k)) if primary == "claude" else world.codex_host_cli
    trusted_interpreter_checks(world)
    scene.setup = world.setup(world.answers(providers={
        "codex": "on", "claude": "on", "claude_models": CLAUDE_MODEL, "bridge": "off"}))
    world.claude_frames(e2e_support.claude_probe_frames())
    world.cli("provider-observe", "--provider", "codex")
    world.cli("provider-observe", "--provider", "claude")
    world.host_cli("claude", *PROBE)  # the billing probe the Claude admission rests on (as L3, L4)

    def save(file_name: str, data) -> str:
        path = world.base / file_name
        path.write_text(json.dumps(data))
        return str(path)

    def step(label: str, *args, always: bool = False) -> dict | None:
        """One host command; its reply is kept. A refusal stops every later step but a close."""
        if scene.failed is not None and not always:
            return None
        code, reply, _ = world.validate_package(primary) if label == "validate" else host(*args)
        scene.steps[label] = (code, reply)
        if code != 0 or reply is None or reply.get("ready") is False or reply.get("valid") is False:
            scene.failed = scene.failed or label
        return reply

    step("status", "learning-status")
    step("validate")
    scene.orchestration = step("open_orchestration", "learning-open-orchestration") or {}
    handle = scene.orchestration.get("orchestration_handle", "")
    grant_file = save("parent.json", WORK_GRANT)

    def child(prefix: str, task: dict, objective: str, context: list, verifier: bool) -> None:
        task_file = save(prefix + "task.json", task)
        route = step(prefix + "route", "learning-route", "--task", task_file)
        if route is None:
            return
        route_file = save(prefix + "route.json", route)
        assignment = {
            "assignment_id": task["id"] + "-a1", "task_id": task["id"], "objective": objective,
            "role": route.get("role"), "model": route.get("model"), "effort": route.get("effort"), "attempt": 1,
            "meaningful": True, "owned_paths": task["owned_paths"], "context": context, "permissions": WORK_GRANT,
            "expected_output": "Each value, as name=value, in its evidence entry", "required_evidence": WORK_EVIDENCE,
            "verification_required": route.get("verification_required"), "bounds": route.get("bounds"),
            "stopping_conditions": ["every value found"], "prohibited_actions": ["network"]}
        assignment_file = save(prefix + "assignment.json", assignment)
        common = ["--task", task_file, "--assignment", assignment_file, "--parent-grant", grant_file]
        step(prefix + "validate_assignment", "validate-assignment", *common)
        template = (step(prefix + "result_preflight", "result-preflight", *common) or {}).get("template")
        step(prefix + "export_preflight", "dispatch-preflight", *common)
        provider = route.get("provider")
        step(prefix + "observe", "provider-observe", "--provider", provider,
             *(["--billing-probe"] if provider == "claude" else []))
        opened = step(prefix + "open", "learning-open-host-run", *common, "--route", route_file,
                      "--orchestration-handle", handle)
        if opened is None or template is None:
            return
        scene.answers = getattr(scene, "answers", {})
        scene.answers[prefix] = child_answer(template, verifier, verifier_verdict)
        answer_frames(provider, world, scene.answers[prefix])
        launched = step(prefix + "dispatch", "dispatch", *common, "--route", route_file,
                        "--run-handle", opened.get("run_handle", ""))
        if launched is None:
            return
        result_file = save(prefix + "result.json", launched.get("result"))
        scene.files[prefix] = SimpleNamespace(assignment=assignment_file, result=result_file)
        step(prefix + "validate_result", "validate-result", "--result", result_file, *common)
        observation = {"run_handle": opened.get("run_handle"),
                       "result_attempts": [{"status": "accepted", "reason_code": None, "receipt_id": None}]}
        terminal = (launched.get("bridge_report") or {}).get("terminal_state")
        if terminal:
            observation["terminal_state"] = terminal
        recorded = step(prefix + "record", "learning-record-host-result", *common, "--result", result_file,
                        "--observation", save(prefix + "observation.json", observation))
        scene.digests[prefix] = (recorded or {}).get("host_outcome_digest")

    child("", {**ROUTE_TASK, "owned_paths": [WORK_PATH]}, "Read the fixture and report each value", [WORK_PATH], False)
    worker = scene.digests.get("")
    if worker:
        child("v", {**ROUTE_TASK, "id": "task-1-verify", "objective": "Check the recorded answer",
                    "family": "verification", "owned_paths": [WORK_PATH], "verification_for_family": "inventory",
                    "verification_for_provider": scene.steps["route"][1].get("provider"),
                    "verification_for_outcome": worker},
              "Check the recorded answer against the source", [worker], True)
    if scene.digests.get("v"):
        pair = ["--host-outcome-digest", worker, "--verifier-host-outcome-digest", scene.digests["v"]]
        checks = step("checks", "learning-run-operational-checks", *pair)
        if checks is not None:
            step("qa", "learning-record-operational-qa", *pair, "--check-receipt", checks.get("check_receipt", ""),
                 "--evaluator-assignment", scene.files[""].assignment, "--evaluator-result", scene.files[""].result,
                 "--verifier-assignment", scene.files["v"].assignment, "--verifier-result", scene.files["v"].result)
    if handle:
        step("close", "learning-close-orchestration", "--orchestration-handle", handle, "--status",
             "completed" if scene.failed is None else "failed", always=True)
        closed = (scene.steps.get("close") or (0, None))[1] or {}
        identifier = closed.get("orchestration_id") or scene.orchestration.get("orchestration_id")
        if identifier:
            step("report", "learning-orchestration-report", "--orchestration-id", identifier, always=True)
    return scene


def step_reply(phase: Phase, scene, label: str, what: str) -> dict:
    """The reply of a lifecycle step, after expecting that it ran and was not refused."""
    code, reply = scene.steps.get(label, (None, None))
    if (reply or {}).get("reason_code"):
        phase.reason(reply["reason_code"])
    ran = label in scene.steps
    phase.expect(ran, f"{what} (it was not reached: an earlier step was refused)")
    phase.expect(code == 0 and reply is not None and reply.get("ready") is not False
                 and reply.get("valid") is not False, what)
    return reply


def lifecycle_preflights(phase: Phase, scene) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    status = step_reply(phase, scene, "status", "learning-status answers ready")
    phase.expect(status.get("ready") is True, "learning-status reports the project ready")
    package = step_reply(phase, scene, "validate", "validate answers ready")
    phase.expect(package.get("package_ready") is True and package.get("project_ready") is True,
                 "validate reports the package and the project ready")
    head = scene.world.git("rev-parse", "HEAD").stdout.strip()
    for prefix, who in (("", "worker"), ("v", "verifier")):
        step_reply(phase, scene, prefix + "route", f"the {who}'s task is routed")
        assignment = step_reply(phase, scene, prefix + "validate_assignment", f"the {who}'s assignment validates")
        phase.expect(assignment.get("task_id") is not None, f"the {who}'s assignment validation answers its task")
        preflight = step_reply(phase, scene, prefix + "result_preflight", f"the {who}'s result preflight is ready")
        wanted = [entry["id"] for entry in WORK_EVIDENCE]
        phase.expect([entry.get("criterion_id") for entry in preflight["template"]["evidence"]] == wanted,
                     f"the {who}'s result template has one entry for each required evidence item")
        export = step_reply(phase, scene, prefix + "export_preflight", f"the {who}'s export preflight is ready")
        phase.expect(export.get("preflight_only") is True and export.get("admission_created") is False,
                     f"the {who}'s export preflight admits and launches nothing")
        phase.expect(export.get("export_commit") == head, f"the {who}'s export preflight names the committed snapshot")
        phase.expect(WORK_PATH in [entry.get("path") for entry in export.get("files", [])],
                     f"the {who}'s export preflight lists the assigned file")


def recorded_result(scene, prefix: str) -> dict:
    return json.loads(Path(scene.files[prefix].result).read_text())


def answered_values(result: dict) -> dict:
    """The values an answer states: its evidence details, each as `name=value`."""
    values = {}
    for entry in result.get("evidence", []):
        match = re.match(r"([a-z]+)=(\d+)", entry.get("details") or "")
        if match:
            values[match.group(1)] = int(match.group(2))
    return values


def lifecycle_worker_recorded(phase: Phase, scene) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    route = step_reply(phase, scene, "route", "the task is routed")
    phase.expect(route.get("dispatch") == "bridge",
                 "the route is bridged, so no native path (and no structural repair) applies")
    opened = step_reply(phase, scene, "open", "the run opens")
    phase.expect(opened.get("admission") == "admitted", "the run opens admitted")
    launched = step_reply(phase, scene, "dispatch", "the dispatch runs the child")
    result = launched.get("result") or {}
    phase.expect(result.get("execution_state") == "completed", "the child's run completed")
    step_reply(phase, scene, "validate_result", "the child's result validates")
    recorded = step_reply(phase, scene, "record", "the host result is recorded")
    phase.expect(isinstance(recorded.get("host_outcome_digest"), str) and recorded["host_outcome_digest"],
                 "recording the result gives its host outcome digest")
    oracle = json.loads((scene.world.project / "fixture" / "oracle.json").read_text())
    stored = recorded_result(scene, "")
    phase.expect(answered_values(stored) == oracle, "the worker's answer values match the oracle")
    phase.expect(stored.get("evidence") and all(entry.get("outcome") in ("pass", "observed")
                                                for entry in stored["evidence"]),
                 "each required evidence outcome is pass or observed")
    phase.expect(sorted(entry.get("criterion_id") for entry in stored["evidence"]) == sorted(oracle),
                 "the answer has one evidence entry for each required criterion")


def verifier_input(world, provider: str) -> dict | None:
    """The reserved input the bridge placed in the last child launched for `provider` (what the fake client saw)."""
    log = world.tools / f"{provider}-invocations.jsonl"
    if not log.exists():
        return None
    runs = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    runs = [run for run in runs if run.get("reserved_inputs")]
    return runs[-1]["reserved_inputs"] if runs else None


def lifecycle_verified_and_closed(phase: Phase, scene) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    worker = scene.digests.get("")
    phase.expect(bool(worker), "the worker's outcome digest exists")
    opened = step_reply(phase, scene, "vopen", "the verifier's run opens")
    phase.expect(opened.get("admission") == "admitted", "the verifier's run opens admitted for the worker's outcome")
    step_reply(phase, scene, "vdispatch", "the verifier's dispatch runs the child")
    step_reply(phase, scene, "vvalidate_result", "the verifier's result validates")
    step_reply(phase, scene, "vrecord", "the verifier's host result is recorded")
    provider = scene.steps["vroute"][1].get("provider")
    reserved = verifier_input(scene.world, provider) or {}
    given = reserved.get("worker-result.json") or {}
    phase.expect(bool(given.get("content")) and json.loads(given["content"]) == recorded_result(scene, ""),
                 "the verifier was given exactly the worker's recorded result to check")
    stored = recorded_result(scene, "v")
    phase.expect(stored.get("semantic_verdict") in ("pass", "ready"), "the verifier's verdict is pass or ready")
    phase.expect(stored.get("evidence") and all(entry.get("outcome") == "pass" for entry in stored["evidence"])
                 and sorted(entry["criterion_id"] for entry in stored["evidence"]) == sorted(ORACLE),
                 "the verifier passes every required evidence item")
    phase.expect(not stored.get("unverified_claims") and not stored.get("remaining_risks"),
                 "the verifier leaves no unverified claim or remaining risk")
    checks = step_reply(phase, scene, "checks", "the operational checks run")
    phase.expect(checks.get("checks_passed") is True, "the operational checks pass")
    qa = step_reply(phase, scene, "qa", "operational QA is recorded")
    phase.expect(qa.get("verifier_disagreement") is False, "the verifier and the worker do not disagree")
    phase.expect(qa.get("required_criteria_passed") == qa.get("required_criteria_total") == len(ORACLE),
                 "both pass every required criterion")
    phase.expect(qa.get("deterministic_pass") is True and qa.get("security_pass") is True,
                 "QA records the deterministic and security checks as passed")
    phase.expect(qa.get("accepted") is True, "QA records accepted: true")
    closed = step_reply(phase, scene, "close", "the orchestration closes")
    phase.expect(((closed.get("current") or {}).get("snapshot") or {}).get("status") == "completed",
                 "the orchestration closes as completed")
    report = step_reply(phase, scene, "report", "the orchestration report is read")
    phase.expect(report.get("status") == "completed", "the report says the orchestration completed")
    runs = report.get("runs") or {}
    phase.expect(runs.get("opened") == 2 and runs.get("terminal") == 2 and runs.get("pending") == 0
                 and runs.get("current_active") == 0 and runs.get("abandoned") == 0,
                 "the report counts the worker and the verifier, both finished, none abandoned or active")
    phase.expect(report.get("repairs") == 0, "the report counts no repairs")
    phase.expect((report.get("qa") or {}).get("passed") == 1 and (report.get("qa") or {}).get("failed") == 0,
                 "the report counts one QA passed and none failed")


@check("L5.1", group="life_claude")
def l5_1_claude_primary_preflights_pass(phase: Phase) -> None:
    lifecycle_preflights(phase, lifecycle_scene("claude"))


@check("L5.1", group="life_codex")
def l5_1_codex_primary_preflights_pass(phase: Phase) -> None:
    lifecycle_preflights(phase, lifecycle_scene("codex"))


@check("L5.2", group="life_claude")
def l5_2_claude_primary_open_dispatch_validate_record(phase: Phase) -> None:
    lifecycle_worker_recorded(phase, lifecycle_scene("claude"))


@check("L5.2", group="life_codex")
def l5_2_codex_primary_open_dispatch_validate_record(phase: Phase) -> None:
    lifecycle_worker_recorded(phase, lifecycle_scene("codex"))


@check("L5.3", group="life_claude")
def l5_3_claude_primary_verifier_qa_accepted_and_close(phase: Phase) -> None:
    lifecycle_verified_and_closed(phase, lifecycle_scene("claude"))


@check("L5.3", group="life_claude_ready")
def l5_3_claude_primary_worker_pass_verifier_ready_qa_accepted(phase: Phase) -> None:
    """The verifier may say ready where the worker said pass; QA must still accept (requirement L5.3)."""
    scene = lifecycle_scene("claude", "ready")
    phase.expect(scene.answers.get("v", {}).get("semantic_verdict") == "ready" and
                 scene.answers.get("", {}).get("semantic_verdict") == "pass",
                 "the worker answered pass and the verifier answered ready")
    lifecycle_verified_and_closed(phase, scene)


@check("L5.3", group="life_codex")
def l5_3_codex_primary_verifier_qa_accepted_and_close(phase: Phase) -> None:
    lifecycle_verified_and_closed(phase, lifecycle_scene("codex"))


# ---------------------------------------------------------------------------------------------
# Every flow: G2, G3, G4 on the run lifecycle's scenes.
#
# Each value is read through a different real path than the command that produced it: the worker's answer as
# the verifier was handed it by the bridge (not the dispatch reply or the file saved from it), and the outcome,
# verdict, criteria and QA the service stored in the project's own history (read-only), not what the record,
# QA or close commands replied.

def same_digest(first, second) -> bool:
    """Two digests are the same only when both are there: two missing digests prove nothing (G3, G4)."""
    return isinstance(first, str) and bool(first) and first == second


def stored_outcome(scene, prefix: str) -> dict:
    """The project history's latest outcome record of the run whose host outcome digest `scene.digests[prefix]` is.
    It is found through the stored admission that names that digest, so the digest the record command answered
    must be one the history holds."""
    digest = scene.digests.get(prefix)
    admissions = [row for row in scene.world.history_records("host_result_admission")
                  if row.get("host_outcome_digest") == digest]
    if not digest or len(admissions) != 1:
        return {}
    outcomes = [row for row in scene.world.history_records("outcome") if row.get("run_id") == admissions[0]["run_id"]]
    return outcomes[-1] if outcomes else {}


def stored_qa(scene) -> dict:
    rows = scene.world.history_qa()
    return rows[-1] if len(rows) == 1 else {}


def worker_result_as_given_to_verifier(scene) -> dict:
    provider = (scene.steps.get("vroute") or (None, {}))[1].get("provider")
    given = (verifier_input(scene.world, provider) or {}).get("worker-result.json") or {}
    return json.loads(given["content"]) if given.get("content") else {}


def worker_answers_match_oracle(phase: Phase, scene) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    oracle = json.loads((scene.world.project / "fixture" / "oracle.json").read_text())
    given = worker_result_as_given_to_verifier(scene)
    phase.expect(bool(given), "the verifier was handed the worker's recorded result")
    phase.expect(answered_values(given) == oracle, "the worker's answer values, as the verifier was handed them, match the oracle")
    phase.expect(sorted(entry.get("criterion_id") for entry in given.get("evidence", [])) == sorted(oracle),
                 "the worker answered each required criterion")
    phase.expect(all(entry.get("outcome") in ("pass", "observed") for entry in given.get("evidence", [])),
                 "each required evidence outcome the worker gave is pass or observed")
    stored = stored_outcome(scene, "")
    phase.expect(bool(stored), "the project history holds the worker's outcome under the recorded digest")
    phase.expect(stored.get("execution_state") == "completed", "the history says the worker's run completed")
    phase.expect(stored.get("semantic_verdict") == "pass", "the history holds the worker's verdict pass")
    phase.expect(stored.get("required_criteria_total") == len(oracle) == stored.get("required_criteria_passed"),
                 "the history counts every required criterion of the worker passed")


def verifier_answers_pass(phase: Phase, scene, verdict: str) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    worker, verifier = scene.digests.get(""), scene.digests.get("v")
    phase.expect(bool(worker) and bool(verifier), "the worker's and the verifier's outcome digests exist")
    qa = stored_qa(scene)
    phase.expect(bool(qa), "the project history holds one QA record")
    phase.expect(same_digest(qa.get("host_outcome_digest"), worker)
                 and same_digest(qa.get("verifier_host_outcome_digest"), verifier),
                 "the stored QA names the worker's outcome and the verifier's that was opened for it")
    given = worker_result_as_given_to_verifier(scene)
    phase.expect(bool(given) and answered_values(given) == json.loads(
        (scene.world.project / "fixture" / "oracle.json").read_text()),
        "the verifier was opened for the worker's recorded answer")
    stored = stored_outcome(scene, "v")
    phase.expect(stored.get("execution_state") == "completed", "the history says the verifier's run completed")
    phase.expect(stored.get("semantic_verdict") == verdict and verdict in ("pass", "ready"),
                 "the history holds the verifier's verdict, pass or ready")
    # The verifier's own outcome keeps its pre-QA counts; the QA record holds the criteria the verifier passed.
    verifier_result = [row for row in scene.world.history_records("dispatch_result")
                       if row.get("run_id") == stored.get("run_id")]
    phase.expect(len(verifier_result) == 1
                 and same_digest(qa.get("verifier_result_digest"), verifier_result[0].get("result_digest")),
                 "the stored QA judged exactly the result the history recorded for the verifier")
    phase.expect(qa.get("required_criteria_passed") == qa.get("required_criteria_total") == len(ORACLE),
                 "the stored QA counts every required criterion passed")
    phase.expect(qa.get("verifier_disagreement") is False, "the stored QA records no discrepancy between verifier and worker")
    report = (scene.steps.get("report") or (None, {}))[1] or {}
    phase.expect((report.get("outcome_status") or {}).get("accepted") == 1,
                 "the orchestration report counts the worker's outcome accepted after the verification")


def qa_accepted(phase: Phase, scene) -> None:
    phase.expect(scene.setup.code == 0, "the scripted setup finishes")
    qa = stored_qa(scene)
    phase.expect(bool(qa), "the project history holds one QA record")
    phase.expect(qa.get("accepted") is True, "the stored QA records accepted: true")
    phase.expect(qa.get("deterministic_pass") is True and qa.get("security_pass") is True,
                 "the stored QA passed its deterministic and security checks")
    stored = stored_outcome(scene, "")
    worker_result = [row for row in scene.world.history_records("dispatch_result") if row.get("run_id") == stored.get("run_id")]
    phase.expect(len(worker_result) == 1
                 and same_digest(qa.get("evaluator_result_digest"), worker_result[0].get("result_digest")),
                 "the stored QA accepted exactly the result the history recorded for the worker")
    phase.expect(stored.get("accepted") is True and stored.get("status") == "accepted",
                 "the worker's outcome in the history is accepted")
    report = (scene.steps.get("report") or (None, {}))[1] or {}
    phase.expect((report.get("qa") or {}).get("passed") == 1 and (report.get("qa") or {}).get("failed") == 0
                 and (report.get("qa") or {}).get("attention_required") is False,
                 "the orchestration report counts one QA passed, none failed, none needing attention")


@check("G2", group="life_claude")
def g2_claude_primary_worker_answers_match_oracle_in_history(phase: Phase) -> None:
    worker_answers_match_oracle(phase, lifecycle_scene("claude"))


@check("G2", group="life_codex")
def g2_codex_primary_worker_answers_match_oracle_in_history(phase: Phase) -> None:
    worker_answers_match_oracle(phase, lifecycle_scene("codex"))


@check("G3", group="life_claude")
def g3_claude_primary_verifier_passes_every_item_in_history(phase: Phase) -> None:
    verifier_answers_pass(phase, lifecycle_scene("claude"), "pass")


@check("G3", group="life_codex")
def g3_codex_primary_verifier_passes_every_item_in_history(phase: Phase) -> None:
    verifier_answers_pass(phase, lifecycle_scene("codex"), "pass")


@check("G3", group="life_claude_ready")
def g3_claude_primary_verifier_ready_passes_every_item_in_history(phase: Phase) -> None:
    verifier_answers_pass(phase, lifecycle_scene("claude", "ready"), "ready")


@check("G4", group="life_claude")
def g4_claude_primary_qa_accepted_in_history(phase: Phase) -> None:
    qa_accepted(phase, lifecycle_scene("claude"))


@check("G4", group="life_codex")
def g4_codex_primary_qa_accepted_in_history(phase: Phase) -> None:
    qa_accepted(phase, lifecycle_scene("codex"))


@check("G4", group="life_claude_ready")
def g4_claude_primary_qa_accepted_after_verifier_ready_in_history(phase: Phase) -> None:
    qa_accepted(phase, lifecycle_scene("claude", "ready"))
