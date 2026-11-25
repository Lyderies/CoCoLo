"""API-7..15, 17..23, TERM-13: what the core API answers and does with the records, the events, the talk, the tokens,
the scopes, the confirm step, the owner wall and the audit (T7 slice S6, part 2; part 1 is
scripts/e2e_checks_core_api.py, whose real-process harness this module reuses).

Same ground rules as part 1: a REAL headless core process from the installed project's own package, spoken to only
over the unix socket as JSON-RPC; every value is compared with something read another way (the records on disk, the
audit files, the token store, a helper process of the product's own library). The producer of the records (the
runner, A5/A6) is not built, so the records are written by hand, and each check says so in its reason code
(`records_hand_written_runner_not_built`); each ID here is about what the CORE does with records it is given.

NOT checked here, each marked `deferred` with its reason in tests/e2e/requirements.json (decision 259: a check is
green only through the real product path; the stand-in versions are in commit bb7ae3cd):
- API-16 and API-17 (delivery at real safe points: nothing in 0.8.60 calls the core's safe-point delivery, so the quoted
  delivery of a push cannot be shown on the real path; API-17's label, size and rate caps are covered by
  packages/codex-agents/tests/test_core_api_*.py), TERM-6 and TERM-7
  (run counts and usage from a real run into the core's records: no runner writes them), TERM-5 and TERM-17 (no
  terminal client exists to drive).
- API-3: a connection from another Unix user needs a second Unix user the harness does not have.
- API-26, API-27: later adapters (dashboard milestone, tested last).

Test-only wiring is the constructor's own documented arguments (a limits core: small caps, a fake clock), never a
product knob. The faults are chosen ONLY by the runner's explicit `--fault NAME` argument."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path
import e2e_support  # noqa: E402
from e2e_checks_core_api import HAND_WRITTEN, TWO_STEP, compare_fault, UNITS, Core, CoreWorld, Refused, Wire, change  # noqa: E402
from e2e_checks_shared import write_patches  # noqa: E402
from e2e_core import _SCENES, Phase, check  # noqa: E402

B_IDS = ("API-7", "API-8", "API-9", "API-10", "API-11", "API-12", "API-13", "API-14", "API-15",
         "API-18", "API-19", "API-20", "API-21", "API-22", "API-23", "TERM-13")

FAULT: str | None = None
FAULTS = {
    "core_records_broken": [
        # API-19, API-20, TERM-13: the read scope may call the owner's and the control methods.
        {"module": "codex_agents.core_api",
         "old": "    \"read\": READ_METHODS,\n",
         "new": "    \"read\": READ_METHODS | CONFIRM_ONLY | OWNER_ONLY,\n"},
        # API-21: a confirm id is no longer bound to the call's parameters.
        {"module": "codex_agents.core_api",
         "old": "            if entry is None or entry[\"key\"] != key:\n",
         "new": "            if entry is None:\n"},
        # API-22: the owner wall is open.
        {"module": "codex_agents.core_api",
         "old": "        if method in WALLED:\n            raise ApiError(\"owner_terminal_only\"",
         "new": "        if False:\n            raise ApiError(\"owner_terminal_only\""},
        # API-14: a secret-shaped run in an event's text is no longer removed.
        {"module": "codex_agents.core_api",
         "old": "        line = \"\".join(c for c in line if ord(c) >= 32)[:EVENT_VALUE_CHARS]\n        return _no_money_text(SECRET_VALUE.sub(SECRET_REMOVED, line))\n",
         "new": "        line = \"\".join(c for c in line if ord(c) >= 32)[:EVENT_VALUE_CHARS]\n        return line\n"},
        # API-13: a kind filter lets every event through.
        {"module": "codex_agents.core_api",
         "old": "        if kinds and event.get(\"kind\") not in kinds:\n            return False\n",
         "new": "        if False:\n            return False\n"},
        # API-12: an expired cursor is replayed as if it were current.
        {"module": "codex_agents.core_api",
         "old": "            if from_seq < seq_at and from_seq + 1 < oldest:\n",
         "new": "            if False:\n"},
        # API-8: a missing ci record is answered with a number instead of unknown.
        {"module": "codex_agents.core_api",
         "old": "                return {key: _clean(value) if value is not None else \"unknown\"}\n",
         "new": "                return {key: _clean(value) if value is not None else 0}\n"},
        # API-7: the run counts are wrong.
        {"module": "codex_agents.core_api",
         "old": "            counts = collections.Counter(_plain(r.get(\"state\")) or \"unknown\" for r in runs)\n",
         "new": "            counts = collections.Counter(\"unknown\" for r in runs)\n"},
        # API-23: the audit row keeps every parameter the caller sent, secrets included.
        {"module": "codex_agents.core_api",
         "old": "        summary = {k: self._audit_value(k, v, method, params) for k, v in params.items() if k in self.AUDIT_KEYS}\n",
         "new": "        summary = dict(params)\n"}]}
# Two narrower faults, so each ID they name is red on the assertion it is about (the bundle above is red on the FIRST
# failing assertion of an ID, which for API-18 is the owner wall and proves nothing about revocation).
FAULTS["core_records_revocation_broken"] = [
    # API-18: a revoked token keeps working on a connection that is already open.
    {"module": "codex_agents.core_api",
     "old": "        if session.token_id is not None and self._lookup_token_by_id(session.token_id) is None:\n            raise ApiError(\"unauthorized\", \"the token was revoked\")\n",
     "new": "        if False:\n            raise ApiError(\"unauthorized\", \"the token was revoked\")\n"}]
FAULTS["core_records_reads_broken"] = [
    # API-9: damage to the state record is no longer a finding.
    {"module": "codex_agents.core_api",
     "old": "        elif self._state() is None:\n            add(\"state_unreadable\"",
     "new": "        elif False:\n            add(\"state_unreadable\""},
    # API-10: the core keeps a cache file beside the records.
    {"module": "codex_agents.core_api",
     "old": "        self._clear_stale(path)\n        self._seq = self._last_seq_in_file()\n",
     "new": "        self._clear_stale(path)\n        (self.records / \"units.cache\").write_text(\"cached\")\n        self._seq = self._last_seq_in_file()\n"},
    # API-11: a live subscriber is told a wrong sequence number for a confirmed command (the file keeps the right one).
    {"module": "codex_agents.core_api",
     "old": "                    sub.offer(event)\n",
     "new": "                    sub.offer(dict(event, seq=event[\"seq\"] + 1) if kind == \"command.requested\" else event)\n"},
    # API-15: a canned answer is said to have cost tokens.
    {"module": "codex_agents.core_api",
     "old": "        return {\"answered_by\": \"records\", \"text\": text, \"tokens_used\": 0, \"sources\": sources, \"as_of\": self.clock()}\n",
     "new": "        return {\"answered_by\": \"records\", \"text\": text, \"tokens_used\": 7, \"sources\": sources, \"as_of\": self.clock()}\n"}]
FAULT_TARGETS = {"core_records_broken": ("API-7", "API-8", "API-12", "API-13", "API-14", "API-18", "API-19",
                                         "API-20", "API-21", "API-22", "API-23", "TERM-13"),
                 "core_records_revocation_broken": ("API-18",),
                 "core_records_reads_broken": ("API-9", "API-10", "API-11", "API-15")}
FAULT_ALSO_RED: dict[str, tuple] = {name: () for name in FAULT_TARGETS}


def fault_patches() -> list:
    return FAULTS[FAULT] if FAULT else []


def fault_isolated(normal: dict, fault: dict, name: str = "core_records_broken", ids: tuple = B_IDS) -> dict:
    """The fault must turn every target ID red (from a green normal run) and change no other ID."""
    return compare_fault(normal, fault, FAULT_TARGETS[name], FAULT_ALSO_RED.get(name, ()), ids)


# ---------------------------------------------------------------------------------------------------------
# The records the producer would write (by hand, until the runner exists).

USAGE = {"input": 1234, "output": 56}
EVENT_COUNT = 1100  # more than the core keeps for a resume (1000), so an old cursor really has expired
SECRETS = ("SECRET-TRANSCRIPT-TEXT", "tok-SECRET-VALUE", "pw-SECRET-VALUE", "hunter2hunter2", "abcdefghijklmnop")
STOP = {"type": "stop", "id": "H-2", "code": "guard_context", "reason": "The context window was nearly full.",
        "by": "cocolo", "ts": 1_000_100, "next": "Start a new session from the hand-off.", "unit_id": "U-0002", "v": 1}
DECISIONS = [{"id": "D-0001", "text": "Merge now?", "why": "CI is green", "options": ["yes", "no"], "ts": 1_000_050}]
UNIT_ROWS = [dict(UNITS[0]), dict(UNITS[1]), {"id": "U-0003", "kind": "child", "state": "failed", "title": "Check it",
                                              "role": "verifier", "started": 5, "last": 9, "claude_agent_id": "z"},
             dict(UNITS[0], state="done", last=20)]


def event_rows() -> list[dict]:
    rows = []
    for seq in range(1, EVENT_COUNT + 1):
        kind, data = "unit.updated", {"summary": f"step {seq}"}
        if seq % 100 == 0:
            kind, data = "command.requested", {"method": "control.pause", "command_id": f"C-{seq // 100:04d}"}
        elif seq % 50 == 0:
            kind, data = "alert.guard", {"why": "context is high"}
        elif seq % 7 == 0:
            kind, data = "state.changed", {"what": "phase"}
        rows.append({"seq": seq, "ts": 1_000_000 + seq, "kind": kind, "unit_id": "U-0001", "data": data, "v": 1})
    rows[-1] = {"seq": EVENT_COUNT, "ts": 1_001_100, "kind": "unit.updated", "unit_id": "U-0001", "v": 1,
                "data": {"summary": "edited 3 files; password=hunter2hunter2 Bearer abcdefghijklmnop",
                         "transcript": "SECRET-TRANSCRIPT-TEXT", "token": "tok-SECRET-VALUE",
                         "passphrase": "pw-SECRET-VALUE"}}
    return rows


def jsonl(rows) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def write_records(directory: Path, usage: dict) -> None:
    (directory / "units.jsonl").write_text(jsonl(UNIT_ROWS))
    (directory / "state.json").write_text(json.dumps(state_for(usage)))
    (directory / "events.jsonl").write_text(jsonl(event_rows()))
    (directory / "history.jsonl").write_text(jsonl([{"id": "H-1", "summary": "an earlier note"}, STOP,
                                                     {"id": "H-3", "summary": "UNRELATED LATER ENTRY"}]))
    (directory / "usage.jsonl").write_text(jsonl([{"ts": 1_000_001, "input": 100, "output": 10},
                                                  {"ts": 1_000_002, "input": 1234, "output": 56, "claude_plan": "pro"}]))
    (directory / "decisions.jsonl").write_text(jsonl(DECISIONS))
    for name in ("inbox.jsonl", "commands.jsonl"):
        (directory / name).write_text("")


def state_for(usage: dict) -> dict:
    return {"phase": "build", "session": "S-7", "ctx": 120000, "window": "unknown", "ci": {"status": "green"},
            "versions": {"installed": "0.8.60"}, "claude_session_id": "abc",
            "usage": {"input": usage["input"], "output": usage["output"],
                      "windows": [{"label": "5h", "pct": 40, "resets": "later", "spend": 9}]}}


NEUTRAL_UNIT = ("id", "kind", "state", "title", "role", "started", "last", "result", "exit", "verdict", "verdict_by",
                "verdict_ts")
NEUTRAL_STATE = ("phase", "session", "now", "ctx", "window", "usage", "ci", "versions", "handoff_replies")


def as_unit(row: dict) -> dict:
    """What a unit record looks like through the contract: neutral fields on top, the rest under `extra`."""
    return {**{k: v for k, v in row.items() if k in NEUTRAL_UNIT}, "extra": {k: v for k, v in row.items() if k not in NEUTRAL_UNIT}}


def as_usage(usage: dict) -> dict:
    """A usage record through the contract: its counts, and its windows by label and percentage only (no spend)."""
    return {"input": usage["input"], "output": usage["output"],
            "windows": [{"label": w["label"], "pct": w["pct"]} for w in usage["windows"]]}


def as_state(row: dict) -> dict:
    """A state record through the contract: what the record lacks reads "unknown", provider detail is under `extra`."""
    out = {k: row.get(k, "unknown") for k in ("phase", "session", "now", "ctx", "window", "handoff_replies")}
    out.update(usage=as_usage(row["usage"]), ci=row["ci"], versions=row["versions"])
    out["extra"] = {k: v for k, v in row.items() if k not in NEUTRAL_STATE}
    return out


def files_of(records: Path, *, skip: tuple = ()) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(records.iterdir()) if p.is_file() and p.name not in skip}


def stream(core: Core, token: str, params: dict | None = None, *, count: int | None = None, timeout: float = 8.0):
    """Subscribe on a connection of its own and read events until `count` arrived, the stream ended or time ran out.
    Returns (events, notices, raw bytes read)."""
    wire = Wire(core.socket, timeout=timeout)
    wire.init(token)
    wire.call("events.subscribe", params or {})
    events, notices, deadline = [], [], time.monotonic() + timeout
    while time.monotonic() < deadline and (count is None or len(events) < count):
        message = wire.message(max(0.1, deadline - time.monotonic()))
        if message is None:
            break
        if message.get("method") == "events.event":
            events.append(message["params"])
        elif message.get("method"):
            notices.append(message)
            if message["method"] == "events.end":
                break
    raw = json.dumps([events, notices])
    wire.close()
    return events, notices, raw


def refusal_of(wire: Wire, method: str, params: dict | None = None) -> str:
    """The fixed code a call is refused with; "answered" when it was not refused."""
    return wire.refusal(method, params) or "answered"


class Restore:
    """Put a record file back as it was (a check that damages a record leaves the others a sound one)."""

    def __init__(self, path: Path):
        self.path, self.saved = path, path.read_bytes() if path.exists() else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_bytes(self.saved)


REVOKE = """\
import sys
sys.path.insert(0, {orchestration!r})
from codex_agents import core_api
print(core_api.revoke_token({records!r}, {token!r}))
"""


def records_scene():
    """One installed fake project (a real setup), then a core process over hand-written records."""
    if "core_records" in _SCENES:
        return _SCENES["core_records"]
    world = CoreWorld().__enter__()
    scene = SimpleNamespace(world=world, usage=USAGE)
    _SCENES["core_records"] = scene
    write_patches(world, fault_patches())
    scene.setup = world.setup(world.answers())
    if scene.setup.code != 0:
        raise e2e_support.HarnessError("the scripted setup that installs the project did not finish")
    core = scene.core = Core(world, "main", seed=False)
    write_records(core.records, USAGE)
    core.start()
    scopes = ("read", "agent", "orchestrator", "confirm", "owner")
    scene.tokens = dict(zip(scopes, core.mint_many([(scope, "e2e-" + scope) for scope in scopes])))
    return scene


def wire_of(scene, scope: str) -> Wire:
    return scene.core.wire(scene.tokens[scope])


def note(phase: Phase) -> None:
    phase.reason(HAND_WRITTEN)


# ---------------------------------------------------------------------------------------------------------
# API-7, API-8, API-9, API-10

@check("API-7", group="core_records")
def api7_state_units_and_runs_equal_the_record_files(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    wire = wire_of(scene, "read")
    rows = [json.loads(line) for line in (core.records / "units.jsonl").read_text().splitlines()]
    latest = {}
    for row in rows:
        latest[row["id"]] = row
    # Every unit, field for field: the contract's neutral fields at the top, everything else under `extra`.
    listed = wire.call("units.list")["units"]
    phase.expect([u["id"] for u in listed] == list(latest), "units.list names every unit of the file, once, in file order")
    for unit in listed:
        phase.expect(unit == as_unit(latest[unit["id"]]), "a listed unit equals its record, every field: " + unit["id"])
        phase.expect(wire.call("units.get", {"id": unit["id"]})["unit"] == as_unit(latest[unit["id"]]),
                     "units.get equals the record, every field: " + unit["id"])
        every = [as_unit(row) for row in rows if row["id"] == unit["id"]]
        phase.expect(wire.call("units.history", {"id": unit["id"]})["records"] == every,
                     "units.history gives every record of the unit, oldest first, every field: " + unit["id"])
    phase.expect(refusal_of(wire, "units.get", {"id": "U-9999"}) == "not_found", "an unknown unit is not_found")
    runs = wire.call("runs.list")
    counts = collections.Counter(row["state"] for row in latest.values())
    phase.expect(runs["total"] == len(latest) and runs["counts"] == dict(counts),
                 "runs.list total and counts equal the states in the file")
    phase.expect(runs["runs"] == [as_unit(row) for row in latest.values()], "runs.list lists the runs, every field")
    state = wire.call("state.get")["state"]
    on_disk = json.loads((core.records / "state.json").read_text())
    phase.expect(state == as_state(on_disk), "state.get equals the state record, every field")
    # A change in the file shows at once (the file stays canonical): a new unit, then an update of one.
    with Restore(core.records / "units.jsonl"):
        with open(core.records / "units.jsonl", "a") as handle:
            handle.write(json.dumps({"id": "U-0004", "kind": "child", "state": "running", "title": "New"}) + "\n")
        phase.expect(wire.call("runs.list")["total"] == 4, "a unit added to the file is counted by the next read")


@check("API-8", group="core_records")
def api8_usage_history_ci_versions_equal_records_and_unknown_is_never_a_number(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    wire = wire_of(scene, "read")
    on_disk = json.loads((core.records / "state.json").read_text())
    phase.expect(wire.call("usage.get")["usage"] == as_usage(on_disk["usage"]),
                 "usage.get equals the record: counts, and windows by label and percentage")
    phase.expect(wire.call("ci.get")["ci"] == on_disk["ci"], "ci.get equals the record")
    phase.expect(wire.call("versions.get")["versions"] == on_disk["versions"], "versions.get equals the record")
    usage_rows = [json.loads(line) for line in (core.records / "usage.jsonl").read_text().splitlines()]
    expected = []
    for row in usage_rows:
        sample = {k: v for k, v in row.items() if k in ("ts", "input", "output", "total", "windows")}
        if any(k not in sample for k in row):
            sample["extra"] = {k: v for k, v in row.items() if k not in sample}
        expected.append(sample)
    phase.expect(wire.call("usage.history")["samples"] == expected,
                 "usage.history equals the usage record, in order, every field (provider detail under extra)")
    history_rows = [json.loads(line) for line in (core.records / "history.jsonl").read_text().splitlines()]
    phase.expect(wire.call("history.list")["history"] == history_rows, "history.list equals the history record, every entry, every field")
    # Unreadable or missing data is "unknown", never a guessed number.
    state_file = core.records / "state.json"
    with Restore(state_file):
        state_file.write_text("{broken")
        phase.expect(wire.call("usage.get")["usage"] == "unknown", "an unreadable state record: usage is unknown")
        phase.expect(wire.call("ci.get")["ci"] == "unknown" and wire.call("versions.get")["versions"] == "unknown",
                     "an unreadable state record: ci and versions are unknown")
        phase.expect(wire.call("state.get")["state"]["ctx"] == "unknown", "an unreadable state record: ctx is unknown")
        state_file.write_text(json.dumps({"phase": "build"}))
        phase.expect(wire.call("usage.get")["usage"] == "unknown" and wire.call("ci.get")["ci"] == "unknown",
                     "a state record without them: still unknown")
        state_file.unlink()
        phase.expect(wire.call("usage.get")["usage"] == "unknown", "a missing state record: usage is unknown")
    with Restore(core.records / "units.jsonl"):
        (core.records / "units.jsonl").unlink()
        phase.expect(wire.call("units.list")["units"] == "unknown", "a missing units record: units are unknown, not empty")
        runs = wire.call("runs.list")
        phase.expect(runs["total"] == "unknown" and runs["counts"] == "unknown", "and the run counts are unknown")
    with Restore(core.records / "usage.jsonl"):
        (core.records / "usage.jsonl").unlink()
        phase.expect(wire.call("usage.history")["samples"] == "unknown", "a missing usage history is unknown")
    with Restore(core.records / "history.jsonl"):
        with open(core.records / "history.jsonl", "a") as handle:
            handle.write("{damaged\n")
        reply = wire.call("history.list")
        phase.expect(reply.get("damaged_lines") == 1 and reply["history"] == history_rows,
                     "a damaged line is skipped and counted, never hidden, and the sound entries are all there")


@check("API-9", group="core_records")
def api9_health_findings_have_a_code_and_a_plain_text(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    wire = wire_of(scene, "read")
    phase.expect(wire.call("health.get")["findings"] == [], "sound records give no finding")
    with Restore(core.records / "state.json"), Restore(core.records / "history.jsonl"), Restore(core.records / "usage.jsonl"):
        (core.records / "state.json").write_text("{broken")
        with open(core.records / "history.jsonl", "a") as handle:
            handle.write("{damaged\n")
        (core.records / "usage.jsonl").write_bytes(b"\xff\xfe\n")
        findings = wire.call("health.get")["findings"]
        codes = {f["code"] for f in findings}
        phase.expect({"state_unreadable", "history_damaged"} <= codes, "each damage is a finding with its own code")
        for finding in findings:
            phase.expect(set(finding) == {"code", "text"}, "a finding is a code and a text")
            phase.expect(bool(finding["code"]) and finding["code"].replace("_", "").isalnum() and finding["code"].islower(),
                         "the code is a plain reason code")
            phase.expect(len(finding["text"]) > 20 and finding["text"].endswith("."), "the text is a plain sentence")
            phase.expect("Traceback" not in finding["text"] and str(core.records) not in finding["text"]
                         and str(Path.home()) not in finding["text"], "the text carries no traceback and no private path")
    phase.expect(wire.call("health.get")["findings"] == [], "the findings go when the records are sound again")


@check("API-10", group="core_records")
def api10_files_stay_canonical_a_restart_answers_the_same(phase: Phase) -> None:
    scene = records_scene()
    world = scene.world
    note(phase)
    core = Core(world, "canon", seed=False)
    write_records(core.records, scene.usage)
    core.start()
    token = core.mint("owner", "e2e-canon")
    wire = core.wire(token)
    methods = [("state.get", {}), ("units.list", {}), ("usage.get", {}), ("runs.list", {}), ("units.history", {"id": "U-0001"}),
               ("history.list", {}), ("decisions.list", {}), ("ci.get", {}), ("versions.get", {}), ("schema.get", {})]
    wire.call("context.push", {"kind": "note", "text": "kept across restarts"})
    first = {m: wire.call(m, p) for m, p in methods}
    known = {"units.jsonl", "state.json", "events.jsonl", "history.jsonl", "usage.jsonl", "decisions.jsonl", "inbox.jsonl",
             "commands.jsonl", "audit.jsonl", "tokens.json", "ids.json", ".records.lock", ".audit.lock"}
    phase.expect(not [n for n in os.listdir(core.records) if n not in known and not n.endswith(".lock")],
                 "the folder holds the producer's record files and the core's own records, no cache")
    for cache in core.records.glob("*.cache"):
        cache.unlink()
    saved = files_of(core.records, skip=("audit.jsonl",))
    for how in ("stop", "kill"):
        wire.close()
        core.stop() if how == "stop" else core.kill9()
        core.start()
        wire = core.wire(token)
        again = {m: wire.call(m, p) for m, p in methods}
        phase.expect(again == first, f"after a restart ({how}) every answer is identical")
        phase.expect(files_of(core.records, skip=("audit.jsonl",)) == saved, f"the records are byte for byte as before ({how})")
    inbox = [json.loads(line) for line in (core.records / "inbox.jsonl").read_text().splitlines()]
    phase.expect([r["text"] for r in inbox] == ["kept across restarts"], "what the core wrote (the inbox) survives its restarts")
    core.stop()


# ---------------------------------------------------------------------------------------------------------
# API-11, API-12, API-13, API-14

@check("API-11", group="core_records")
def api11_events_are_ordered_gap_free_and_have_the_envelope(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    top = last_seq(core)
    events, _, _ = stream(core, scene.tokens["read"], {"from_seq": top - 30}, count=30)
    seqs = [e["seq"] for e in events]
    phase.expect(seqs == list(range(top - 29, top + 1)), "replayed events are consecutive and increasing")
    for event in events:
        phase.expect({"seq", "ts", "kind", "data", "v"} <= set(event) and set(event) - {"unit_id", "session"} <=
                     {"seq", "ts", "kind", "data", "v"}, "an event has the envelope {seq, ts, kind, data, v}")
    # A live event: a confirmed pause is announced with the next seq, which is also the last line of the events file.
    wire = wire_of(scene, "confirm")
    live = {}
    reader = threading.Thread(target=lambda: live.update(zip(("events", "notices", "raw"), stream(
        core, scene.tokens["read"], {}, count=2, timeout=8))))
    reader.start()
    time.sleep(0.5)
    first = wire.request("control.pause", {})["error"]["data"]["confirm_id"]
    wire.call("control.pause", {"confirm_id": first})
    second = wire.request("control.resume", {})["error"]["data"]["confirm_id"]
    wire.call("control.resume", {"confirm_id": second})
    reader.join(15)
    got = live.get("events", [])
    phase.expect([e["seq"] for e in got] == [top + 1, top + 2], "live events take the next seq numbers in order")
    phase.expect([e["kind"] for e in got] == ["command.requested"] * 2, "and say what happened")
    last = json.loads((core.records / "events.jsonl").read_text().splitlines()[-1])
    phase.expect(last["seq"] == top + 2 and last["kind"] == "command.requested", "the events file ends with the same seq")


@check("API-12", group="core_records")
def api12_resume_replays_exactly_the_missed_events_an_old_cursor_is_expired(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    top = last_seq(core)
    events, _, _ = stream(core, scene.tokens["read"], {"from_seq": top - 5}, count=5)
    phase.expect([e["seq"] for e in events] == list(range(top - 4, top + 1)), "resume from a cursor replays exactly the missed events")
    nothing, _, _ = stream(core, scene.tokens["read"], {"from_seq": top}, count=1, timeout=1.5)
    phase.expect(nothing == [], "a cursor at the end replays nothing")
    wire = wire_of(scene, "read")
    try:
        wire.call("events.subscribe", {"from_seq": 3})
        code, data = None, {}
    except Refused as refused:
        code, data = refused.code, refused.data
    phase.expect(code == "cursor_expired", "a cursor older than the kept events is refused (cursor_expired)")
    snapshot = data.get("snapshot") or {}
    phase.expect(snapshot.get("seq_at") == top, "the refusal carries a snapshot at the current seq")
    phase.expect(snapshot.get("units") == wire.call("units.list")["units"] and
                 snapshot.get("state") == wire.call("state.get")["state"],
                 "the snapshot equals what the reads answer now")
    phase.expect(refusal_of(wire_of(scene, "read"), "events.subscribe", {"from_seq": top + 50}) == "invalid_params",
                 "a cursor from the future is refused")


def last_seq(core: Core) -> int:
    return json.loads((core.records / "events.jsonl").read_text().splitlines()[-1])["seq"]


@check("API-13", group="core_records")
def api13_kind_filter_and_a_slow_subscriber_is_dropped_without_delaying_others(phase: Phase) -> None:
    scene = records_scene()
    world, core = scene.world, scene.core
    note(phase)
    top = last_seq(core)
    stored = [json.loads(line) for line in (core.records / "events.jsonl").read_text().splitlines()]
    wanted = [e["seq"] for e in stored if e["seq"] > top - 400 and e["kind"] == "alert.guard"]
    events, _, _ = stream(core, scene.tokens["read"], {"from_seq": top - 400, "filter": {"kinds": ["alert.guard"]}},
                          count=len(wanted), timeout=8)
    phase.expect(wanted and [e["seq"] for e in events] == wanted, "a kind filter delivers exactly the events of that kind")
    phase.expect({e["kind"] for e in events} == {"alert.guard"}, "and nothing of another kind")
    wire = wire_of(scene, "read")
    phase.expect(refusal_of(wire, "events.subscribe", {"filter": {"kinds": ["no.such.kind"]}}) == "invalid_params",
                 "a filter naming an unknown kind is refused")
    phase.expect(refusal_of(wire, "events.subscribe", {"filter": {"secret": 1}}) == "invalid_params", "so is a filter with other keys")
    # A slow subscriber: a core with a small buffer, one reader that never reads, one that keeps up, a writer that
    # makes 300 events through the API. The writer and the healthy reader are not delayed; the stalled one is dropped.
    burst = Core(world, "burst", limits={"event_buffer": 50, "max_subscriptions": 4, "write_seconds": 0.5}).start()
    owner, reader_token = burst.mint_many([("owner", "e2e-burst-owner"), ("read", "e2e-burst-read")])
    import socket as socket_module
    stalled = socket_module.socket(socket_module.AF_UNIX)
    stalled.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_RCVBUF, 2048)
    stalled.settimeout(5)
    stalled.connect(str(burst.socket))
    stalled.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocol": "1.0", "token": reader_token}}) + "\n").encode())
    stalled.recv(4096)
    stalled.sendall(b'{"jsonrpc":"2.0","id":2,"method":"events.subscribe","params":{}}\n')
    healthy = {}
    reader = threading.Thread(target=lambda: healthy.update(zip(("events", "notices", "raw"), stream(
        burst, reader_token, {}, count=300, timeout=40))))
    reader.start()
    time.sleep(0.5)
    writer = burst.wire(owner)
    slowest, started = 0.0, time.monotonic()
    for index in range(300):
        before = time.monotonic()
        writer.call("unit.verdict", {"id": "U-0001", "verdict": "pass" if index % 2 else "fail"})
        slowest = max(slowest, time.monotonic() - before)
    total = time.monotonic() - started
    reader.join(45)
    got = [e["seq"] for e in healthy.get("events", [])]
    phase.expect(len(got) == 300 and got == list(range(got[0], got[0] + 300)), "a healthy reader gets every event, in order, no gap")
    phase.expect(slowest < 2.0, "no call of the writer waited on the stalled subscriber")
    phase.expect(total < 40, "the writer's whole burst was not held up")
    dropped = False
    deadline = time.monotonic() + 15
    stalled.settimeout(2)
    while time.monotonic() < deadline and not dropped:
        try:
            chunk = stalled.recv(65536)
        except OSError:
            continue
        if not chunk or b"events.dropped" in chunk:
            dropped = True
    phase.expect(dropped, "the stalled subscriber was told it was dropped or its connection was closed")
    stalled.close()
    extra = burst.wire(reader_token)
    phase.expect(extra.refusal("events.subscribe", {}) is None, "its subscription slot was freed")
    burst.stop()


@check("API-14", group="core_records")
def api14_events_carry_ids_and_summaries_never_secrets_or_transcripts(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    events, _, raw = stream(core, scene.tokens["read"], {"from_seq": EVENT_COUNT - 3}, count=3)
    last = [e for e in events if e["seq"] == EVENT_COUNT]
    phase.expect(len(last) == 1, "the event that carries the planted secrets was replayed")
    for secret in SECRETS:
        phase.expect(secret not in raw, "a planted secret or transcript never appears in an event")
    phase.expect(last[0]["data"].get("summary", "").startswith("edited 3 files"), "the summary stays")
    phase.expect(set(last[0]["data"]) == {"summary"}, "fields that are not ids or summaries are dropped")
    # A forwarded question that holds a secret shows up in its event only as an id.
    asker = wire_of(scene, "confirm")
    collected = {}
    reader = threading.Thread(target=lambda: collected.update(zip(("events", "notices", "raw"), stream(
        core, scene.tokens["read"], {"filter": {"kinds": ["query.queued"]}}, count=1, timeout=8))))
    reader.start()
    time.sleep(0.5)
    answer = asker.call("query.ask", {"text": "Should we use the key tok-SECRET-VALUE? password=hunter2hunter2", "mode": "forward"})
    reader.join(12)
    queued = collected.get("events", [])
    phase.expect(len(queued) == 1 and set(queued[0]["data"]) == {"query_id"} and queued[0]["data"]["query_id"] == answer["query_id"],
                 "a queued question is announced by its id only")
    for secret in SECRETS:
        phase.expect(secret not in collected.get("raw", ""), "and its text never appears in the event")


# ---------------------------------------------------------------------------------------------------------
# API-15 (API-16 and API-17 are unchecked: see the module docstring)

CANNED = [("What is the status?", {"state.json", "units.jsonl"}, "Phase build"),
          ("What is the usage?", {"state.json"}, "1234"),
          ("How is CI?", {"state.json"}, "green"),
          ("Which versions are installed?", {"state.json"}, "0.8.60"),
          ("Show the history", {"history.jsonl"}, "3 history entries"),
          ("Why did U-0002 stop?", {"history.jsonl"}, "The context window was nearly full.")]


@check("API-15", group="core_records")
def api15_canned_questions_are_answered_from_records_and_nothing_is_queued(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    inbox_before = (core.records / "inbox.jsonl").read_bytes()
    for token_scope in ("read", "confirm"):
        wire = wire_of(scene, token_scope)
        for question, sources, expect in CANNED:
            answer = wire.call("query.ask", {"text": question})
            phase.expect(answer["answered_by"] == "records", "a canned question is answered from the records: " + question)
            phase.expect(answer["tokens_used"] == 0, "at no token cost")
            phase.expect(sources <= set(answer["sources"]) and answer["sources"], "and names its sources")
            phase.expect(isinstance(answer["as_of"], (int, float)), "and says as of when")
            phase.expect(expect in answer["text"], "and the answer is what the record says: " + expect)
    why = wire_of(scene, "read").call("query.ask", {"text": "Why did U-0002 stop?"})["text"]
    phase.expect("UNRELATED" not in why and "CoCoLo" in why, "why-stopped reads the stop record, not the last entry")
    phase.expect((core.records / "inbox.jsonl").read_bytes() == inbox_before, "nothing was written to the inbox")
    phase.expect(wire_of(scene, "read").call("query.ask", {"text": "Should we rewrite the parser?", "mode": "records_only"})[
                     "answered_by"] == "no_records_answer", "a judgment question is not answered from records")
    phase.expect((core.records / "inbox.jsonl").read_bytes() == inbox_before, "nor queued by a records-only ask")
    phase.expect(refusal_of(wire_of(scene, "read"), "query.ask", {"text": "Should we rewrite the parser?", "mode": "forward"}) == "forbidden",
                 "a read token cannot forward")


# ---------------------------------------------------------------------------------------------------------
# API-18 .. API-23, TERM-13

def token_store(core: Core) -> dict:
    return json.loads((core.records / "tokens.json").read_text())


@check("API-18", group="core_records")
def api18_tokens_are_256_bit_hashed_revocable_and_minted_only_at_the_owners_terminal(phase: Phase) -> None:
    scene = records_scene()
    world = scene.world
    note(phase)
    core = Core(world, "tokens", seed=True).start()
    owner, default, second = core.mint_many([("owner", "e2e-owner"), ("agent", "e2e-default-agent"), ("read", "e2e-read")])
    for token in (owner, default, second):
        phase.expect(len(token) == 64 and len(bytes.fromhex(token)) == 32, "a token is 256 bits")
    phase.expect(len({owner, default, second}) == 3, "every mint is a new token")
    store_text = (core.records / "tokens.json").read_text()
    stored = token_store(core)["tokens"]
    ids = {row["id"] for row in stored}
    phase.expect(all(t not in store_text for t in (owner, default, second)), "no token is stored")
    phase.expect({hashlib.sha256(t.encode()).hexdigest() for t in (owner, default, second)} <= ids, "each is stored as its SHA-256")
    phase.expect(stat.S_IMODE(os.stat(core.records / "tokens.json").st_mode) == 0o600, "the token store is mode 0600")
    # Minted only at the owner's terminal: no terminal, no token; and not over the API, even with the owner token.
    before = (core.records / "tokens.json").read_bytes()
    refused = core.mint_refused_without_terminal("owner")
    phase.expect(refused.returncode != 0 and "own terminal" in refused.stderr and not refused.stdout.strip(),
                 "minting with no terminal is refused and prints no token")
    phase.expect((core.records / "tokens.json").read_bytes() == before, "and changes nothing")
    wire = core.wire(owner)
    phase.expect(refusal_of(wire, "token.mint", {"scope": "owner"}) == "owner_terminal_only", "minting over the API is refused for an owner token")
    phase.expect((core.records / "tokens.json").read_bytes() == before, "and changes nothing")
    # An agent token (the default scope) is read plus context.push.
    agent = core.wire(default)
    agent.call("state.get")
    agent.call("context.push", {"kind": "note", "text": "hi"})
    phase.expect(refusal_of(agent, "control.pause") == "forbidden", "an agent token may not control")
    phase.expect(refusal_of(agent, "query.reply", {"query_id": "Q-0001", "text": "x"}) == "forbidden", "nor reply")
    # Revocable: the token stops working at once, on a connection already open and at the handshake.
    live = core.wire(second)
    live.call("state.get")
    phase.expect(core.helper(REVOKE, token=second).strip() == "True", "the owner revokes a token")
    phase.expect(refusal_of(live, "state.get") == "unauthorized", "an open connection is refused at its next call")
    fresh = Wire(core.socket)
    phase.expect(fresh.refusal("initialize", {"protocol": "1.0", "token": second}) == "unauthorized", "and the handshake refuses it")
    phase.expect(hashlib.sha256(second.encode()).hexdigest() not in {r["id"] for r in token_store(core)["tokens"]},
                 "the revoked token is gone from the store")
    core.stop()


# Methods by who may call them, from the requirement and the tests (tests/test_core_api_talk_control.py). Each entry is
# the method and a function of the records the call reaches (ids the matrix core made) that gives parameters under
# which the call SUCCEEDS: an allowed method must really answer, not merely "not be forbidden".
ROUTING = {"profile": "p", "providers": ["a"], "rows": []}
READS = [("schema.get", lambda ids: {}), ("state.get", lambda ids: {}), ("units.list", lambda ids: {}),
         ("units.get", lambda ids: {"id": "U-0001"}), ("units.history", lambda ids: {"id": "U-0001"}),
         ("runs.list", lambda ids: {}), ("usage.get", lambda ids: {}), ("usage.history", lambda ids: {}),
         ("ci.get", lambda ids: {}), ("versions.get", lambda ids: {}), ("history.list", lambda ids: {}),
         ("decisions.list", lambda ids: {}), ("queries.list", lambda ids: {}),
         ("queries.get", lambda ids: {"id": ids["query"]}), ("health.get", lambda ids: {}),
         ("events.subscribe", lambda ids: {}), ("query.ask", lambda ids: {"text": "What is the status?"}),
         ("units.activity", lambda ids: {"id": "U-0001"}), ("changes.list", lambda ids: {}),
         ("changes.diff", lambda ids: {"path": "a.py"}), ("messages.list", lambda ids: {}),
         ("routing.get", lambda ids: {}), ("commands.list", lambda ids: {}),
         ("commands.get", lambda ids: {"id": ids["command"]})]
PUSHES = [("context.push", lambda ids: {"kind": "note", "text": "hello"})]
CONFIRMS = [("control.pause", lambda ids: {}), ("control.resume", lambda ids: {}), ("control.handoff", lambda ids: {}),
            ("unit.cancel", lambda ids: {"id": "U-0001"})]
REPLIES = [("query.reply", lambda ids: {"query_id": ids["reply"], "text": "Yes."})]
OWNERS = [("control.stop", lambda ids: {}), ("decision.answer", lambda ids: {"id": "D-0001", "choice": "yes"}),
          ("unit.verdict", lambda ids: {"id": "U-0001", "verdict": "pass"}),
          ("unit.message", lambda ids: {"id": "U-0001", "text": "hi"}), ("routing.set", lambda ids: ROUTING)]
ALLOWED = {"read": READS, "agent": READS + PUSHES, "orchestrator": READS + PUSHES + REPLIES,
           "confirm": READS + PUSHES + CONFIRMS, "owner": READS + PUSHES + CONFIRMS + REPLIES + OWNERS}
EVERY = READS + PUSHES + CONFIRMS + REPLIES + OWNERS
CHANGES_OF_MATRIX = {m for m, _ in PUSHES + CONFIRMS + REPLIES + OWNERS}


def matrix_core(scene):
    """A core of its own (the matrix changes records: decisions, commands, queries) whose records make every method
    of the contract answerable, with one token of each scope and the ids the calls need."""
    core = Core(scene.world, "matrix", seed=False)
    write_records(core.records, USAGE)
    (core.records / "activity.jsonl").write_text(jsonl([{"ts": 1, "unit_id": "U-0001", "kind": "tool", "name": "Edit", "text": "edit a.py"},
                                                        {"ts": 2, "unit_id": "U-0001", "kind": "output", "text": "done"}]))
    (core.records / "changes.json").write_text(json.dumps({"worktrees": [{"name": "w", "branch": "b", "files": [
        {"path": "a.py", "status": "M"}], "commits": [{"id": "abc", "summary": "a change"}]}]}))
    (core.records / "diffs.jsonl").write_text(jsonl([{"path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"}]))
    (core.records / "messages.jsonl").write_text(jsonl([{"id": "M-1", "ts": 1, "role": "orchestrator", "text": "hi", "unit_id": "U-0001"}]))
    (core.records / "routing.json").write_text(json.dumps({"profile": "p", "providers": ["a"], "rows": [], "set_by": "owner", "ts": 1}))
    core.start()
    scopes = ("read", "agent", "orchestrator", "confirm", "owner")
    tokens = dict(zip(scopes, core.mint_many([(scope, "e2e-" + scope) for scope in scopes])))
    asker = core.wire(tokens["confirm"])
    queries = [asker.call("query.ask", {"text": f"Judgment question {n}?", "mode": "forward"})["query_id"] for n in range(3)]
    first, reply = change(asker, "control.pause", {})  # a command record for commands.get
    return core, tokens, queries, reply["result"]["command_id"]


@check("API-19", group="core_records")
def api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden(phase: Phase) -> None:
    scene = records_scene()
    note(phase)
    core, tokens, queries, command = matrix_core(scene)
    expected_changes = []
    for scope, allowed in ALLOWED.items():
        wire = core.wire(tokens[scope])
        ids = {"query": queries[0], "command": command,
               "reply": {"orchestrator": queries[1], "owner": queries[2]}.get(scope)}
        names = {m for m, _ in allowed}
        for method, params in EVERY:
            body = params(ids)
            if method in names:
                first, reply = change(wire, method, body)
                result = reply.get("result")
                phase.expect(result is not None and isinstance(result, dict) and "error" not in reply,
                             f"{scope} calls {method} and it really answers")
                phase.expect((first is not None) == (method in TWO_STEP), f"{method} asks for the confirm step exactly when the contract says so")
                if method in CHANGES_OF_MATRIX:
                    expected_changes.append((scope, method))
            else:
                reply = wire.request(method, body)
                phase.expect(((reply.get("error") or {}).get("data") or {}).get("code") == "forbidden",
                             f"{scope} is refused {method} (forbidden, before any confirm step)")
        wire.close()
    # What the answers claim is on record: each change made by a scope is an ok line of commands.jsonl under that caller.
    ok = {(r["caller"].split(":")[0], r["method"]) for r in core.audit("commands.jsonl") if r.get("outcome") == "ok"}
    phase.expect(set(expected_changes) <= ok, "every change a scope made is an ok line of commands.jsonl under its own caller")
    reader = core.wire(tokens["read"])
    phase.expect(reader.call("changes.diff", {"path": "a.py"})["diff"].startswith("@@ -1 +1 @@"), "an allowed read answers with the record")
    phase.expect(reader.call("decisions.list")["decisions"][0]["answer"] == "yes", "the owner's decision answer is on record")
    # The core's own declaration agrees with what was observed: every method names the least scope that may call it.
    schema = reader.call("schema.get")["methods"]
    order = ["read", "agent", "orchestrator", "confirm", "owner"]
    for method, _ in EVERY:
        lowest = next(s for s in order if method in {m for m, _ in ALLOWED[s]})
        phase.expect(schema[method]["scope"] == lowest or (method in ("query.reply",) and schema[method]["scope"] == "orchestrator"),
                     "schema.get names the scope of " + method)
    core.stop()


@check("API-20", group="core_records")
def api20_only_the_owner_role_answers_a_decision(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    before = (core.records / "decisions.jsonl").read_bytes()
    for scope in ("read", "agent", "orchestrator", "confirm"):
        phase.expect(refusal_of(wire_of(scene, scope), "decision.answer", {"id": "D-0001", "choice": "yes"}) == "forbidden",
                     f"a {scope} token may not answer a decision")
    phase.expect((core.records / "decisions.jsonl").read_bytes() == before, "nothing was written by the refusals")
    owner = wire_of(scene, "owner")
    phase.expect(refusal_of(owner, "decision.answer", {"id": "D-0001", "choice": "maybe"}) == "invalid_params",
                 "an answer that is not one of the options is refused")
    owner.call("decision.answer", {"id": "D-0001", "choice": "yes"})
    rows = [json.loads(line) for line in (core.records / "decisions.jsonl").read_text().splitlines()]
    phase.expect(rows[-1]["answer"] == "yes" and rows[-1]["answered_by"].startswith("owner:"), "the owner's answer is on record with who gave it")
    phase.expect(wire_of(scene, "read").call("decisions.list")["decisions"][0]["answer"] == "yes", "and read back")
    phase.expect(refusal_of(owner, "decision.answer", {"id": "D-0001", "choice": "no"}) == "conflict", "a decision is answered once")
    (core.records / "decisions.jsonl").write_bytes(before)  # the record as it was, for the checks that read it later


@check("API-21", group="core_records")
def api21_a_control_needs_two_steps_executes_once_and_the_id_is_bound(phase: Phase) -> None:
    scene = records_scene()
    world = scene.world
    note(phase)
    clock = Core(world, "clock21", limits={}).start()
    mine, other = clock.mint_many([("confirm", "e2e-a"), ("confirm", "e2e-b")])
    wire, stranger = clock.wire(mine), clock.wire(other)
    params = {"id": "U-0001"}
    units_before = (clock.records / "units.jsonl").read_bytes()

    def commands() -> list[dict]:
        return [r for r in clock.audit("commands.jsonl") if r.get("type") == "command"]

    first = wire.request("unit.cancel", params)["error"]
    phase.expect(first["data"]["code"] == "confirm_required" and first["data"].get("confirm_id") and first["data"].get("expires_in") == 60,
                 "the first call returns confirm_required with an id and 60 seconds")
    phase.expect(commands() == [] and (clock.records / "units.jsonl").read_bytes() == units_before, "nothing happened at the first step")
    confirm_id = first["data"]["confirm_id"]
    phase.expect(refusal_of(stranger, "unit.cancel", {**params, "confirm_id": confirm_id}) == "confirm_invalid", "another token's confirm id is refused")
    phase.expect(refusal_of(wire, "unit.cancel", {"id": "U-0002", "confirm_id": confirm_id}) == "confirm_invalid", "other parameters are refused")
    # (a confirm id that was refused for its binding is still its own: a wrong try does not use it up)
    done = wire.call("unit.cancel", {**params, "confirm_id": confirm_id})
    phase.expect(done["state"] == "requested" and done["outcome"] == "cancel_requested", "the second call is executed (requested)")
    phase.expect(len(commands()) == 1 and commands()[0]["action"] == "cancel" and commands()[0]["unit_id"] == "U-0001",
                 "it made exactly one command record")
    phase.expect(refusal_of(wire, "unit.cancel", {**params, "confirm_id": confirm_id}) == "confirm_invalid", "the id cannot be used twice")
    phase.expect(len(commands()) == 1, "the reuse made no second command")
    # Expiry: a minute later the id is refused (the core's clock moved, in its own process).
    late = wire.request("unit.cancel", {"id": "U-0002"})["error"]["data"]["confirm_id"]
    clock.advance(61)
    phase.expect(refusal_of(wire, "unit.cancel", {"id": "U-0002", "confirm_id": late}) == "confirm_expired", "after 60 seconds the id is expired")
    phase.expect(len(commands()) == 1, "an expired id made no command")
    listed = clock.wire(mine).call("commands.list")["commands"]
    phase.expect(len(listed) == 1 and listed[0]["state"] == "requested", "commands.list agrees: one command, requested")
    clock.stop()


WALLED = ["install.run", "update.run", "setup.run", "install.reviewed", "records.write", "billing.attest",
          "client_range.widen", "token.mint"]


@check("API-22", group="core_records")
def api22_walled_actions_are_refused_even_with_an_owner_token(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    owner = wire_of(scene, "owner")
    before = files_of(core.records, skip=("audit.jsonl", "audit.jsonl.1"))
    schema_walled = wire_of(scene, "read").call("schema.get")["walled"]
    phase.expect(set(WALLED) <= set(schema_walled), "schema.get lists the walled actions")
    for method in WALLED:
        phase.expect(refusal_of(owner, method, {}) == "owner_terminal_only", f"{method} is refused over the API (owner_terminal_only)")
        phase.expect(refusal_of(owner, method, {"confirm_id": "x", "scope": "owner"}) == "owner_terminal_only",
                     f"{method} is refused with a confirm id too")
    phase.expect(files_of(core.records, skip=("audit.jsonl", "audit.jsonl.1")) == before,
                 "no record changed (the refusals are audit rows only)")
    rows = core.audit()
    phase.expect({r["method"] for r in rows if r["outcome"] == "owner_terminal_only"} >= set(WALLED), "each refusal is an audit row")


@check("API-23", group="core_records")
def api23_every_change_is_a_command_line_every_refusal_an_audit_line_and_no_secret_in_either(phase: Phase) -> None:
    scene = records_scene()
    world = scene.world
    note(phase)
    core = Core(world, "audit", limits={"audit_max_bytes": 3000}).start()
    tokens = dict(zip(("agent", "confirm", "owner", "read"), core.mint_many([(s, "e2e-" + s) for s in ("agent", "confirm", "owner", "read")])))
    secrets = ("tok-SECRET-VALUE", "pw-SECRET-VALUE")
    agent = core.wire(tokens["agent"])
    for index in range(3):
        agent.call("context.push", {"kind": "note", "text": f"note {index}", "token": secrets[0], "passphrase": secrets[1]})
    confirm = core.wire(tokens["confirm"])
    cid = confirm.request("control.pause", {})["error"]["data"]["confirm_id"]
    confirm.call("control.pause", {"confirm_id": cid})
    owner = core.wire(tokens["owner"])
    owner.call("unit.verdict", {"id": "U-0001", "verdict": "pass"})
    read = core.wire(tokens["read"])
    read.call("state.get")
    read.call("units.list")
    commands = core.audit("commands.jsonl")
    audit_rows = [r for r in commands if "outcome" in r]
    methods = [(r["method"], r["outcome"]) for r in audit_rows]
    phase.expect(methods.count(("context.push", "ok")) == 3 and methods.count(("context.push", "intent")) == 3,
                 "every push is an intent line and an ok line of commands.jsonl")
    phase.expect(("control.pause", "confirm_required") in methods and ("control.pause", "ok") in methods,
                 "a confirm step and its execution are commands.jsonl lines")
    phase.expect(("unit.verdict", "ok") in methods, "an owner change is a commands.jsonl line")
    phase.expect(not any(m in ("state.get", "units.list") for m, _ in methods), "reads are not audited")
    for row in audit_rows:
        phase.expect({"caller", "method", "outcome"} <= set(row), "each line has caller, method and outcome")
    callers = {r["method"]: r["caller"] for r in audit_rows if r["outcome"] == "ok"}
    phase.expect(callers["context.push"] == "agent:" + hashlib.sha256(tokens["agent"].encode()).hexdigest()[:8],
                 "the caller is the scope and the first eight characters of the token's hash")
    # Refusals go to audit.jsonl, which rotates apart from the commands.
    for _ in range(40):
        agent.refusal("control.pause")
        read.refusal("decision.answer", {"id": "D-0001", "choice": "yes"})
    refusals = core.audit("audit.jsonl")
    phase.expect(all({"caller", "method", "outcome"} <= set(r) for r in refusals) and refusals, "each refusal line has caller, method, outcome")
    phase.expect({r["outcome"] for r in refusals} >= {"forbidden"}, "a refusal is an audit.jsonl line")
    phase.expect((core.records / "audit.jsonl.1").exists(), "audit.jsonl rotated by itself (its own, small cap)")
    phase.expect(not (core.records / "commands.jsonl.1").exists(), "commands.jsonl never rotates")
    after = [r for r in core.audit("commands.jsonl") if "outcome" in r]
    phase.expect(after[:len(audit_rows)] == audit_rows and len(after) == len(audit_rows),
                 "the refusals pushed none of the change lines out")
    # No token, no secret in either file (nor the rotated one).
    text = "".join((core.records / n).read_text() for n in ("commands.jsonl", "audit.jsonl", "audit.jsonl.1") if (core.records / n).exists())
    for secret in (*secrets, *tokens.values()):
        phase.expect(secret not in text, "no token and no secret is written to the audit files")
    core.stop()


@check("TERM-13", group="core_records")
def term13_authority_is_decided_by_the_core_not_by_what_the_client_says(phase: Phase) -> None:
    scene = records_scene()
    core = scene.core
    note(phase)
    # A client that says it is more than its token: the handshake and every envelope field are ignored for authority.
    wire = Wire(core.socket)
    reply = wire.call("initialize", {"protocol": "1.0", "token": scene.tokens["read"], "scope": "owner", "role": "owner"})
    phase.expect(reply["scope"] == "read", "the core answers with the token's scope, not the one the client claimed")
    wire.send((json.dumps({"jsonrpc": "2.0", "id": 5, "scope": "owner", "method": "control.stop", "params": {"scope": "owner"}}) + "\n").encode())
    phase.expect(((wire.message() or {}).get("error") or {}).get("data", {}).get("code") == "forbidden",
                 "a read token that claims owner in its request is refused")
    nobody = Wire(core.socket)
    nobody.init()
    phase.expect(refusal_of(nobody, "state.get", {"scope": "owner"}) == "unauthorized", "no token, no authority, whatever is claimed")
    forged = Wire(core.socket)
    phase.expect(forged.refusal("initialize", {"protocol": "1.0", "token": "f" * 64, "scope": "owner"}) == "unauthorized",
                 "a made-up token is refused")
    swapped = Wire(core.socket)
    swapped.init(scene.tokens["confirm"])
    phase.expect(refusal_of(swapped, "decision.answer", {"id": "D-0001", "choice": "yes"}) == "forbidden" and
                 refusal_of(swapped, "control.stop", {}) == "forbidden", "a confirm token cannot do the owner's methods")
    phase.expect(refusal_of(swapped, "unit.cancel", {"id": "U-0001"}) == "confirm_required", "but its own controls need the confirm step")
    # The tiers are the core's: each client that holds a token sees its own scope in the core's reply.
    for scope in ("read", "agent", "orchestrator", "confirm", "owner"):
        probe = Wire(core.socket)
        phase.expect(probe.init(scene.tokens[scope])["scope"] == scope, f"a {scope} token is a {scope} session")
    rows = core.audit()
    phase.expect(any(r["outcome"] == "forbidden" and r["method"] == "control.stop" for r in rows),
                 "the refusals are rows of the core's own audit file")
