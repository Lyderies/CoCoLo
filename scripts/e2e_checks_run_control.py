"""L6, L7, L9 and G5: run control (T7 slice S1): the containment the product can prove with fake clients, stopping
a running child (cancel, silence, hard timeout) and the capacity limits and releases.

Every value is read through a different path than the command that produced it: the project history (read-only),
the account ledger's capacity table (read-only), the account service's own per-run capacity read
(`account.capacity_status`, over its socket) and the project service's live run status, never the dispatch reply
alone. The children are the fake clients; the installed CLI is the only CLI run (World.cli)."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_run import child_answer, trusted_interpreter_checks  # noqa: E402
from e2e_core import CLAUDE_MODEL, PROBE, _SCENES, Phase, check  # noqa: E402
import e2e_checks_shared as shared  # noqa: E402
from e2e_checks_shared import (INIT, add_project, bridge_run, capacity_read, child_frames, ledger_slots,  # noqa: E402,F401
                               project_records, project_view, slot_of, write_patches)

SECOND = 1_000_000_000
# The run-control scenes' fault (a fault injected into the installed product, to show the slice goes red). It is
# chosen ONLY by the runner's explicit `--fault NAME` argument (e2e_lifecycle.py sets FAULT in the process that runs
# the scenes); no environment variable selects it, and World environments carry no E2E_ variable (e2e_support).
FAULT: str | None = None
S1_IDS = ("L6.1", "L6.2", "L7.1", "L7.2", "L7.3", "L9.1", "L9.2", "L9.3", "G5")
FAULTS = {
    # A finished run's slot is never released: both UPDATEs of active_run_capacity that release it are made to match nothing.
    "slot_not_released": [
        {"module": "codex_agents.learning_store",
         "old": "                if not decided:\n                    self._connection.execute(\n"
                "                        \"UPDATE active_run_capacity SET released_at_ms",
         "new": "                if False:\n                    self._connection.execute(\n"
                "                        \"UPDATE active_run_capacity SET released_at_ms"},
        {"module": "codex_agents.account_cleanup",
         "old": "'WHERE project_id=? AND handle_digest=?', (timestamp, receipt_digest, project_id, key.handle_digest))\n"
                "            before_commit()\n            _write_journal",
         "new": "'WHERE project_id=? AND handle_digest=? AND 0', (timestamp, receipt_digest, project_id, key.handle_digest))\n"
                "            before_commit()\n            _write_journal"}]}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


# The S1 IDs each fault must turn red. L6.2 (refused before admission: no slot is ever claimed, decision 190c) and
# L9.1 are the S1 IDs a release fault cannot reach; L9.1: it judges the refusals
# while the six slots are held for the FIRST time, before any slot is released, so it stays green under
# slot_not_released by construction (its refusals are proven in the normal run).
FAULT_TARGETS = {"slot_not_released": tuple(i for i in S1_IDS if i not in ("L9.1", "L6.2"))}
# IDs a fault may also turn red without being a target: under slot_not_released the later expected-success dispatches
# (fourth_again, c_again) are refused, which G1 (every step not expected to refuse exits 0) rightly reports. Nothing
# else may change: L9.1 red, or any other ID, is not isolation.
FAULT_ALSO_RED = {"slot_not_released": ("G1",)}


def fault_isolated(normal: dict, fault: dict, name: str = "slot_not_released") -> dict:
    """Compare a normal report with a fault report over EVERY requirement ID: the fault run must hold the same IDs,
    turn every target S1 ID of the fault red and change no ID outside the fault's declared targets (FAULT_TARGETS, and
    FAULT_ALSO_RED for G1); a newly red L9.1 or any other ID is not isolation."""
    before, after = normal.get("requirements") or {}, fault.get("requirements") or {}
    changed = sorted(i for i in set(before) & set(after) if bool(before[i].get("passed")) != bool(after[i].get("passed")))
    allowed = set(FAULT_TARGETS[name]) | set(FAULT_ALSO_RED.get(name, ()))
    outside = [i for i in changed if i not in allowed]
    green_s1 = sorted(i for i in FAULT_TARGETS[name] if (after.get(i) or {}).get("passed") is not False)
    missing = sorted(set(before) ^ set(after)) + sorted(i for i in S1_IDS if i not in after)
    return {"isolated": not outside and not green_s1 and not missing, "changed": changed,
            "changed_outside_targets": outside, "changed_outside_s1": outside, "s1_not_red": green_s1, "ids_differ": missing}


# ---------------------------------------------------------------------------------------------
# Building blocks


def liveness_patch(startup: int, quiet: int, stale: int, silence: int) -> dict:
    """Decision 128: the project service's silence limits, scaled for the run by the stand-in Python (test only)."""
    old = "LIVENESS = LivenessLimits(180 * SECOND, 120 * SECOND, 300 * SECOND, 600 * SECOND)"
    return {"module": "codex_agents.project_service", "old": old,
            "new": f"LIVENESS = LivenessLimits({startup} * SECOND, {quiet} * SECOND, {stale} * SECOND, {silence} * SECOND)"}


def stray(tool: str) -> dict:
    """What a fake client prints for a child that calls a tool the run does not allow."""
    return {"type": "assistant", "message": {"model": CLAUDE_MODEL,
                                             "content": [{"type": "tool_use", "name": tool, "input": {}}]}}


def set_up(view) -> None:
    shared.set_up(view, "run-control")


class Ctl(shared.Ctl):
    """S1's primary-session driver (e2e_checks_shared.Ctl): the harness's default dispatch timeout, 90 s to finish."""
    DISPATCH_TIMEOUT = None
    FINISH_TIMEOUT = 90


# The reads that do not go through the command that made the run --------------------------------


def history_rows(world, run, record_type: str) -> list[dict]:
    run_id = (run.opened or {}).get("run_id")
    return [row for row in project_records(world, run.project_id, record_type) if run_id and row.get("run_id") == run_id]


def settled_slots(world, timeout: float = 20) -> list[dict]:
    """The ledger's capacity table once every slot is released (the release follows the child's end by a moment);
    after `timeout` seconds, whatever it holds."""
    deadline = time.monotonic() + timeout
    while True:
        rows = ledger_slots(world)
        if all(row["released_at_ms"] is not None for row in rows) or time.monotonic() >= deadline:
            return rows
        time.sleep(0.3)


def released_everywhere(view, run) -> dict:
    """Whether a run's slot is released, by each of the two reads (the account service's, the ledger's table)."""
    read, row = capacity_read(view, run), slot_of(view, run)
    return {"service": read.get("state"), "ledger": None if row is None else row["released_at_ms"] is not None}


# ---------------------------------------------------------------------------------------------
# Scene 1: one project, runs that are stopped (L6.1, L6.2, L7.1, L7.3)

def control_scene():
    if "run_ctl" in _SCENES:
        return _SCENES["run_ctl"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world, failed=None)
    _SCENES["run_ctl"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())
    set_up(world)
    ctl = scene.ctl = Ctl(world, "p")
    ctl.host("provider-observe", "--provider", "claude", "--billing-probe")
    # L7.1: cancel a running child.
    child_frames(world, {"wait_for": str(world.tools / "never-opened")})
    scene.cancelled = ctl.child()
    ctl.dispatch_in_background(scene.cancelled)
    scene.cancel_before = ctl.wait_running(scene.cancelled)
    scene.cancel_reply = ctl.cancel(scene.cancelled)
    ctl.finish(scene.cancelled)
    scene.cancel_after = ctl.status(scene.cancelled)
    ctl.record(scene.cancelled)
    # L7.3: a hard timeout (the assignment's own bound, below the route's).
    child_frames(world, {"sleep": 40})
    scene.limit = 4
    scene.timed = ctl.child(bounds={"timeout_seconds": scene.limit})
    ctl.dispatch(scene.timed)
    scene.timed_after = ctl.status(scene.timed)
    ctl.record(scene.timed)
    # L6.1: a child that calls a tool the run does not allow (write, network, a shell) is stopped.
    scene.denied = {}
    for tool in ("Write", "WebFetch", "Bash"):
        child_frames(world, stray(tool))
        scene.denied[tool] = ctl.child()
        ctl.dispatch(scene.denied[tool])
        ctl.record(scene.denied[tool])
    # L6.1: a child the client itself denied a tool: the result frame's denials are reported.
    scene.client_denied = ctl.child()
    template = (ctl.host("result-preflight", *scene.client_denied.common)[1] or {}).get("template")
    answer = child_answer(template, False)
    child_frames(world, {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(answer),
                         "structured_output": answer, "usage": {"input_tokens": 1, "output_tokens": 1},
                         "total_cost_usd": 0.1, "permission_denials": [{"tool_name": "Write"}]})
    ctl.dispatch(scene.client_denied)
    ctl.record(scene.client_denied)
    # L6.2: a read path that is not committed (a file the owner has not committed). The admission history is read
    # before the preflight, after it, after the open attempt (which must refuse) and after a dispatch attempt.
    (world.project / "fixture" / "draft.txt").write_text("uncommitted\n")
    scene.admissions_before = admission_history(world, ctl.project_id)
    scene.uncommitted = ctl.child(path="fixture/draft.txt", open_run=False)
    scene.uncommitted_preflight = ctl.host("dispatch-preflight", *scene.uncommitted.common, refuses=True)
    scene.admissions_after_preflight = admission_history(world, ctl.project_id)
    ctl.open(scene.uncommitted, refuses=True)  # exit judged by the L6.2 check (it must refuse)
    scene.admissions_after_open = admission_history(world, ctl.project_id)
    ctl.dispatch(scene.uncommitted, refuses=True)  # no run handle after the refused open: not judged for its reason
    # decision 172: the preflight of the same path, run again after the open and dispatch attempts, is independent of them
    scene.uncommitted_preflight_again = ctl.host("dispatch-preflight", *scene.uncommitted.common, refuses=True)
    scene.admissions_after_dispatch = admission_history(world, ctl.project_id)
    return scene


def names_uncommitted(reply) -> bool:
    """The reply refuses for the expected reason: the uncommitted read path (reason_code read_path_not_committed, or a
    reason that says the path is not committed). An empty reply, or any other reason, is not that refusal."""
    reply = reply or {}
    return reply.get("reason_code") == "read_path_not_committed" or "not committed" in (reply.get("reason") or "")


def admission_history(world, project_id: str | None) -> dict:
    """What an admission leaves, read-only and not through the command: the project history's open provenance
    records, the account ledger's run bindings and its capacity slots."""
    counts = world.ledger_counts()
    return {"host_open_provenance": len(project_records(world, project_id, "host_open_provenance")),
            "host_run_bindings": counts.get("host_run_bindings"),
            "active_run_capacity": counts.get("active_run_capacity")}


def stored_result_names(run, reason: str) -> bool:
    """Whether the run's stored result is a failure naming `reason`. The history keeps the child's result only as a
    keyed digest (dispatch_result.result_digest, with its terminal_state); the result the dispatch returned is the
    one the product stores, and recording it as the primary does (learning-record-host-result) succeeds only when
    it matches that stored digest. So: the recording was accepted, and that result is failed and names `reason`."""
    result = (run.reply or {}).get("result") or {}
    return (run.recorded is not None and run.recorded[0] == 0 and result.get("execution_state") == "failed"
            and reason in (result.get("summary") or ""))


def terminal_state(world, run) -> str | None:
    rows = history_rows(world, run, "dispatch_result")
    return rows[-1].get("terminal_state") if len(rows) == 1 else None


def stop_event(world, run) -> str | None:
    rows = history_rows(world, run, "event")
    return rows[-1].get("stop_reason") if rows else None


@check("L7.1", group="run_ctl")
def l7_1_cancel_run_stops_a_running_child_ends_cancelled_capacity_released(phase: Phase) -> None:
    scene = control_scene()
    world, run = scene.world, scene.cancelled
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    phase.expect((scene.cancel_before.get("liveness") or {}).get("last_activity_category") == "startup"
                 and scene.cancel_before.get("stop_requested") is False,
                 "the service reports the child running before it is cancelled")
    phase.expect(scene.cancel_reply.get("ready") is True and scene.cancel_reply.get("stop_requested") is True
                 and scene.cancel_reply.get("stop_reason") == "cancel_requested", "learning-cancel-run is accepted")
    phase.reason("cancel_requested")
    phase.expect(run.thread is not None and not run.thread.is_alive() and run.code == 0,
                 "the cancelled dispatch ends instead of waiting for the child")
    phase.expect(run.seconds is not None and run.seconds < 30, "the child was stopped, not waited for")
    phase.expect(terminal_state(world, run) == "cancelled", "the project history holds the run's terminal state: cancelled")
    phase.expect(stop_event(world, run) is not None, "the project history holds the run's stop event")
    phase.expect(scene.cancel_after.get("stop_reason") == "cancel_requested"
                 and scene.cancel_after.get("cleanup_confirmed") is True and scene.cancel_after.get("terminal_observed") is True,
                 "the service reports the stop reason and the child's cleanup confirmed")
    released = released_everywhere(project_view(world), run)
    phase.expect(released["service"] == "released", "the account service's per-run read says the slot is released")
    phase.expect(released["ledger"] is True, "the ledger's capacity table says the slot is released")


@check("L7.3", group="run_ctl")
def l7_3_hard_timeout_stops_a_child_ends_timed_out_capacity_released(phase: Phase) -> None:
    scene = control_scene()
    world, run = scene.world, scene.timed
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    bounds = ((run.reply or {}).get("bridge_report") or {}).get("effective_bounds") or {}
    phase.expect(bounds.get("hard_timeout_seconds") == scene.limit, "the hard timeout in force is the assignment's bound")
    phase.expect(run.code == 0 and run.seconds is not None and scene.limit <= run.seconds < 35,
                 "the child is stopped at the hard timeout, not waited for")
    phase.expect(terminal_state(world, run) == "timed_out", "the project history holds the run's terminal state: timed_out")
    phase.expect(stop_event(world, run) == "timeout", "the project history holds the stop reason: timeout")
    phase.expect(scene.timed_after.get("stop_reason") == "absolute_deadline"
                 and scene.timed_after.get("cleanup_confirmed") is True,
                 "the service reports the deadline as the stop reason and the child's cleanup confirmed")
    phase.reason("absolute_deadline")
    released = released_everywhere(project_view(world), run)
    phase.expect(released["service"] == "released" and released["ledger"] is True,
                 "the slot is released by the account service's per-run read and the ledger's table")


@check("L6.1", group="run_ctl")
def l6_1_child_calling_a_tool_the_run_does_not_allow_is_stopped_and_result_says_so(phase: Phase) -> None:
    """The fake client cannot enforce an OS sandbox (owner-only proof, see S1-STATUS.md): this checks COCOLO's own
    handling of a child that calls a write, network or shell tool the run does not allow."""
    scene = control_scene()
    world = scene.world
    for tool, run in scene.denied.items():
        phase.expect(run.open_code == 0 and run.code == 0, f"the {tool} run opens and its dispatch answers")
        report = (run.reply or {}).get("bridge_report") or {}
        result = (run.reply or {}).get("result") or {}
        phase.expect(report.get("outcome") == "claude_tool_not_allowed",
                     f"the {tool} call stops the child with a plain reason")
        phase.expect(result.get("execution_state") == "failed" and "claude_tool_not_allowed" in (result.get("summary") or ""),
                     f"the {tool} run's result says the tool was not allowed")
        phase.expect(terminal_state(world, run) == "failed", f"the history holds the {tool} run as failed")
        phase.expect(stored_result_names(run, "claude_tool_not_allowed"),
                     f"the {tool} run's stored result (recorded against its digest) names claude_tool_not_allowed")
        phase.expect(report.get("capacity_state") == "retained" and slot_of(world, run) is not None
                     and slot_of(world, run)["released_at_ms"] is not None, f"the {tool} run's slot is released")
    phase.reason("claude_tool_not_allowed")


@check("L6.1", group="run_ctl")
def l6_1_tool_the_client_denied_is_reported_in_the_result(phase: Phase) -> None:
    """FINDING (S1-STATUS.md): the bridge builds the report's permission_denials (bridge.py:1631) but the dispatch reply
    drops it, so a child the client denied a tool ends completed and nothing in the reply or result says so. This
    check states the requirement and is red until the primary decides (fix the product or narrow the requirement)."""
    scene = control_scene()
    run = scene.client_denied
    report = (run.reply or {}).get("bridge_report") or {}
    phase.expect(run.open_code == 0 and run.code == 0, "the run opens and the dispatch answers")
    stored = history_rows(scene.world, run, "dispatch_result")
    phase.expect(len(stored) == 1, "the history holds the run's result")
    phase.expect(stored[0].get("terminal_state") == "failed",
                 "the history holds the run as failed, never completed or with no terminal state")
    phase.expect(report.get("outcome") == "claude_tool_not_allowed", "the dispatch reply ends the run claude_tool_not_allowed")
    phase.expect(stored_result_names(run, "claude_tool_not_allowed"),
                 "the stored result (recorded against its digest) is failed and names claude_tool_not_allowed")
    phase.expect((report.get("permission_denials") or {}).get("count") == 1
                 and (report.get("permission_denials") or {}).get("tools") == ["Write"],
                 "the report counts the client's permission denial and names the tool")
    phase.expect(report.get("read_containment") == "enforced", "the report says read containment was enforced")


@check("L6.2", group="run_ctl")
def l6_2_uncommitted_read_path_refused_before_admission(phase: Phase) -> None:
    """FINDING (S1-STATUS.md, decision 151): learning-open-host-run admits a run for the uncommitted path; this check
    is red until the product fix (fix/l6-findings) makes the open refuse."""
    scene = control_scene()
    code, reply, text = scene.uncommitted_preflight
    phase.expect(code == 2 and (reply or {}).get("ready") is False, "the export preflight refuses the assignment")
    phase.expect("not committed" in ((reply or {}).get("reason") or ""), "the refusal says the read path is not committed")
    phase.expect("admission_created" in (reply or {}) and reply["admission_created"] is False,
                 "the refusal says it admitted nothing (admission_created false)")
    phase.expect(scene.admissions_after_preflight == scene.admissions_before,
                 "the preflight made no admission record, run binding or capacity slot")
    phase.reason("read_path_not_committed")
    run = scene.uncommitted
    phase.expect(run.open_code not in (0, None) and (run.opened or {}).get("admission") != "admitted"
                 and (run.opened or {}).get("ready") is not True and names_uncommitted(run.opened),
                 "opening a run for the uncommitted path is refused, with the read-path reason (a crash with an empty reply is no refusal)")
    phase.expect(scene.admissions_after_open == scene.admissions_before,
                 "after the open attempt the admission history is unchanged (re-read after it)")
    again_code, again, _ = scene.uncommitted_preflight_again
    phase.expect(again_code not in (0, None) and (again or {}).get("ready") is not True and names_uncommitted(again)
                 and (again or {}).get("admission_created") is False,
                 "dispatch-preflight of the uncommitted path, run again after the open and dispatch attempts, refuses with the "
                 "read-path reason and admission_created false")
    phase.expect(scene.admissions_after_dispatch == scene.admissions_before,
                 "after both attempts the admission history shows no new admission, run binding or capacity slot")


# ---------------------------------------------------------------------------------------------
# Scene 2: a child that goes silent (L7.2), the service's limits scaled for this run (decision 128)

SILENCE = (3, 1, 2, 4)  # startup, quiet, stale, silence cutoff (seconds); the product's own are 180, 120, 300, 600


def silence_scene():
    if "run_silence" in _SCENES:
        return _SCENES["run_silence"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["run_silence"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, [liveness_patch(*SILENCE), *fault_patches()])
    set_up(world)
    ctl = scene.ctl = Ctl(world, "s")
    ctl.host("provider-observe", "--provider", "claude", "--billing-probe")
    child_frames(world, {"sleep": 45})  # one frame (init) and then nothing
    scene.run = ctl.child()
    ctl.dispatch_in_background(scene.run)
    scene.during = ctl.wait_running(scene.run)
    ctl.finish(scene.run)
    scene.after = ctl.status(scene.run)
    return scene


@check("L7.2", group="run_silence")
def l7_2_silent_child_stopped_at_the_cutoff_ends_work_silence_capacity_released(phase: Phase) -> None:
    scene = silence_scene()
    world, run = scene.world, scene.run
    phase.expect(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted", "the run opens admitted")
    limits = (scene.during.get("liveness") or {})
    phase.expect(limits.get("silence_remaining_ms") is not None and limits["silence_remaining_ms"] <= SILENCE[3] * 1000,
                 "the service holds the scaled silence cutoff for the run")
    phase.expect(run.thread is not None and not run.thread.is_alive() and run.code == 0,
                 "the silent child's dispatch ends")
    phase.expect(SILENCE[3] <= run.seconds < 40, "the silent child is stopped at the scaled cutoff, not waited for")
    report = (run.reply or {}).get("bridge_report") or {}
    phase.expect(report.get("outcome") == "work_silence", "the run ends with work_silence")
    phase.reason("work_silence")
    phase.expect(scene.after.get("stop_reason") == "work_silence" and scene.after.get("cleanup_confirmed") is True,
                 "the service reports work_silence as the stop reason and the child's cleanup confirmed")
    phase.expect(terminal_state(world, run) == "timed_out", "the project history holds the run's terminal state: timed_out")
    released = released_everywhere(project_view(world), run)
    phase.expect(released["service"] == "released" and released["ledger"] is True,
                 "the slot is released by the account service's per-run read and the ledger's table")


# ---------------------------------------------------------------------------------------------
# Scene 3: capacity across three projects of one HOME (L9.1, L9.2, L9.3, G5)

# The billing probes of this scene are made for a second Claude account of the same machine, attested by the
# owner key in project C and never probed before: its billing reading does not exist, so the installed reuse and
# cooldown (five minutes each, unchanged: decision 128 scales only the silence limits) cannot answer for it, and
# the probe is a genuine claim on a machine slot. The children run on the account the setups attested.
PROBE_ACCOUNT = "capacity-probe@example.invalid"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def attest_probe_account(world, view) -> str:
    """Observe the second account in `view`'s project and attest its billing with the owner key, as the owner's
    tool does (a signed statement); the account's fingerprint. The fake client is the first account again after."""
    from e2e_checks_owner_key import sign_with_key  # the owner-key checks' own signer (scratch key, askpass)
    import base64
    world.claude_account(PROBE_ACCOUNT)
    try:
        code, observed, _ = view.cli("provider-observe", "--provider", "claude")
        identity = [r for r in (observed or {}).get("records", []) if r.get("record_type") == "provider_identity"]
        fingerprint = (identity[0].get("account_fingerprint") if identity else None) or ""
        grant = world.base / "tmp" / "probe-account-grant.json"
        grant.write_text(json.dumps({"version": 1, "provider": "claude", "account_fingerprint": fingerprint,
                                     "no_paid_overage": True, "no_usage_credits": True,
                                     "issued_at": int(time.time() * 1000)}))
        base = ("learning-record-billing-attestation", "--provider", "claude", "--grant", str(grant))
        code, prepared, _ = view.cli(*base)
        if code != 0 or "signing_payload_b64" not in (prepared or {}):
            raise e2e_support.HarnessError("the second account's billing statement was not prepared")
        signature = sign_with_key(world, world.home / ".ssh" / "codex-agents-owner",
                                  base64.b64decode(prepared["signing_payload_b64"]))
        if signature is None or view.cli(*base, "--signature", str(signature))[0] != 0:
            raise e2e_support.HarnessError("the second account's billing statement was not recorded")
    finally:
        world.claude_account(None)
    view.cli("provider-observe", "--provider", "claude")
    return fingerprint


def probe_as_second_account(world, view, *, refuses: bool):
    """One billing probe of project C made while the fake client is the second account; the client is the first
    account again after it (and C observes it)."""
    world.claude_account(PROBE_ACCOUNT)
    try:
        return view.host_cli("claude", *PROBE, refuses=refuses)
    finally:
        world.claude_account(None)
        view.cli("provider-observe", "--provider", "claude")


def ledger_records(world, record_type: str) -> list[dict]:
    """The account ledger's records of one kind, read-only, with the time the ledger stored for each."""
    path = world.home / ".local" / "state" / "codex-agents" / "learning.sqlite3"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found = []
        for observed, content in connection.execute(
                "select observed_at_ms, content from records where record_type = ? order by observed_at_ms", (record_type,)):
            try:
                record = json.loads(content)
            except (TypeError, ValueError):
                continue
            found.append({**record, "observed_at_ms": observed}
                         if isinstance(record, dict) else {"observed_at_ms": observed})
        return found
    finally:
        connection.close()


def capacity_scene():
    if "run_capacity" in _SCENES:
        return _SCENES["run_capacity"]
    world = e2e_support.World().__enter__()
    scene = SimpleNamespace(world=world)
    _SCENES["run_capacity"] = scene
    trusted_interpreter_checks(world)
    write_patches(world, fault_patches())  # no billing constant is rewritten (decision 128)
    views = [project_view(world), add_project(world, "project-b"), add_project(world, "project-c")]
    for view in views:  # every setup first: a later setup restarts the shared collector
        set_up(view)
    scene.views = views
    scene.probe_fingerprint = attest_probe_account(world, views[2])
    ctls = scene.ctls = [Ctl(view, tag) for view, tag in zip(views, "abc")]
    a, b, c = ctls
    gate = world.tools / "capacity-gate"
    scene.a_runs = [a.child() for _ in range(4)]  # the fourth one is held back
    scene.b_runs = [b.child() for _ in range(3)]
    scene.c_run = c.child()
    child_frames(world, {"wait_for": str(gate)})
    scene.started = scene.a_runs[:3] + scene.b_runs
    for ctl, runs in ((a, scene.a_runs[:3]), (b, scene.b_runs)):
        for run in runs:
            ctl.dispatch_in_background(run)
    scene.running = [ctl.wait_running(run) for ctl, runs in ((a, scene.a_runs[:3]), (b, scene.b_runs)) for run in runs]
    # Full: every slot of the machine is held (3 + 3 of 6).
    scene.slots_full = ledger_slots(world)
    scene.fourth = scene.a_runs[3]
    scene.refused = [scene.fourth, scene.c_run]  # the two dispatches expected to be refused: they leave no result
    a.dispatch(scene.fourth, refuses=True)  # a fourth child of project A: refused (the project's own limit)
    c.dispatch(scene.c_run, refuses=True)  # a child of project C: every machine slot is held
    # A billing probe the provider must answer (the second account has no reading to reuse): no slot either.
    scene.probe_full = probe_as_second_account(world, views[2], refuses=True)
    scene.slots_after_refusals = ledger_slots(world)
    # Let every child end: each ends without a result, which is a terminal that releases its slot.
    gate.write_text("open")
    for ctl, runs in ((a, scene.a_runs[:3]), (b, scene.b_runs)):
        for run in runs:
            ctl.finish(run)
    scene.slots_ended = settled_slots(world)
    for ctl, runs in ((a, scene.a_runs[:3]), (b, scene.b_runs)):
        for run in runs:
            ctl.record(run)
    scene.reads = {run.name + str(index): capacity_read(run_view, run)
                   for index, (run_view, run) in enumerate([(views[0], r) for r in scene.a_runs[:3]]
                                                           + [(views[1], r) for r in scene.b_runs])}
    # Capacity is free again: the refused children now run.
    scene.fourth_again, scene.c_again = a.child(), c.child()
    child_frames(world)
    a.dispatch(scene.fourth_again)
    c.dispatch(scene.c_again)
    a.record(scene.fourth_again), c.record(scene.c_again)
    # G5: close every orchestration first; then ONE probe (the second account's, the one refused while full; now expected to succeed), its launch
    # read in the account ledger; then the eight per-run reads and the ledger's capacity table.
    scene.reports = [ctl.close() for ctl in ctls]
    scene.closed_at_ms = now_ms()
    world.claude_frames(e2e_support.claude_probe_frames())
    scene.probe_started_ms = now_ms()
    scene.probe_free = probe_as_second_account(world, views[2], refuses=False)  # expected to succeed: G1 and G5 judge it
    scene.probe_admissions = [r for r in ledger_records(world, "probe_admission")
                              if r.get("account_fingerprint") == scene.probe_fingerprint]
    scene.probe_billing = [r for r in ledger_records(world, "provider_billing")
                           if r.get("account_fingerprint") == scene.probe_fingerprint
                           and r["observed_at_ms"] >= scene.closed_at_ms]
    scene.reads_started_ms = now_ms()
    scene.final_reads = [(view, run, capacity_read(view, run)) for view, run in
                         [(views[0], r) for r in scene.a_runs[:3] + [scene.fourth_again]]
                         + [(views[1], r) for r in scene.b_runs] + [(views[2], scene.c_again)]]
    scene.slots_final = settled_slots(world)
    return scene


@check("L9.1", group="run_capacity")
def l9_1_children_run_at_once_and_one_over_the_limit_is_refused_with_its_reason(phase: Phase) -> None:
    scene = capacity_scene()
    phase.expect(all(run.open_code == 0 and (run.opened or {}).get("admission") == "admitted"
                     for run in scene.a_runs + scene.b_runs + [scene.c_run]), "every run opens admitted")
    phase.expect(len(scene.running) == 6 and all((status.get("liveness") or {}).get("last_activity_category")
                                                 for status in scene.running),
                 "the services report six children running at once, three in each of two projects")
    held = [row for row in scene.slots_full if row["released_at_ms"] is None]
    phase.expect(len(held) == 6, "the ledger holds six slots while the six children run")
    fourth = scene.fourth.reply or {}
    phase.expect(scene.fourth.code != 0 and fourth.get("ready") is False and fourth.get("reason_code") == "preparation_full",
                 "a fourth child of one project is refused with preparation_full")
    phase.reason("preparation_full")
    third_project = scene.c_run.reply or {}
    phase.expect(scene.c_run.code != 0 and third_project.get("ready") is False
                 and third_project.get("reason_code") == "claim_busy",
                 "a child of a third project is refused with claim_busy: every machine slot is held")
    phase.reason("claim_busy")
    code, probe, _ = scene.probe_full
    phase.expect(code != 0 and (probe or {}).get("reason_code") == "probe_busy",
                 "the billing probe is refused with probe_busy while the machine is full")
    phase.reason("probe_busy")
    phase.expect(len([row for row in scene.slots_after_refusals if row["released_at_ms"] is None]) == 6
                 and len(scene.slots_after_refusals) == len(scene.slots_full),
                 "the refusals took no slot and released none")


@check("L9.2", group="run_capacity")
def l9_2_capacity_released_after_each_terminal_shown_by_per_run_read(phase: Phase) -> None:
    scene = capacity_scene()
    phase.expect(len(scene.reads) == 6, "every started child has a per-run capacity read")
    phase.expect(all(read.get("state") == "released" and read.get("released_at_ms") for read in scene.reads.values()),
                 "the account service's per-run read says each ended child's slot is released")
    phase.expect(all(row["released_at_ms"] is not None for row in scene.slots_ended)
                 and len(scene.slots_ended) == 6, "the ledger's capacity table holds all six slots released")
    for run in scene.a_runs[:3] + scene.b_runs:
        phase.expect(terminal_state(scene.world, run) is not None, "the history holds a terminal for each child")
    phase.expect(all(run.code == 0 for run in scene.a_runs[:3] + scene.b_runs),
                 "each of the six first-wave dispatches exits 0 (an expected success, never a refusal)")
    phase.expect(scene.fourth_again.code == 0 and (scene.fourth_again.reply or {}).get("ready") is True,
                 "a fourth child of the project runs once its slots are free")
    phase.expect(scene.c_again.code == 0 and (scene.c_again.reply or {}).get("ready") is True,
                 "the third project's child runs once the machine has a free slot")


@check("L9.3", group="run_capacity")
def l9_3_two_projects_share_one_collector_each_with_its_own_runs_and_capacity(phase: Phase) -> None:
    scene = capacity_scene()
    a, b = scene.ctls[0], scene.ctls[1]
    phase.expect(bool(a.project_id) and bool(b.project_id) and a.project_id != b.project_id,
                 "the two projects have their own identities")
    phase.expect(len(list((scene.world.home / ".local" / "state" / "codex-agents").glob("collector.sock"))) == 1,
                 "the projects share one collector socket")
    full = scene.slots_full
    phase.expect(sorted(row["project_id"] for row in full if row["project_id"] in (a.project_id, b.project_id))
                 == sorted([a.project_id] * 3 + [b.project_id] * 3),
                 "the ledger holds three slots of each project while both run")
    phase.expect(not [row for row in full if row["project_id"] == scene.ctls[2].project_id],
                 "the third project held no slot")
    phase.expect(len({row["bridge_run_id"] for row in full}) == 6, "each project's runs are its own")
    # Each project's history holds only its own runs: read each project's own history file.
    homes = sorted((scene.world.home / ".local" / "state" / "codex-agents" / "projects").glob("*/history.sqlite3"))
    phase.expect(len(homes) == 3, "each project has its own history")
    # Exact run IDs, not counts (a result exchanged between two projects keeps every count): each project's
    # results are exactly the runs it dispatched, and every record of its history that names a run names one of
    # the runs opened for it.
    for ctl, count in zip(scene.ctls, (4, 3, 1)):
        opened = {(run.opened or {}).get("run_id") for run in ctl.runs} - {None}
        # the runs SCHEDULED to succeed (all but the ones the scene expects to be refused), not the runs that happened
        # to exit 0: a failed dispatch and its missing result must not disappear together
        scheduled = sorted((run.opened or {}).get("run_id") for run in ctl.runs if run not in scene.refused)
        results = sorted(row.get("run_id") for row in project_records(scene.world, ctl.project_id, "dispatch_result"))
        phase.expect(results == scheduled and len(results) == count and None not in results,
                     "each project's history holds exactly the results of the runs scheduled to succeed, by run ID (4, 3, 1)")
        named = {row.get("run_id") for kind in ("dispatch_result", "event", "host_open_provenance")
                 for row in project_records(scene.world, ctl.project_id, kind) if row.get("run_id")}
        phase.expect(named <= opened, "no record of a project's history names a run opened for another project")
    phase.expect(sum(len(ctl.runs) for ctl in scene.ctls) == len({(run.opened or {}).get("run_id")
                                                                  for ctl in scene.ctls for run in ctl.runs}),
                 "no run ID was opened for two projects")
    admitted = [(report.get("runs") or {}).get("admitted") for report in scene.reports]
    phase.expect(admitted == [5, 3, 2], "each project's orchestration report counts its own admitted runs (5, 3, 2)")


@check("G5", group="run_capacity")
def g5_after_close_every_claimed_run_released_next_probe_gets_a_slot_and_nothing_active(phase: Phase) -> None:
    scene = capacity_scene()
    for report in scene.reports:
        runs = (report or {}).get("runs") or {}
        phase.expect(runs.get("current_active") == 0 and runs.get("pending") == 0,
                     "the final statistics show no active or pending run")
    phase.expect(scene.closed_at_ms <= scene.probe_started_ms <= scene.reads_started_ms,
                 "the orchestrations are closed first, then the probe is made, then the per-run reads are taken")
    code, probe, _ = scene.probe_free
    phase.expect(code == 0 and (probe or {}).get("ready") is True and (probe or {}).get("probe") == "launched",
                 "the next billing probe, the one refused while the machine was full, gets a slot and completes")
    passed = [r for r in scene.probe_admissions if r.get("decision") == "pass"]
    phase.expect(len(passed) == 1 and passed[0]["observed_at_ms"] >= scene.closed_at_ms,
                 "the account ledger holds exactly one admitted probe of that account, made after the close")
    phase.expect(len(scene.probe_billing) >= 1, "the account ledger holds the launched probe's billing reading")
    phase.expect(len(scene.final_reads) == 8, "every claimed run has a per-run capacity read")
    phase.expect(all(read.get("state") == "released" for _view, _run, read in scene.final_reads),
                 "the account service's per-run read says every claimed run is released")
    phase.expect(len(scene.slots_final) == 8 and all(row["released_at_ms"] is not None for row in scene.slots_final),
                 "the ledger holds eight claimed runs, every one released")
