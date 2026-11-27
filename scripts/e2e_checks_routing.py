"""L4: routing."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_core import CLAUDE_MODEL, PROBE, ROUTE_KEY, ROUTE_TASK, _SCENES, Phase, check  # noqa: E402


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
    # A route whose provider is edited is the one edit the open does not refuse: a changed provider invalidates
    # the advertised execution settings, and only the fresh installed route supplies their replacements
    # (cli.py, learning-open-host-run). So the edit is offered to its own orchestration and assignment (the first
    # run holds the task), and what is admitted must be the installed route, with no trace of the edit.
    scene.other_provider = other
    other_handle = (host("learning-open-orchestration")[1] or {}).get("orchestration_handle", "")
    (world.base / "assignment-provider.json").write_text(json.dumps({**assigned, "assignment_id": "assignment-2"}))
    edited = world.base / "edited-provider.json"
    edited.write_text(json.dumps({**route, "provider": other}))
    scene.provider_edit = host("learning-open-host-run", *context(edited, world.base / "assignment-provider.json"),
                               "--orchestration-handle", other_handle)
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


@check("L4.1", group="route_claude")
def l4_1_claude_primary_route_and_child_run_match_policy(phase: Phase) -> None:
    scene = route_scene("claude")
    phase.expect(scene.expected["provider"] == "claude", "a Claude primary's first allowed candidate is a Claude model")
    child_run_matches_route(phase, scene)


@check("L4.1", group="route_codex")
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
    # The one edit that is not refused: an edited provider is replaced by the installed route (see route_scene).
    code, reply, _ = scene.provider_edit
    phase.expect(scene.other_provider != scene.route.get("provider"), "the edit names the other provider")
    phase.expect(code == 0 and reply is not None and reply.get("admission") == "admitted",
                 "a route with its provider edited opens with the installed route in its place")
    admitted = (reply or {}).get("route") or {}
    for key in ("provider", "model", "effort", "role", "agent_type", "sandbox_mode", "dispatch"):
        phase.expect(admitted.get(key) == scene.route.get(key), f"the admitted route's {key} is the installed route's, not the edit's")
    code, reply, _ = scene.late_edit
    phase.expect(code == 2 and reply is not None and reply.get("ready") is not True,
                 "a route edited after the run opened is refused at dispatch")
    code, reply, _ = scene.opened
    phase.expect(code == 0 and reply is not None and reply.get("admission") == "admitted",
                 "the same route unedited still opens (the refusals were about the edit)")


@check("L4.2", group="route_claude")
def l4_2_claude_primary_edited_route_refused(phase: Phase) -> None:
    edited_route_refused(phase, route_scene("claude"))


@check("L4.2", group="route_codex")
def l4_2_codex_primary_edited_route_refused(phase: Phase) -> None:
    edited_route_refused(phase, route_scene("codex"))


# ---------------------------------------------------------------------------------------------
# L5: the run lifecycle
